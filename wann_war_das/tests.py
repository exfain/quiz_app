import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession
from games_hub.views import get_leaderboard_data

from .consumers import WannWarDasConsumer
from .models import WannWarDasGame, WannWarDasParticipant, WannWarDasQuestion


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class WannWarDasStartFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('start-host', password='pw', is_staff=True)
        self.question = WannWarDasQuestion.objects.create(
            created_by=self.user,
            question_text='Wann begann der Test?',
            correct_answer=2026,
        )
        self.game = WannWarDasGame.objects.create(title='Startflow', creator=self.user)
        self.game.selected_questions.add(self.question)
        self.session = HubSession.objects.create(
            code='START1',
            name='Startflow Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        HubParticipant.objects.create(
            session=self.session,
            nickname='Alice',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='wann_war_das',
            room_code=self.game.room_code,
            title=self.game.title,
        )

    def make_consumer(self):
        consumer = WannWarDasConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'wann_war_das_{self.game.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_monitor_hydration_does_not_mark_players_as_already_in_game(self):
        client = Client()
        client.force_login(self.user)

        response = client.get(
            reverse('admin_dashboard:wann_war_das_monitor', args=[self.game.room_code]),
            {'hub_session': self.session.code},
        )

        self.assertEqual(response.status_code, 200)
        participant = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertFalse(participant.is_active)
        self.assertContains(response, 'Spiel starten')
        self.assertContains(response, 'Frage starten')

    def test_player_route_before_start_stays_in_waiting_state_without_blocking_start(self):
        response = Client().get(
            reverse('wann_war_das:play', args=[self.game.room_code, 'Alice']),
            {'hub_session': self.session.code},
        )

        self.assertEqual(response.status_code, 200)
        participant = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertFalse(participant.is_active)
        self.assertContains(response, 'Warte darauf, dass der Host das Spiel startet.')

        consumer, sent_messages = self.make_consumer()
        async_to_sync(consumer.handle_admin_start_game)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(sent_messages, [])
        self.assertEqual(self.game.status, 'active')
        self.assertTrue(participant.is_active)

    def test_question_cannot_start_before_game_and_does_not_implicitly_start_it(self):
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_question)({
            'hub_session': self.session.code,
            'question_id': self.question.id,
        })

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'waiting')
        self.assertIsNone(self.game.current_question)
        self.assertIsNone(self.game.question_started_at)
        self.assertIn('Starte zuerst das Spiel', sent_messages[-1]['message'])
        self.assertEqual(consumer.channel_layer.group_messages, [])

    def test_game_start_activates_run_without_starting_first_question(self):
        other_session = HubSession.objects.create(
            code='OTHER1',
            name='Andere Session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=other_session,
            order=0,
            game_key='wann_war_das',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_game)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.active_hub_session_code, self.session.code)
        self.assertIsNone(self.game.current_question)
        self.assertEqual(self.game.question_state, 'ready')
        self.assertIsNone(self.game.question_started_at)
        game_event = next(
            message for group, message in consumer.channel_layer.group_messages
            if group == consumer.room_group_name
        )
        self.assertEqual(game_event['event_type'], 'game_started')
        hub_group, hub_event = next(
            (group, message) for group, message in consumer.channel_layer.group_messages
            if group.startswith('hub_')
        )
        self.assertEqual(hub_group, f'hub_{self.session.code}')
        self.assertEqual(hub_event['event']['type'], 'quiz_started')

    def test_question_start_after_game_broadcasts_live_state_with_server_timestamp(self):
        self.game.start_quiz(self.session.code)
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_question)({
            'hub_session': self.session.code,
            'question_id': self.question.id,
        })

        self.game.refresh_from_db()
        self.assertEqual(self.game.question_state, 'active')
        self.assertEqual(self.game.current_question, self.question)
        self.assertIsNotNone(self.game.question_started_at)
        question_event = next(
            message for group, message in consumer.channel_layer.group_messages
            if group == consumer.room_group_name
        )
        self.assertEqual(question_event['event_type'], 'question_started')
        self.assertIsNotNone(question_event['started_at'])
        self.assertIsNotNone(question_event['timer'])

    def test_rejoin_state_distinguishes_waiting_started_active_and_revealed(self):
        waiting = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(waiting['game']['status'], 'waiting')
        self.assertIsNone(waiting['question'])
        self.assertFalse(waiting['can_answer'])

        self.game.start_quiz(self.session.code)
        started = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(started['game']['status'], 'active')
        self.assertEqual(started['question_state'], 'ready')
        self.assertIsNone(started['question'])

        self.assertTrue(self.game.start_question(self.question, self.session.code))
        active = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(active['question_state'], 'active')
        self.assertTrue(active['can_answer'])
        self.assertIsNotNone(active['timer'])

        self.game.reveal_current_question()
        revealed = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(revealed['question_state'], 'revealed')
        self.assertFalse(revealed['can_answer'])


class WannWarDasEndFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('end-host', password='pw', is_staff=True)
        self.question = WannWarDasQuestion.objects.create(
            created_by=self.user,
            question_text='Wann endet der Test?',
            correct_answer=2026,
        )
        self.game = WannWarDasGame.objects.create(title='Endflow', creator=self.user)
        self.game.selected_questions.add(self.question)
        self.session = HubSession.objects.create(
            code='END001',
            name='Endflow Session',
            is_active=True,
            started_at=timezone.now(),
        )
        self.hub_participant = HubParticipant.objects.create(
            session=self.session,
            nickname='Alice',
            scoring_eligible=True,
        )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='wann_war_das',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)
        self.game.start_quiz(self.session.code)
        self.participant = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.game.start_question(self.question, self.session.code)
        self.game.reveal_current_question()

    def make_consumer(self):
        consumer = WannWarDasConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'wann_war_das_{self.game.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_host_and_player_end_views_have_working_lobby_routes(self):
        admin_client = Client()
        admin_client.force_login(self.user)
        host_response = admin_client.get(
            reverse('admin_dashboard:wann_war_das_monitor', args=[self.game.room_code]),
            {'hub_session': self.session.code},
        )
        player_response = Client().get(
            reverse('wann_war_das:play', args=[self.game.room_code, self.participant.name]),
            {'hub_session': self.session.code},
        )

        self.assertContains(host_response, 'Zur Lobby')
        self.assertContains(host_response, 'Zur Session-Übersicht')
        self.assertContains(player_response, 'Zur Lobby zurückkehren')
        self.assertContains(player_response, 'participant-return-to-lobby')

    def test_participant_return_marks_inactive_and_lobby_suppresses_auto_redirect(self):
        response = Client().post(
            reverse('games_hub:participant_return_to_lobby', args=[self.session.code]),
            data=json.dumps({
                'game_key': 'wann_war_das',
                'room_code': self.game.room_code,
                'participant_name': self.participant.name,
            }),
            content_type='application/json',
        )
        lobby_response = Client().get(
            reverse('games_hub:lobby', args=[self.session.code]),
            {'nickname': self.participant.name, 'return': '1'},
        )

        self.participant.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.participant.is_active)
        self.assertContains(lobby_response, "returnedToLobby")
        self.assertContains(lobby_response, "if (returnedToLobby)")

    def test_end_game_does_not_reactivate_returned_participant_and_only_finalizes_once(self):
        self.participant.is_active = False
        self.participant.save(update_fields=['is_active'])
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})
        first_group_message_count = len(consumer.channel_layer.group_messages)
        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        self.participant.refresh_from_db()
        self.assertEqual(self.game.status, 'completed')
        self.assertFalse(self.participant.is_active)
        self.assertEqual(first_group_message_count, 2)
        self.assertEqual(len(consumer.channel_layer.group_messages), first_group_message_count)
        self.assertEqual(sent_messages[-1]['type'], 'wann_war_das_state')

    def test_completed_rejoin_result_has_lobby_button_and_answers_are_rejected(self):
        self.game.end_quiz()
        result_response = Client().get(
            reverse('wann_war_das:result', args=[self.game.room_code, self.participant.name]),
            {'hub_session': self.session.code},
        )
        answer, error = self.game.submit_answer(self.participant, '2026')

        self.assertContains(result_response, 'Zur Lobby zurückkehren')
        self.assertContains(result_response, 'participant-return-to-lobby')
        self.assertIsNone(answer)
        self.assertIn('nicht aktiv', error)


class WannWarDasTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('admin', password='pw', is_staff=True)
        self.question = WannWarDasQuestion.objects.create(
            created_by=self.user,
            question_text='Wann wurde der Europaeische Gerichtshof gegruendet?',
            correct_answer=1952,
            unit='Jahr',
            start_tolerance=0,
            tolerance_increment=1,
            seconds_per_step=5,
            max_tolerance=10,
            max_points=10,
            min_points=1,
        )
        self.game = WannWarDasGame.objects.create(title='Zeitachse', creator=self.user)
        self.game.selected_questions.add(self.question)
        self.game.question_order = [self.question.id]
        self.game.save(update_fields=['question_order'])
        self.session = HubSession.objects.create(
            code='W12345',
            name='Wann war das Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            locked_participant_count=2,
        )
        self.alice_hub = HubParticipant.objects.create(
            session=self.session,
            nickname='Alice',
            scoring_eligible=True,
        )
        self.bob_hub = HubParticipant.objects.create(
            session=self.session,
            nickname='Bob',
            scoring_eligible=True,
        )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='wann_war_das',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)
        self.game.start_quiz(self.session.code)
        self.alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)

    def start_question_at_elapsed(self, elapsed_seconds=0):
        self.game.start_question(self.question, self.session.code)
        started_at = timezone.now() - timezone.timedelta(seconds=elapsed_seconds)
        self.game.question_started_at = started_at
        self.game.save(update_fields=['question_started_at'])
        self.game.refresh_from_db()
        return started_at

    def test_admin_can_create_game_and_question_with_tolerance_parameters(self):
        client = Client()
        client.force_login(self.user)

        question_response = client.post(
            reverse('admin_dashboard:add_wann_war_das_question'),
            data=json.dumps({
                'question_text': 'Wann war Apollo 11?',
                'correct_answer': 1969,
                'unit': 'Jahr',
                'start_tolerance': 0,
                'tolerance_increment': 2,
                'seconds_per_step': 4,
                'max_tolerance': 8,
                'max_points': 12,
                'min_points': 2,
            }),
            content_type='application/json',
        )

        self.assertEqual(question_response.status_code, 200)
        question_id = question_response.json()['question_id']
        created_question = WannWarDasQuestion.objects.get(id=question_id)
        self.assertEqual(created_question.correct_answer, 1969)
        self.assertEqual(created_question.tolerance_increment, 2)

        game_response = client.post(
            reverse('admin_dashboard:create_wann_war_das_game'),
            data=json.dumps({'title': 'Jahreszahlen', 'question_ids': [question_id]}),
            content_type='application/json',
        )

        self.assertEqual(game_response.status_code, 200)
        created_game = WannWarDasGame.objects.get(id=game_response.json()['game_id'])
        self.assertEqual(created_game.title, 'Jahreszahlen')
        self.assertEqual(list(created_game.selected_questions.values_list('id', flat=True)), [question_id])

    def test_invalid_question_parameters_are_rejected(self):
        question = WannWarDasQuestion(
            created_by=self.user,
            question_text='ungueltig',
            correct_answer=1,
            start_tolerance=5,
            tolerance_increment=0,
            seconds_per_step=0,
            max_tolerance=3,
            max_points=1,
            min_points=2,
        )

        with self.assertRaises(ValidationError):
            question.full_clean()

    def test_exact_early_answer_gets_maximum_points(self):
        started_at = self.start_question_at_elapsed(0)

        answer, error = self.game.submit_answer(
            self.alice,
            '1952',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )

        self.assertIsNone(error)
        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.tolerance_at_submit, 0)
        self.assertEqual(answer.points_earned, 10)

    def test_later_answer_inside_expanded_tolerance_gets_reduced_points(self):
        started_at = self.start_question_at_elapsed(0)

        answer, _ = self.game.submit_answer(
            self.alice,
            '1954',
            submitted_at=started_at + timezone.timedelta(seconds=10),
        )

        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.step_index, 2)
        self.assertEqual(answer.tolerance_at_submit, 2)
        self.assertEqual(answer.points_earned, 8)

    def test_answer_outside_current_tolerance_gets_zero_points(self):
        started_at = self.start_question_at_elapsed(0)

        answer, _ = self.game.submit_answer(
            self.alice,
            '1954',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )

        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.points_earned, 0)

    def test_late_answer_inside_max_tolerance_gets_minimum_points(self):
        started_at = self.start_question_at_elapsed(0)

        answer, _ = self.game.submit_answer(
            self.alice,
            '1962',
            submitted_at=started_at + timezone.timedelta(seconds=54),
        )

        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.tolerance_at_submit, 10)
        self.assertEqual(answer.points_earned, 1)

    def test_non_numeric_and_empty_answers_store_zero_points(self):
        started_at = self.start_question_at_elapsed(0)

        first, _ = self.game.submit_answer(
            self.alice,
            'keine zahl',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )
        second, _ = self.game.submit_answer(
            self.bob,
            '',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )

        self.assertFalse(first.is_numeric)
        self.assertEqual(first.points_earned, 0)
        self.assertFalse(second.is_numeric)
        self.assertEqual(second.points_earned, 0)

    def test_answer_after_effective_time_limit_is_rejected(self):
        self.question.time_limit = 10
        self.question.save(update_fields=['time_limit'])
        started_at = self.start_question_at_elapsed(0)

        answer, error = self.game.submit_answer(
            self.alice,
            '1952',
            submitted_at=started_at + timezone.timedelta(seconds=10),
        )

        self.assertIsNone(answer)
        self.assertIn('beendet', error)

    def test_first_answer_is_kept_for_rejoin_and_duplicate_submit(self):
        started_at = self.start_question_at_elapsed(0)

        first, _ = self.game.submit_answer(
            self.alice,
            '1952',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )
        duplicate, message = self.game.submit_answer(
            self.alice,
            '1954',
            submitted_at=started_at + timezone.timedelta(seconds=10),
        )

        state = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(duplicate.id, first.id)
        self.assertIn('bereits', message)
        self.assertEqual(state['own_answer']['raw_answer'], '1952')
        self.assertFalse(state['can_answer'])

    def test_spectator_and_late_pending_participants_cannot_answer(self):
        started_at = self.start_question_at_elapsed(0)
        late = WannWarDasParticipant.objects.create(
            quiz=self.game,
            name='Charlie',
            hub_session_code=self.session.code,
        )

        spectator_answer, spectator_error = self.game.submit_answer(
            None,
            '1952',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )
        late_answer, late_error = self.game.submit_answer(
            late,
            '1952',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )

        self.assertIsNone(spectator_answer)
        self.assertIn('spielberechtigt', spectator_error)
        self.assertIsNone(late_answer)
        self.assertIn('spielberechtigt', late_error)

    def test_reveal_scorebox_and_total_score_use_server_results(self):
        started_at = self.start_question_at_elapsed(0)
        self.game.submit_answer(self.alice, '1952', submitted_at=started_at + timezone.timedelta(seconds=1))
        self.game.submit_answer(self.bob, '1900', submitted_at=started_at + timezone.timedelta(seconds=1))
        self.game.reveal_current_question()

        state = self.game.serialize_state(self.session.code, 'Alice')
        alice_row = next(row for row in state['scorebox']['rows'] if row['participant_name'] == 'Alice')
        bob_row = next(row for row in state['scorebox']['rows'] if row['participant_name'] == 'Bob')

        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertEqual(self.alice.total_score, 10)
        self.assertEqual(self.bob.total_score, 0)
        self.assertEqual(alice_row['entries'][0]['points'], 10)
        self.assertEqual(bob_row['entries'][0]['points'], 0)
        self.assertTrue(state['own_answer']['is_correct'])

    def test_tutorial_question_does_not_score_or_enter_regular_scorebox(self):
        self.game.tutorial_question = self.question
        self.game.save(update_fields=['tutorial_question'])
        self.game.start_question(self.question, self.session.code, is_tutorial_round=True)
        started_at = self.game.question_started_at

        answer, _ = self.game.submit_answer(
            self.alice,
            '1952',
            submitted_at=started_at + timezone.timedelta(seconds=1),
        )
        self.game.reveal_current_question()
        state = self.game.serialize_state(self.session.code, 'Alice')

        self.alice.refresh_from_db()
        self.assertEqual(answer.points_earned, 0)
        self.assertEqual(self.alice.total_score, 0)
        self.assertEqual(state['scorebox']['questions'], [])

    def test_scores_feed_simple_and_ranking_leaderboards(self):
        started_at = self.start_question_at_elapsed(0)
        self.game.submit_answer(self.alice, '1952', submitted_at=started_at + timezone.timedelta(seconds=1))
        self.game.submit_answer(self.bob, '1900', submitted_at=started_at + timezone.timedelta(seconds=1))
        self.game.end_quiz()

        simple = get_leaderboard_data(self.session)
        alice = next(row for row in simple['participants'] if row['name'] == 'Alice')
        bob = next(row for row in simple['participants'] if row['name'] == 'Bob')
        self.assertEqual(alice['game_scores'][f'step:{self.step.id}'], 10)
        self.assertEqual(bob['game_scores'][f'step:{self.step.id}'], 0)

        self.session.overall_scoring_mode = HubSession.OVERALL_SCORING_RANKING
        self.session.save(update_fields=['overall_scoring_mode'])
        ranking = get_leaderboard_data(self.session)
        alice_ranked = next(row for row in ranking['participants'] if row['name'] == 'Alice')
        bob_ranked = next(row for row in ranking['participants'] if row['name'] == 'Bob')

        self.assertEqual(alice_ranked['game_base_scores'][f'step:{self.step.id}'], 2)
        self.assertEqual(bob_ranked['game_base_scores'][f'step:{self.step.id}'], 1)

    def test_new_session_does_not_hydrate_old_current_question(self):
        self.start_question_at_elapsed(0)
        other_session = HubSession.objects.create(code='W67890', name='Neu', is_active=True, started_at=timezone.now())
        HubGameStep.objects.create(
            session=other_session,
            order=0,
            game_key='wann_war_das',
            room_code=self.game.room_code,
            title=self.game.title,
        )

        state = self.game.serialize_state(other_session.code)

        self.assertEqual(state['scorebox']['rows'], [])
        self.assertFalse(state['can_answer'])
