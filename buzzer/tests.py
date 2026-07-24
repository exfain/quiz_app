import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession
from games_hub.views import get_leaderboard_data

from .consumers import BuzzerConsumer
from .models import BuzzerGame, BuzzerParticipant, BuzzerRound


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class BuzzerStartFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('buzzer-start-host', password='pw', is_staff=True)
        self.game = BuzzerGame.objects.create(title='Buzzer Startflow', creator=self.user, points_per_correct=2)
        self.session = HubSession.objects.create(
            code='BUZZ01',
            name='Buzzer Startflow',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(
                session=self.session,
                nickname=name,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )

    def make_consumer(self):
        consumer = BuzzerConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'buzzer_{self.game.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_monitor_and_player_hydration_before_start_remain_waiting(self):
        client = Client()
        client.force_login(self.user)
        monitor = client.get(
            reverse('admin_dashboard:buzzer_monitor', args=[self.game.room_code]),
            {'hub_session': self.session.code},
        )
        player = Client().get(
            reverse('buzzer:play', args=[self.game.room_code, 'Alice']),
            {'hub_session': self.session.code},
        )

        self.assertEqual(monitor.status_code, 200)
        self.assertEqual(player.status_code, 200)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertFalse(alice.is_active)
        self.assertContains(player, 'Warte darauf, dass der Host das Spiel startet.')
        self.assertContains(monitor, 'Spiel starten')

    def test_round_cannot_start_before_game_and_does_not_start_it_implicitly(self):
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_round)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'waiting')
        self.assertEqual(self.game.current_round_number, 0)
        self.assertIsNone(self.game.current_round)
        self.assertIn('Starte zuerst das Spiel', sent_messages[-1]['message'])
        self.assertEqual(consumer.channel_layer.group_messages, [])

    def test_game_start_broadcasts_to_exact_session_without_opening_round(self):
        other_session = HubSession.objects.create(
            code='BUZZ02',
            name='Andere Session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=other_session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_game)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.active_hub_session_code, self.session.code)
        self.assertEqual(self.game.current_round_number, 0)
        self.assertIsNone(self.game.current_round)
        self.assertFalse(self.game.buzzer_open)
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

    def test_round_buzz_and_host_decisions_broadcast_each_live_transition(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_round)({'hub_session': self.session.code})
        async_to_sync(consumer.handle_admin_open_buzzer)({'hub_session': self.session.code})
        async_to_sync(consumer.handle_participant_buzz)({
            'hub_session': self.session.code,
            'participant_name': 'Alice',
        })
        async_to_sync(consumer.handle_admin_mark_wrong)({'hub_session': self.session.code})

        event_types = [
            message['event_type']
            for group, message in consumer.channel_layer.group_messages
            if group == consumer.room_group_name
        ]
        self.assertEqual(
            event_types,
            ['round_started', 'buzzer_opened', 'buzz_accepted', 'answer_marked_wrong'],
        )
        alice_state = self.game.serialize_state(self.session.code, 'Alice')
        bob_state = self.game.serialize_state(self.session.code, 'Bob')
        self.assertTrue(alice_state['participant']['blocked'])
        self.assertFalse(alice_state['can_buzz'])
        self.assertTrue(bob_state['can_buzz'])
        self.assertEqual(alice.total_score, 0)

        async_to_sync(consumer.handle_participant_buzz)({
            'hub_session': self.session.code,
            'participant_name': 'Bob',
        })
        async_to_sync(consumer.handle_admin_mark_correct)({'hub_session': self.session.code})

        bob.refresh_from_db()
        self.assertEqual(bob.total_score, 2)
        final_event = [
            message for group, message in consumer.channel_layer.group_messages
            if group == consumer.room_group_name
        ][-1]
        self.assertEqual(final_event['event_type'], 'answer_marked_correct')
        self.assertEqual(final_event['round']['status'], 'answered')

    def test_participant_from_previous_session_cannot_buzz_in_current_round(self):
        old_session = HubSession.objects.create(
            code='OLD001',
            name='Alte Buzzer Session',
            is_active=True,
            started_at=timezone.now(),
        )
        old_hub_participant = HubParticipant.objects.create(
            session=old_session,
            nickname='Old Alice',
            scoring_eligible=True,
        )
        old_step = HubGameStep.objects.create(
            session=old_session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.objects.create(
            session=old_session,
            game_step=old_step,
            participant=old_hub_participant,
            active_player=True,
            included_in_scoring=True,
        )
        old_player = BuzzerParticipant.objects.create(
            quiz=self.game,
            name='Old Alice',
            hub_session_code=old_session.code,
            is_active=True,
        )

        self.game.start_quiz(self.session.code)
        self.game.start_round(self.session.code)
        self.game.open_buzzer()
        old_player.refresh_from_db()
        old_player.is_active = True
        old_player.save(update_fields=['is_active'])

        success, message = self.game.accept_buzz(old_player)

        self.assertFalse(success)
        self.assertIn('buzzberechtigt', message)

    def test_rejoin_state_covers_waiting_open_locked_wrong_next_round_and_completed(self):
        waiting = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(waiting['game']['status'], 'waiting')
        self.assertEqual(waiting['round']['number'], 0)
        self.assertFalse(waiting['can_buzz'])

        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        started = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(started['game']['status'], 'active')
        self.assertEqual(started['round']['number'], 0)

        self.assertIsNotNone(self.game.start_round(self.session.code))
        self.assertTrue(self.game.open_buzzer())
        opened = self.game.serialize_state(self.session.code, 'Alice')
        self.assertTrue(opened['can_buzz'])

        self.assertTrue(self.game.accept_buzz(alice)[0])
        locked = self.game.serialize_state(self.session.code, 'Alice')
        self.assertTrue(locked['participant']['has_answer_right'])
        self.assertFalse(locked['can_buzz'])

        self.assertTrue(self.game.mark_current_wrong())
        blocked = self.game.serialize_state(self.session.code, 'Alice')
        self.assertTrue(blocked['participant']['blocked'])
        self.assertFalse(blocked['can_buzz'])

        self.assertTrue(self.game.end_current_round())
        self.assertIsNotNone(self.game.start_round(self.session.code))
        next_round = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(next_round['round']['number'], 2)
        self.assertFalse(next_round['participant']['blocked'])

        self.game.end_quiz()
        completed = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(completed['game']['status'], 'completed')
        self.assertFalse(completed['can_buzz'])


class BuzzerEndFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('buzzer-end-host', password='pw', is_staff=True)
        self.game = BuzzerGame.objects.create(title='Buzzer Endflow', creator=self.user, points_per_correct=2)
        self.session = HubSession.objects.create(
            code='BEND01',
            name='Buzzer Endflow',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(
                session=self.session,
                nickname=name,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)
        self.game.start_quiz(self.session.code)
        self.alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)

    def make_consumer(self):
        consumer = BuzzerConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'buzzer_{self.game.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_host_and_player_views_have_working_lobby_routes(self):
        admin_client = Client()
        admin_client.force_login(self.user)
        host_response = admin_client.get(
            reverse('admin_dashboard:buzzer_monitor', args=[self.game.room_code]),
            {'hub_session': self.session.code},
        )
        player_response = Client().get(
            reverse('buzzer:play', args=[self.game.room_code, self.alice.name]),
            {'hub_session': self.session.code},
        )

        self.assertContains(host_response, 'Zur Lobby')
        self.assertContains(host_response, 'Zur Session-Übersicht')
        self.assertContains(player_response, 'Zur Lobby zurückkehren')
        self.assertContains(player_response, 'participant-return-to-lobby')
        self.assertContains(player_response, 'id="returnToLobbyActions" hidden')
        self.assertContains(player_response, 'id="returnToLobbyBtn" disabled aria-hidden="true"')

    def test_participant_return_marks_inactive_and_lobby_suppresses_auto_redirect(self):
        response = Client().post(
            reverse('games_hub:participant_return_to_lobby', args=[self.session.code]),
            data=json.dumps({
                'game_key': 'buzzer',
                'room_code': self.game.room_code,
                'participant_name': self.alice.name,
            }),
            content_type='application/json',
        )
        lobby_response = Client().get(
            reverse('games_hub:lobby', args=[self.session.code]),
            {'nickname': self.alice.name, 'return': '1'},
        )

        self.alice.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.alice.is_active)
        self.assertContains(lobby_response, 'returnedToLobby')
        self.assertContains(lobby_response, 'if (returnedToLobby)')

    def test_end_game_does_not_reactivate_returned_participant_and_only_finalizes_once(self):
        self.alice.is_active = False
        self.alice.save(update_fields=['is_active'])
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})
        first_group_message_count = len(consumer.channel_layer.group_messages)
        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        self.alice.refresh_from_db()
        self.assertEqual(self.game.status, 'completed')
        self.assertFalse(self.alice.is_active)
        self.assertEqual(first_group_message_count, 2)
        self.assertEqual(len(consumer.channel_layer.group_messages), first_group_message_count)
        self.assertEqual(sent_messages[-1]['type'], 'buzzer_state')

    def test_completed_rejoin_result_has_lobby_button_and_actions_are_rejected(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()
        self.assertTrue(self.game.accept_buzz(self.alice)[0])
        self.game.end_quiz()
        previous_score = self.alice.total_score

        result_response = Client().get(
            reverse('buzzer:result', args=[self.game.room_code, self.alice.name]),
            {'hub_session': self.session.code},
        )
        buzz_success, buzz_message = self.game.accept_buzz(self.bob)
        correct_success = self.game.mark_current_correct()
        round_result = self.game.start_round(self.session.code)
        self.alice.refresh_from_db()

        self.assertContains(result_response, 'Zur Lobby zurückkehren')
        self.assertContains(result_response, 'participant-return-to-lobby')
        self.assertFalse(buzz_success)
        self.assertIn('nicht freigegeben', buzz_message)
        self.assertFalse(correct_success)
        self.assertIsNone(round_result)
        self.assertEqual(self.alice.total_score, previous_score)

    def test_completed_host_monitor_shows_final_state_and_lobby_route(self):
        self.game.end_quiz()
        admin_client = Client()
        admin_client.force_login(self.user)

        response = admin_client.get(
            reverse('admin_dashboard:buzzer_monitor', args=[self.game.room_code]),
            {'hub_session': self.session.code},
        )

        self.assertContains(response, 'Spiel beendet. Der finale Spielstand ist sichtbar.')
        self.assertContains(response, 'Zur Lobby')
        self.assertContains(response, 'Zur Session-Übersicht')


class BuzzerGameTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('admin', password='pw', is_staff=True)
        self.game = BuzzerGame.objects.create(
            title='Mündliche Fragen',
            creator=self.user,
            points_per_correct=2,
        )
        self.session = HubSession.objects.create(
            code='S12345',
            name='Buzzer Abend',
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
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)
        self.game.start_quiz(self.session.code)
        self.alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)

    def test_admin_can_create_buzzer_game_without_questions(self):
        client = Client()
        client.force_login(self.user)

        response = client.post(
            reverse('admin_dashboard:create_buzzer_game'),
            data=json.dumps({
                'title': 'Buzzer ohne Fragebank',
                'points_per_correct': 3,
                'planned_rounds': 5,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        created = BuzzerGame.objects.get(title='Buzzer ohne Fragebank')
        self.assertEqual(created.points_per_correct, 3)
        self.assertEqual(created.planned_rounds, 5)
        self.assertFalse(hasattr(created, 'selected_questions'))

    def test_buzz_outside_open_phase_is_rejected(self):
        self.game.start_round(self.session.code)

        success, message = self.game.accept_buzz(self.alice)

        self.assertFalse(success)
        self.assertIn('nicht freigegeben', message)

    def test_first_valid_buzz_wins_and_second_is_rejected(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()

        first_success, _ = self.game.accept_buzz(self.alice)
        second_success, second_message = self.game.accept_buzz(self.bob)

        self.game.refresh_from_db()
        self.assertTrue(first_success)
        self.assertFalse(second_success)
        self.assertEqual(self.game.current_buzz_participant, self.alice)
        self.assertFalse(self.game.buzzer_open)
        self.assertIn('schneller', second_message)

    def test_spectator_late_pending_and_blocked_participants_cannot_buzz(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()

        spectator_success, spectator_message = self.game.accept_buzz(None)
        late = BuzzerParticipant.objects.create(
            quiz=self.game,
            name='Charlie',
            hub_session_code=self.session.code,
        )
        late_success, late_message = self.game.accept_buzz(late)

        self.assertFalse(spectator_success)
        self.assertIn('buzzberechtigt', spectator_message)
        self.assertFalse(late_success)
        self.assertIn('buzzberechtigt', late_message)

        self.assertTrue(self.game.accept_buzz(self.alice)[0])
        self.game.mark_current_wrong()
        blocked_success, blocked_message = self.game.accept_buzz(self.alice)

        self.assertFalse(blocked_success)
        self.assertIn('gesperrt', blocked_message)

    def test_wrong_answer_reopens_buzzer_for_others_and_new_round_resets_locks(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()
        self.assertTrue(self.game.accept_buzz(self.alice)[0])

        self.game.mark_current_wrong()
        self.game.refresh_from_db()
        state = self.game.serialize_state(self.session.code)

        self.assertTrue(self.game.buzzer_open)
        self.assertIn('Alice', state['round']['blocked_participants'])
        self.assertTrue(self.game.accept_buzz(self.bob)[0])

        self.game.end_current_round()
        self.game.start_round(self.session.code)
        next_state = self.game.serialize_state(self.session.code)

        self.assertEqual(next_state['round']['number'], 2)
        self.assertEqual(next_state['round']['blocked_participants'], [])

    def test_correct_answer_awards_configured_points(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()
        self.assertTrue(self.game.accept_buzz(self.alice)[0])

        self.assertTrue(self.game.mark_current_correct())
        self.alice.refresh_from_db()
        self.game.refresh_from_db()

        self.assertEqual(self.alice.total_score, 2)
        self.assertEqual(self.game.round_state, 'answered')
        self.assertFalse(self.game.buzzer_open)

    def test_player_rejoin_state_contains_button_status_round_and_score(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()

        state = self.game.serialize_state(self.session.code, 'Alice')

        self.assertEqual(state['round']['number'], 1)
        self.assertTrue(state['can_buzz'])
        self.assertEqual(state['participant']['score'], 0)

    def test_round_scorebox_uses_current_session_and_preserves_zero(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()
        self.assertTrue(self.game.accept_buzz(self.alice)[0])
        self.assertTrue(self.game.mark_current_correct())
        BuzzerRound.objects.create(
            quiz=self.game,
            hub_session_code='OLD01',
            round_number=1,
            status='answered',
            correct_participant=self.bob,
        )

        alice_state = self.game.serialize_state(self.session.code, 'Alice')
        bob_state = self.game.serialize_state(self.session.code, 'Bob')

        self.assertEqual(alice_state['round_results'], [{
            'number': 1,
            'status': 'answered',
            'points_earned': 1,
            'max_points': 1,
        }])
        self.assertEqual(bob_state['round_results'], [{
            'number': 1,
            'status': 'answered',
            'points_earned': 0,
            'max_points': 1,
        }])

    def test_buzzer_player_template_has_vhs_identity_and_scorebox_hooks(self):
        response = Client().get(
            reverse('buzzer:play', args=[self.game.room_code, self.alice.name]),
            {'hub_session': self.session.code},
        )

        self.assertContains(response, '<title>Buzzer: Mündliche Fragen - QuizMaster</title>', html=True)
        self.assertContains(response, 'data-participant-name="Alice"')
        self.assertContains(response, 'id="buzzerScoreBox"')
        self.assertContains(response, 'id="buzzerScoreRows"')
        self.assertContains(response, 'class="buzzer-vhs-game-meta d-none"')
        self.assertContains(
            response,
            '<span class="buzzer-vhs-game-meta d-none" title="Mündliche Fragen · Spiel 1">'
            'MÜNDLICHE FRAGEN · SPIEL 1'
            '</span>',
            html=True,
        )
        self.assertContains(response, 'setBuzzedState(button, Boolean(participant.has_answer_right));')
        self.assertContains(response, 'if (button.disabled || button.classList.contains(\'is-buzzed\')) return;')
        self.assertNotContains(
            response,
            'window.setTimeout(() => button.classList.remove(\'is-buzzed\'), 260)',
        )
        self.assertNotContains(response, "querySelector('.buzzer-vhs-game-meta')")

        self.game.title = 'Buzztest'
        self.game.save(update_fields=['title'])
        buzztest_response = Client().get(
            reverse('buzzer:play', args=[self.game.room_code, self.alice.name]),
            {'hub_session': self.session.code},
        )
        self.assertContains(
            buzztest_response,
            '<span class="buzzer-vhs-game-meta d-none" title="Buzztest · Spiel 1">'
            'BUZZTEST · SPIEL 1'
            '</span>',
            html=True,
        )

    def test_new_session_does_not_hydrate_old_current_round(self):
        self.game.start_round(self.session.code)
        self.game.open_buzzer()
        other_session = HubSession.objects.create(code='S67890', name='Neu', is_active=True, started_at=timezone.now())
        HubGameStep.objects.create(
            session=other_session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )

        state = self.game.serialize_state(other_session.code)

        self.assertEqual(state['round']['number'], 0)
        self.assertFalse(state['round']['buzzer_open'])

    def test_buzzer_scores_feed_simple_and_ranking_leaderboards(self):
        self.alice.total_score = 4
        self.alice.save(update_fields=['total_score'])
        self.bob.total_score = 0
        self.bob.save(update_fields=['total_score'])
        self.game.end_quiz()

        simple = get_leaderboard_data(self.session)
        alice = next(row for row in simple['participants'] if row['name'] == 'Alice')
        bob = next(row for row in simple['participants'] if row['name'] == 'Bob')
        self.assertEqual(alice['game_scores'][f'step:{self.step.id}'], 4)
        self.assertEqual(bob['game_scores'][f'step:{self.step.id}'], 0)

        self.session.overall_scoring_mode = HubSession.OVERALL_SCORING_RANKING
        self.session.save(update_fields=['overall_scoring_mode'])
        ranking = get_leaderboard_data(self.session)
        alice_ranked = next(row for row in ranking['participants'] if row['name'] == 'Alice')
        bob_ranked = next(row for row in ranking['participants'] if row['name'] == 'Bob')

        self.assertEqual(alice_ranked['game_base_scores'][f'step:{self.step.id}'], 2)
        self.assertEqual(bob_ranked['game_base_scores'][f'step:{self.step.id}'], 1)
