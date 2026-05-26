import json
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .consumers import QuizConsumer
from .models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion, QuizSession


User = get_user_model()


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
        self.assertEqual(question.correct_answer_2, 'Lovelace')
        self.assertEqual(question.answer_label_1, 'Vorname')
        self.assertEqual(question.answer_label_2, 'Nachname')

    def test_admin_update_question_supports_four_short_answer_fields(self):
        question = QuizQuestion.objects.create(
            question_text='Before update',
            question_type='short_answer',
            correct_answer='A',
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

    def test_create_game_page_renders_short_answer_builder_ui(self):
        response = self.client.get(reverse('admin_dashboard:create_game'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Weiteres Textfeld hinzufügen')
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

    def test_participant_rejoin_during_active_tutorial_receives_tutorial_start(self):
        participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.tutorial_active = True
        self.quiz.save(update_fields=['status', 'tutorial_active'])

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })

        message_types = [message['type'] for message in self.direct_messages]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)

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
                {'id': question_one.id, 'number': 1, 'is_correct': True, 'status': 'played'},
                {'id': question_two.id, 'number': 2, 'is_correct': None, 'status': 'current'},
                {'id': question_three.id, 'number': 3, 'is_correct': None, 'status': 'upcoming'},
            ],
        )
        self.assertEqual(response.context['score_total_correct'], 1)
        self.assertEqual(response.context['score_total_questions'], 3)
        self.assertContains(response, 'class="quiz-score-box score-box"')
        self.assertContains(response, 'id="quizScoreTotal"')
        self.assertContains(response, 'id="quizScoreTotal">1/3</div>')

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
                {'id': question_three.id, 'number': 1, 'is_correct': None, 'status': 'current'},
                {'id': question_one.id, 'number': 2, 'is_correct': None, 'status': 'upcoming'},
                {'id': question_two.id, 'number': 3, 'is_correct': None, 'status': 'upcoming'},
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
                {'id': question_three.id, 'number': 1, 'is_correct': True, 'status': 'played'},
                {'id': question_two.id, 'number': 2, 'is_correct': None, 'status': 'current'},
                {'id': question_one.id, 'number': 3, 'is_correct': None, 'status': 'upcoming'},
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
        self.assertContains(response, 'this.storePendingProgressEvaluation(data.is_correct, data.question_id);')
        self.assertContains(response, 'this.applyPendingProgressEvaluation(endedQuestionId);')
        self.assertContains(response, 'this.applyPendingProgressEvaluationIfEnded(data.question_id);')
        self.assertContains(response, "case 'answer_corrected':")
        self.assertContains(response, 'this.applyManualAnswerCorrection(data);')
        self.assertContains(response, 'this.pushProgressEntry(!!data.is_correct, data.question_id);')
        self.assertContains(response, 'if (!awaitingFinalizedAnswer) {')
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
            [{'id': question.id, 'number': 1, 'is_correct': None, 'status': 'current'}],
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
            'answer': 'True',
            'time_taken': 2.5,
        })

        answer_message = next(message for message in direct_messages if message['type'] == 'answer_submitted')
        self.assertEqual(answer_message['question_id'], question.id)
        self.assertTrue(answer_message['is_correct'])


class QuizHostManualCorrectTests(TestCase):
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
            answer_text='Bärlin',
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
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)
        self.assertFalse(payload['can_mark_correct'])
        self.assertEqual(
            [(field['key'], field['is_correct']) for field in payload['field_results']],
            [('answer_1', True), ('answer_2', True)],
        )

    @patch('admin_dashboard.views.get_channel_layer')
    def test_host_promote_broadcasts_participant_scorebox_update(self, channel_layer_mock):
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
        self.assertNotContains(response, '<option value="double_answer">', html=False)
