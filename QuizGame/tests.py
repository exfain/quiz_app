import json
import os
import time
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.check_in import complete_session_check_in, participant_check_in, start_session_check_in
from games_hub.models import HubGameStep, HubGameTutorialRuntime, HubParticipant, HubSession
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser
from games_hub.tutorial_runtime import (
    activate_tutorial_runtime as activate_game_tutorial_runtime,
    mark_tutorial_completed as mark_game_tutorial_completed,
)
from games_hub.unit_tutorial_runtime import (
    get_unit_tutorial_state,
    prepare_unit_tutorial_runtime,
    start_unit_tutorial_if_needed,
)

from .consumers import QuizConsumer
from .models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion, QuizSession
from games_website.asgi import application


User = get_user_model()

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserLiveServerTestCase
except ImportError:
    _BrowserLiveServerTestCase = LiveServerTestCase


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group_name, message):
        self.group_messages.append((group_name, message))


class QuizQuestionShortAnswerTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='quiz_author', password='testpass123')

    def test_legacy_single_field_short_answer_remains_valid(self):
        question = QuizQuestion.objects.create(
            question_text='Capital of Germany?',
            question_type='short_answer',
            correct_answer='Berlin',
            created_by=self.user,
        )

        self.assertEqual(question.get_short_answer_field_count(), 1)
        self.assertEqual(question.get_short_answer_fields(), [
            {'index': 1, 'key': 'answer_1', 'label': '', 'correct_answer': 'Berlin'}
        ])
        self.assertTrue(question.is_correct_answer(' berlin '))
        self.assertEqual(question.get_formatted_correct_answer(), 'Berlin')

    def test_short_answer_with_two_fields_is_position_bound(self):
        question = QuizQuestion.objects.create(
            question_text='Name the person',
            question_type='short_answer',
            correct_answer='Ada',
            correct_answer_2='Lovelace',
            answer_label_1='Vorname',
            answer_label_2='Nachname',
            created_by=self.user,
        )

        self.assertEqual(question.get_short_answer_field_count(), 2)
        self.assertEqual(
            [field['label'] for field in question.get_short_answer_fields()],
            ['Vorname', 'Nachname'],
        )
        self.assertTrue(question.is_correct_answer({'answer_1': 'ada', 'answer_2': 'lovelace'}))
        self.assertFalse(question.is_correct_answer({'answer_1': 'Lovelace', 'answer_2': 'Ada'}))
        self.assertEqual(question.get_formatted_correct_answer(), 'Vorname: Ada | Nachname: Lovelace')

    def test_short_answer_with_four_fields_is_supported(self):
        question = QuizQuestion.objects.create(
            question_text='Enter the sequence',
            question_type='short_answer',
            correct_answer='A',
            correct_answer_2='B',
            correct_answer_3='C',
            correct_answer_4='D',
            answer_label_1='1',
            answer_label_2='2',
            answer_label_3='3',
            answer_label_4='4',
            created_by=self.user,
        )

        self.assertEqual(question.get_short_answer_field_count(), 4)
        self.assertEqual(len(question.get_short_answer_fields()), 4)
        self.assertTrue(question.is_correct_answer({
            'answer_1': 'a',
            'answer_2': 'b',
            'answer_3': 'c',
            'answer_4': 'd',
        }))
        self.assertFalse(question.is_correct_answer({
            'answer_1': 'a',
            'answer_2': 'b',
            'answer_3': 'c',
            'answer_4': '',
        }))


class QuizShortAnswerViewIntegrationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='quiz_admin',
            email='',
            password='testpass123',
        )
        self.client.force_login(self.user)

    def test_quiz_status_exposes_multi_field_short_answer_labels(self):
        question = QuizQuestion.objects.create(
            question_text='Name',
            question_type='short_answer',
            correct_answer='Ada',
            correct_answer_2='Lovelace',
            answer_label_1='Vorname',
            answer_label_2='Nachname',
            created_by=self.user,
        )
        quiz = Quiz.objects.create(title='Quick Quiz', creator=self.user, status='active')
        participant = QuizParticipant.objects.create(quiz=quiz, name='Tim', is_active=True)
        quiz.current_question = question
        quiz.save(update_fields=['current_question'])

        response = self.client.get(reverse('quiz:quiz_status', args=[quiz.room_code, participant.name]))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['current_question']['question_type'], 'short_answer')
        self.assertEqual(
            data['current_question']['short_answer_fields'],
            [
                {'index': 1, 'key': 'answer_1', 'label': 'Vorname'},
                {'index': 2, 'key': 'answer_2', 'label': 'Nachname'},
            ],
        )

    def test_admin_add_question_supports_two_short_answer_fields_with_labels(self):
        response = self.client.post(
            reverse('admin_dashboard:add_question'),
            data={
                'question_text': 'Two fields',
                'question_type': 'short_answer',
                'points': 10,
                'time_limit': 30,
                'correct_answer': 'Ada',
                'correct_answer_2': 'Lovelace',
                'answer_label_1': 'Vorname',
                'answer_label_2': 'Nachname',
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        question = QuizQuestion.objects.get(id=response.json()['question_id'])
        self.assertEqual(question.question_type, 'short_answer')
        self.assertEqual(question.points, 1)
        self.assertEqual(question.correct_answer_2, 'Lovelace')
        self.assertEqual(question.answer_label_1, 'Vorname')
        self.assertEqual(question.answer_label_2, 'Nachname')

    def test_admin_update_question_supports_four_short_answer_fields(self):
        question = QuizQuestion.objects.create(
            question_text='Before update',
            question_type='short_answer',
            correct_answer='A',
            points=42,
            created_by=self.user,
        )

        response = self.client.post(
            reverse('admin_dashboard:update_quiz_question'),
            data={
                'question_id': question.id,
                'question_text': 'Four fields',
                'question_type': 'short_answer',
                'points': 20,
                'time_limit': 45,
                'correct_answer': 'A',
                'correct_answer_2': 'B',
                'correct_answer_3': 'C',
                'correct_answer_4': 'D',
                'answer_label_1': '1',
                'answer_label_2': '2',
                'answer_label_3': '3',
                'answer_label_4': '4',
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'])
        question.refresh_from_db()
        self.assertEqual(question.correct_answer_4, 'D')
        self.assertEqual(question.answer_label_4, '4')
        self.assertEqual(question.get_short_answer_field_count(), 4)
        self.assertEqual(question.points, 42)

    def test_quiz_monitor_loads_for_legacy_double_answer_question(self):
        question = QuizQuestion.objects.create(
            question_text='Legacy double answer',
            question_type='double_answer',
            correct_answer='Ada',
            correct_answer_2='Lovelace',
            double_answer_label_1='Vorname',
            double_answer_label_2='Nachname',
            created_by=self.user,
        )
        quiz = Quiz.objects.create(title='Quick Quiz', creator=self.user, status='waiting')
        quiz.selected_questions.add(question)

        response = self.client.get(reverse('admin_dashboard:quiz_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Legacy double answer')
        self.assertContains(response, 'Vorname: Ada | Nachname: Lovelace')

    def test_quiz_monitor_loads_with_empty_pending_answer_state(self):
        quiz = Quiz.objects.create(title='Quick Quiz', creator=self.user, status='waiting')
        session = QuizSession.objects.create(quiz=quiz)
        second_quiz = Quiz.objects.create(title='Quick Quiz 2', creator=self.user, status='waiting')
        second_session = QuizSession.objects.create(quiz=second_quiz)

        response = self.client.get(reverse('admin_dashboard:quiz_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.pending_answers, {})
        self.assertEqual(second_session.pending_answers, {})
        self.assertIs(QuizSession._meta.get_field('pending_answers').default, dict)

    def test_create_game_page_renders_short_answer_builder_ui(self):
        response = self.client.get(reverse('admin_dashboard:create_game'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Weiteres Textfeld hinzufügen')
        self.assertNotContains(response, 'id="quizPoints"', html=False)
        self.assertNotContains(response, 'id="points"', html=False)
        self.assertNotContains(response, 'name="points"', html=False)
        self.assertNotContains(response, '<th>Points</th>', html=False)
        self.assertNotContains(response, '<th>Punkte</th>', html=False)
        self.assertNotContains(response, '<option value="double_answer">', html=False)


class QuizTutorialRuntimeTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='quiz-tutorial-user', password='pass')
        self.quiz = Quiz.objects.create(
            title='Quick Tutorial Quiz',
            creator=self.user,
            room_code='QTUT',
            tutorial_enabled=True,
            tutorial_title='Welcome',
            tutorial_text='Read this first.',
        )
        self.consumer = QuizConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'quiz_{self.quiz.room_code}'
        self.consumer.channel_layer = FakeChannelLayer()
        self.consumer.channel_name = 'quiz-tutorial-channel'
        self.direct_messages = []

        async def _capture_send(*args, **kwargs):
            text_data = kwargs.get('text_data')
            if text_data is None and args:
                text_data = args[0]
            if text_data:
                self.direct_messages.append(json.loads(text_data))

        self.consumer.send = _capture_send

    def _create_hub_step_with_officials(self, names):
        session = HubSession.objects.create(
            code='HUB1',
            name='Tutorial Hub',
            is_active=True,
            started_at=timezone.now(),
        )
        for name in names:
            HubParticipant.objects.create(session=session, nickname=name)
        self.assertTrue(start_session_check_in(session)['success'])
        for name in names:
            self.assertTrue(participant_check_in(session, name)['success'])
        self.assertTrue(complete_session_check_in(session)['success'])
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='quiz',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        return session

    @patch('QuizGame.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('QuizGame.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_admin_start_quiz_with_tutorial_sets_runtime_state_and_broadcasts_tutorial(self, _ready_mock, _activation_mock):
        async_to_sync(self.consumer.handle_admin_start_quiz)({'show_tutorial': True})

        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.tutorial_active)
        message_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)
        tutorial_message = next(message for _, message in self.consumer.channel_layer.group_messages if message['type'] == 'tutorial_start')
        self.assertEqual(tutorial_message['tutorial_title'], 'Welcome')
        self.assertEqual(tutorial_message['tutorial_text'], 'Read this first.')

    @patch('QuizGame.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('QuizGame.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_admin_start_quiz_without_tutorial_flag_keeps_runtime_inactive(self, _ready_mock, _activation_mock):
        async_to_sync(self.consumer.handle_admin_start_quiz)({})

        self.quiz.refresh_from_db()
        self.assertFalse(self.quiz.tutorial_active)
        message_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]
        self.assertIn('quiz_started', message_types)
        self.assertNotIn('tutorial_start', message_types)

    @patch('QuizGame.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('QuizGame.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_admin_start_quiz_blocks_missing_unit_tutorial_before_activation(self, _ready_mock, activation_mock):
        async_to_sync(self.consumer.handle_admin_start_quiz)({'play_tutorial': True})

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'waiting')
        self.assertFalse(activation_mock.called)
        self.assertEqual(self.direct_messages[-1]['type'], 'tutorial_question_missing')
        message_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]
        self.assertNotIn('quiz_started', message_types)

    def test_unit_tutorial_round_is_played_before_first_scored_question_without_points(self):
        session = self._create_hub_step_with_officials(['Alice'])
        tutorial_question = QuizQuestion.objects.create(
            question_text='Tutorial question',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='Correct',
            option_b='Wrong',
            created_by=self.user,
        )
        normal_question = QuizQuestion.objects.create(
            question_text='Scored question',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='Correct',
            option_b='Wrong',
            created_by=self.user,
        )
        self.quiz.selected_questions.set([tutorial_question, normal_question])
        self.quiz.question_order = [tutorial_question.id, normal_question.id]
        self.quiz.tutorial_question = tutorial_question
        self.quiz.status = 'active'
        self.quiz.started_at = timezone.now()
        self.quiz.save(update_fields=['question_order', 'tutorial_question', 'status', 'started_at'])
        participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=session.code,
        )
        prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, session.code, True)

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': normal_question.id,
            'hub_session_code': session.code,
        })

        question_started = [
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        ][-1]
        self.assertEqual(question_started['question']['id'], tutorial_question.id)
        self.assertTrue(question_started['question']['is_tutorial_round'])
        self.assertEqual(question_started['question']['points'], 0)
        self.assertTrue(get_unit_tutorial_state('quiz', self.quiz.room_code, session.code)['current_unit_is_tutorial'])

        tutorial_answer = async_to_sync(self.consumer.save_participant_answer)(
            'Alice',
            session.code,
            'A',
            1,
        )
        participant.refresh_from_db()
        self.assertTrue(tutorial_answer['is_correct'])
        self.assertTrue(tutorial_answer['is_tutorial_round'])
        self.assertEqual(tutorial_answer['points_earned'], 0)
        self.assertEqual(participant.total_score, 0)

        async_to_sync(self.consumer.handle_admin_end_question)({'hub_session_code': session.code})
        tutorial_state = get_unit_tutorial_state('quiz', self.quiz.room_code, session.code)
        self.assertTrue(tutorial_state['tutorial_has_been_played'])
        self.assertFalse(tutorial_state['current_unit_is_tutorial'])

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, participant.name]),
            {'hub_session': session.code},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row['id'] for row in response.context['question_scoreboard']],
            [normal_question.id],
        )

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': normal_question.id,
            'hub_session_code': session.code,
        })
        scored_question_started = [
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        ][-1]
        self.assertEqual(scored_question_started['question']['id'], normal_question.id)
        self.assertFalse(scored_question_started['question']['is_tutorial_round'])
        self.assertEqual(scored_question_started['question']['points'], 1)

        scored_answer = async_to_sync(self.consumer.save_participant_answer)(
            'Alice',
            session.code,
            'A',
            1,
        )
        participant.refresh_from_db()
        self.assertFalse(scored_answer['is_tutorial_round'])
        self.assertEqual(scored_answer['points_earned'], 1)
        self.assertEqual(participant.total_score, 1)

    def test_active_unit_tutorial_is_not_rendered_as_regular_scorebox_question(self):
        session = self._create_hub_step_with_officials(['Alice'])
        tutorial_question = QuizQuestion.objects.create(
            question_text='Tutorial question',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='Correct',
            option_b='Wrong',
            created_by=self.user,
        )
        normal_question = QuizQuestion.objects.create(
            question_text='Scored question',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='Correct',
            option_b='Wrong',
            created_by=self.user,
        )
        self.quiz.selected_questions.set([tutorial_question, normal_question])
        self.quiz.question_order = [tutorial_question.id, normal_question.id]
        self.quiz.tutorial_question = tutorial_question
        self.quiz.status = 'active'
        self.quiz.current_question = tutorial_question
        self.quiz.save(update_fields=['question_order', 'tutorial_question', 'status', 'current_question'])
        participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=session.code,
        )
        prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, session.code, True)
        start_unit_tutorial_if_needed('quiz', self.quiz.room_code, session.code)

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, participant.name]),
            {'hub_session': session.code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['current_unit_is_tutorial'])
        self.assertEqual(
            [row['id'] for row in response.context['question_scoreboard']],
            [normal_question.id],
        )
        self.assertContains(response, 'Tutorialfrage - keine Wertung')
        self.assertContains(response, 'class="unit-tutorial-notice"', html=False)

    def test_first_question_start_clears_tutorial_active(self):
        question = QuizQuestion.objects.create(
            question_text='Question one',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='A',
            option_b='B',
            option_c='C',
            option_d='D',
            created_by=self.user,
        )
        self.quiz.status = 'active'
        self.quiz.tutorial_active = True
        self.quiz.save(update_fields=['status', 'tutorial_active'])

        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': question.id})

        self.quiz.refresh_from_db()
        self.assertFalse(self.quiz.tutorial_active)

    def test_admin_send_question_starts_active_quick_quiz_question(self):
        question = QuizQuestion.objects.create(
            question_text='Question one',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='A',
            option_b='B',
            created_by=self.user,
        )
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])

        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': question.id})

        self.quiz.refresh_from_db()
        self.assertEqual(self.direct_messages, [])
        self.assertEqual(self.quiz.current_question_id, question.id)
        question_started = next(
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        )
        self.assertEqual(question_started['question']['id'], question.id)
        self.assertEqual(question_started['question']['question_text'], 'Question one')

    def test_admin_send_question_without_question_id_returns_visible_error(self):
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])

        async_to_sync(self.consumer.handle_admin_send_question)({})

        self.assertEqual(self.direct_messages[-1]['type'], 'error')
        self.assertIn('Frage-ID fehlt', self.direct_messages[-1]['message'])
        self.assertEqual(self.consumer.channel_layer.group_messages, [])

    def test_admin_send_question_before_start_returns_visible_error(self):
        question = QuizQuestion.objects.create(
            question_text='Question one',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='A',
            option_b='B',
            created_by=self.user,
        )

        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': question.id})

        self.assertEqual(self.direct_messages[-1]['type'], 'error')
        self.assertIn('Start the quiz', self.direct_messages[-1]['message'])
        self.assertEqual(self.direct_messages[-1]['question_id'], question.id)
        self.assertEqual(self.consumer.channel_layer.group_messages, [])

    def test_admin_send_question_with_unknown_question_returns_visible_error(self):
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])

        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': 999999})

        self.assertEqual(self.direct_messages[-1]['type'], 'error')
        self.assertIn('nicht gefunden', self.direct_messages[-1]['message'])
        self.assertEqual(self.direct_messages[-1]['question_id'], 999999)
        self.assertEqual(self.consumer.channel_layer.group_messages, [])

    def test_unknown_websocket_action_returns_visible_error(self):
        async_to_sync(self.consumer.receive)(json.dumps({'type': 'send_question'}))

        self.assertEqual(self.direct_messages[-1]['type'], 'error')
        self.assertIn('Unknown action: send_question', self.direct_messages[-1]['message'])

    def test_participant_rejoin_during_active_tutorial_receives_tutorial_start(self):
        session = self._create_hub_step_with_officials(['Alice'])
        participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=session.code,
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])
        activate_game_tutorial_runtime('quiz', self.quiz.room_code, session.code, self.quiz, True)

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })

        message_types = [message['type'] for message in self.direct_messages]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)

    def test_acknowledged_participant_rejoin_does_not_receive_tutorial_again(self):
        session = self._create_hub_step_with_officials(['Alice'])
        participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=session.code,
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])
        activate_game_tutorial_runtime('quiz', self.quiz.room_code, session.code, self.quiz, True)
        mark_game_tutorial_completed('quiz', self.quiz.room_code, session.code, participant.name)

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })

        message_types = [message['type'] for message in self.direct_messages]
        self.assertIn('quiz_started', message_types)
        self.assertNotIn('tutorial_start', message_types)

    def test_first_question_start_warns_when_tutorial_acknowledgements_are_open(self):
        session = self._create_hub_step_with_officials(['Alice', 'Bob'])
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])
        activate_game_tutorial_runtime('quiz', self.quiz.room_code, session.code, self.quiz, True)
        mark_game_tutorial_completed('quiz', self.quiz.room_code, session.code, 'Alice')
        question = QuizQuestion.objects.create(
            question_text='Question one',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='A',
            option_b='B',
            created_by=self.user,
        )

        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': question.id})

        warning = next(message for message in self.direct_messages if message['type'] == 'tutorial_ack_warning')
        self.assertEqual(warning['message'], 'Nicht alle Teilnehmer haben die Erläuterung bestätigt')
        self.assertEqual(warning['completed'], 1)
        self.assertEqual(warning['total'], 2)
        group_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]
        self.assertNotIn('question_started', group_types)

    def test_force_continue_starts_first_question_and_closes_tutorial(self):
        session = self._create_hub_step_with_officials(['Alice', 'Bob'])
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])
        activate_game_tutorial_runtime('quiz', self.quiz.room_code, session.code, self.quiz, True)
        question = QuizQuestion.objects.create(
            question_text='Question one',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='A',
            option_b='B',
            created_by=self.user,
        )

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': question.id,
            'force_tutorial_continue': True,
        })

        group_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]
        self.assertIn('tutorial_force_close', group_types)
        self.assertIn('question_started', group_types)
        runtime = HubGameTutorialRuntime.objects.get(game_step__session=session, game_step__room_code=self.quiz.room_code)
        self.assertFalse(runtime.active)

    def test_quiz_play_template_contains_generic_tutorial_overlay_hook(self):
        participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, participant.name]),
            {'hub_session': participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'gameTutorialOverlay')
        self.assertContains(response, "case 'tutorial_start':")


class QuickQuizWebSocketLiveFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='quick-live-host', password='pass')
        self.quiz = Quiz.objects.create(
            title='Quick Live Quiz',
            creator=self.user,
            room_code='QLIV',
            status='active',
            started_at=timezone.now(),
        )
        self.question = QuizQuestion.objects.create(
            question_text='Live question?',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='A',
            option_b='B',
            created_by=self.user,
        )
        self.participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUBLIVE',
            is_active=True,
        )

    async def _receive_until(self, communicator, message_type, attempts=6):
        for _ in range(attempts):
            message = await communicator.receive_json_from(timeout=2)
            if message.get('type') == message_type:
                return message
        self.fail(f'Did not receive {message_type}')

    def test_host_send_question_reaches_host_and_participant_websockets(self):
        async def scenario():
            host = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            player = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            host_connected, _ = await host.connect()
            player_connected, _ = await player.connect()
            self.assertTrue(host_connected)
            self.assertTrue(player_connected)

            await self._receive_until(host, 'connection_established')
            await self._receive_until(player, 'connection_established')
            await player.send_json_to({
                'type': 'participant_join',
                'participant_name': self.participant.name,
                'hub_session': self.participant.hub_session_code,
            })
            await self._receive_until(player, 'participant_joined')

            await host.send_json_to({
                'type': 'admin_send_question',
                'question_id': self.question.id,
                'hub_session': self.participant.hub_session_code,
            })

            host_message = await self._receive_until(host, 'question_started')
            player_message = await self._receive_until(player, 'question_started')

            await host.disconnect()
            await player.disconnect()

            return host_message, player_message

        host_message, player_message = async_to_sync(scenario)()
        self.quiz.refresh_from_db()

        self.assertEqual(self.quiz.current_question_id, self.question.id)
        self.assertEqual(host_message['question']['id'], self.question.id)
        self.assertEqual(player_message['question']['id'], self.question.id)

    def test_end_quiz_reaches_host_websocket_without_reload(self):
        async def scenario():
            host = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            host_connected, _ = await host.connect()
            self.assertTrue(host_connected)

            await self._receive_until(host, 'connection_established')
            await host.send_json_to({
                'type': 'admin_end_quiz',
                'hub_session': self.participant.hub_session_code,
            })

            host_message = await self._receive_until(host, 'quiz_ended')

            await host.disconnect()
            return host_message

        host_message = async_to_sync(scenario)()
        self.quiz.refresh_from_db()

        self.assertEqual(self.quiz.status, 'completed')
        self.assertEqual(host_message['type'], 'quiz_ended')
        self.assertIn('Quiz has ended', host_message['message'])
        self.assertIn('final_scores', host_message)

    def test_quick_quiz_start_routes_lobby_participant_to_play_screen(self):
        session = HubSession.objects.create(
            code='HUBLIVE',
            name='Quick Live Session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubParticipant.objects.create(session=session, nickname=self.participant.name)
        self.assertTrue(start_session_check_in(session)['success'])
        self.assertTrue(participant_check_in(session, self.participant.name)['success'])
        self.assertTrue(complete_session_check_in(session)['success'])
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='quiz',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        self.participant.is_active = False
        self.participant.save(update_fields=['is_active'])
        self.quiz.status = 'waiting'
        self.quiz.started_at = None
        self.quiz.save(update_fields=['status', 'started_at'])

        async def scenario():
            hub = WebsocketCommunicator(application, f'/ws/hub/{session.code}/')
            host = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            hub_connected, _ = await hub.connect()
            host_connected, _ = await host.connect()
            self.assertTrue(hub_connected)
            self.assertTrue(host_connected)

            await self._receive_until(hub, 'connection_established')
            await self._receive_until(host, 'connection_established')
            await host.send_json_to({
                'type': 'admin_start_quiz',
                'hub_session': session.code,
            })

            navigate = await self._receive_until(hub, 'navigate')

            await host.disconnect()
            await hub.disconnect()
            return navigate

        navigate = async_to_sync(scenario)()
        self.quiz.refresh_from_db()

        self.assertEqual(self.quiz.status, 'active')
        self.assertEqual(navigate['step']['game_key'], 'quiz')
        self.assertEqual(navigate['step']['room_code'], self.quiz.room_code)


class QuickQuizBrowserLiveFlowTests(_BrowserLiveServerTestCase):
    """Browser regression coverage for the real Quick Quiz host/player live flow."""

    TIMEOUT = 15_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls._pw, cls._browser = start_chromium_browser(headless=True)
            cls._playwright_available = True
        except Exception as exc:
            cls._playwright_available = False
            cls._playwright_error = exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, '_playwright_available', False):
            cls._browser.close()
            cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._playwright_available:
            self.skipTest(f'Playwright/Chromium nicht verfuegbar: {self._playwright_error}')

        self.password = 'testpass123'
        self.user = User.objects.create_superuser(
            username='quick-browser-host',
            password=self.password,
            email='',
        )
        self.quiz = Quiz.objects.create(
            title='Quick Browser Quiz',
            creator=self.user,
            room_code='QB99',
            status='active',
            started_at=timezone.now(),
        )
        self.question = QuizQuestion.objects.create(
            question_text='Browser live question?',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='Answer A',
            option_b='Answer B',
            points=1,
            time_limit=30,
            created_by=self.user,
        )
        self.quiz.selected_questions.set([self.question])
        self.quiz.question_order = [self.question.id]
        self.quiz.save(update_fields=['question_order', 'updated_at'])
        QuizSession.objects.get_or_create(quiz=self.quiz)
        self.session = HubSession.objects.create(
            code='QBLIVE',
            name='Quick Browser Session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        HubParticipant.objects.create(session=self.session, nickname='Alice')
        self.participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )

        self.host_context = self._browser.new_context()
        self.player_context = self._browser.new_context()
        install_browser_test_stubs(self.host_context)
        install_browser_test_stubs(self.player_context)
        self.host_page = self.host_context.new_page()
        self.player_page = self.player_context.new_page()
        self.browser_errors = []
        self.websocket_urls = {'host': [], 'player': []}
        self.websocket_frames = {
            'host': {'sent': [], 'received': []},
            'player': {'sent': [], 'received': []},
        }
        self._instrument_page('host', self.host_page)
        self._instrument_page('player', self.player_page)
        self._admin_login()

    def tearDown(self):
        for page in (getattr(self, 'host_page', None), getattr(self, 'player_page', None)):
            if page:
                page.close()
        for context in (getattr(self, 'host_context', None), getattr(self, 'player_context', None)):
            if context:
                context.close()

    def _instrument_page(self, label, page):
        page.on('console', lambda msg: self._record_console(label, msg))
        page.on('pageerror', lambda exc: self.browser_errors.append(f'{label} pageerror: {exc}'))
        page.on('websocket', lambda ws: self._record_websocket(label, ws))

    def _record_console(self, label, msg):
        if msg.type in {'error'}:
            self.browser_errors.append(f'{label} console {msg.type}: {msg.text}')

    def _record_websocket(self, label, websocket):
        self.websocket_urls[label].append(websocket.url)
        websocket.on('framesent', lambda payload: self.websocket_frames[label]['sent'].append(str(payload)))
        websocket.on(
            'framereceived',
            lambda payload: self.websocket_frames[label]['received'].append(str(payload)),
        )

    def _admin_login(self):
        self.host_page.goto(f'{self.live_server_url}{reverse("admin_dashboard:login")}')
        self.host_page.fill("input[name='username']", self.user.username)
        self.host_page.fill("input[name='password']", self.password)
        self.host_page.click("button[type='submit']")
        self.host_page.wait_for_url(f'**{reverse("admin_dashboard:home")}**', timeout=self.TIMEOUT)

    def _wait_for_ws_url(self, label, path):
        deadline = time.time() + (self.TIMEOUT / 1000)
        while time.time() < deadline:
            if any(path in url for url in self.websocket_urls[label]):
                return
            self.host_page.wait_for_timeout(100)
        self.fail(f'{label} did not open WebSocket {path}. URLs: {self.websocket_urls[label]}')

    def _wait_for_frame(self, label, direction, needle):
        deadline = time.time() + (self.TIMEOUT / 1000)
        while time.time() < deadline:
            if any(needle in frame for frame in self.websocket_frames[label][direction]):
                return
            self.host_page.wait_for_timeout(100)
        self.fail(
            f'{label} did not receive {needle!r} in {direction} frames. '
            f'Frames: {self.websocket_frames[label][direction]}'
        )

    def _assert_no_browser_errors(self):
        js_errors = [
            error for error in self.browser_errors
            if (
                'favicon.ico' not in error
                and '404' not in error
                and 'ERR_NETWORK_ACCESS_DENIED' not in error
            )
        ]
        self.assertEqual(js_errors, [])

    def test_send_question_and_end_quiz_update_host_and_player_without_reload(self):
        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:quiz_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )
        play_url = (
            f'{self.live_server_url}'
            f'{reverse("quiz:play", args=[self.quiz.room_code, self.participant.name])}'
            f'?hub_session={self.session.code}'
        )

        self.host_page.goto(monitor_url)
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.wait_for_function('() => !!window.adminGameMonitor', timeout=self.TIMEOUT)
        self.player_page.goto(play_url)
        self.player_page.wait_for_selector('#questionState', state='attached', timeout=self.TIMEOUT)
        self._wait_for_ws_url('host', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_frame('host', 'received', 'connection_established')
        self._wait_for_frame('player', 'received', 'connection_established')

        self.host_page.click('.send-question-btn')
        self._wait_for_frame('host', 'sent', 'admin_send_question')
        self._wait_for_frame('host', 'received', 'question_started')
        self._wait_for_frame('player', 'received', 'question_started')
        self._assert_no_browser_errors()

        self.host_page.wait_for_selector('#activeQuestion', timeout=self.TIMEOUT)
        self.host_page.wait_for_function(
            """() => document.querySelector('#activeQuestion')?.textContent.includes('Browser live question?')""",
            timeout=self.TIMEOUT,
        )
        self.player_page.wait_for_selector('#questionState:not(.d-none)', timeout=self.TIMEOUT)
        self.player_page.wait_for_function(
            """() => document.querySelector('#questionText')?.textContent.includes('Browser live question?')""",
            timeout=self.TIMEOUT,
        )
        self.player_page.wait_for_selector('#submitAnswerBtn', timeout=self.TIMEOUT)

        self.host_page.once('dialog', lambda dialog: dialog.accept())
        self.host_page.click('#endQuizBtn')
        self._wait_for_frame('host', 'sent', 'admin_end_quiz')
        self._wait_for_frame('host', 'received', 'quiz_ended')
        self._wait_for_frame('player', 'received', 'quiz_ended')

        self.host_page.wait_for_function(
            "() => document.body.dataset.hostGameStatus === 'completed'",
            timeout=self.TIMEOUT,
        )
        self.assertEqual(self.host_page.locator('#hostEndStateBanner').count(), 0)
        self.player_page.wait_for_selector('#quizEndedState:not(.d-none)', timeout=self.TIMEOUT)

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'completed')
        self._assert_no_browser_errors()

    def test_manual_correction_during_active_question_is_revealed_only_after_question_end(self):
        self.question.question_type = 'short_answer'
        self.question.correct_answer = 'Berlin'
        self.question.option_a = ''
        self.question.option_b = ''
        self.question.save(update_fields=['question_type', 'correct_answer', 'option_a', 'option_b', 'updated_at'])

        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:quiz_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )
        play_url = (
            f'{self.live_server_url}'
            f'{reverse("quiz:play", args=[self.quiz.room_code, self.participant.name])}'
            f'?hub_session={self.session.code}'
        )
        score_selector = f'.quiz-score-row[data-question-id="{self.question.id}"] .quiz-score-mark'

        self.host_page.goto(monitor_url)
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.wait_for_function('() => !!window.adminGameMonitor', timeout=self.TIMEOUT)
        self.player_page.goto(play_url)
        self.player_page.wait_for_selector('#questionState', state='attached', timeout=self.TIMEOUT)
        self._wait_for_ws_url('host', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')

        self.host_page.click('.send-question-btn')
        self._wait_for_frame('host', 'received', 'question_started')
        self._wait_for_frame('player', 'received', 'question_started')
        self.player_page.wait_for_selector('#shortAnswerInput1', timeout=self.TIMEOUT)
        self.player_page.fill('#shortAnswerInput1', 'Baerlin')
        self.player_page.click('#submitAnswerBtn')
        self._wait_for_frame('player', 'received', 'answer_submitted')
        self.host_page.wait_for_selector('.promote-correct-btn', timeout=self.TIMEOUT)

        self.host_page.once('dialog', lambda dialog: dialog.accept())
        self.host_page.click('.promote-correct-btn')
        self._wait_for_frame('host', 'received', 'answer_corrected')
        self.host_page.wait_for_function(
            "() => document.querySelector('#liveResponses')?.textContent.includes('Correct (manual)')",
            timeout=self.TIMEOUT,
        )
        self.player_page.wait_for_timeout(500)
        self.assertIn('__', self.player_page.locator(score_selector).inner_text())
        self.assertFalse(any(
            'answer_corrected' in frame
            for frame in self.websocket_frames['player']['received']
        ))
        self.assertFalse(any(
            'answer_submitted' in frame and 'is_correct' in frame
            for frame in self.websocket_frames['player']['received']
        ))

        self.host_page.click('#endQuestionBtn')
        self._wait_for_frame('player', 'received', 'question_ended')
        self._wait_for_frame('player', 'received', 'answer_results')
        self.player_page.wait_for_function(
            """(selector) => {
                const text = document.querySelector(selector)?.textContent || '';
                return text.includes('✓') || text.includes('1/1');
            }""",
            arg=score_selector,
            timeout=self.TIMEOUT,
        )
        self.player_page.wait_for_function(
            "() => document.querySelector('#quizScoreTotal')?.textContent.trim() === '1/1'",
            timeout=self.TIMEOUT,
        )

        self._assert_no_browser_errors()


class QuizPlayScoreBoxTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='quiz-score-user', password='pass')
        self.quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            room_code='7123',
        )
        self.participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess1',
            is_active=True,
        )

    def _create_question(self, text, correct_answer):
        return QuizQuestion.objects.create(
            question_text=text,
            question_type='true_false',
            correct_answer=correct_answer,
            points=10,
            created_by=self.user,
        )

    def test_quiz_play_builds_per_question_score_box_with_bottom_total(self):
        question_one = self._create_question('Question one', 'True')
        question_two = self._create_question('Question two', 'False')
        question_three = self._create_question('Question three', 'True')
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_two
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['question_order', 'current_question', 'status'])

        QuizAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            answer_text='True',
            time_taken=3.5,
        )

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {'id': question_one.id, 'number': 1, 'is_correct': True, 'status': 'played', 'points_earned': 1, 'max_points': 1},
                {'id': question_two.id, 'number': 2, 'is_correct': None, 'status': 'current', 'points_earned': None, 'max_points': 1},
                {'id': question_three.id, 'number': 3, 'is_correct': None, 'status': 'upcoming', 'points_earned': None, 'max_points': 1},
            ],
        )
        self.assertEqual(response.context['score_total_correct'], 1)
        self.assertEqual(response.context['score_total_questions'], 3)
        self.assertContains(response, 'class="quiz-score-box score-box"')
        self.assertContains(response, 'id="quizScoreTotal"')
        self.assertContains(response, 'id="quizScoreTotal">1/3</div>')

    def test_quiz_play_hides_active_question_answer_until_question_end(self):
        question_one = self._create_question('Question one', 'True')
        question_two = self._create_question('Question two', 'False')
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_one.id, question_two.id]
        self.quiz.status = 'active'
        self.quiz.current_question = question_one
        self.quiz.question_start_time = timezone.now()
        self.quiz.save(update_fields=['question_order', 'status', 'current_question', 'question_start_time'])
        QuizSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
        )
        QuizAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            answer_text='True',
            time_taken=2.0,
        )

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['initial_progress_history'], [])
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {'id': question_one.id, 'number': 1, 'is_correct': None, 'status': 'current', 'points_earned': None, 'max_points': 1},
                {'id': question_two.id, 'number': 2, 'is_correct': None, 'status': 'upcoming', 'points_earned': None, 'max_points': 1},
            ],
        )
        self.assertEqual(response.context['score_total_correct'], 0)
        self.assertContains(response, 'id="quizScoreTotal">0/2</div>')

    def test_quiz_play_uses_actual_current_send_order_not_config_order(self):
        question_one = self._create_question('Question one', 'True')
        question_two = self._create_question('Question two', 'False')
        question_three = self._create_question('Question three', 'True')
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_three
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['question_order', 'current_question', 'status'])

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {'id': question_three.id, 'number': 1, 'is_correct': None, 'status': 'current', 'points_earned': None, 'max_points': 1},
                {'id': question_one.id, 'number': 2, 'is_correct': None, 'status': 'upcoming', 'points_earned': None, 'max_points': 1},
                {'id': question_two.id, 'number': 3, 'is_correct': None, 'status': 'upcoming', 'points_earned': None, 'max_points': 1},
            ],
        )

    def test_quiz_play_keeps_unplayed_questions_neutral_after_out_of_order_start(self):
        question_one = self._create_question('Question one', 'True')
        question_two = self._create_question('Question two', 'False')
        question_three = self._create_question('Question three', 'True')
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_two
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['question_order', 'current_question', 'status'])

        QuizAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_three,
            answer_text='True',
            time_taken=2.1,
        )

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {'id': question_three.id, 'number': 1, 'is_correct': True, 'status': 'played', 'points_earned': 1, 'max_points': 1},
                {'id': question_two.id, 'number': 2, 'is_correct': None, 'status': 'current', 'points_earned': None, 'max_points': 1},
                {'id': question_one.id, 'number': 3, 'is_correct': None, 'status': 'upcoming', 'points_earned': None, 'max_points': 1},
            ],
        )

    def test_quiz_play_template_waits_for_answer_evaluation_before_marking_scorebox(self):
        question_one = self._create_question('Question one', 'True')
        question_two = self._create_question('Question two', 'False')
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_one.id, question_two.id]
        self.quiz.current_question = question_one
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['question_order', 'current_question', 'status'])

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'this.pushProgressEntry(!!data.is_correct);')
        self.assertNotContains(response, 'this.pushProgressEntry(data.is_correct, data.question_id);')
        self.assertContains(response, 'data.evaluation_revealed')
        self.assertContains(response, 'this.applyPendingProgressEvaluation(endedQuestionId);')
        self.assertContains(response, 'this.applyPendingProgressEvaluationIfEnded(data.question_id);')
        self.assertContains(response, 'this.applyQuestionEndResult(data, endedQuestionId);')
        self.assertContains(response, 'data.answer_results.find')
        self.assertContains(response, "case 'answer_corrected':")
        self.assertContains(response, 'this.applyManualAnswerCorrection(data);')
        self.assertContains(response, 'this.storePendingProgressEvaluation(data.is_correct, data.question_id, data.points_earned);')
        self.assertContains(response, '!this.hasProgressEntry(endedQuestionId)')
        self.assertContains(response, 'this.reorderScoreboardForQuestionStart(question.id);')
        self.assertContains(response, '<span class="quiz-score-empty score-box__empty">__</span>', html=True)
        self.assertContains(response, 'id="quizScoreTotal">0/2</div>')
        self.assertNotContains(response, 'id="myScoreHeader"')

    def test_quiz_answer_scoring_uses_one_point_per_correct_answer(self):
        question = QuizQuestion.objects.create(
            question_text='High value legacy question',
            question_type='true_false',
            correct_answer='True',
            points=25,
            created_by=self.user,
        )

        answer = QuizAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            answer_text='True',
            time_taken=1.0,
        )

        self.participant.refresh_from_db()
        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(self.participant.total_score, 1)

    def test_multi_field_short_answer_scores_one_point_per_correct_field(self):
        question = QuizQuestion.objects.create(
            question_text='Three-part answer',
            question_type='short_answer',
            correct_answer='Mercury',
            correct_answer_2='Venus',
            correct_answer_3='Earth',
            points=50,
            created_by=self.user,
        )

        answer = QuizAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            answer_text=question.serialize_short_answer_submission({
                'answer_1': 'Mercury',
                'answer_2': 'wrong',
                'answer_3': 'Earth',
            }),
            time_taken=2.0,
        )

        self.participant.refresh_from_db()
        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.points_earned, 2)
        self.assertEqual(self.participant.total_score, 2)

        full_participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Full scorer',
            hub_session_code='sess1',
            is_active=True,
        )
        full_answer = QuizAnswer.objects.create(
            quiz=self.quiz,
            participant=full_participant,
            question=question,
            answer_text=question.serialize_short_answer_submission({
                'answer_1': 'Mercury',
                'answer_2': 'Venus',
                'answer_3': 'Earth',
            }),
            time_taken=1.5,
        )

        full_participant.refresh_from_db()
        self.assertTrue(full_answer.is_correct)
        self.assertEqual(full_answer.points_earned, 3)
        self.assertEqual(full_participant.total_score, 3)

    def test_quiz_play_keeps_legacy_quiz_without_selected_questions_loadable(self):
        question = self._create_question('Legacy question', 'True')
        self.quiz.current_question = question
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['current_question', 'status'])

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [{'id': question.id, 'number': 1, 'is_correct': None, 'status': 'current', 'points_earned': None, 'max_points': 1}],
        )
        self.assertContains(response, 'quizScoreList')

    def test_participant_submit_answer_payload_includes_question_id_for_scorebox_sync(self):
        question = self._create_question('Question one', 'True')
        self.quiz.status = 'active'
        self.quiz.current_question = question
        self.quiz.save(update_fields=['status', 'current_question'])

        consumer = QuizConsumer()
        consumer.room_code = self.quiz.room_code
        consumer.room_group_name = f'quiz_{self.quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        consumer.channel_name = 'quiz-score-channel'
        direct_messages = []

        async def _capture_send(*args, **kwargs):
            text_data = kwargs.get('text_data')
            if text_data is None and args:
                text_data = args[0]
            if text_data:
                direct_messages.append(json.loads(text_data))

        consumer.send = _capture_send

        async_to_sync(consumer.handle_participant_submit_answer)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
            'question_id': question.id,
            'answer': 'True',
            'time_taken': 2.5,
        })

        answer_message = next(message for message in direct_messages if message['type'] == 'answer_submitted')
        self.assertEqual(answer_message['question_id'], question.id)
        self.assertTrue(answer_message['evaluation_pending'])
        self.assertNotIn('is_correct', answer_message)
        self.assertNotIn('points_earned', answer_message)
        self.assertNotIn('total_score', answer_message)


class QuizHostManualCorrectTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='quiz_host_manual',
            email='',
            password='testpass123',
        )
        self.client.force_login(self.user)

    def test_live_responses_expose_formatted_short_answer_and_manual_action(self):
        question = QuizQuestion.objects.create(
            question_text='Name the person',
            question_type='short_answer',
            correct_answer='Ada',
            correct_answer_2='Lovelace',
            answer_label_1='First',
            answer_label_2='Last',
            points=12,
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        participant = QuizParticipant.objects.create(quiz=quiz, name='Alice', is_active=True)
        answer = QuizAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text=question.serialize_short_answer_submission({
                'answer_1': 'Grace',
                'answer_2': 'Hopper',
            }),
            time_taken=2.4,
        )

        response = self.client.get(reverse('admin_dashboard:api_live_responses', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['responses']), 1)
        live_response = payload['responses'][0]
        self.assertEqual(live_response['answer_id'], answer.id)
        self.assertEqual(live_response['answer_text'], 'First: Grace | Last: Hopper')
        self.assertFalse(live_response['is_correct'])
        self.assertTrue(live_response['can_mark_correct'])
        self.assertEqual(
            [(field['key'], field['is_correct']) for field in live_response['field_results']],
            [('answer_1', False), ('answer_2', False)],
        )

    def test_host_can_promote_short_answer_to_correct_without_double_scoring(self):
        question = QuizQuestion.objects.create(
            question_text='Capital?',
            question_type='short_answer',
            correct_answer='Berlin',
            points=15,
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        participant = QuizParticipant.objects.create(quiz=quiz, name='Bob', is_active=True)
        answer = QuizAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Baerlin',
            time_taken=3.1,
        )

        self.assertFalse(answer.is_correct)
        self.assertEqual(participant.total_score, 0)

        response = self.client.post(
            reverse('admin_dashboard:promote_quiz_answer_correct', args=[quiz.room_code]),
            data=json.dumps({'answer_id': answer.id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        answer.refresh_from_db()
        participant.refresh_from_db()
        payload = response.json()

        self.assertTrue(payload['success'])
        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)
        self.assertTrue(payload['is_manual_override'])

        second_response = self.client.post(
            reverse('admin_dashboard:promote_quiz_answer_correct', args=[quiz.room_code]),
            data=json.dumps({'answer_id': answer.id}),
            content_type='application/json',
        )

        self.assertEqual(second_response.status_code, 400)
        answer.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)

    def test_host_can_correct_one_short_answer_field(self):
        question = QuizQuestion.objects.create(
            question_text='Song and artist?',
            question_type='short_answer',
            correct_answer='Imagine',
            correct_answer_2='John Lennon',
            answer_label_1='Titel',
            answer_label_2='Sänger',
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        participant = QuizParticipant.objects.create(quiz=quiz, name='Alice', is_active=True)
        answer = QuizAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text=question.serialize_short_answer_submission({
                'answer_1': 'Imagine',
                'answer_2': 'John Lenon',
            }),
            time_taken=4.2,
        )

        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.points_earned, 1)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, 1)
        self.assertEqual(
            [(field['key'], field['is_correct']) for field in answer.get_short_answer_field_results()],
            [('answer_1', True), ('answer_2', False)],
        )

        response = self.client.post(
            reverse('admin_dashboard:promote_quiz_answer_correct', args=[quiz.room_code]),
            data=json.dumps({'answer_id': answer.id, 'field_key': 'answer_2'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        answer.refresh_from_db()
        participant.refresh_from_db()
        payload = response.json()

        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 2)
        self.assertEqual(participant.total_score, 2)
        self.assertFalse(payload['can_mark_correct'])
        self.assertEqual(
            [(field['key'], field['is_correct']) for field in payload['field_results']],
            [('answer_1', True), ('answer_2', True)],
        )

    @patch('admin_dashboard.views.get_channel_layer')
    def test_host_promote_during_active_question_broadcasts_host_only_update(self, channel_layer_mock):
        question = QuizQuestion.objects.create(
            question_text='Capital?',
            question_type='short_answer',
            correct_answer='Berlin',
            points=15,
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        QuizSession.objects.create(quiz=quiz, is_question_active=True)
        participant = QuizParticipant.objects.create(quiz=quiz, name='Bob', is_active=True)
        answer = QuizAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='BÃ¤rlin',
            time_taken=3.1,
        )
        fake_channel_layer = FakeChannelLayer()
        channel_layer_mock.return_value = fake_channel_layer

        response = self.client.post(
            reverse('admin_dashboard:promote_quiz_answer_correct', args=[quiz.room_code]),
            data=json.dumps({'answer_id': answer.id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(fake_channel_layer.group_messages), 1)
        group_name, message = fake_channel_layer.group_messages[0]
        self.assertEqual(group_name, f'quiz_{quiz.room_code}')
        self.assertEqual(message['type'], 'answer_corrected')
        self.assertEqual(message['participant_id'], participant.id)
        self.assertEqual(message['participant_name'], participant.name)
        self.assertEqual(message['question_id'], question.id)
        self.assertTrue(message['is_correct'])
        self.assertEqual(message['total_score'], 1)
        self.assertFalse(message['visible_to_participants'])

    def test_question_end_payload_preserves_manual_correction_as_final_result(self):
        question = QuizQuestion.objects.create(
            question_text='Capital?',
            question_type='short_answer',
            correct_answer='Berlin',
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        QuizSession.objects.create(quiz=quiz, is_question_active=True)
        participant = QuizParticipant.objects.create(quiz=quiz, name='Bob', is_active=True)
        answer = QuizAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Bärlin',
            time_taken=3.1,
        )
        answer.promote_short_answer_to_correct()

        consumer = QuizConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'quiz_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()

        async_to_sync(consumer.handle_admin_end_question)({})

        question_ended = next(
            message for _, message in consumer.channel_layer.group_messages
            if message['type'] == 'question_ended'
        )
        self.assertEqual(question_ended['correct_answer']['question_id'], question.id)
        self.assertEqual(
            question_ended['answer_results'],
            [{
                'participant_name': participant.name,
                'question_id': question.id,
                'is_correct': True,
                'points_earned': 1,
            }],
        )

    def test_quiz_monitor_contains_manual_correct_hook(self):
        quiz = Quiz.objects.create(title='Quick Quiz', creator=self.user, status='active')

        response = self.client.get(reverse('admin_dashboard:quiz_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'promote-correct-btn')
        self.assertContains(response, 'promote-field-correct-btn')
        self.assertContains(response, reverse('admin_dashboard:promote_quiz_answer_correct', args=[quiz.room_code]))

    def test_waiting_quiz_monitor_clears_stale_runtime_state(self):
        question = QuizQuestion.objects.create(
            question_text='Old running question',
            question_type='true_false',
            correct_answer='True',
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='waiting',
            started_at=timezone.now() - timedelta(days=5),
            current_question=question,
            question_start_time=timezone.now() - timedelta(days=5),
        )
        session = QuizSession.objects.create(
            quiz=quiz,
            current_question_number=3,
            total_questions_sent=3,
            is_question_active=True,
            question_end_time=timezone.now() - timedelta(days=5),
        )
        participant = QuizParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            total_score=1,
            questions_answered=1,
            is_active=True,
        )
        QuizAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='True',
            time_taken=1.0,
        )

        response = self.client.get(reverse('admin_dashboard:quiz_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        quiz.refresh_from_db()
        session.refresh_from_db()
        participant.refresh_from_db()
        self.assertIsNone(quiz.started_at)
        self.assertIsNone(quiz.current_question)
        self.assertIsNone(quiz.question_start_time)
        self.assertFalse(session.is_question_active)
        self.assertIsNone(session.question_end_time)
        self.assertEqual(QuizAnswer.objects.filter(quiz=quiz).count(), 0)
        self.assertEqual(participant.total_score, 0)

    def test_quiz_monitor_keeps_host_on_question_review_after_question_end(self):
        question = QuizQuestion.objects.create(
            question_text='Review this question',
            question_type='true_false',
            correct_answer='True',
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            current_question=question,
        )

        response = self.client.get(reverse('admin_dashboard:quiz_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="returnToQuestionOverviewBtn"')
        self.assertContains(response, 'this.showQuestionReviewState();')
        self.assertContains(response, 'returnToQuestionOverview()')
        self.assertNotContains(response, 'setTimeout(() => location.reload(), 3000);')

    def test_quiz_monitor_hides_host_timer_when_question_review_starts(self):
        question = QuizQuestion.objects.create(
            question_text='Hide this timer',
            question_type='true_false',
            correct_answer='True',
            created_by=self.user,
        )
        quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            current_question=question,
        )

        response = self.client.get(reverse('admin_dashboard:quiz_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="questionTimerWrapper"')
        self.assertContains(response, 'if (this.questionTimer) {')
        self.assertContains(response, 'clearInterval(this.questionTimer);')
        self.assertContains(response, 'this.questionTimer = null;')
        self.assertContains(response, "const timerWrapper = document.getElementById('questionTimerWrapper');")
        self.assertContains(response, "if (timerWrapper) timerWrapper.style.display = 'none';")

    def test_quiz_management_modal_removes_double_answer_option(self):
        response = self.client.get(reverse('admin_dashboard:quiz_management'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Weiteres Textfeld hinzufügen')
        self.assertNotContains(response, 'id="points"', html=False)
        self.assertNotContains(response, 'name="points"', html=False)
        self.assertNotContains(response, '<th>Points</th>', html=False)
        self.assertNotContains(response, '<th>Punkte</th>', html=False)
        self.assertNotContains(response, '<option value="double_answer">', html=False)
