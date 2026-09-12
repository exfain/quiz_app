import asyncio
import json
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import LiveServerTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from admin_dashboard.models import DashboardSettings
from games_hub.check_in import complete_session_check_in, participant_check_in, start_session_check_in
from games_hub.authoritative_state import (
    QUESTION_PRESENTATION_DELAY_MS,
    current_snapshot,
    finish_question_flow,
    observe_snapshot,
    open_answering,
    present_question,
    reset_question_flow,
    reveal_question_content,
)
from games_hub.models import GameRuntimeState, HubGameStep, HubGameTutorialRuntime, HubParticipant, HubSession
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
from .presentation import question_typewriter_duration_ms
from games_website.asgi import application


User = get_user_model()

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserLiveServerTestCase
except ImportError:
    _BrowserLiveServerTestCase = LiveServerTestCase


class QuickQuizTypewriterConfigurationTests(TestCase):
    def test_duration_counts_unicode_code_points_and_line_breaks(self):
        text = 'Ärger – déjà vu\n„größer“'

        self.assertEqual(
            question_typewriter_duration_ms(text, 75),
            len(text) * 75,
        )
        fast_duration = question_typewriter_duration_ms(text, 35)
        normal_duration = question_typewriter_duration_ms(text, 75)
        slow_duration = question_typewriter_duration_ms(text, 180)
        self.assertLess(fast_duration, normal_duration)
        self.assertLess(normal_duration, slow_duration)
        self.assertEqual(
            round(1000 / DashboardSettings.DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER, 1),
            13.3,
        )

    def test_dashboard_setting_is_global_and_clamped(self):
        settings = DashboardSettings.load()
        self.assertEqual(settings.question_reveal_ms_per_character, 75)
        self.assertEqual(DashboardSettings.normalize_question_reveal_speed(5), 35)
        self.assertEqual(DashboardSettings.normalize_question_reveal_speed(500), 180)

    def test_dashboard_options_persist_question_reveal_speed(self):
        user = User.objects.create_superuser('settings-host', '', 'testpass123')
        self.client.force_login(user)

        response = self.client.post(
            reverse('admin_dashboard:settings'),
            {'question_reveal_ms_per_character': 85},
        )

        self.assertRedirects(response, reverse('admin_dashboard:settings'))
        self.assertEqual(
            DashboardSettings.load().question_reveal_ms_per_character,
            85,
        )

    def test_missing_settings_table_falls_back_only_for_that_table(self):
        missing_table = OperationalError(
            f'no such table: {DashboardSettings._meta.db_table}'
        )
        with patch.object(DashboardSettings, 'load', side_effect=missing_table):
            self.assertEqual(DashboardSettings.question_reveal_speed(), 75)

        with patch.object(
            DashboardSettings,
            'load',
            side_effect=OperationalError('database is locked'),
        ):
            with self.assertRaises(OperationalError):
                DashboardSettings.question_reveal_speed()


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

    def _phase_action(self, question_id, session_code=None):
        snapshot = current_snapshot('quiz', self.quiz.room_code, session_code)
        return {
            'question_id': question_id,
            'hub_session_code': session_code,
            'game_id': snapshot.get('game_id'),
            'state_revision': snapshot['state_revision'],
            'client_action_id': str(uuid.uuid4()),
        }

    def _send_question(self, payload):
        payload = dict(payload)
        session_code = payload.get('hub_session_code') or payload.get('hub_session')
        snapshot = current_snapshot('quiz', self.quiz.room_code, session_code)
        if snapshot.get('question_flow_mode') != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE:
            reset_question_flow(
                game_key='quiz',
                room_code=self.quiz.room_code,
                session_code=session_code,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
        payload.update(self._phase_action(payload['question_id'], session_code))
        async_to_sync(self.consumer.handle_admin_send_question)(payload)

    def _open_answering(self, question_id, session_code=None):
        question = QuizQuestion.objects.get(id=question_id)
        presentation_duration_ms = question_typewriter_duration_ms(
            question.question_text,
            DashboardSettings.question_reveal_speed(),
        )
        GameRuntimeState.objects.filter(
            game_key='quiz',
            room_code=self.quiz.room_code,
        ).update(
            question_presented_at=(
                timezone.now()
                - timedelta(
                    milliseconds=(
                        QUESTION_PRESENTATION_DELAY_MS + presentation_duration_ms
                    )
                )
            )
        )
        async_to_sync(self.consumer.handle_admin_reveal_question_content)(
            self._phase_action(question_id, session_code)
        )
        if question.question_type in {'multiple_choice', 'true_false'}:
            deadline = time.time() + 2
            while time.time() < deadline:
                snapshot = current_snapshot(
                    'quiz', self.quiz.room_code, session_code,
                )
                if snapshot.get('answering_allowed'):
                    return
                time.sleep(0.02)
            self.fail('Answering did not open after its answer-reveal timeline.')
        async_to_sync(self.consumer.handle_admin_open_answering)(
            self._phase_action(question_id, session_code)
        )

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
        reset_question_flow(
            game_key='quiz',
            room_code=self.quiz.room_code,
            session_code=session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        observe_snapshot(
            'quiz',
            self.quiz.room_code,
            {'type': 'quiz_started', 'phase': 'active'},
            session.code,
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
    def test_quiz_started_includes_current_instance_question_total(self, _ready_mock, _activation_mock):
        questions = [
            QuizQuestion.objects.create(
                question_text=f'Question {index}',
                question_type='true_false',
                correct_answer='True',
                created_by=self.user,
            )
            for index in range(1, 5)
        ]
        self.quiz.selected_questions.set(questions)
        self.quiz.question_order = [question.id for question in questions]
        self.quiz.save(update_fields=['question_order'])

        async_to_sync(self.consumer.handle_admin_start_quiz)({})

        quiz_started = next(
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'quiz_started'
        )
        self.assertEqual(quiz_started['total_questions'], 4)

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

        self._send_question({
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
        self.assertEqual(question_started['question']['total_questions'], 1)
        self.assertTrue(get_unit_tutorial_state('quiz', self.quiz.room_code, session.code)['current_unit_is_tutorial'])

        phase_snapshot = current_snapshot('quiz', self.quiz.room_code, session.code)
        self.assertEqual(phase_snapshot['question_phase'], 'prompt_visible')
        self.assertIsNone(phase_snapshot['answering_deadline_at'])
        self.assertIsNone(async_to_sync(self.consumer.save_participant_answer)(
            'Alice', session.code, 'A', 1,
        ))
        self._open_answering(tutorial_question.id, session.code)
        opened_snapshot = current_snapshot('quiz', self.quiz.room_code, session.code)
        self.assertEqual(
            opened_snapshot['question_phase'],
            'answering_open',
            self.direct_messages,
        )
        quiz_session = QuizSession.objects.get(quiz=self.quiz)
        self.assertTrue(quiz_session.is_question_active)
        self.assertIsNotNone(quiz_session.question_end_time)

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

        self._send_question({
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
        self.assertEqual(scored_question_started['question']['total_questions'], 1)

        self._open_answering(normal_question.id, session.code)

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

        self._send_question({'question_id': question.id})

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

        self._send_question({'question_id': question.id})

        self.quiz.refresh_from_db()
        self.assertEqual(self.direct_messages, [])
        self.assertEqual(self.quiz.current_question_id, question.id)
        question_started = next(
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        )
        self.assertEqual(question_started['question']['id'], question.id)
        self.assertEqual(question_started['question']['question_text'], 'Question one')

    def test_admin_send_question_survives_missing_dashboard_settings_table(self):
        question = QuizQuestion.objects.create(
            question_text='Question during rolling migration',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='A',
            option_b='B',
            created_by=self.user,
        )
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])

        with patch.object(
            DashboardSettings,
            'load',
            side_effect=OperationalError(
                f'no such table: {DashboardSettings._meta.db_table}'
            ),
        ):
            self._send_question({'question_id': question.id})

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, question.id)
        question_started = next(
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        )
        self.assertEqual(question_started['question']['id'], question.id)
        self.assertEqual(
            question_started['question']['question_reveal_ms_per_character'],
            DashboardSettings.DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER,
        )

    def test_question_start_and_rejoin_include_instance_question_total(self):
        questions = [
            QuizQuestion.objects.create(
                question_text=f'Question {index}',
                question_type='multiple_choice',
                correct_answer='A',
                option_a='A',
                option_b='B',
                created_by=self.user,
            )
            for index in range(1, 5)
        ]
        self.quiz.selected_questions.set(questions)
        self.quiz.question_order = [question.id for question in questions]
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['question_order', 'status'])

        self._send_question({
            'question_id': questions[1].id,
        })

        question_started = next(
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        )
        rejoin_question = async_to_sync(self.consumer.get_current_question_data)()

        self.assertEqual(question_started['question']['total_questions'], 4)
        self.assertEqual(rejoin_question['id'], questions[1].id)
        self.assertEqual(rejoin_question['total_questions'], 4)
        self.assertIsNone(question_started['question']['starts_at'])
        self.assertIsNone(question_started['question']['ends_at'])
        self.assertEqual(
            question_started['question']['starts_at'],
            rejoin_question['starts_at'],
        )
        self.assertEqual(
            question_started['question']['ends_at'],
            rejoin_question['ends_at'],
        )

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

        self._send_question({
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


class QuickQuizPreparedQuestionFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='prepared-quiz-host',
            password='pass',
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.quiz = Quiz.objects.create(
            title='Prepared Quick Quiz',
            creator=self.user,
            room_code='QPRE',
            status='active',
            started_at=timezone.now(),
        )
        self.session = QuizSession.objects.create(quiz=self.quiz)
        self.question = QuizQuestion.objects.create(
            question_text='Prepared question must stay private',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='Private A',
            option_b='Private B',
            time_limit=25,
            created_by=self.user,
        )
        self.quiz.selected_questions.add(self.question)
        reset_question_flow(
            game_key='quiz',
            room_code=self.quiz.room_code,
            session_code=None,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        self.consumer = QuizConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'quiz_{self.quiz.room_code}'
        self.consumer.channel_layer = FakeChannelLayer()
        self.direct_messages = []

        async def _capture_send(*args, **kwargs):
            text_data = kwargs.get('text_data')
            if text_data is None and args:
                text_data = args[0]
            if text_data:
                self.direct_messages.append(json.loads(text_data))

        self.consumer.send = _capture_send

    def _action(self, question_id=None):
        snapshot = current_snapshot('quiz', self.quiz.room_code)
        return {
            'question_id': question_id,
            'game_id': snapshot.get('game_id'),
            'state_revision': snapshot['state_revision'],
            'client_action_id': str(uuid.uuid4()),
        }

    def _prepare(self):
        async_to_sync(self.consumer.handle_admin_prepare_question)(
            self._action(self.question.id)
        )

    def test_prepare_persists_only_a_blank_public_question_shell(self):
        self._prepare()

        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        snapshot = current_snapshot('quiz', self.quiz.room_code)
        prepared_event = next(
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_prepared'
        )

        self.assertIsNone(self.quiz.current_question_id)
        self.assertFalse(self.session.is_question_active)
        self.assertEqual(snapshot['prepared_question_id'], str(self.question.id))
        self.assertTrue(snapshot['question_shell_prepared'])
        self.assertIsNone(snapshot['question_phase'])
        self.assertIsNone(snapshot['question_presented_at'])
        self.assertIsNone(snapshot['answering_started_at'])
        self.assertIsNone(snapshot['answering_deadline_at'])
        self.assertNotIn('question', prepared_event)
        self.assertNotIn(self.question.question_text, json.dumps(prepared_event))

    def test_monitor_reload_renders_prepared_detail_instead_of_visible_overview(self):
        self._prepare()

        response = self.client.get(
            reverse('admin_dashboard:quiz_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['prepared_question'], self.question)
        self.assertContains(response, 'id="questionDetailScreen"')
        self.assertContains(response, 'id="sendPreparedQuestionBtn"')
        self.assertContains(response, 'ZURUECK ZUR FRAGENUEBERSICHT')
        self.assertContains(
            response,
            'host-monitor-question-list d-none" id="questionSelection"',
            html=False,
        )

    def test_player_reload_renders_blank_shell_without_prepared_question_content(self):
        participant = QuizParticipant.objects.create(quiz=self.quiz, name='Alice')
        self._prepare()

        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="questionState" class="game-state "', html=False)
        self.assertContains(response, 'id="quickQuizResponseArea" hidden aria-hidden="true"', html=False)
        self.assertNotContains(response, self.question.question_text)
        self.assertNotContains(response, self.question.option_a)

    def test_participant_rejoin_receives_prepared_shell_instead_of_stale_result(self):
        participant = QuizParticipant.objects.create(quiz=self.quiz, name='Alice')
        self.session.last_question_result = {
            'correct_answer': {'formatted_answer': 'Old answer'},
        }
        self.session.save(update_fields=['last_question_result', 'updated_at'])
        self._prepare()
        self.direct_messages.clear()

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': None,
        })

        message_types = [message['type'] for message in self.direct_messages]
        self.assertIn('question_prepared', message_types)
        self.assertNotIn('question_ended', message_types)

    def test_send_after_prepare_publishes_question_and_clears_shell_marker(self):
        self._prepare()
        self.consumer.channel_layer.group_messages.clear()

        async_to_sync(self.consumer.handle_admin_send_question)(
            self._action(self.question.id)
        )

        self.quiz.refresh_from_db()
        snapshot = current_snapshot('quiz', self.quiz.room_code)
        self.assertEqual(self.quiz.current_question_id, self.question.id)
        self.assertNotIn('prepared_question_id', snapshot)
        self.assertNotIn('question_shell_prepared', snapshot)
        question_started = next(
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        )
        self.assertEqual(question_started['question']['question_text'], self.question.question_text)
        self.assertEqual(question_started['question_phase'], 'prompt_visible')

    def test_short_answer_opens_directly_after_prompt_without_content_reveal(self):
        self.question.question_type = 'short_answer'
        self.question.correct_answer = 'Berlin'
        self.question.option_a = ''
        self.question.option_b = ''
        self.question.save(update_fields=[
            'question_type', 'correct_answer', 'option_a', 'option_b', 'updated_at',
        ])
        self._prepare()
        async_to_sync(self.consumer.handle_admin_send_question)(
            self._action(self.question.id)
        )
        GameRuntimeState.objects.filter(
            game_key='quiz', room_code=self.quiz.room_code,
        ).update(question_presented_at=timezone.now() - timedelta(days=1))

        self.direct_messages.clear()
        self.consumer.channel_layer.group_messages.clear()
        async_to_sync(self.consumer.handle_admin_reveal_question_content)(
            self._action(self.question.id)
        )
        self.assertEqual(self.direct_messages[-1]['code'], 'invalid_phase')
        self.assertEqual(
            current_snapshot('quiz', self.quiz.room_code)['question_phase'],
            'prompt_visible',
        )

        self.direct_messages.clear()
        async_to_sync(self.consumer.handle_admin_open_answering)(
            self._action(self.question.id)
        )

        snapshot = current_snapshot('quiz', self.quiz.room_code)
        group_types = [
            message['type']
            for _, message in self.consumer.channel_layer.group_messages
        ]
        self.assertEqual(snapshot['question_phase'], 'answering_open')
        self.assertIsNone(snapshot['content_revealed_at'])
        self.assertIsNotNone(snapshot['answering_started_at'])
        self.assertIsNotNone(snapshot['answering_deadline_at'])
        self.assertIn('question_answering_opened', group_types)
        self.assertNotIn('question_content_revealed', group_types)

    def test_back_to_overview_clears_selection_but_keeps_player_shell_neutral(self):
        self._prepare()

        async_to_sync(self.consumer.handle_admin_clear_prepared_question)(
            self._action(self.question.id)
        )

        snapshot = current_snapshot('quiz', self.quiz.room_code)
        response = self.client.get(
            reverse('admin_dashboard:quiz_monitor', args=[self.quiz.room_code])
        )
        self.assertNotIn('prepared_question_id', snapshot)
        self.assertTrue(snapshot['question_shell_prepared'])
        self.assertIsNone(response.context['prepared_question'])
        self.assertContains(
            response,
            'host-monitor-main d-none" id="questionDetailScreen"',
            html=False,
        )
        self.assertNotContains(
            response,
            'host-monitor-question-list d-none" id="questionSelection"',
            html=False,
        )


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
        self.hub_session = HubSession.objects.create(
            code=self.participant.hub_session_code,
            name='Quick Live Session',
            creator=self.user,
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=self.hub_session,
            order=0,
            game_key='quiz',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        self.runtime_snapshot = reset_question_flow(
            game_key='quiz',
            room_code=self.quiz.room_code,
            session_code=self.participant.hub_session_code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    async def _receive_until(self, communicator, message_type, attempts=6):
        for _ in range(attempts):
            message = await communicator.receive_json_from(timeout=2)
            if message.get('type') == message_type:
                return message
        self.fail(f'Did not receive {message_type}')

    def test_host_send_question_reaches_host_and_participant_websockets(self):
        submitted_answer = (
            'True' if self.question.question_type == 'true_false' else 'A'
        )

        async def scenario():
            host = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            player = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            host.scope['user'] = self.user
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
            quiz_state = await sync_to_async(current_snapshot)(
                'quiz', self.quiz.room_code, self.participant.hub_session_code,
            )

            await host.send_json_to({
                'type': 'admin_send_question',
                'question_id': self.question.id,
                'hub_session': self.participant.hub_session_code,
                'game_id': quiz_state.get('game_id'),
                'state_revision': quiz_state['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            })

            host_message = await self._receive_until(host, 'question_started')
            player_message = await self._receive_until(player, 'question_started')
            await player.send_json_to({
                'type': 'participant_submit_answer',
                'answer': submitted_answer,
                'question_id': self.question.id,
                'game_id': player_message.get('game_id'),
                'state_revision': player_message['state_revision'],
                'round_id': player_message.get('current_round_id'),
                'set_id': player_message.get('current_set_id'),
                'client_action_id': str(uuid.uuid4()),
            })
            prompt_rejection = await self._receive_until(player, 'action_rejected')

            await host.send_json_to({
                'type': 'admin_reveal_question_content',
                'question_id': self.question.id,
                'hub_session': self.participant.hub_session_code,
                'game_id': host_message.get('game_id'),
                'state_revision': host_message['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            })
            early_reveal_rejection = await self._receive_until(host, 'action_rejected')
            await sync_to_async(
                GameRuntimeState.objects.filter(
                    game_key='quiz',
                    room_code=self.quiz.room_code,
                ).update
            )(
                question_presented_at=(
                    timezone.now()
                    - timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS)
                )
            )

            reveal_payload = {
                'type': 'admin_reveal_question_content',
                'question_id': self.question.id,
                'hub_session': self.participant.hub_session_code,
                'game_id': host_message.get('game_id'),
                'state_revision': host_message['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            }
            await host.send_json_to(reveal_payload)
            host_content = await self._receive_until(host, 'question_content_revealed')
            player_content = await self._receive_until(player, 'question_content_revealed')
            await player.send_json_to({
                'type': 'participant_submit_answer',
                'answer': submitted_answer,
                'question_id': self.question.id,
                'game_id': player_content.get('game_id'),
                'state_revision': player_content['state_revision'],
                'round_id': player_content.get('current_round_id'),
                'set_id': player_content.get('current_set_id'),
                'client_action_id': str(uuid.uuid4()),
            })
            content_rejection = await self._receive_until(player, 'action_rejected')

            await host.send_json_to(reveal_payload)
            duplicate_content = await self._receive_until(host, 'question_content_revealed')
            await self._receive_until(player, 'question_content_revealed')
            await host.send_json_to({
                'type': 'admin_open_answering',
                'question_id': self.question.id,
                'hub_session': self.participant.hub_session_code,
                'game_id': duplicate_content.get('game_id'),
                'state_revision': duplicate_content['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            })
            manual_open_rejection = await self._receive_until(host, 'action_rejected')
            await asyncio.sleep(0.8)

            await player.send_json_to({
                'type': 'participant_submit_answer',
                'answer': submitted_answer,
                'question_id': self.question.id,
                'game_id': player_content.get('game_id'),
                'state_revision': player_content['state_revision'],
                'round_id': player_content.get('current_round_id'),
                'set_id': player_content.get('current_set_id'),
                'client_action_id': str(uuid.uuid4()),
            })
            accepted_answer = await player.receive_json_from(timeout=2)

            await host.disconnect()
            await player.disconnect()

            self.assertEqual(early_reveal_rejection['code'], 'question_not_visible')

            return (
                host_message, player_message, prompt_rejection, host_content,
                player_content, content_rejection, duplicate_content,
                manual_open_rejection, accepted_answer,
            )

        (
            host_message, player_message, prompt_rejection, host_content,
            player_content, content_rejection, duplicate_content,
            manual_open_rejection, accepted_answer,
        ) = async_to_sync(scenario)()
        self.quiz.refresh_from_db()

        self.assertEqual(self.quiz.current_question_id, self.question.id)
        self.assertEqual(host_message['question']['id'], self.question.id)
        self.assertEqual(player_message['question']['id'], self.question.id)
        self.assertEqual(host_message['question_phase'], 'prompt_visible')
        self.assertEqual(player_message['question']['options'], [])
        self.assertIn(prompt_rejection['code'], {'invalid_phase', 'stale_action'})
        self.assertEqual(player_content['question_phase'], 'answering_open')
        self.assertEqual(len(player_content['question']['options']), 2)
        self.assertEqual(content_rejection['code'], 'invalid_phase')
        self.assertFalse(player_content['answering_allowed'])
        reveal_started_at = parse_datetime(player_content['content_revealed_at'])
        answering_started_at = parse_datetime(player_content['answering_started_at'])
        self.assertEqual(
            answering_started_at,
            reveal_started_at + timedelta(milliseconds=700),
        )
        self.assertIsNotNone(player_content['answering_deadline_at'])
        self.assertEqual(
            duplicate_content['answering_deadline_at'],
            host_content['answering_deadline_at'],
        )
        self.assertEqual(manual_open_rejection['code'], 'invalid_phase')
        self.assertEqual(accepted_answer.get('type'), 'answer_submitted', accepted_answer)
        self.assertEqual(accepted_answer['question_id'], self.question.id)

    def test_automatic_answer_reveal_types_have_no_manual_open_answering_control(self):
        template_source = (
            Path(__file__).resolve().parent.parent
            / 'templates'
            / 'admin_dashboard'
            / 'quiz_monitor.html'
        ).read_text(encoding='utf-8')

        self.assertIn(
            "question_runtime.question_phase == 'content_visible' and quiz.current_question.question_type != 'multiple_choice' and quiz.current_question.question_type != 'true_false'",
            template_source,
        )
        self.assertIn(
            "this.questionPhase === 'content_visible'\n                && question.question_type !== 'multiple_choice'",
            template_source,
        )
        self.assertIn(
            "&& question.question_type !== 'true_false'",
            template_source,
        )

    def test_end_quiz_reaches_host_websocket_without_reload(self):
        async def scenario():
            host = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            host.scope['user'] = self.user
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
        session = self.hub_session
        self.quiz.status = 'waiting'
        self.quiz.started_at = None
        self.quiz.save(update_fields=['status', 'started_at'])
        HubParticipant.objects.create(session=session, nickname=self.participant.name)
        self.assertTrue(start_session_check_in(session)['success'])
        self.assertTrue(participant_check_in(session, self.participant.name)['success'])
        self.assertTrue(complete_session_check_in(session)['success'])
        self.participant.is_active = False
        self.participant.save(update_fields=['is_active'])

        async def scenario():
            hub = WebsocketCommunicator(application, f'/ws/hub/{session.code}/')
            host = WebsocketCommunicator(application, f'/ws/quiz/{self.quiz.room_code}/')
            host.scope['user'] = self.user
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


class QuickQuizTrueFalseWebSocketLiveFlowTests(QuickQuizWebSocketLiveFlowTests):
    def setUp(self):
        super().setUp()
        self.question.question_type = 'true_false'
        self.question.correct_answer = 'True'
        self.question.option_a = ''
        self.question.option_b = ''
        self.question.save(update_fields=[
            'question_type',
            'correct_answer',
            'option_a',
            'option_b',
            'updated_at',
        ])


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
            option_c='Answer C',
            option_d='Answer D',
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
        reset_question_flow(
            game_key='quiz',
            room_code=self.quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        observe_snapshot(
            'quiz',
            self.quiz.room_code,
            {'type': 'quiz_started', 'phase': 'active'},
            self.session.code,
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

    def _open_answering_from_host(self):
        self.host_page.wait_for_selector('#openAnsweringBtn:not([disabled])', timeout=self.TIMEOUT)
        self.assertEqual(self.host_page.locator('#revealQuestionContentBtn').count(), 0)
        self.host_page.click('#openAnsweringBtn')
        self._wait_for_frame('host', 'received', 'question_answering_opened')
        self._wait_for_frame('player', 'received', 'question_answering_opened')

    def _activate_question_for_browser(self, question):
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.session.code)
        action = {
            'question_id': question.id,
            'game_id': snapshot.get('game_id'),
            'state_revision': snapshot['state_revision'],
            'client_action_id': str(uuid.uuid4()),
        }
        presented = present_question(
            game_key='quiz', room_code=self.quiz.room_code,
            session_code=self.session.code, action=action,
            at=(
                timezone.now()
                - timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS)
            ),
        )
        self.assertTrue(presented.accepted)
        quiz_session = QuizSession.objects.get(quiz=self.quiz)
        quiz_session.present_question(question)
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.session.code)
        revealed = reveal_question_content(
            game_key='quiz', room_code=self.quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': snapshot.get('game_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
        )
        self.assertTrue(revealed.accepted)
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.session.code)
        opened_at = timezone.now()
        opened = open_answering(
            game_key='quiz', room_code=self.quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': snapshot.get('game_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            answer_duration_seconds=question.time_limit,
            at=opened_at,
        )
        self.assertTrue(opened.accepted)
        quiz_session.open_answering(
            question,
            started_at=opened_at,
            answer_duration_seconds=question.time_limit,
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

    def _dispatch_player_question_started(self, question):
        self.quiz.refresh_from_db()
        quiz_session = QuizSession.objects.get(quiz=self.quiz)
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.session.code)
        payload = {
            'type': 'question_started',
            **{
                key: snapshot.get(key)
                for key in (
                    'state_revision', 'server_now', 'game_id', 'question_phase',
                    'question_presented_at', 'content_revealed_at',
                    'answering_started_at', 'answering_deadline_at',
                    'answering_allowed', 'starts_at', 'ends_at',
                )
            },
            'question': {
                'id': question.id,
                'question_text': question.question_text,
                'question_type': question.get_effective_question_type(),
                'options': [
                    {'key': 'True', 'text': 'True'},
                    {'key': 'False', 'text': 'False'},
                ],
                'short_answer_fields': [],
                'time_limit': question.time_limit,
                'points': question.get_effective_max_points(),
                'is_tutorial_round': False,
                'starts_at': (
                    self.quiz.question_start_time.isoformat()
                    if self.quiz.question_start_time
                    else None
                ),
                'ends_at': (
                    quiz_session.question_end_time.isoformat()
                    if quiz_session.question_end_time
                    else None
                ),
                'server_now': timezone.now().isoformat(),
                'question_phase': snapshot.get('question_phase'),
                'question_presented_at': snapshot.get('question_presented_at'),
                'content_revealed_at': snapshot.get('content_revealed_at'),
            },
        }
        self.player_page.evaluate(
            """payload => {
                const socket = (window.__quickQuizTestSockets || []).find(candidate => (
                    candidate.url.includes('/ws/quiz/')
                    && candidate.readyState === WebSocket.OPEN
                ));
                if (!socket || typeof socket.onmessage !== 'function') {
                    throw new Error('Active Quick Quiz socket not captured');
                }
                socket.onmessage(new MessageEvent('message', {
                    data: JSON.stringify(payload),
                }));
            }""",
            payload,
        )

    def test_true_false_first_click_survives_duplicate_rejoin_state_across_questions(self):
        self.question.question_type = 'true_false'
        self.question.correct_answer = 'True'
        self.question.option_a = ''
        self.question.option_b = ''
        self.question.time_limit = 90
        self.question.save(update_fields=[
            'question_type',
            'correct_answer',
            'option_a',
            'option_b',
            'time_limit',
            'updated_at',
        ])
        second_question = QuizQuestion.objects.create(
            question_text='Second true or false question?',
            question_type='true_false',
            correct_answer='False',
            points=1,
            time_limit=90,
            created_by=self.user,
        )
        self.quiz.selected_questions.add(second_question)
        self.quiz.question_order = [self.question.id, second_question.id]
        self.quiz.save(update_fields=['question_order', 'updated_at'])
        quiz_session = QuizSession.objects.get(quiz=self.quiz)
        self._activate_question_for_browser(self.question)
        self.player_page.add_init_script(
            """(() => {
                const NativeWebSocket = window.WebSocket;
                window.__quickQuizTestSockets = [];
                window.__quickQuizStateTransitions = [];
                document.addEventListener('DOMContentLoaded', () => {
                    const questionState = document.getElementById('questionState');
                    const waitingState = document.getElementById('waitingQuizState');
                    const recordState = () => {
                        if (questionState && !questionState.classList.contains('d-none')) {
                            window.__quickQuizStateTransitions.push('question');
                        } else if (waitingState && !waitingState.classList.contains('d-none')) {
                            window.__quickQuizStateTransitions.push('waiting');
                        }
                    };
                    recordState();
                    new MutationObserver(recordState).observe(document.body, {
                        attributes: true,
                        attributeFilter: ['class'],
                        subtree: true,
                    });
                }, { once: true });
                window.WebSocket = new Proxy(NativeWebSocket, {
                    construct(Target, args) {
                        const socket = new Target(...args);
                        window.__quickQuizTestSockets.push(socket);
                        return socket;
                    },
                });
            })();"""
        )
        self.player_page.add_init_script(
            "localStorage.setItem('participant_interface_theme', 'vhs');"
        )
        play_url = (
            f'{self.live_server_url}'
            f'{reverse("quiz:play", args=[self.quiz.room_code, self.participant.name])}'
            f'?hub_session={self.session.code}'
        )
        self.player_page.goto(play_url)
        self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')
        self.player_page.wait_for_selector(
            '#questionState:not(.d-none) .answer-option[data-value="True"]',
            timeout=self.TIMEOUT,
        )
        self.player_page.wait_for_function(
            "() => document.documentElement.dataset.participantTheme === 'vhs'",
            timeout=self.TIMEOUT,
        )
        self.player_page.wait_for_selector(
            '.qa-theme-button-shell > .answer-option[data-value="True"]',
            timeout=self.TIMEOUT,
        )

        first_option = self.player_page.locator('.answer-option[data-value="True"]')
        self.assertEqual(first_option.evaluate("element => element.tagName"), 'BUTTON')
        self.assertEqual(first_option.get_attribute('type'), 'button')
        self.assertFalse(first_option.is_disabled())
        self.assertFalse(first_option.evaluate("element => element.classList.contains('disabled')"))
        self.assertEqual(first_option.get_attribute('aria-disabled'), 'false')
        self.assertNotIn(
            'waiting',
            self.player_page.evaluate("window.__quickQuizStateTransitions.slice(1)"),
        )
        first_option.evaluate("element => { element.dataset.regressionNode = 'first'; }")
        first_option.click()
        self.assertTrue(first_option.evaluate("element => element.classList.contains('selected')"))
        self.assertFalse(self.player_page.locator('#submitAnswerBtn').is_disabled())

        self._dispatch_player_question_started(self.question)
        self.player_page.wait_for_timeout(300)
        first_option = self.player_page.locator('.answer-option[data-value="True"]')
        self.assertEqual(first_option.get_attribute('data-regression-node'), 'first')
        self.assertTrue(first_option.evaluate("element => element.classList.contains('selected')"))
        self.assertFalse(self.player_page.locator('#submitAnswerBtn').is_disabled())

        self.player_page.locator('#submitAnswerBtn').dblclick()
        self._wait_for_frame('player', 'received', 'answer_submitted')
        self.assertTrue(first_option.is_disabled())
        self.assertTrue(first_option.evaluate("element => element.classList.contains('disabled')"))
        self.assertEqual(first_option.get_attribute('aria-disabled'), 'true')
        self.assertEqual(
            QuizAnswer.objects.filter(
                quiz=self.quiz,
                participant=self.participant,
                question=self.question,
            ).count(),
            1,
        )

        self.player_page.reload()
        self.player_page.wait_for_selector(
            '#answerSubmittedState:not(.d-none)',
            timeout=self.TIMEOUT,
        )
        rejoined_locked_option = self.player_page.locator('.answer-option[data-value="True"]')
        self.assertTrue(rejoined_locked_option.is_disabled())
        self.assertTrue(
            rejoined_locked_option.evaluate("element => element.classList.contains('disabled')")
        )
        self.assertEqual(rejoined_locked_option.get_attribute('aria-disabled'), 'true')

        self._dispatch_player_question_started(self.question)
        self.player_page.wait_for_timeout(300)
        self.assertTrue(
            self.player_page.locator('#answerSubmittedState').evaluate(
                "element => !element.classList.contains('d-none')"
            )
        )

        quiz_session.refresh_from_db()
        finish_question_flow(
            game_key='quiz',
            room_code=self.quiz.room_code,
            session_code=self.session.code,
            question_id=self.question.id,
        )
        quiz_session.end_current_question()
        self._activate_question_for_browser(second_question)
        self._dispatch_player_question_started(second_question)
        self.player_page.wait_for_function(
            """() => (
                document.querySelector('#questionState:not(.d-none) #questionText')
                    ?.textContent.includes('true or false question?')
            )""",
            timeout=self.TIMEOUT,
        )
        second_option = self.player_page.locator('.answer-option[data-value="False"]')
        self.player_page.wait_for_selector(
            '.qa-theme-button-shell > .answer-option[data-value="False"]',
            timeout=self.TIMEOUT,
        )
        self.assertFalse(second_option.is_disabled())
        self.assertFalse(second_option.evaluate("element => element.classList.contains('disabled')"))
        self.assertEqual(second_option.get_attribute('aria-disabled'), 'false')
        second_option.hover()
        self.assertNotEqual(
            second_option.evaluate("element => getComputedStyle(element).transform"),
            'none',
        )
        second_option.focus()
        self.player_page.keyboard.press('Space')
        self.assertTrue(second_option.evaluate("element => element.classList.contains('selected')"))
        self.assertNotEqual(
            second_option.evaluate("element => getComputedStyle(element).outlineStyle"),
            'none',
        )
        self.player_page.locator('#questionText').hover()
        second_option.evaluate("element => element.blur()")
        self.assertIn(
            'gradient',
            second_option.evaluate("element => getComputedStyle(element).backgroundImage"),
        )
        second_option.evaluate("element => { element.dataset.regressionNode = 'second'; }")

        stale_question_end = {
            'type': 'question_ended',
            'correct_answer': {
                'question_id': self.question.id,
                'correct_answer': 'True',
                'question_text': self.question.question_text,
            },
            'answer_results': [],
            'auto_finalized_answers': [],
            'is_tutorial_round': False,
        }
        self.player_page.evaluate(
            """payload => {
                const socket = (window.__quickQuizTestSockets || []).find(candidate => (
                    candidate.url.includes('/ws/quiz/')
                    && candidate.readyState === WebSocket.OPEN
                ));
                socket.onmessage(new MessageEvent('message', {
                    data: JSON.stringify(payload),
                }));
            }""",
            stale_question_end,
        )
        self.assertFalse(second_option.is_disabled())
        self.assertFalse(second_option.evaluate("element => element.classList.contains('disabled')"))
        self.assertTrue(
            self.player_page.locator('#questionState').evaluate(
                "element => !element.classList.contains('d-none')"
            )
        )

        self._dispatch_player_question_started(second_question)
        self.player_page.wait_for_timeout(300)
        second_option = self.player_page.locator('.answer-option[data-value="False"]')
        self.assertEqual(second_option.get_attribute('data-regression-node'), 'second')
        self.assertTrue(second_option.evaluate("element => element.classList.contains('selected')"))
        self.assertIn(
            'gradient',
            second_option.evaluate("element => getComputedStyle(element).backgroundImage"),
        )

        self.player_page.reload()
        self.player_page.wait_for_selector(
            '#questionState:not(.d-none) .answer-option[data-value="True"]',
            timeout=self.TIMEOUT,
        )
        self.assertEqual(self.player_page.locator('#answerOptions .answer-option').count(), 2)
        reloaded_option = self.player_page.locator('.answer-option[data-value="True"]')
        reloaded_option.click()
        self.assertTrue(reloaded_option.evaluate("element => element.classList.contains('selected')"))
        self.assertFalse(self.player_page.locator('#submitAnswerBtn').is_disabled())

        mobile_context = self._browser.new_context(
            viewport={'width': 390, 'height': 844},
            has_touch=True,
            is_mobile=True,
        )
        install_browser_test_stubs(mobile_context)
        mobile_page = mobile_context.new_page()
        try:
            mobile_page.add_init_script(
                "localStorage.setItem('participant_interface_theme', 'vhs');"
            )
            mobile_page.goto(play_url)
            mobile_page.wait_for_selector(
                '#questionState:not(.d-none) .answer-option[data-value="False"]',
                timeout=self.TIMEOUT,
            )
            mobile_option = mobile_page.locator('.answer-option[data-value="False"]')
            mobile_option.tap()
            self.assertTrue(
                mobile_option.evaluate("element => element.classList.contains('selected')")
            )
        finally:
            mobile_page.close()
            mobile_context.close()
        self._assert_no_browser_errors()

    def test_true_false_reveal_opens_answering_automatically_for_two_questions(self):
        self.question.question_type = 'true_false'
        self.question.correct_answer = 'True'
        self.question.option_a = ''
        self.question.option_b = ''
        self.question.save(update_fields=[
            'question_type', 'correct_answer', 'option_a', 'option_b', 'updated_at',
        ])
        second_question = QuizQuestion.objects.create(
            question_text='Second automatic true or false question?',
            question_type='true_false',
            correct_answer='False',
            points=1,
            time_limit=30,
            created_by=self.user,
        )
        self.quiz.selected_questions.add(second_question)
        self.quiz.question_order = [self.question.id, second_question.id]
        self.quiz.save(update_fields=['question_order', 'updated_at'])
        HubParticipant.objects.create(session=self.session, nickname='Bob')
        second_participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )
        second_context = self._browser.new_context()
        install_browser_test_stubs(second_context)
        self.addCleanup(second_context.close)
        second_page = second_context.new_page()
        self.websocket_urls['player2'] = []
        self.websocket_frames['player2'] = {'sent': [], 'received': []}
        self._instrument_page('player2', second_page)

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
        second_play_url = (
            f'{self.live_server_url}'
            f'{reverse("quiz:play", args=[self.quiz.room_code, second_participant.name])}'
            f'?hub_session={self.session.code}'
        )
        self.host_page.goto(monitor_url)
        self.player_page.goto(play_url)
        second_page.goto(second_play_url)
        self._wait_for_ws_url('host', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_ws_url('player2', f'/ws/quiz/{self.quiz.room_code}/')

        for index, question in enumerate((
            self.question,
            second_question,
        )):
            self.websocket_frames['host']['received'].clear()
            self.websocket_frames['player']['received'].clear()
            self.websocket_frames['player2']['received'].clear()
            send_selector = (
                f'.send-question-btn[data-question-id="{question.id}"]:not([disabled])'
            )
            self.host_page.wait_for_selector(send_selector, timeout=self.TIMEOUT)
            self.host_page.click(send_selector)
            self.host_page.wait_for_selector('#sendPreparedQuestionBtn', timeout=self.TIMEOUT)
            self.host_page.click('#sendPreparedQuestionBtn')
            self._wait_for_frame('host', 'received', 'question_started')
            self._wait_for_frame('player', 'received', 'question_started')
            self._wait_for_frame('player2', 'received', 'question_started')
            self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())

            self.host_page.wait_for_selector(
                '#revealQuestionContentBtn:not([disabled])',
                timeout=self.TIMEOUT,
            )
            self.host_page.click('#revealQuestionContentBtn')
            self._wait_for_frame('host', 'received', 'question_content_revealed')
            self._wait_for_frame('player', 'received', 'question_content_revealed')
            self._wait_for_frame('player2', 'received', 'question_content_revealed')
            self.player_page.wait_for_selector(
                '#answerOptions .answer-option.is-content-visible',
                timeout=self.TIMEOUT,
            )
            self.assertEqual(
                self.player_page.locator('#answerOptions .answer-option').evaluate_all(
                    'buttons => buttons.map(button => Number(button.dataset.revealDelayMs))'
                ),
                [0, 300],
            )
            self.assertTrue(
                self.player_page.locator('#answerOptions .answer-option').last.is_disabled()
            )
            self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())

            if index == 0:
                self.host_page.reload()
                self.host_page.wait_for_function(
                    '() => !!window.adminGameMonitor', timeout=self.TIMEOUT,
                )
            else:
                second_page.reload()
                second_page.wait_for_selector(
                    '#questionState:not(.d-none) #answerOptions .answer-option',
                    timeout=self.TIMEOUT,
                )
            self.assertEqual(self.host_page.locator('#openAnsweringBtn').count(), 0)
            for page in (self.player_page, second_page):
                page.wait_for_function(
                    """() => {
                        const buttons = Array.from(document.querySelectorAll('#answerOptions .answer-option'));
                        return buttons.length === 2 && buttons.every(button => (
                            button.classList.contains('is-content-visible') && !button.disabled
                        ));
                    }""",
                    timeout=self.TIMEOUT,
                )
            self.assertFalse(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())
            self.assertFalse(second_page.locator('#playerQuestionTimerWrapper').is_hidden())
            self.host_page.click('#endQuestionBtn')
            self._wait_for_frame('host', 'received', 'question_ended')
            self._wait_for_frame('player', 'received', 'question_ended')
            if index == 0:
                self.host_page.wait_for_selector(
                    '#returnToQuestionOverviewBtn:not(.d-none)', timeout=self.TIMEOUT,
                )
                self.host_page.click('#returnToQuestionOverviewBtn')
        self._assert_no_browser_errors()

    def test_host_layout_stays_compact_without_horizontal_overflow(self):
        self._activate_question_for_browser(self.question)
        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:quiz_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )
        self.host_page.goto(monitor_url)
        self.host_page.wait_for_selector('#endQuestionBtn', timeout=self.TIMEOUT)

        for width, height in ((1920, 1080), (1366, 768), (1280, 720), (1024, 768)):
            with self.subTest(viewport=f'{width}x{height}'):
                self.host_page.set_viewport_size({'width': width, 'height': height})
                self.host_page.wait_for_timeout(100)
                metrics = self.host_page.evaluate("""
                    () => ({
                        scrollWidth: document.documentElement.scrollWidth,
                        clientWidth: document.documentElement.clientWidth,
                    })
                """)
                self.assertLessEqual(metrics['scrollWidth'], metrics['clientWidth'] + 1)

                main_box = self.host_page.locator('.quiz-primary-column').bounding_box()
                participant_box = self.host_page.locator('.quiz-participant-column').bounding_box()
                action_box = self.host_page.locator('.question-actions-panel').bounding_box()
                grid_box = self.host_page.locator('.quiz-monitor-grid').bounding_box()
                self.assertIsNotNone(main_box)
                self.assertIsNotNone(participant_box)
                self.assertIsNotNone(action_box)
                self.assertIsNotNone(grid_box)
                self.assertTrue(self.host_page.locator('.quiz-question-list-card').is_hidden())
                self.assertGreater(participant_box['x'], main_box['x'])
                self.assertLessEqual(action_box['y'] + action_box['height'], height + 1)

        participant_panel = self.host_page.locator('.quiz-participant-column').inner_text()
        self.assertNotIn('Answer A', participant_panel)
        self.assertEqual(self.host_page.locator('[data-quick-quiz-title]').count(), 1)
        self.assertEqual(self.host_page.locator('.quiz-type-label').inner_text(), 'Spieltyp: Quiz')
        self._assert_no_browser_errors()

    def test_host_layout_keeps_same_information_architecture_across_states(self):
        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:quiz_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )
        self.host_page.set_viewport_size({'width': 1366, 'height': 768})

        def assert_layout_artifact(state_name):
            detail_visible = state_name != 'overview'
            geometry = self.host_page.evaluate("""
                () => {
                    const rect = selector => {
                        const bounds = document.querySelector(selector).getBoundingClientRect();
                        return {
                            x: bounds.x,
                            y: bounds.y,
                            width: bounds.width,
                            height: bounds.height,
                            bottom: bounds.bottom,
                        };
                    };
                    return {
                        detailHidden: document.querySelector('#questionDetailScreen').classList.contains('d-none'),
                        listHidden: document.querySelector('#questionSelection').classList.contains('d-none'),
                        current: document.querySelector('#questionDetailScreen').classList.contains('d-none') ? null : rect('.host-monitor-current'),
                        actions: document.querySelector('#questionDetailScreen').classList.contains('d-none') ? null : rect('.host-monitor-actions'),
                        participants: document.querySelector('#questionDetailScreen').classList.contains('d-none') ? null : rect('.host-monitor-participants'),
                        scrollWidth: document.documentElement.scrollWidth,
                        clientWidth: document.documentElement.clientWidth,
                    };
                }
            """)
            self.assertEqual(geometry['detailHidden'], not detail_visible, state_name)
            self.assertEqual(geometry['listHidden'], detail_visible, state_name)
            if detail_visible:
                self.assertGreater(geometry['participants']['x'], geometry['current']['x'], state_name)
                self.assertGreaterEqual(geometry['actions']['y'], geometry['current']['bottom'] - 1, state_name)
            self.assertLessEqual(geometry['scrollWidth'], geometry['clientWidth'] + 1, state_name)
            self.assertGreater(len(self.host_page.screenshot(full_page=True)), 10_000, state_name)

        self.quiz.status = 'waiting'
        self.quiz.started_at = None
        self.quiz.save(update_fields=['status', 'started_at', 'updated_at'])
        self.host_page.goto(monitor_url)
        self.host_page.wait_for_selector('#questionSelection:not(.d-none)', timeout=self.TIMEOUT)
        assert_layout_artifact('overview')

        self.quiz.status = 'active'
        self.quiz.started_at = timezone.now()
        self.quiz.save(update_fields=['status', 'started_at', 'updated_at'])
        reset_question_flow(
            game_key='quiz',
            room_code=self.quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        observe_snapshot(
            'quiz',
            self.quiz.room_code,
            {'type': 'quiz_started', 'phase': 'active'},
            self.session.code,
        )
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.session.code)
        presented = present_question(
            game_key='quiz',
            room_code=self.quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': self.question.id,
                'game_id': snapshot.get('game_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            at=timezone.now() - timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS),
        )
        self.assertTrue(presented.accepted)
        QuizSession.objects.get(quiz=self.quiz).present_question(self.question)

        self.host_page.reload()
        self.host_page.wait_for_selector(
            '#revealQuestionContentBtn:not([disabled])',
            timeout=self.TIMEOUT,
        )
        assert_layout_artifact('prepared')

        self.host_page.click('#revealQuestionContentBtn')
        self._wait_for_frame('host', 'received', 'question_content_revealed')
        self.host_page.reload()
        self.host_page.wait_for_function('() => !!window.adminGameMonitor', timeout=self.TIMEOUT)
        self.assertEqual(self.host_page.locator('#openAnsweringBtn').count(), 0)
        self.host_page.wait_for_selector('#endQuestionBtn', timeout=self.TIMEOUT)
        self.host_page.wait_for_selector('#questionTimerWrapper:not(.d-none)', timeout=self.TIMEOUT)
        assert_layout_artifact('answering')
        self._assert_no_browser_errors()

    def test_send_question_and_end_quiz_update_host_and_player_without_reload(self):
        second_question = QuizQuestion.objects.create(
            question_text='Second browser live question?',
            question_type='multiple_choice',
            correct_answer='B',
            option_a='Second A',
            option_b='Second B',
            option_c='Second C',
            option_d='Second D',
            points=1,
            time_limit=30,
            created_by=self.user,
        )
        third_question = QuizQuestion.objects.create(
            question_text='Third browser live question?',
            question_type='multiple_choice',
            correct_answer='C',
            option_a='Third A',
            option_b='Third B',
            option_c='Third C',
            option_d='Third D',
            points=1,
            time_limit=30,
            created_by=self.user,
        )
        self.quiz.selected_questions.add(second_question, third_question)
        self.quiz.question_order = [self.question.id, second_question.id, third_question.id]
        self.quiz.save(update_fields=['question_order', 'updated_at'])
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

        send_selector = f'.send-question-btn[data-question-id="{self.question.id}"]'
        self.host_page.click(send_selector)
        self._wait_for_frame('player', 'received', 'question_prepared')
        self.host_page.wait_for_selector('#sendPreparedQuestionBtn', timeout=self.TIMEOUT)
        self.assertTrue(self.host_page.locator('#questionSelection').is_hidden())
        self.assertFalse(self.host_page.locator('#questionDetailScreen').is_hidden())
        self.player_page.wait_for_selector('#questionState:not(.d-none)', timeout=self.TIMEOUT)
        self.assertEqual(self.player_page.locator('#questionText').text_content(), '')
        self.assertTrue(self.player_page.locator('#quickQuizResponseArea').is_hidden())
        self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())
        self.host_page.click('#sendPreparedQuestionBtn')
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
        self.assertTrue(
            self.player_page.locator('#questionText').evaluate(
                "element => element.classList.contains('is-typewriting')"
            )
        )
        self.assertTrue(self.host_page.locator('#revealQuestionContentBtn').is_disabled())
        self.assertTrue(self.player_page.locator('#quickQuizResponseArea').is_hidden())
        self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())
        self.player_page.wait_for_function(
            """() => (
                document.querySelector('#questionText')?.textContent.includes('Browser live question?')
                && !document.querySelector('#questionText')?.classList.contains('is-typewriting')
            )""",
            timeout=self.TIMEOUT,
        )
        self.assertEqual(self.player_page.locator('#answerOptions .answer-option').count(), 0)
        self.assertTrue(self.player_page.locator('#quickQuizResponseArea').is_hidden())
        self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())

        self.host_page.wait_for_selector('#revealQuestionContentBtn:not([disabled])', timeout=self.TIMEOUT)
        self.player_page.evaluate(
            """() => {
                window.__answerRevealEntries = {};
                new MutationObserver(records => {
                    records.forEach(record => {
                        const option = record.target;
                        if (
                            record.attributeName === 'class'
                            && option.matches('.answer-option.is-content-visible')
                            && !window.__answerRevealEntries[option.dataset.value]
                        ) {
                            const style = getComputedStyle(option);
                            const entry = {
                                initialOpacity: Number(style.opacity),
                                at: performance.now(),
                            };
                            window.__answerRevealEntries[option.dataset.value] = entry;
                            requestAnimationFrame(() => requestAnimationFrame(() => {
                                const activeStyle = getComputedStyle(option);
                                entry.opacity = Number(activeStyle.opacity);
                                entry.transform = activeStyle.transform;
                                entry.sampled = true;
                            }));
                        }
                    });
                }).observe(document.querySelector('#answerOptions'), {
                    attributes: true,
                    attributeFilter: ['class'],
                    subtree: true,
                });
            }"""
        )
        self.host_page.click('#revealQuestionContentBtn')
        self._wait_for_frame('player', 'received', 'question_content_revealed')
        self.player_page.wait_for_selector('#answerOptions .answer-option.is-content-visible', timeout=self.TIMEOUT)
        self.assertEqual(
            self.player_page.locator('#answerOptions .answer-option').evaluate_all(
                "buttons => buttons.map(button => Number(button.dataset.revealDelayMs))"
            ),
            [0, 300, 600, 900],
        )
        self.player_page.wait_for_function(
            """() => {
                const entries = Object.values(window.__answerRevealEntries);
                return entries.length === 4 && entries.every(entry => entry.sampled);
            }""",
            timeout=self.TIMEOUT,
        )
        reveal_entries = self.player_page.evaluate('window.__answerRevealEntries')
        self.assertTrue(
            all(entry['opacity'] < 1 for entry in reveal_entries.values()),
            reveal_entries,
        )
        self.assertTrue(all(entry['transform'] != 'none' for entry in reveal_entries.values()))
        self.assertEqual(
            sorted(reveal_entries, key=lambda key: reveal_entries[key]['at']),
            ['A', 'B', 'C', 'D'],
        )
        self.assertTrue(self.player_page.locator('#answerOptions .answer-option').first.is_disabled())
        self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())

        self.player_page.reload()
        self.player_page.wait_for_selector(
            '#questionState:not(.d-none) #answerOptions .answer-option',
            timeout=self.TIMEOUT,
        )
        self.assertEqual(self.host_page.locator('#openAnsweringBtn').count(), 0)
        self.player_page.wait_for_function(
            """() => {
                const buttons = Array.from(document.querySelectorAll('#answerOptions .answer-option'));
                return buttons.length === 4 && buttons.every(button => (
                    button.classList.contains('is-content-visible') && !button.disabled
                ));
            }""",
            timeout=self.TIMEOUT,
        )
        self.assertFalse(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())

        self.player_page.click('.answer-option[data-value="A"]')
        self.player_page.click('#submitAnswerBtn')
        self._wait_for_frame('player', 'received', 'answer_submitted')
        self.host_page.click('#endQuestionBtn')
        self._wait_for_frame('host', 'received', 'question_ended')
        self._wait_for_frame('player', 'received', 'question_ended')
        self.host_page.wait_for_selector('#returnToQuestionOverviewBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.host_page.click('#returnToQuestionOverviewBtn')
        second_selector = f'.send-question-btn[data-question-id="{second_question.id}"]:not([disabled])'
        self.host_page.wait_for_selector(second_selector, timeout=self.TIMEOUT)

        self.websocket_frames['host']['received'].clear()
        self.websocket_frames['player']['received'].clear()
        self.host_page.click(second_selector)
        self._wait_for_frame('player', 'received', 'question_prepared')
        self.host_page.wait_for_selector('#sendPreparedQuestionBtn', timeout=self.TIMEOUT)
        self.player_page.wait_for_selector('#questionState:not(.d-none)', timeout=self.TIMEOUT)
        self.assertTrue(self.player_page.locator('#correctAnswerState').is_hidden())
        self.assertEqual(self.player_page.locator('#questionText').text_content(), '')
        self.assertTrue(self.player_page.locator('#quickQuizResponseArea').is_hidden())
        self.host_page.click('#sendPreparedQuestionBtn')
        self._wait_for_frame('host', 'received', 'question_started')
        self._wait_for_frame('player', 'received', 'question_started')
        self.player_page.wait_for_function(
            "() => document.querySelector('#questionText')?.textContent.includes('Second browser live question?')",
            timeout=self.TIMEOUT,
        )
        self.host_page.wait_for_selector('#revealQuestionContentBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#revealQuestionContentBtn')
        self._wait_for_frame('player', 'received', 'question_content_revealed')
        self.assertEqual(self.host_page.locator('#openAnsweringBtn').count(), 0)
        self.player_page.wait_for_function(
            """() => {
                const buttons = Array.from(document.querySelectorAll('#answerOptions .answer-option'));
                return buttons.length === 4 && buttons.every(button => (
                    button.classList.contains('is-content-visible') && !button.disabled
                ));
            }""",
            timeout=self.TIMEOUT,
        )
        self.player_page.click('.answer-option[data-value="B"]')
        self.player_page.click('#submitAnswerBtn')
        self._wait_for_frame('player', 'received', 'answer_submitted')
        self.host_page.click('#endQuestionBtn')
        self._wait_for_frame('host', 'received', 'question_ended')
        self._wait_for_frame('player', 'received', 'question_ended')

        self.host_page.wait_for_selector('#returnToQuestionOverviewBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.host_page.click('#returnToQuestionOverviewBtn')
        third_selector = f'.send-question-btn[data-question-id="{third_question.id}"]:not([disabled])'
        self.host_page.wait_for_selector(third_selector, timeout=self.TIMEOUT)
        self.websocket_frames['player']['received'].clear()
        self.host_page.click(third_selector)
        self._wait_for_frame('player', 'received', 'question_prepared')
        self.host_page.wait_for_selector('#sendPreparedQuestionBtn', timeout=self.TIMEOUT)
        self.assertTrue(self.player_page.locator('#correctAnswerState').is_hidden())
        self.assertEqual(self.player_page.locator('#questionText').text_content(), '')
        self.host_page.click('#sendPreparedQuestionBtn')
        self._wait_for_frame('player', 'received', 'question_started')
        self.player_page.wait_for_function(
            "() => document.querySelector('#questionText')?.textContent.includes('Third browser live question?')",
            timeout=self.TIMEOUT,
        )
        self.host_page.wait_for_selector('#revealQuestionContentBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#revealQuestionContentBtn')
        self._wait_for_frame('player', 'received', 'question_content_revealed')
        self.player_page.wait_for_function(
            """() => {
                const buttons = Array.from(document.querySelectorAll('#answerOptions .answer-option'));
                return buttons.length === 4 && buttons.every(button => !button.disabled);
            }""",
            timeout=self.TIMEOUT,
        )
        self.player_page.locator('.answer-option[data-value="C"]').evaluate(
            'element => element.click()'
        )
        self.player_page.wait_for_selector('#submitAnswerBtn:not([disabled])', timeout=self.TIMEOUT)
        self.player_page.click('#submitAnswerBtn')
        self._wait_for_frame('player', 'received', 'answer_submitted')
        self.host_page.click('#endQuestionBtn')
        self._wait_for_frame('host', 'received', 'question_ended')
        self._wait_for_frame('player', 'received', 'question_ended')

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

    def test_question_typewriter_is_visible_for_player_and_spectator_and_resumes_after_reload(self):
        question_text = 'Welcher Fluss fließt durch die Städte Mainz, Koblenz und Köln?\nÄ, ö, ü, ß, é – „Test“'
        self.question.question_text = question_text
        self.question.save(update_fields=['question_text', 'updated_at'])
        self.quiz.started_at = self.session.started_at
        self.quiz.save(update_fields=['started_at', 'updated_at'])
        dashboard_settings = DashboardSettings.load()
        dashboard_settings.question_reveal_ms_per_character = 75
        dashboard_settings.save(update_fields=['question_reveal_ms_per_character'])

        spectator_context = self._browser.new_context()
        install_browser_test_stubs(spectator_context)
        spectator_page = spectator_context.new_page()
        spectator_page.on(
            'pageerror',
            lambda exc: self.browser_errors.append(f'spectator pageerror: {exc}'),
        )
        try:
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
            spectator_url = (
                f'{self.live_server_url}'
                f'{reverse("games_hub:spectate_session", args=[self.session.code])}'
            )
            self.host_page.goto(monitor_url)
            self.player_page.goto(play_url)
            spectator_page.goto(spectator_url)
            self._wait_for_ws_url('host', f'/ws/quiz/{self.quiz.room_code}/')
            self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')

            self.host_page.click(
                f'.send-question-btn[data-question-id="{self.question.id}"]'
            )
            self._wait_for_frame('player', 'received', 'question_prepared')
            self.assertEqual(self.player_page.locator('#questionText').text_content(), '')
            self.host_page.click('#sendPreparedQuestionBtn')
            self._wait_for_frame('player', 'received', 'question_started')

            self.player_page.wait_for_function(
                """length => {
                    const text = document.querySelector('#questionText')?.textContent || '';
                    return text.length > 0 && text.length < length;
                }""",
                arg=len(question_text),
                timeout=self.TIMEOUT,
            )
            first_player_text = self.player_page.locator('#questionText').text_content()
            self.player_page.wait_for_timeout(250)
            second_player_text = self.player_page.locator('#questionText').text_content()
            self.assertGreater(len(second_player_text), len(first_player_text))
            self.assertTrue(question_text.startswith(second_player_text))
            self.assertTrue(self.host_page.locator('#revealQuestionContentBtn').is_disabled())

            spectator_page.reload()
            spectator_state = spectator_page.evaluate(
                """async url => (await fetch(url, {cache: 'no-store'})).json()""",
                reverse('games_hub:spectate_session_state', args=[self.session.code]),
            )
            self.assertEqual(spectator_state['game']['question']['text'], question_text)
            spectator_page.wait_for_timeout(500)
            self.assertIn(
                'data-quick-quiz-typewriter',
                spectator_page.locator('#stage').inner_html(),
                spectator_state,
            )
            spectator_page.wait_for_selector(
                '[data-quick-quiz-typewriter]', state='attached', timeout=self.TIMEOUT,
            )
            self.assertNotEqual(
                spectator_page.locator('[data-quick-quiz-typewriter]').text_content(),
                question_text,
            )
            spectator_page.wait_for_function(
                """length => {
                    const text = document.querySelector('[data-quick-quiz-typewriter]')?.textContent || '';
                    return text.length > 0 && text.length < length;
                }""",
                arg=len(question_text),
                timeout=self.TIMEOUT,
            )
            spectator_partial = spectator_page.locator(
                '[data-quick-quiz-typewriter]'
            ).text_content()
            self.assertTrue(question_text.startswith(spectator_partial))

            before_reload_length = len(second_player_text)
            self.player_page.reload()
            self.player_page.wait_for_selector('#questionState:not(.d-none)', timeout=self.TIMEOUT)
            self.player_page.wait_for_function(
                """minimum => (document.querySelector('#questionText')?.textContent || '').length >= minimum""",
                arg=before_reload_length,
                timeout=self.TIMEOUT,
            )
            self.assertGreaterEqual(
                len(self.player_page.locator('#questionText').text_content()),
                before_reload_length,
            )

            self.player_page.wait_for_function(
                "text => document.querySelector('#questionText')?.textContent === text",
                arg=question_text,
                timeout=self.TIMEOUT,
            )
            spectator_page.wait_for_function(
                "text => document.querySelector('[data-quick-quiz-typewriter]')?.textContent === text",
                arg=question_text,
                timeout=self.TIMEOUT,
            )
            self.host_page.wait_for_selector(
                '#revealQuestionContentBtn:not([disabled])', timeout=self.TIMEOUT,
            )
            snapshot = current_snapshot('quiz', self.quiz.room_code, self.session.code)
            self.assertEqual(snapshot['question_reveal_ms_per_character'], 75)
            self.assertEqual(
                snapshot['question_presentation_duration_ms'],
                len(question_text) * 75,
            )
            self._assert_no_browser_errors()
        finally:
            spectator_page.close()
            spectator_context.close()

    def test_vhs_typewriter_keeps_first_word_node_and_font_stable(self):
        question_text = 'Was ist die Hauptstadt von Deutschland?'
        self.question.question_text = question_text
        self.question.save(update_fields=['question_text', 'updated_at'])
        dashboard_settings = DashboardSettings.load()
        dashboard_settings.question_reveal_ms_per_character = 150
        dashboard_settings.save(update_fields=['question_reveal_ms_per_character'])
        self.player_context.add_init_script(
            "localStorage.setItem('participant_interface_theme', 'vhs');"
        )
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
        self.player_page.goto(play_url)
        self._wait_for_ws_url('host', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')
        self.host_page.click(
            f'.send-question-btn[data-question-id="{self.question.id}"]'
        )
        self._wait_for_frame('player', 'received', 'question_prepared')
        self.player_page.evaluate(
            """() => {
                window.__quickTypewriterLeadSequence = [];
                const question = document.querySelector('#questionText');
                new MutationObserver(() => {
                    const value = question.querySelector('.vhs-question-lead')?.textContent || '';
                    const sequence = window.__quickTypewriterLeadSequence;
                    if (value && sequence[sequence.length - 1] !== value) sequence.push(value);
                }).observe(question, { childList: true, characterData: true, subtree: true });
            }"""
        )
        self.host_page.click('#sendPreparedQuestionBtn')
        self._wait_for_frame('player', 'received', 'question_started')

        self.player_page.wait_for_function(
            "() => document.querySelector('.vhs-question-lead')?.textContent.length > 0",
            timeout=self.TIMEOUT,
        )
        self.assertEqual(
            self.player_page.locator('.question-typewriter-cursor').evaluate(
                "element => element.parentElement.className"
            ),
            'vhs-question-lead',
        )
        first_word_state = self.player_page.locator('.vhs-question-lead').evaluate(
            "element => { element.dataset.typewriterIdentity = 'stable'; return { text: element.textContent, fontSize: getComputedStyle(element).fontSize }; }"
        )
        self.assertLess(len(first_word_state['text']), len('Was'))
        self.player_page.wait_for_function(
            "() => document.querySelector('.vhs-question-body')?.textContent.trim().startsWith('i')",
            timeout=self.TIMEOUT,
        )

        stable_first_word = self.player_page.locator('.vhs-question-lead')
        self.assertEqual(stable_first_word.get_attribute('data-typewriter-identity'), 'stable')
        self.assertEqual(
            stable_first_word.evaluate('element => getComputedStyle(element).fontSize'),
            first_word_state['fontSize'],
        )
        self.assertEqual(
            stable_first_word.evaluate('element => element.firstChild.nodeValue'),
            'Was',
        )
        self.assertTrue(
            self.player_page.locator('.vhs-question-body').text_content().startswith('i')
        )
        typewriter_layout = self.player_page.evaluate(
            """() => {
                const lead = document.querySelector('.vhs-question-lead');
                const body = document.querySelector('.vhs-question-body');
                const cursor = document.querySelector('.question-typewriter-cursor');
                const textNode = body.firstChild;
                const range = document.createRange();
                range.setStart(textNode, textNode.length);
                range.collapse(true);
                const textEnd = range.getBoundingClientRect();
                const cursorBox = cursor.getBoundingClientRect();
                return {
                    cursorParent: cursor.parentElement.className,
                    cursorDistance: Math.abs(cursorBox.left - textEnd.right),
                    cursorTopDistance: Math.abs(cursorBox.top - textEnd.top),
                    firstWordGap: body.getBoundingClientRect().top - lead.getBoundingClientRect().bottom,
                };
            }"""
        )
        self.assertEqual(typewriter_layout['cursorParent'], 'vhs-question-body')
        self.assertLessEqual(typewriter_layout['cursorDistance'], 6)
        self.assertLessEqual(typewriter_layout['cursorTopDistance'], 6)
        self.assertGreaterEqual(typewriter_layout['firstWordGap'], 0)
        self.assertLessEqual(typewriter_layout['firstWordGap'], 14)
        lead_sequence = self.player_page.evaluate('window.__quickTypewriterLeadSequence')
        self.assertEqual(lead_sequence[:3], ['W', 'Wa', 'Was'])
        self.assertEqual(
            self.player_page.evaluate(
                """() => ({
                    unicode: window.QuestionTypewriter.splitFirstWord('Über welche Größe verfügt Österreich?'),
                    oneWord: window.QuestionTypewriter.splitFirstWord('Wann?'),
                    punctuation: window.QuestionTypewriter.splitFirstWord('Warum, genau genommen?'),
                })"""
            ),
            {
                'unicode': {'firstWord': 'Über', 'separator': ' ', 'rest': 'welche Größe verfügt Österreich?'},
                'oneWord': {'firstWord': 'Wann?', 'separator': '', 'rest': ''},
                'punctuation': {'firstWord': 'Warum,', 'separator': ' ', 'rest': 'genau genommen?'},
            },
        )
        self.player_page.wait_for_function(
            "text => document.querySelector('#questionText')?.textContent === text",
            arg=question_text,
            timeout=self.TIMEOUT,
        )
        self.assertEqual(
            self.player_page.locator('.question-typewriter-cursor').count(),
            0,
        )
        self._assert_no_browser_errors()

    def test_question_typewriter_uses_fast_global_dashboard_speed(self):
        question_text = 'Wie heißt Rom?'
        self.question.question_text = question_text
        self.question.save(update_fields=['question_text', 'updated_at'])
        dashboard_settings = DashboardSettings.load()
        dashboard_settings.question_reveal_ms_per_character = 35
        dashboard_settings.save(update_fields=['question_reveal_ms_per_character'])
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
        self.player_page.goto(play_url)
        self._wait_for_ws_url('host', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')
        self.host_page.click(
            f'.send-question-btn[data-question-id="{self.question.id}"]'
        )
        self._wait_for_frame('player', 'received', 'question_prepared')
        started = time.monotonic()
        self.host_page.click('#sendPreparedQuestionBtn')
        self._wait_for_frame('player', 'received', 'question_started')
        self.player_page.wait_for_function(
            "text => document.querySelector('#questionText')?.textContent === text",
            arg=question_text,
            timeout=self.TIMEOUT,
        )

        self.assertLess(time.monotonic() - started, 2)
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.session.code)
        self.assertEqual(snapshot['question_reveal_ms_per_character'], 35)
        self.assertEqual(snapshot['question_presentation_duration_ms'], len(question_text) * 35)
        self._assert_no_browser_errors()

    def test_four_answers_remain_selectable_with_stale_timer_context_and_fast_release(self):
        HubParticipant.objects.create(session=self.session, nickname='Bob')
        second_participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )
        second_context = self._browser.new_context()
        install_browser_test_stubs(second_context)
        second_page = second_context.new_page()
        self.websocket_urls['player2'] = []
        self.websocket_frames['player2'] = {'sent': [], 'received': []}
        self._instrument_page('player2', second_page)

        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:quiz_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )
        player_url = (
            f'{self.live_server_url}'
            f'{reverse("quiz:play", args=[self.quiz.room_code, self.participant.name])}'
            f'?hub_session={self.session.code}'
        )
        second_player_url = (
            f'{self.live_server_url}'
            f'{reverse("quiz:play", args=[self.quiz.room_code, second_participant.name])}'
            f'?hub_session={self.session.code}'
        )

        try:
            self.host_page.goto(monitor_url)
            self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
            self.player_page.goto(player_url)
            second_page.goto(second_player_url)
            for label in ('host', 'player', 'player2'):
                self._wait_for_ws_url(label, f'/ws/quiz/{self.quiz.room_code}/')

            # Reproduce the participant-local collision that previously reused the
            # anonymous timer bucket from an already expired question.
            self.player_page.evaluate(
                """() => {
                    const now = new Date();
                    const expired = new Date(now.getTime() - 1000);
                    return window.AuthoritativeGameState.remainingMilliseconds({
                        ends_at: expired.toISOString(),
                        server_now: now.toISOString(),
                    });
                }"""
            )

            self.host_page.click('.send-question-btn')
            self.host_page.wait_for_selector('#sendPreparedQuestionBtn', timeout=self.TIMEOUT)
            self.host_page.click('#sendPreparedQuestionBtn')
            for label in ('player', 'player2'):
                self._wait_for_frame(label, 'received', 'question_started')
            self.host_page.wait_for_selector('#revealQuestionContentBtn:not([disabled])', timeout=self.TIMEOUT)
            self.host_page.click('#revealQuestionContentBtn')
            for label in ('player', 'player2'):
                self._wait_for_frame(label, 'received', 'question_content_revealed')

            self.assertEqual(self.host_page.locator('#openAnsweringBtn').count(), 0)

            for page in (self.player_page, second_page):
                page.wait_for_function(
                    """() => {
                        const buttons = Array.from(document.querySelectorAll('#answerOptions .answer-option'));
                        return buttons.length === 4 && buttons.every(button => (
                            button.classList.contains('is-content-visible') && !button.disabled
                        ));
                    }""",
                    timeout=self.TIMEOUT,
                )
                diagnostics = page.locator('#answerOptions .answer-option').evaluate_all(
                    """buttons => buttons.map(button => {
                        const rect = button.getBoundingClientRect();
                        const topElement = document.elementFromPoint(
                            rect.left + (rect.width / 2),
                            rect.top + (rect.height / 2)
                        );
                        return {
                            disabled: button.disabled,
                            matchesDisabled: button.matches(':disabled'),
                            ariaDisabled: button.getAttribute('aria-disabled'),
                            inertAncestor: Boolean(button.closest('[inert]')),
                            pointerEvents: getComputedStyle(button).pointerEvents,
                            topElementIsButton: topElement === button || button.contains(topElement),
                        };
                    })"""
                )
                self.assertEqual(len(diagnostics), 4)
                for state in diagnostics:
                    self.assertFalse(state['disabled'])
                    self.assertFalse(state['matchesDisabled'])
                    self.assertEqual(state['ariaDisabled'], 'false')
                    self.assertFalse(state['inertAncestor'])
                    self.assertNotEqual(state['pointerEvents'], 'none')
                    self.assertTrue(state['topElementIsButton'])

            first_player_answers = self.player_page.locator('#answerOptions .answer-option')
            for index in range(4):
                first_player_answers.nth(index).click()
                self.assertEqual(
                    first_player_answers.nth(index).get_attribute('aria-pressed'),
                    'true',
                )
            self.player_page.click('#submitAnswerBtn')
            second_page.locator('#answerOptions .answer-option').first.click()
            second_page.click('#submitAnswerBtn')
            for label in ('player', 'player2'):
                self._wait_for_frame(label, 'received', 'answer_submitted')

            self.assertEqual(
                QuizAnswer.objects.filter(quiz=self.quiz, question=self.question).count(),
                2,
            )
            self.assertEqual(
                set(QuizAnswer.objects.filter(
                    quiz=self.quiz,
                    question=self.question,
                ).values_list('answer_text', flat=True)),
                {'A', 'D'},
            )
            self._assert_no_browser_errors()
        finally:
            second_page.close()
            second_context.close()

    def test_short_answer_prepared_shell_shows_game_meta_and_skips_answer_reveal(self):
        self.question.question_type = 'short_answer'
        self.question.correct_answer = 'Berlin'
        self.question.option_a = ''
        self.question.option_b = ''
        self.question.save(update_fields=[
            'question_type', 'correct_answer', 'option_a', 'option_b', 'updated_at',
        ])
        self.player_context.add_init_script(
            "localStorage.setItem('participant_interface_theme', 'vhs');"
        )
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
        self.player_page.goto(play_url)
        self._wait_for_ws_url('host', f'/ws/quiz/{self.quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/quiz/{self.quiz.room_code}/')
        self.host_page.click(
            f'.send-question-btn[data-question-id="{self.question.id}"]'
        )
        self._wait_for_frame('player', 'received', 'question_prepared')

        self.player_page.wait_for_selector(
            '#questionState:not(.d-none) .vhs-question-kicker',
            timeout=self.TIMEOUT,
        )
        prepared_meta = self.player_page.locator(
            '#questionState .vhs-question-kicker'
        ).inner_text()
        self.assertIn('QUICK BROWSER QUIZ', prepared_meta)
        self.assertIn('SPIEL 1', prepared_meta)
        self.assertEqual(self.player_page.locator('#questionText').inner_text(), '')
        self.assertTrue(self.player_page.locator('#quickQuizResponseArea').is_hidden())
        self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())

        self.player_page.reload()
        self.player_page.wait_for_selector(
            '#questionState:not(.d-none) .vhs-question-kicker',
            timeout=self.TIMEOUT,
        )
        self.assertIn(
            'QUICK BROWSER QUIZ',
            self.player_page.locator('#questionState .vhs-question-kicker').inner_text(),
        )
        self.assertEqual(self.player_page.locator('#questionText').inner_text(), '')

        self.websocket_frames['host']['received'].clear()
        self.websocket_frames['player']['received'].clear()
        self.host_page.click('#sendPreparedQuestionBtn')
        self._wait_for_frame('host', 'received', 'question_started')
        self._wait_for_frame('player', 'received', 'question_started')
        self.host_page.wait_for_selector(
            '#openAnsweringBtn:not([disabled])', timeout=self.TIMEOUT,
        )
        self.assertEqual(self.host_page.locator('#revealQuestionContentBtn').count(), 0)
        self.assertEqual(self.player_page.locator('#shortAnswerInput1').count(), 0)
        self.assertTrue(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())

        self.host_page.click('#openAnsweringBtn')
        self._wait_for_frame('player', 'received', 'question_answering_opened')
        self.player_page.wait_for_selector('#shortAnswerInput1', timeout=self.TIMEOUT)
        self.assertFalse(self.player_page.locator('#shortAnswerInput1').is_disabled())
        self.assertFalse(self.player_page.locator('#playerQuestionTimerWrapper').is_hidden())
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
        self.host_page.wait_for_selector('#sendPreparedQuestionBtn', timeout=self.TIMEOUT)
        self.host_page.click('#sendPreparedQuestionBtn')
        self._wait_for_frame('host', 'received', 'question_started')
        self._wait_for_frame('player', 'received', 'question_started')
        self._open_answering_from_host()
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
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertEqual(response.context['total_question_count'], 3)
        self.assertContains(response, 'class="quiz-score-box score-box"')
        self.assertContains(response, 'id="quizScoreTotal"')
        self.assertContains(response, 'id="quizScoreTotal">1/3</div>')
        self.assertContains(
            response,
            '<span id="vhsQuizProgress" hidden aria-hidden="true">2/3</span>',
            html=True,
        )

    def test_quiz_play_hides_invalid_progress_when_no_questions_are_configured(self):
        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_question_count'], 0)
        self.assertContains(
            response,
            '<span id="vhsQuizProgress" hidden aria-hidden="true"></span>',
            html=True,
        )

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
                'display_answer': answer.answer_text,
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
