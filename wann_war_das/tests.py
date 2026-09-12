import json
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from games_hub.authoritative_state import current_snapshot, reset_question_flow
from games_hub.models import (
    GameRuntimeState,
    HubGameParticipantSnapshot,
    HubGameStep,
    HubParticipant,
    HubSession,
)
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

    def enable_manual_question_flow(self):
        return reset_question_flow(
            game_key='wann_war_das',
            room_code=self.game.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def phase_action(self, question_id, **extra):
        snapshot = current_snapshot('wann_war_das', self.game.room_code, self.session.code)
        return {
            'hub_session': self.session.code,
            'question_id': question_id,
            'game_id': snapshot.get('game_id'),
            'state_revision': snapshot['state_revision'],
            'client_action_id': str(uuid.uuid4()),
            **extra,
        }

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
        self.assertContains(response, 'FRAGE SENDEN')
        self.assertContains(response, 'ANTWORT FREIGEBEN')
        self.assertNotContains(response, 'ANTWORTBEREICH ANZEIGEN')

    def test_player_route_before_start_stays_in_waiting_state_without_blocking_start(self):
        response = Client().get(
            reverse('wann_war_das:play', args=[self.game.room_code, 'Alice']),
            {'hub_session': self.session.code},
        )

        self.assertEqual(response.status_code, 200)
        participant = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertFalse(participant.is_active)
        self.assertContains(response, 'beginnt gleich!')

        consumer, sent_messages = self.make_consumer()
        async_to_sync(consumer.handle_admin_start_game)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(sent_messages, [])
        self.assertEqual(self.game.status, 'active')
        self.assertTrue(participant.is_active)

    def test_player_template_has_scoped_vhs_chrome_scorebox_and_tolerance_scale(self):
        response = Client().get(
            reverse('wann_war_das:play', args=[self.game.room_code, 'Alice']),
            {'hub_session': self.session.code},
        )
        content = response.content.decode('utf-8')

        self.assertIn('<title>Wann war das?: Startflow - QuizMaster</title>', content)
        self.assertIn('data-participant-name="Alice"', content)
        self.assertIn('id="wannWarDasScoreRows"', content)
        self.assertIn('id="toleranceScale"', content)
        self.assertIn('class="wann-war-das-meta"', content)
        self.assertIn('class="wann-war-das-game-line"', content)
        self.assertIn('class="wann-war-das-round-meta text-secondary"', content)
        self.assertIn('class="wann-war-das-score-box score-box d-none"', content)
        self.assertIn('class="wann-war-das-reveal__hero"', content)
        self.assertIn('class="wann-war-das-reveal__metrics"', content)
        self.assertIn(
            "document.getElementById('correctAnswerText').textContent = fmt(question.correct_answer);",
            content,
        )
        self.assertNotIn(
            "document.getElementById('correctAnswerText').textContent = question.formatted_correct_answer",
            content,
        )
        self.assertIn('wann-war-das-tolerance__timer--top', content)
        self.assertIn('wann-war-das-tolerance__timer--bottom', content)
        self.assertEqual(
            content.count('wann-war-das-tolerance__fill vhs-theme-timer-fill'),
            2,
        )
        self.assertNotIn('>Restzeit<', content)
        self.assertNotIn(
            'Je frueher du innerhalb der Toleranz antwortest, desto mehr Punkte bekommst du.',
            content,
        )
        self.assertIn('timer?.elapsed_seconds', content)
        self.assertIn('timer?.step_index', content)
        self.assertIn('remainingQuestionPresentationDelay(state)', content)
        self.assertIn('timer.current_points ?? question.max_points', content)
        self.assertIn('const segmentStart = stepIndex === 0 ? 0 : stepIndex - 0.5;', content)
        self.assertIn('const segmentEnd = stepIndex + 0.5;', content)
        self.assertIn('const halfScaleWidth = sideCellCount + 0.5;', content)
        self.assertIn(
            'entry?.points !== null && entry?.points !== undefined',
            content,
        )

        css_path = finders.find('themes/vhs/vhs.css')
        self.assertIsNotNone(css_path)
        css = Path(css_path).read_text(encoding='utf-8')
        self.assertIn(
            'body.wann-war-das-play-page .wann-war-das-tolerance__tick.is-active',
            css,
        )
        self.assertIn(
            'body.wann-war-das-play-page .vhs-theme-shell #questionArea .vhs-quick-quiz-submit',
            css,
        )
        self.assertIn(
            'body.wann-war-das-play-page .qa-score-widget\n'
            '  #wannWarDasScoreRows .score-box__row',
            css,
        )
        self.assertIn(
            'background: linear-gradient(90deg, #efe4ba, #e7c84d, #cf6338, #78627e, #557d9b);',
            css,
        )
        self.assertIn('grid-auto-rows: 42px;', css)
        self.assertIn('height: 42px;', css)
        self.assertIn('inset: 0 3px;', css)
        self.assertIn('width: min(100%, 480px);', css)
        self.assertIn('grid-template-columns: minmax(0, 1fr) minmax(138px, 170px);', css)
        self.assertIn(
            'body.wann-war-das-play-page .wann-war-das-game-line {\n'
            '  display: grid;',
            css,
        )
        self.assertNotIn(
            'body.wann-war-das-play-page\n'
            '  .wann-war-das-game-line .session-game-number::before',
            css,
        )
        self.assertIn(
            '#revealArea.wann-war-das-reveal {',
            css,
        )
        self.assertNotIn(
            'body.wann-war-das-play-page .wann-war-das-tolerance__tick.is-center {\n'
            '  font-weight: 1000;\n'
            '  box-shadow:',
            css,
        )

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
        self.enable_manual_question_flow()
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_question)(self.phase_action(self.question.id))

        self.game.refresh_from_db()
        self.assertEqual(self.game.question_state, 'ready')
        self.assertEqual(self.game.current_question, self.question)
        self.assertIsNone(self.game.question_started_at)
        question_event = next(
            message for group, message in consumer.channel_layer.group_messages
            if group == consumer.room_group_name
        )
        self.assertEqual(question_event['event_type'], 'question_started')
        self.assertEqual(question_event['question_phase'], 'prompt_visible')
        self.assertIsNotNone(question_event['question_presented_at'])
        self.assertIsNotNone(question_event['question_visible_at'])
        self.assertIsNone(question_event['answering_started_at'])
        self.assertIsNone(question_event['answering_deadline_at'])
        self.assertIsNone(question_event['timer'])

    def test_open_answering_is_rejected_before_question_is_visible(self):
        self.game.start_quiz(self.session.code)
        self.enable_manual_question_flow()
        consumer, sent_messages = self.make_consumer()
        async_to_sync(consumer.handle_admin_start_question)(self.phase_action(self.question.id))

        async_to_sync(consumer.handle_admin_open_answering)(self.phase_action(self.question.id))

        self.game.refresh_from_db()
        self.assertEqual(self.game.question_state, 'ready')
        self.assertIsNone(self.game.question_started_at)
        self.assertEqual(sent_messages[-1]['type'], 'action_rejected')
        self.assertEqual(sent_messages[-1]['code'], 'question_not_visible')

    def test_ten_seconds_of_host_reading_do_not_advance_tolerance(self):
        self.game.start_quiz(self.session.code)
        self.enable_manual_question_flow()
        consumer, _ = self.make_consumer()
        presented_at = timezone.now()
        present_action = self.phase_action(self.question.id)
        decision = async_to_sync(consumer.present_wann_war_das_question)(
            self.game.id,
            self.question.id,
            self.session.code,
            False,
            present_action,
            self.question.get_effective_time_limit(),
            presented_at,
        )
        self.assertTrue(decision.accepted)
        participant = self.game.participants.get(
            name='Alice',
            hub_session_code=self.session.code,
        )
        early_answer, early_error = self.game.submit_answer(
            participant,
            str(self.question.correct_answer),
            submitted_at=presented_at + timezone.timedelta(seconds=10),
        )
        self.assertIsNone(early_answer)
        self.assertIn('nicht aktiv', early_error)

        release_at = presented_at + timezone.timedelta(seconds=11)
        open_action = self.phase_action(self.question.id)
        opened = async_to_sync(consumer.open_wann_war_das_answering)(
            self.game.id,
            self.question.id,
            self.session.code,
            open_action,
            release_at,
        )

        self.assertTrue(opened.accepted)
        self.game.refresh_from_db()
        self.assertEqual(self.game.question_started_at, release_at)
        self.assertEqual(opened.snapshot['question_phase'], 'answering_open')
        self.assertIsNone(opened.snapshot['content_revealed_at'])
        at_release = self.question.get_timer_state(self.game.question_started_at, release_at)
        five_seconds_later = self.question.get_timer_state(
            self.game.question_started_at,
            release_at + timezone.timedelta(seconds=5),
        )
        self.assertEqual(at_release['elapsed_seconds'], 0)
        self.assertEqual(at_release['current_tolerance'], self.question.start_tolerance)
        self.assertEqual(five_seconds_later['elapsed_seconds'], 5)
        self.assertEqual(
            five_seconds_later['current_tolerance'],
            self.question.tolerance_for_step(self.question.step_index_for_elapsed(5)),
        )
        deadline = parse_datetime(opened.snapshot['answering_deadline_at'])
        self.assertEqual(
            deadline,
            release_at + timezone.timedelta(seconds=self.question.get_effective_time_limit()),
        )
        answer, answer_error = self.game.submit_answer(
            participant,
            str(self.question.correct_answer),
            submitted_at=release_at + timezone.timedelta(seconds=1),
        )
        self.assertIsNone(answer_error)
        self.assertEqual(answer.tolerance_at_submit, self.question.start_tolerance)

        duplicate = async_to_sync(consumer.open_wann_war_das_answering)(
            self.game.id,
            self.question.id,
            self.session.code,
            open_action,
            release_at + timezone.timedelta(seconds=5),
        )
        self.game.refresh_from_db()
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(self.game.question_started_at, release_at)

    def test_reload_reconstructs_running_tolerance_from_authoritative_start(self):
        self.game.start_quiz(self.session.code)
        self.enable_manual_question_flow()
        consumer, _ = self.make_consumer()
        now = timezone.now()
        presented_at = now - timezone.timedelta(seconds=7)
        decision = async_to_sync(consumer.present_wann_war_das_question)(
            self.game.id,
            self.question.id,
            self.session.code,
            False,
            self.phase_action(self.question.id),
            self.question.get_effective_time_limit(),
            presented_at,
        )
        self.assertTrue(decision.accepted)
        opened_at = now - timezone.timedelta(seconds=5)
        opened = async_to_sync(consumer.open_wann_war_das_answering)(
            self.game.id,
            self.question.id,
            self.session.code,
            self.phase_action(self.question.id),
            opened_at,
        )
        self.assertTrue(opened.accepted)

        state = self.game.serialize_state(self.session.code, 'Alice')

        self.assertEqual(state['question_phase'], 'answering_open')
        self.assertTrue(state['can_answer'])
        self.assertTrue(state['timer_running'])
        self.assertAlmostEqual(state['timer']['elapsed_seconds'], 5, delta=0.5)
        self.assertEqual(
            state['timer']['current_tolerance'],
            self.question.tolerance_for_step(self.question.step_index_for_elapsed(5)),
        )

    def test_second_question_resets_the_previous_answer_clock(self):
        second_question = WannWarDasQuestion.objects.create(
            created_by=self.user,
            question_text='Wann begann die zweite Runde?',
            correct_answer=2027,
        )
        self.game.selected_questions.add(second_question)
        self.game.start_quiz(self.session.code)
        self.enable_manual_question_flow()
        consumer, _ = self.make_consumer()
        first_presented_at = timezone.now() - timezone.timedelta(seconds=3)
        first = async_to_sync(consumer.present_wann_war_das_question)(
            self.game.id,
            self.question.id,
            self.session.code,
            False,
            self.phase_action(self.question.id),
            self.question.get_effective_time_limit(),
            first_presented_at,
        )
        self.assertTrue(first.accepted)
        opened = async_to_sync(consumer.open_wann_war_das_answering)(
            self.game.id,
            self.question.id,
            self.session.code,
            self.phase_action(self.question.id),
            first_presented_at + timezone.timedelta(seconds=2),
        )
        self.assertTrue(opened.accepted)
        self.game.refresh_from_db()
        self.assertTrue(self.game.reveal_current_question(self.session.code))

        second_presented_at = timezone.now()
        second = async_to_sync(consumer.present_wann_war_das_question)(
            self.game.id,
            second_question.id,
            self.session.code,
            False,
            self.phase_action(second_question.id),
            second_question.get_effective_time_limit(),
            second_presented_at,
        )

        self.assertTrue(second.accepted)
        self.game.refresh_from_db()
        state = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(self.game.current_question_id, second_question.id)
        self.assertEqual(self.game.question_state, 'ready')
        self.assertIsNone(self.game.question_started_at)
        self.assertEqual(state['question_phase'], 'prompt_visible')
        self.assertIsNone(state['answering_started_at'])
        self.assertIsNone(state['answering_deadline_at'])
        self.assertIsNone(state['timer'])
        self.assertFalse(state['can_answer'])

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
        self.assertGreater(started['state_revision'], waiting['state_revision'])

        self.assertTrue(self.game.start_question(self.question, self.session.code))
        active = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(active['question_state'], 'active')
        self.assertTrue(active['can_answer'])
        self.assertIsNotNone(active['timer'])
        self.assertGreater(active['state_revision'], started['state_revision'])

        self.game.reveal_current_question()
        revealed = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(revealed['question_state'], 'revealed')
        self.assertFalse(revealed['can_answer'])
        self.assertGreater(revealed['state_revision'], active['state_revision'])


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

        self.assertContains(host_response, 'Zur Session-Übersicht')
        self.assertContains(host_response, 'data-host-game-leave')
        self.assertNotContains(host_response, f'href="/hub/lobby/{self.session.code}/"')
        self.assertContains(player_response, 'Zur Lobby zurückkehren')
        self.assertContains(player_response, 'participant-return-to-lobby')
        player_content = player_response.content.decode('utf-8')
        self.assertIn('class="d-none" id="returnToLobbyActions" hidden', player_content)
        self.assertIn('id="returnToLobbyBtn" disabled aria-hidden="true"', player_content)
        self.assertIn('setReturnToLobbyAvailable(gameEnded)', player_content)

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

    def test_timer_stages_match_tolerance_walls_and_finish_after_last_stage(self):
        self.question.max_tolerance = 5
        self.question.save(update_fields=['max_tolerance'])
        started_at = timezone.now()

        before_first_wall = self.question.get_timer_state(
            started_at,
            started_at + timezone.timedelta(seconds=4.999),
        )
        at_first_wall = self.question.get_timer_state(
            started_at,
            started_at + timezone.timedelta(seconds=5),
        )
        at_last_inner_wall = self.question.get_timer_state(
            started_at,
            started_at + timezone.timedelta(seconds=25),
        )
        before_outer_wall = self.question.get_timer_state(
            started_at,
            started_at + timezone.timedelta(seconds=29.999),
        )

        self.assertEqual(self.question.get_effective_time_limit(), 30)
        self.assertEqual(before_first_wall['current_tolerance'], 0)
        self.assertEqual(at_first_wall['current_tolerance'], 1)
        self.assertEqual(at_last_inner_wall['current_tolerance'], 5)
        self.assertEqual(before_outer_wall['current_tolerance'], 5)
        self.assertEqual(before_outer_wall['remaining_seconds'], 1)

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
        self.assertEqual(state['scorebox']['questions'][0]['max_points'], 10)
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
