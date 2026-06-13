import json
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.tutorial_runtime import activate_tutorial_runtime as activate_game_tutorial_runtime
from games_hub.unit_tutorial_runtime import get_unit_tutorial_state, prepare_unit_tutorial_runtime

from .consumers import BlackJackConsumer
from .models import BlackJackAnswer, BlackJackParticipant, BlackJackQuestion, BlackJackQuiz, BlackJackSession


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group_name, message):
        self.group_messages.append((group_name, message))


class BlackJackSetLogicTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='blackjack_admin',
            password='testpass123',
            email='',
        )
        self.quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Set Logic Quiz',
        )
        self.participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
        )

    def _question(self, correct_answer):
        return BlackJackQuestion.objects.create(
            question_text=f'Question {correct_answer}',
            correct_answer=correct_answer,
            created_by=self.user,
        )

    def _answer(self, question, user_answer, question_number):
        self.quiz.current_question_number = question_number
        self.quiz.save(update_fields=['current_question_number'])
        return BlackJackAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            user_answer=user_answer,
            question_number=question_number,
        )

    def test_question_points_are_absolute_difference(self):
        question = self._question(correct_answer=42)

        self.assertEqual(question.calculate_points(39), 3)
        self.assertEqual(question.calculate_points(45), 3)
        self.assertEqual(question.calculate_points(42), 0)

    def test_simple_mode_points_match_running_valid_set_stars(self):
        first = self._question(correct_answer=10)
        second = self._question(correct_answer=20)
        third = self._question(correct_answer=30)

        self._answer(first, 7, 1)    # 3 stars
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_points, 3)
        self.assertFalse(self.participant.is_busted)

        self._answer(second, 16, 2)  # 4 stars
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_points, 7)
        self.assertFalse(self.participant.is_busted)

        self._answer(third, 25, 3)   # 5 stars
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_points, 12)
        self.assertFalse(self.participant.is_busted)

    def test_exactly_twenty_one_stars_stays_valid(self):
        first = self._question(correct_answer=10)
        second = self._question(correct_answer=20)

        self._answer(first, 0, 1)   # 10 stars
        self._answer(second, 9, 2)  # 11 stars

        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_points, 21)
        self.assertFalse(self.participant.is_busted)
        self.assertEqual(self.participant.final_score, 0)
        self.assertEqual(self.participant.get_status(), 'blackjack')

    def test_more_than_twenty_one_stars_counts_as_zero_for_the_set(self):
        first = self._question(correct_answer=10)
        second = self._question(correct_answer=20)

        self._answer(first, 0, 1)   # 10 stars
        self._answer(second, 8, 2)  # 12 stars => 22 total raw

        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_points, 0)
        self.assertTrue(self.participant.is_busted)
        self.assertEqual(self.participant.final_score, 22)
        self.assertEqual(self.participant.get_distance_from_21(), 1)

    def test_new_quiz_starts_with_clean_set_points(self):
        first = self._question(correct_answer=10)
        second = self._question(correct_answer=20)

        self._answer(first, 0, 1)
        self._answer(second, 8, 2)

        next_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Set Quiz',
        )
        next_participant = BlackJackParticipant.objects.create(
            quiz=next_quiz,
            name='Alice',
        )

        self.assertEqual(next_participant.total_points, 0)
        self.assertFalse(next_participant.is_busted)
        self.assertEqual(next_participant.questions_answered, 0)


class BlackJackTotalQuestionsConfigTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='blackjack_config_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)

    def _question(self, correct_answer):
        return BlackJackQuestion.objects.create(
            question_text=f'Question {correct_answer}',
            correct_answer=correct_answer,
            created_by=self.user,
        )

    def test_custom_quiz_creation_persists_total_questions(self):
        response = self.client.post(
            reverse('admin_dashboard:create_black_jack_custom_quiz'),
            data=json.dumps({
                'title': 'Configurable Quiz',
                'question_ids': [],
                'total_questions': 7,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        quiz = BlackJackQuiz.objects.get(title='Configurable Quiz')
        self.assertEqual(quiz.total_questions, 7)

    def test_custom_quiz_update_persists_total_questions(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Editable Quiz',
            total_questions=5,
        )

        response = self.client.post(
            reverse('admin_dashboard:update_black_jack_custom_quiz'),
            data=json.dumps({
                'quiz_id': quiz.id,
                'title': 'Editable Quiz',
                'question_ids': [],
                'total_questions': 9,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        quiz.refresh_from_db()
        self.assertEqual(quiz.total_questions, 9)

    def test_management_page_renders_total_questions_input(self):
        response = self.client.get(reverse('admin_dashboard:blackjack_management'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'customQuizTotalQuestions')
        self.assertContains(response, 'Questions per Set')

    def test_management_page_renders_set_sizes_input(self):
        response = self.client.get(reverse('admin_dashboard:blackjack_management'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'customQuizSetSizes')
        self.assertContains(response, 'Set Sizes')

    def test_management_page_renders_explicit_set_builder(self):
        response = self.client.get(reverse('admin_dashboard:blackjack_management'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'addBlackjackSetBtn')
        self.assertContains(response, 'blackjackSetSummary')
        self.assertContains(response, 'Create explicit sets and assign each selected question to a concrete set.')
        self.assertContains(response, 'question-set-select')
        self.assertContains(response, 'als Tutorialset verwenden')
        self.assertNotContains(response, 'als Tutorial verwenden')
        self.assertContains(response, 'tutorial_set_number')
        self.assertNotContains(response, 'tutorial_question_id')

    def test_active_manage_games_flow_renders_blackjack_set_builder(self):
        response = self.client.get(reverse('admin_dashboard:create_game'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'blackjackSetBuilderCard')
        self.assertContains(response, 'addManageBlackjackSetBtn')
        self.assertContains(response, 'manageBlackjackSetSummary')
        self.assertContains(response, 'manage-blackjack-set-select')
        self.assertContains(response, 'payload.question_sets = questionSets')
        self.assertContains(response, 'payload.set_sizes = questionSets.map(questionSet => questionSet.length).join')
        self.assertContains(response, 'payload.tutorial_set_number = tutorialSetNumber')

    def test_custom_quiz_creation_persists_explicit_question_sets(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]

        response = self.client.post(
            reverse('admin_dashboard:create_black_jack_custom_quiz'),
            data=json.dumps({
                'title': 'Explicit Sets Quiz',
                'question_ids': [question.id for question in questions],
                'total_questions': 5,
                'set_sizes': '2,1',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        quiz = BlackJackQuiz.objects.get(title='Explicit Sets Quiz')
        self.assertEqual(quiz.question_order, [[questions[0].id, questions[1].id], [questions[2].id]])

    def test_custom_quiz_creation_persists_tutorial_set_number(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]

        response = self.client.post(
            reverse('admin_dashboard:create_black_jack_custom_quiz'),
            data=json.dumps({
                'title': 'Tutorial Set Quiz',
                'question_ids': [question.id for question in questions],
                'total_questions': 5,
                'set_sizes': '1,2',
                'tutorial_set_number': 2,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        quiz = BlackJackQuiz.objects.get(title='Tutorial Set Quiz')
        self.assertEqual(quiz.tutorial_set_number, 2)
        self.assertEqual(quiz.get_tutorial_set_question_ids(active_only=False), [questions[1].id, questions[2].id])

    def test_selected_questions_endpoint_returns_explicit_set_metadata(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Editable Explicit Sets',
            total_questions=5,
            question_order=[[questions[0].id], [questions[1].id, questions[2].id]],
        )
        quiz.selected_questions.set(questions)

        response = self.client.get(
            reverse('admin_dashboard:get_blackjack_selected_questions', args=[quiz.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['question_sets'], [[questions[0].id], [questions[1].id, questions[2].id]])
        self.assertEqual(payload['set_sizes'], '1,2')

    def test_selected_questions_endpoint_returns_tutorial_set_not_question_flags(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Editable Tutorial Set',
            total_questions=5,
            question_order=[[questions[0].id], [questions[1].id, questions[2].id]],
            tutorial_set_number=2,
        )
        quiz.selected_questions.set(questions)

        response = self.client.get(
            reverse('admin_dashboard:get_blackjack_selected_questions', args=[quiz.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['tutorial_set_number'], 2)
        self.assertNotIn('tutorial_question_id', payload)
        self.assertTrue(all('is_tutorial' not in row for row in payload['questions']))

    def test_update_custom_quiz_persists_explicit_question_sets(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Editable Explicit Sets',
            total_questions=5,
            question_order=[[questions[0].id, questions[1].id], [questions[2].id]],
        )
        quiz.selected_questions.set(questions)

        response = self.client.post(
            reverse('admin_dashboard:update_black_jack_custom_quiz'),
            data=json.dumps({
                'quiz_id': quiz.id,
                'title': 'Editable Explicit Sets',
                'question_ids': [questions[0].id, questions[1].id, questions[2].id],
                'total_questions': 5,
                'set_sizes': '1,2',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        quiz.refresh_from_db()
        self.assertEqual(quiz.question_order, [[questions[0].id], [questions[1].id, questions[2].id]])

    def test_update_custom_quiz_persists_tutorial_set_number(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Editable Tutorial Set',
            total_questions=5,
            question_order=[[questions[0].id], [questions[1].id, questions[2].id]],
        )
        quiz.selected_questions.set(questions)

        response = self.client.post(
            reverse('admin_dashboard:update_black_jack_custom_quiz'),
            data=json.dumps({
                'quiz_id': quiz.id,
                'title': 'Editable Tutorial Set',
                'question_ids': [questions[0].id, questions[1].id, questions[2].id],
                'total_questions': 5,
                'set_sizes': '1,2',
                'tutorial_set_number': 2,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        quiz.refresh_from_db()
        self.assertEqual(quiz.tutorial_set_number, 2)

    def test_custom_quiz_rejects_multiple_tutorial_sets(self):
        questions = [self._question(answer) for answer in (10, 20)]

        response = self.client.post(
            reverse('admin_dashboard:create_black_jack_custom_quiz'),
            data=json.dumps({
                'title': 'Invalid Tutorial Sets',
                'question_ids': [question.id for question in questions],
                'total_questions': 5,
                'set_sizes': '1,1',
                'tutorial_set_numbers': [1, 2],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json().get('success'))

    def test_custom_quiz_rejects_tutorial_set_outside_configured_sets(self):
        questions = [self._question(answer) for answer in (10, 20)]

        response = self.client.post(
            reverse('admin_dashboard:create_black_jack_custom_quiz'),
            data=json.dumps({
                'title': 'Invalid Tutorial Set',
                'question_ids': [question.id for question in questions],
                'total_questions': 5,
                'set_sizes': '1,1',
                'tutorial_set_number': 3,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json().get('success'))

    def test_selected_questions_endpoint_keeps_legacy_flat_quiz_loadable(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Legacy Flat Quiz',
            total_questions=5,
            question_order=[question.id for question in questions],
        )
        quiz.selected_questions.set(questions)

        response = self.client.get(
            reverse('admin_dashboard:get_blackjack_selected_questions', args=[quiz.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['question_sets'], [[question.id for question in questions]])
        self.assertEqual(payload['set_sizes'], '3')

    def test_active_edit_flow_loads_explicit_blackjack_sets(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Editable Explicit Sets',
            total_questions=5,
            question_order=[[questions[0].id, questions[1].id], [questions[2].id]],
        )
        quiz.selected_questions.set(questions)

        response = self.client.get(
            reverse('admin_dashboard:edit_game', args=['blackjack', quiz.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'blackjackSetBuilderCard')
        self.assertContains(response, 'loadManageBlackJackQuestionSets(data.question_sets || [], items.map(item => item.id));')

    def test_active_edit_flow_keeps_legacy_flat_blackjack_quiz_editable(self):
        questions = [self._question(answer) for answer in (10, 20, 30)]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Legacy Flat Quiz',
            total_questions=5,
            question_order=[question.id for question in questions],
        )
        quiz.selected_questions.set(questions)

        response = self.client.get(
            reverse('admin_dashboard:edit_game', args=['blackjack', quiz.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'blackjackSetBuilderCard')
        self.assertContains(response, 'loadManageBlackJackQuestionSets(data.question_sets || [], items.map(item => item.id));')

    def test_session_ends_after_configured_total_questions(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Two Question Quiz',
            total_questions=2,
        )
        session = BlackJackSession.objects.create(quiz=quiz)
        first = self._question(10)
        second = self._question(20)

        session.send_question(first)
        session.end_current_question()
        quiz.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(quiz.status, 'waiting')
        self.assertEqual(session.current_question_number, 1)

        session.send_question(second)
        session.end_current_question()
        quiz.refresh_from_db()

        self.assertEqual(quiz.status, 'completed')

    def test_submit_answer_questions_remaining_uses_configured_total_questions(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Seven Question Quiz',
            status='active',
            total_questions=7,
            current_question_number=1,
        )
        question = self._question(42)
        quiz.current_question = question
        quiz.save(update_fields=['current_question', 'status', 'total_questions', 'current_question_number'])
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
        )

        response = self.client.post(
            reverse('black_jack_quiz:submit_answer', args=[quiz.room_code, participant.name]),
            data=json.dumps({'user_answer': 40, 'time_taken': 1.2}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['questions_remaining'], 6)


class BlackJackScoringModeConfigTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='blackjack_mode_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)

    def _question(self, correct_answer=10):
        return BlackJackQuestion.objects.create(
            question_text=f'Question {correct_answer}',
            correct_answer=correct_answer,
            created_by=self.user,
        )

    def test_quiz_default_scoring_mode_is_simple(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Default Mode Quiz',
        )

        self.assertEqual(quiz.scoring_mode, 'simple')

    def test_custom_quiz_creation_persists_scoring_mode(self):
        response = self.client.post(
            reverse('admin_dashboard:create_black_jack_custom_quiz'),
            data=json.dumps({
                'title': 'Ranking Quiz',
                'question_ids': [],
                'scoring_mode': 'rank',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        quiz = BlackJackQuiz.objects.get(title='Ranking Quiz')
        self.assertEqual(quiz.scoring_mode, 'rank')

    def test_create_game_page_renders_blackjack_scoring_mode_input(self):
        response = self.client.get(reverse('admin_dashboard:create_game'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'blackjackScoringMode')
        self.assertContains(response, 'Bewertungsmodus')

    def test_blackjack_management_renders_scoring_mode_input(self):
        response = self.client.get(reverse('admin_dashboard:blackjack_management'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'customQuizScoringMode')
        self.assertContains(response, 'Scoring Mode')

    def test_selected_questions_endpoint_returns_scoring_mode(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Editable Ranking Quiz',
            scoring_mode='rank',
            total_questions=6,
        )

        response = self.client.get(
            reverse('admin_dashboard:get_blackjack_selected_questions', args=[quiz.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['scoring_mode'], 'rank')
        self.assertEqual(payload['total_questions'], 6)

    def test_session_scoring_mode_can_change_before_first_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Monitor Quiz',
            scoring_mode='simple',
        )
        BlackJackSession.objects.create(quiz=quiz)

        response = self.client.post(
            reverse('admin_dashboard:set_blackjack_scoring_mode', args=[quiz.room_code]),
            data=json.dumps({'scoring_mode': 'rank'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        quiz.refresh_from_db()
        self.assertEqual(quiz.scoring_mode, 'rank')

    def test_monitor_disables_scoring_mode_after_first_question_sent(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Monitor Copy Quiz',
            scoring_mode='simple',
        )
        session = BlackJackSession.objects.create(quiz=quiz)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="blackjackScoringModeSelect"')
        self.assertNotContains(response, 'id="blackjackScoringModeSelect" disabled')

        session.send_question(self._question(12))

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="blackjackScoringModeSelect" disabled')

    def test_session_scoring_mode_change_is_blocked_after_first_question_started(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Locked Mode Quiz',
            scoring_mode='simple',
        )
        session = BlackJackSession.objects.create(quiz=quiz)
        session.send_question(self._question(15))

        response = self.client.post(
            reverse('admin_dashboard:set_blackjack_scoring_mode', args=[quiz.room_code]),
            data=json.dumps({'scoring_mode': 'rank'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error'],
            'The scoring mode can only be changed before the first question is sent.'
        )
        quiz.refresh_from_db()
        self.assertEqual(quiz.scoring_mode, 'simple')

    def test_update_custom_quiz_blocks_scoring_mode_change_after_first_question_started(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Protected Quiz',
            scoring_mode='simple',
        )
        BlackJackSession.objects.create(quiz=quiz).send_question(self._question(20))

        response = self.client.post(
            reverse('admin_dashboard:update_black_jack_custom_quiz'),
            data=json.dumps({
                'quiz_id': quiz.id,
                'title': quiz.title,
                'question_ids': [],
                'scoring_mode': 'rank',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error'],
            'The scoring mode can only be changed before the first question is sent.'
        )
        quiz.refresh_from_db()
        self.assertEqual(quiz.scoring_mode, 'simple')

    def test_monitor_template_contains_immediate_first_question_lock_hook(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Monitor Hook Quiz',
            scoring_mode='simple',
        )
        BlackJackSession.objects.create(quiz=quiz)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'syncScoringModeHint()')
        self.assertContains(response, 'const scoringModeSelect = document.getElementById(\'blackjackScoringModeSelect\');')
        self.assertContains(response, 'if (scoringModeSelect) scoringModeSelect.disabled = true;')
        self.assertContains(response, 'Nach der ersten gesendeten Frage gesperrt.')
        self.assertContains(response, 'Vor der ersten gesendeten Frage')

    def test_public_participants_api_uses_simple_mode_ordering(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Simple Ordering Quiz',
            scoring_mode='simple',
        )
        lower = BlackJackParticipant.objects.create(quiz=quiz, name='Alice', total_points=7, final_score=14)
        higher = BlackJackParticipant.objects.create(quiz=quiz, name='Bob', total_points=12, final_score=9)
        lower.save(update_fields=['total_points', 'final_score'])
        higher.save(update_fields=['total_points', 'final_score'])

        response = self.client.get(
            reverse('black_jack_quiz:api_participants', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['participants'][0]['name'], 'Bob')
        self.assertEqual(payload['participants'][1]['name'], 'Alice')


class BlackJackMultiSetTransitionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='blackjack_multi_set_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)
        self.quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Multi Set Quiz',
            status='active',
            total_questions=2,
        )
        self.session = BlackJackSession.objects.create(quiz=self.quiz)
        self.participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
        )
        self.questions = [
            BlackJackQuestion.objects.create(
                question_text=f'Question {index}',
                correct_answer=correct_answer,
                created_by=self.user,
            )
            for index, correct_answer in enumerate((10, 20, 30, 40), start=1)
        ]
        self.quiz.selected_questions.set(self.questions)

    def _answer_current_question(self, user_answer):
        return BlackJackAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.quiz.current_question,
            user_answer=user_answer,
        )

    def test_first_set_resets_current_set_stars_and_preserves_overall_points(self):
        self.session.send_question(self.questions[0])
        self._answer_current_question(7)  # 3 stars
        self.session.end_current_question()

        self.session.send_question(self.questions[1])
        self._answer_current_question(16)  # 4 stars
        transition = self.session.end_current_question()

        self.participant.refresh_from_db()
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()

        self.assertTrue(transition['set_complete'])
        self.assertFalse(transition['quiz_complete'])
        self.assertEqual(self.participant.overall_points, 7)
        self.assertEqual(self.participant.total_points, 0)
        self.assertEqual(self.participant.questions_answered, 0)
        self.assertFalse(self.participant.is_busted)
        self.assertEqual(self.session.completed_sets_count, 1)
        self.assertEqual(self.quiz.status, 'active')

    def test_second_set_starts_clean_and_accumulates_overall_points(self):
        self.session.send_question(self.questions[0])
        self._answer_current_question(7)  # 3 stars
        self.session.end_current_question()

        self.session.send_question(self.questions[1])
        self._answer_current_question(16)  # 4 stars
        self.session.end_current_question()

        self.session.send_question(self.questions[2])
        self._answer_current_question(25)  # 5 stars

        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_points, 5)
        self.assertEqual(self.participant.overall_points, 7)
        self.assertEqual(self.participant.questions_answered, 1)

        self.session.end_current_question()
        self.session.send_question(self.questions[3])
        self._answer_current_question(34)  # 6 stars
        transition = self.session.end_current_question()

        self.participant.refresh_from_db()
        self.quiz.refresh_from_db()

        self.assertTrue(transition['set_complete'])
        self.assertTrue(transition['quiz_complete'])
        self.assertEqual(self.participant.overall_points, 18)
        self.assertEqual(self.participant.total_points, 18)
        self.assertEqual(self.quiz.status, 'completed')

    def test_busted_set_scores_zero_and_next_set_starts_clean(self):
        self.session.send_question(self.questions[0])
        self._answer_current_question(0)  # 10 stars
        self.session.end_current_question()

        self.session.send_question(self.questions[1])
        self._answer_current_question(8)  # 12 stars => bust
        self.session.end_current_question()

        self.participant.refresh_from_db()
        self.assertEqual(self.participant.overall_points, 0)
        self.assertEqual(self.participant.total_points, 0)
        self.assertFalse(self.participant.is_busted)

        self.session.send_question(self.questions[2])
        self._answer_current_question(28)  # 2 stars

        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_points, 2)
        self.assertEqual(self.participant.overall_points, 0)
        self.assertFalse(self.participant.is_busted)

    def test_monitor_uses_total_game_questions_for_multi_set_completion(self):
        self.quiz.current_question_number = 2
        self.quiz.current_question = None
        self.quiz.save(update_fields=['current_question_number', 'current_question'])

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/2, 1/2)')
        self.assertNotContains(response, 'All 2 questions have been asked. The quiz is finished.')

    def test_play_view_shows_question_progress_within_current_set(self):
        self.quiz.current_question_number = 3
        self.quiz.current_question = self.questions[2]
        self.quiz.save(update_fields=['current_question_number', 'current_question'])

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="questionProgress">1/2')
        self.assertContains(
            response,
            'Question <span id="currentQuestionNumber">1</span>/<span id="currentSetQuestionCount">2</span>',
            html=True,
        )

    def test_public_participants_api_uses_ranking_mode_ordering(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Ranking Ordering Quiz',
            scoring_mode='rank',
        )
        farther = BlackJackParticipant.objects.create(quiz=quiz, name='Alice', total_points=18, final_score=3)
        closer = BlackJackParticipant.objects.create(quiz=quiz, name='Bob', total_points=20, final_score=1)
        farther.save(update_fields=['total_points', 'final_score'])
        closer.save(update_fields=['total_points', 'final_score'])

        response = self.client.get(
            reverse('black_jack_quiz:api_participants', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['participants'][0]['name'], 'Bob')
        self.assertEqual(payload['participants'][1]['name'], 'Alice')


class BlackJackExplicitSetRuntimeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='blackjack_explicit_set_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)
        self.questions = [
            BlackJackQuestion.objects.create(
                question_text=f'Question {index}',
                correct_answer=index * 10,
                created_by=self.user,
            )
            for index in range(1, 4)
        ]
        self.quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Explicit Set Runtime Quiz',
            status='active',
            total_questions=5,
            question_order=[[self.questions[0].id], [self.questions[1].id, self.questions[2].id]],
        )
        self.quiz.selected_questions.set(self.questions)
        self.session = BlackJackSession.objects.create(quiz=self.quiz)
        self.participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
        )

    def _answer_current_question(self, user_answer):
        return BlackJackAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.quiz.current_question,
            user_answer=user_answer,
        )

    def test_single_question_explicit_set_finalizes_after_first_question(self):
        self.session.send_question(self.questions[0])
        self._answer_current_question(9)  # 1 star

        transition = self.session.end_current_question()
        self.participant.refresh_from_db()
        self.session.refresh_from_db()

        self.assertTrue(transition['set_complete'])
        self.assertFalse(transition['quiz_complete'])
        self.assertEqual(transition['set_number'], 1)
        self.assertEqual(self.participant.overall_points, 1)
        self.assertEqual(self.participant.total_points, 0)
        self.assertEqual(self.session.completed_sets_count, 1)

    def test_second_explicit_set_score_starts_clean(self):
        self.session.send_question(self.questions[0])
        self._answer_current_question(9)
        self.session.end_current_question()

        self.session.send_question(self.questions[1])
        self._answer_current_question(16)  # 4 stars in set 2
        self.participant.refresh_from_db()

        self.assertEqual(self.participant.total_points, 4)
        self.assertEqual(self.participant.overall_points, 1)
        self.assertEqual(self.participant.questions_answered, 1)

    def test_out_of_order_selected_set_needs_all_its_questions_before_completion(self):
        self.session.set_selected_set_number(2)
        self.session.save(update_fields=['selected_set_number'])

        self.session.send_question(self.questions[1])
        self._answer_current_question(16)
        first_transition = self.session.end_current_question()

        self.assertFalse(first_transition['set_complete'])

        self.session.send_question(self.questions[2])
        self._answer_current_question(28)
        second_transition = self.session.end_current_question()

        self.session.refresh_from_db()
        self.assertTrue(second_transition['set_complete'])
        self.assertEqual(second_transition['set_number'], 2)
        self.assertEqual(self.session.completed_sets_count, 1)

    def test_play_view_separates_stars_from_points(self):
        self.quiz.current_question = self.questions[1]
        self.quiz.current_question_number = 2
        self.quiz.save(update_fields=['current_question', 'current_question_number'])
        self.participant.total_points = 4
        self.participant.overall_points = 7
        self.participant.save(update_fields=['total_points', 'overall_points'])

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Current Set Stars')
        self.assertContains(response, 'Awarded Points')
        self.assertContains(response, 'participantStarsText')
        self.assertContains(response, 'participantOverallPointsText')
        self.assertContains(response, 'Stars This Question')
        self.assertContains(response, 'Stars This Set')

    def test_play_view_keeps_question_visible_after_submit_without_immediate_star_feedback(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="questionSubmitFeedback"')
        self.assertContains(response, 'id="questionSubmittedAnswer"')
        self.assertContains(response, "this.pendingAnswerResult = data;")
        self.assertContains(response, "document.getElementById('questionSubmitFeedback').classList.remove('d-none');")
        self.assertContains(response, "document.getElementById('questionSubmittedAnswer').textContent = data.user_answer;")
        self.assertContains(response, "submitBtn.classList.remove('loading');")
        self.assertNotContains(response, "this.showState('answerSubmittedState');")
        self.assertNotContains(response, "document.getElementById('pointsEarned').textContent = data.points_earned;")
        self.assertNotContains(response, "document.getElementById('totalPoints').textContent = data.total_points;")

    def test_play_view_applies_pending_answer_feedback_only_on_question_end(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'if (this.pendingAnswerResult) {')
        self.assertContains(response, 'this.currentPoints = this.pendingAnswerResult.total_points;')
        self.assertContains(response, 'this.overallPoints = this.pendingAnswerResult.overall_points;')
        self.assertContains(response, 'this.isBusted = this.pendingAnswerResult.is_busted;')
        self.assertContains(response, 'if (this.isBusted) {')
        self.assertContains(response, "this.showCorrectAnswer(data.correct_answer);")

    def test_play_view_auto_submits_unlogged_answer_on_question_ending(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "case 'question_ending':")
        self.assertContains(response, 'this.onQuestionEnding(data);')
        self.assertContains(response, 'this.questionClosedAwaitingAutoSubmit = true;')
        self.assertContains(response, 'question_id: options.questionId || null,')
        self.assertContains(response, 'if (this.questionClosedAwaitingAutoSubmit && this.pendingQuestionEndedData) {')
        self.assertContains(response, 'this.finalizeQuestionEnd(endedData);')

    def test_play_view_requests_server_timeout_sync_when_timer_expires_without_input(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "type: 'participant_question_timeout'")
        self.assertContains(response, 'this.requestQuestionTimeoutSync();')
        self.assertContains(response, 'this.disableQuestionInputAfterTimeout();')
        self.assertContains(response, 'this.syncQuestionStateAfterTimeout()')
        self.assertContains(response, "this.applyNoAnswerElimination({")
        self.assertContains(response, "this.showState('bustedState');")

    def test_play_view_keeps_pending_input_timeout_as_answer_before_timeout_sync(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'if (answerInput && answerInput.value.trim()) {')
        self.assertContains(response, 'this.submitAnswer({ questionId: this.currentQuestionId || null });')
        self.assertContains(response, 'this.sendParticipantQuestionTimeout();')
        self.assertContains(response, 'this.scheduleTimeoutStatusSync();')
        self.assertNotContains(response, "user_answer: ''")

    def test_busted_participant_rejoin_in_same_set_still_sees_follow_up_question_with_lock_notice(self):
        self.session.set_selected_set_number(2)
        self.session.save(update_fields=['selected_set_number'])

        self.session.send_question(self.questions[1])
        self._answer_current_question(45)  # 25 stars => busted in set 2
        self.session.end_current_question()
        self.participant.refresh_from_db()
        self.assertTrue(self.participant.is_busted)

        self.session.send_question(self.questions[2])

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.questions[2].question_text)
        self.assertContains(response, 'id="bustedQuestionNotice"')
        self.assertContains(response, 'You are already busted for this set')
        self.assertContains(response, "if (this.isBusted && !this.hasActiveQuestionAtLoad) {")
        self.assertContains(response, "submitBtn.classList.toggle('d-none', this.isBusted);")
        self.assertNotContains(response, "if (this.isBusted) return; // Don't show questions if busted")

    def test_submit_answer_rejects_busted_participant_for_remaining_set_questions(self):
        self.session.set_selected_set_number(2)
        self.session.save(update_fields=['selected_set_number'])

        self.session.send_question(self.questions[1])
        self._answer_current_question(45)  # 25 stars => busted in set 2
        self.session.end_current_question()
        self.participant.refresh_from_db()
        self.assertTrue(self.participant.is_busted)

        self.session.send_question(self.questions[2])

        response = self.client.post(
            reverse('black_jack_quiz:submit_answer', args=[self.quiz.room_code, self.participant.name]),
            data=json.dumps({'user_answer': 31, 'time_taken': 1.5}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertEqual(
            response.json()['error'],
            'You are already busted for this set and cannot answer the remaining questions.'
        )

    def test_unanswered_timeout_eliminates_participant_for_current_set(self):
        self.session.set_selected_set_number(2)
        self.session.save(update_fields=['selected_set_number'])

        self.session.send_question(self.questions[1])
        transition = self.session.end_current_question()

        self.participant.refresh_from_db()
        self.session.refresh_from_db()

        self.assertFalse(transition['set_complete'])
        self.assertEqual(
            transition['no_answer_bust_participants'],
            [{
                'id': self.participant.id,
                'name': self.participant.name,
                'hub_session_code': '',
                'reason': 'no_answer',
                'total_points': 0,
                'overall_points': 0,
                'is_busted': True,
            }],
        )
        self.assertTrue(self.participant.is_busted)
        self.assertEqual(self.participant.total_points, 0)

        self.session.send_question(self.questions[2])
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.questions[2].question_text)
        self.assertContains(response, 'Du bist für dieses Set ausgeschieden.')
        self.assertContains(response, 'id="bustedQuestionNotice"')
        self.assertContains(response, 'd-none" id="answerInterface"', html=False)

    def test_answered_participant_is_not_eliminated_when_question_ends(self):
        self.session.set_selected_set_number(2)
        self.session.save(update_fields=['selected_set_number'])

        self.session.send_question(self.questions[1])
        self._answer_current_question(16)
        transition = self.session.end_current_question()

        self.participant.refresh_from_db()

        self.assertEqual(transition['no_answer_bust_participants'], [])
        self.assertFalse(self.participant.is_busted)
        self.assertEqual(self.participant.total_points, 4)

    def test_play_view_renders_simple_mode_set_score_box_with_bottom_total(self):
        self.session.finalized_set_numbers = [1]
        self.session.completed_sets_count = 1
        self.session.save(update_fields=['finalized_set_numbers', 'completed_sets_count'])
        BlackJackAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.questions[0],
            user_answer=9,
        )

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['set_scoreboard'],
            [
                {'set_number': 1, 'earned_points': 1, 'max_points': 21, 'status': 'played'},
                {'set_number': 2, 'earned_points': None, 'max_points': 21, 'status': 'upcoming'},
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 1)
        self.assertEqual(response.context['score_total_max'], 21)
        self.assertContains(response, 'class="blackjack-score-box score-box"')
        self.assertContains(response, 'id="blackjackScoreTotal"')
        self.assertContains(response, 'id="blackjackScoreTotal">1/21</div>', html=False)
        self.assertRegex(
            response.content.decode('utf-8'),
            r'(?s)data-set-number="1"[^>]*>.*?<div class="blackjack-score-badge score-box__badge">1</div>',
        )
        self.assertRegex(
            response.content.decode('utf-8'),
            r'(?s)data-set-number="2"[^>]*>.*?<div class="blackjack-score-badge score-box__badge">2</div>',
        )

    def test_play_view_renders_set_end_screen_with_last_completed_set_summary(self):
        self.session.send_question(self.questions[0])
        self._answer_current_question(9)
        self.session.end_current_question()

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_initial_set_end_state'])
        self.assertFalse(response.context['show_initial_quiz_end_state'])
        self.assertEqual(
            response.context['last_completed_set_summary'],
            {
                'set_number': 1,
                'set_stars': 1,
                'awarded_points': 1,
                'overall_points': 1,
                'is_busted': False,
                'max_points': 21,
            },
        )
        self.assertContains(response, 'id="setEndedState"')
        self.assertContains(response, 'Set Complete!')
        self.assertContains(response, 'id="setEndedNumber"')
        self.assertContains(response, 'id="setEndedStars"')
        self.assertContains(response, 'id="setEndedAwardedPoints"')
        self.assertContains(response, 'id="setEndedOverallPoints"')

    def test_play_view_counter_uses_played_order_for_out_of_order_questions(self):
        questions = self.questions + [
            BlackJackQuestion.objects.create(
                question_text='Question 4',
                correct_answer=40,
                created_by=self.user,
            )
        ]
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Out Of Order Counter Quiz',
            status='active',
            total_questions=4,
            question_order=[[question.id for question in questions]],
        )
        quiz.selected_questions.set(questions)
        session = BlackJackSession.objects.create(quiz=quiz)
        participant = BlackJackParticipant.objects.create(quiz=quiz, name='Bob')

        session.send_question(questions[2])
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_in_set'], 1)
        self.assertContains(
            response,
            'Question <span id="currentQuestionNumber">1</span>/<span id="currentSetQuestionCount">4</span>',
        )

        session.end_current_question()
        session.send_question(questions[0])
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_in_set'], 2)
        self.assertContains(
            response,
            'Question <span id="currentQuestionNumber">2</span>/<span id="currentSetQuestionCount">4</span>',
        )

    def test_play_view_renders_final_quiz_end_screen_with_last_set_and_all_sets_summary(self):
        final_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Finished Quiz',
            status='active',
            total_questions=1,
            question_order=[[self.questions[0].id]],
        )
        final_quiz.selected_questions.set([self.questions[0]])
        final_session = BlackJackSession.objects.create(quiz=final_quiz)
        final_participant = BlackJackParticipant.objects.create(
            quiz=final_quiz,
            name='Bob',
        )

        final_session.send_question(self.questions[0])
        BlackJackAnswer.objects.create(
            quiz=final_quiz,
            participant=final_participant,
            question=self.questions[0],
            user_answer=9,
        )
        final_session.end_current_question()
        final_quiz.refresh_from_db()
        final_participant.refresh_from_db()

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[final_quiz.room_code, final_participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['show_initial_set_end_state'])
        self.assertTrue(response.context['show_initial_quiz_end_state'])
        self.assertEqual(response.context['last_completed_set_summary']['set_number'], 1)
        self.assertEqual(response.context['last_completed_set_summary']['set_stars'], 1)
        self.assertEqual(response.context['last_completed_set_summary']['awarded_points'], 1)
        self.assertContains(response, 'BlackJack Quiz Complete!')
        self.assertContains(response, 'id="finalLastSetGrid"')
        self.assertContains(response, 'id="finalSetSummaryList"')
        self.assertContains(response, 'All Sets Summary')
        self.assertContains(response, 'createHubLobbyReturnController')
        self.assertContains(response, 'Zur Lobby zur')

    def test_play_view_uses_actual_set_play_order_for_out_of_order_current_set(self):
        self.quiz.current_question = self.questions[0]
        self.quiz.current_question_number = 2
        self.quiz.save(update_fields=['current_question', 'current_question_number'])
        self.session.current_question_number = 2
        self.session.total_questions_sent = 2
        self.session.selected_set_number = 1
        self.session.finalized_set_numbers = [2]
        self.session.completed_sets_count = 1
        self.session.save(
            update_fields=[
                'current_question_number',
                'total_questions_sent',
                'selected_set_number',
                'finalized_set_numbers',
                'completed_sets_count',
            ]
        )
        BlackJackAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.questions[1],
            user_answer=16,
            question_number=1,
        )

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry['set_number'] for entry in response.context['set_scoreboard']],
            [2, 1],
        )
        self.assertEqual(response.context['set_scoreboard'][0]['status'], 'played')
        self.assertEqual(response.context['set_scoreboard'][1]['status'], 'current')
        self.assertContains(response, 'reorderSetScoreRows()')
        self.assertContains(response, 'this.renumberSetScoreRows();')
        self.assertRegex(
            response.content.decode('utf-8'),
            r'(?s)data-set-number="2"[^>]*>.*?<div class="blackjack-score-badge score-box__badge">1</div>',
        )
        self.assertRegex(
            response.content.decode('utf-8'),
            r'(?s)data-set-number="1"[^>]*>.*?<div class="blackjack-score-badge score-box__badge">2</div>',
        )

    def test_play_view_updates_set_and_quiz_end_states_from_question_end_payload(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'this.storeLastCompletedSetSummary(data.set_number, data.set_results);')
        self.assertContains(response, 'this.renderCompletedSetScreens();')
        self.assertContains(response, "this.showState('setEndedState');")
        self.assertContains(response, "this.showState('quizEndedState');")
        self.assertContains(response, "id=\"finalSetSummaryList\"")

    def test_play_view_handles_no_answer_timeout_elimination_payload(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'getOwnNoAnswerElimination(data)')
        self.assertContains(response, 'applyNoAnswerElimination(noAnswerElimination)')
        self.assertContains(response, 'no_answer_bust_participants')
        self.assertContains(response, 'Keine Antwort wurde abgegeben. Du bist für dieses Set ausgeschieden.')
        self.assertContains(response, 'Du kannst die restlichen Fragen dieses Sets weiter ansehen, aber nicht mehr antworten.')

    def test_play_view_renders_without_template_error_when_hub_session_is_missing(self):
        response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'createHubLobbyReturnController')
        self.assertContains(response, "sessionCode: ''")


class BlackJackRankingModeSetScoringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='blackjack_rank_admin',
            password='testpass123',
            email='',
        )

    def test_set_ranking_includes_zero_scores_and_skips_follow_rank_after_ties(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Ranking Set Quiz',
            scoring_mode='rank',
        )
        first = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            total_points=20,
            final_score=1,
            is_busted=False,
        )
        tied_one = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Bob',
            total_points=19,
            final_score=2,
            is_busted=False,
        )
        tied_two = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Cara',
            total_points=19,
            final_score=2,
            is_busted=False,
        )
        valid_zero = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Dina',
            total_points=0,
            final_score=21,
            is_busted=False,
        )
        busted_zero = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Eli',
            total_points=0,
            final_score=22,
            is_busted=True,
        )

        ranking_points = quiz.get_set_ranking_points()

        self.assertEqual(ranking_points[first.id], 5)
        self.assertEqual(ranking_points[tied_one.id], 4)
        self.assertEqual(ranking_points[tied_two.id], 4)
        self.assertEqual(ranking_points[valid_zero.id], 2)
        self.assertEqual(ranking_points[busted_zero.id], 1)

    def test_finalize_completed_set_awards_ranking_points_to_zero_scores(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Ranking Finalize Quiz',
            scoring_mode='rank',
            current_question_number=1,
            total_questions=1,
        )
        session = BlackJackSession.objects.create(quiz=quiz)

        leader = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            total_points=20,
            final_score=1,
            is_busted=False,
        )
        zero_score = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Bob',
            total_points=0,
            final_score=21,
            is_busted=False,
        )
        busted = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Cara',
            total_points=0,
            final_score=23,
            is_busted=True,
        )

        result = session.finalize_completed_set()
        leader.refresh_from_db()
        zero_score.refresh_from_db()
        busted.refresh_from_db()

        self.assertTrue(result['set_complete'])
        self.assertEqual(leader.overall_points, 3)
        self.assertEqual(zero_score.overall_points, 2)
        self.assertEqual(busted.overall_points, 1)

    def test_play_view_uses_participant_count_as_set_denominator_in_ranking_mode(self):
        question = BlackJackQuestion.objects.create(
            question_text='Question 1',
            correct_answer=10,
            created_by=self.user,
        )
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Ranking Play View Quiz',
            status='active',
            scoring_mode='rank',
            question_order=[[question.id]],
        )
        quiz.selected_questions.set([question])
        BlackJackSession.objects.create(
            quiz=quiz,
            finalized_set_numbers=[1],
            completed_sets_count=1,
        )
        alice = BlackJackParticipant.objects.create(quiz=quiz, name='Alice')
        bob = BlackJackParticipant.objects.create(quiz=quiz, name='Bob')
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=alice,
            question=question,
            user_answer=8,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=bob,
            question=question,
            user_answer=9,
        )

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[quiz.room_code, alice.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['set_scoreboard'],
            [{'set_number': 1, 'earned_points': 2, 'max_points': 2, 'status': 'played'}],
        )
        self.assertEqual(response.context['score_total_earned'], 2)
        self.assertEqual(response.context['score_total_max'], 2)


class BlackJackHostSetProgressTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='blackjack_progress_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)
        self.questions = [
            BlackJackQuestion.objects.create(
                question_text=f'Question {index}',
                correct_answer=index * 10,
                created_by=self.user,
            )
            for index in range(1, 5)
        ]

    def _create_quiz(self, total_questions, ordered_questions, selected_questions, current_question_number=0):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Host Progress Quiz',
            status='active',
            total_questions=total_questions,
            question_order=[question.id for question in ordered_questions],
            current_question_number=current_question_number,
        )
        quiz.selected_questions.set(selected_questions)
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=current_question_number,
            total_questions_sent=current_question_number,
        )
        return quiz

    def _create_hub_session(self, code, started_at=None, is_active=True):
        return HubSession.objects.create(
            code=code,
            name=code,
            started_at=started_at,
            is_active=is_active,
        )

    def _seed_stale_runtime_for_new_session(self, quiz, session):
        stale_started_at = timezone.now() - timedelta(days=2, minutes=17)
        quiz.status = 'active'
        quiz.started_at = stale_started_at
        quiz.current_question = self.questions[0]
        quiz.current_question_number = 1
        quiz.question_start_time = stale_started_at
        quiz.save(update_fields=[
            'status',
            'started_at',
            'current_question',
            'current_question_number',
            'question_start_time',
        ])
        session.current_question_number = 1
        session.total_questions_sent = 1
        session.completed_sets_count = 1
        session.selected_set_number = 2
        session.asked_question_ids = [self.questions[0].id]
        session.finalized_set_numbers = [1]
        session.is_question_active = True
        session.question_end_time = timezone.now() + timedelta(seconds=45)
        session.save(update_fields=[
            'current_question_number',
            'total_questions_sent',
            'completed_sets_count',
            'selected_set_number',
            'asked_question_ids',
            'finalized_set_numbers',
            'is_question_active',
            'question_end_time',
        ])

    def test_monitor_uses_question_order_fallback_for_initial_set_questions(self):
        quiz = self._create_quiz(
            total_questions=2,
            ordered_questions=self.questions[:2],
            selected_questions=self.questions[:1],
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/1, 1/2)')
        self.assertNotContains(response, 'All 2 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[0].question_text)
        self.assertContains(response, self.questions[1].question_text)

    def test_monitor_renders_collapsible_set_overview_for_all_sets(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Grouped Sets Quiz',
            status='active',
            total_questions=5,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            selected_set_number=2,
            asked_question_ids=[self.questions[0].id],
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Set-Übersicht')
        self.assertContains(response, '2 Sets')
        self.assertContains(response, 'Set 1')
        self.assertContains(response, 'Set 2')
        self.assertContains(response, '2 Fragen')
        self.assertContains(response, 'data-bs-target="#setQuestions1"')
        self.assertContains(response, 'data-bs-target="#setQuestions2"')
        self.assertContains(response, 'select-set-btn')
        self.assertContains(response, 'data-set-number="2"')
        self.assertContains(response, self.questions[0].question_text)
        self.assertContains(response, self.questions[3].question_text)
        self.assertContains(response, 'data-select-set-url=')
        self.assertNotContains(response, 'id="setQuestions1" class="collapse show"', html=False)

    def test_monitor_start_handler_waits_for_started_event_without_immediate_reload(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Waiting Start Quiz',
            status='waiting',
            total_questions=4,
            question_order=[[question.id for question in self.questions]],
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(quiz=quiz)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        start_function = content.split('startQuiz() {', 1)[1].split('\n        endQuiz() {', 1)[0]
        self.assertIn('this.setStartButtonPending(true);', start_function)
        self.assertIn("type: 'admin_start_quiz'", start_function)
        self.assertNotIn('location.reload()', start_function)
        self.assertIn('this.handleQuizStarted(data);', content)
        self.assertIn('renderActiveControls()', content)

    def test_monitor_keeps_second_question_sendable_after_first_question(self):
        quiz = self._create_quiz(
            total_questions=2,
            ordered_questions=self.questions[:2],
            selected_questions=self.questions[:1],
            current_question_number=1,
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/1, 2/2)')
        self.assertNotContains(response, 'All 2 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[1].question_text)

    def test_monitor_routes_play_button_to_active_question_for_running_set(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Running Set Quiz',
            status='active',
            total_questions=5,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question=self.questions[0],
            current_question_number=1,
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(quiz=quiz)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '· aktuell')

    def test_single_question_set_is_complete_after_first_question(self):
        quiz = self._create_quiz(
            total_questions=1,
            ordered_questions=self.questions[:1],
            selected_questions=[],
            current_question_number=1,
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'All 1 questions have been asked. The quiz is finished.')

    def test_monitor_does_not_mark_next_set_complete_early(self):
        quiz = self._create_quiz(
            total_questions=2,
            ordered_questions=self.questions,
            selected_questions=self.questions[:2],
            current_question_number=2,
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 2/2, 1/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[2].question_text)

    def test_monitor_keeps_four_question_set_active_after_first_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Four Question Set Monitor Quiz',
            status='active',
            total_questions=4,
            question_order=[[question.id for question in self.questions]],
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(quiz=quiz)
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='HUB1',
        )

        session.send_question(self.questions[0])
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            user_answer=self.questions[0].correct_answer + 1,
        )
        session.end_current_question()

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/1, 2/4)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[1].question_text)

    def test_select_set_endpoint_loads_second_set_questions(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Selectable Sets Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            selected_set_number=2,
            asked_question_ids=[self.questions[0].id],
        )

        select_response = self.client.post(
            reverse('admin_dashboard:select_blackjack_set', args=[quiz.room_code]),
            data=json.dumps({'set_number': 2}),
            content_type='application/json',
        )

        self.assertEqual(select_response.status_code, 200)
        self.assertEqual(select_response.json()['selected_set_number'], 2)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 2/2, 1/2)')
        self.assertContains(response, self.questions[2].question_text)
        self.assertContains(response, self.questions[3].question_text)

    def test_selected_two_question_set_keeps_second_question_sendable_after_first_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Selected Two Question Set Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(quiz=quiz)
        session.set_selected_set_number(2)
        session.save(update_fields=['selected_set_number'])
        session.send_question(self.questions[2])
        session.end_current_question()

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 2/2, 2/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[3].question_text)

    def test_first_explicit_two_question_set_keeps_second_question_sendable_after_first_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='First Explicit Set Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(quiz=quiz)

        session.send_question(self.questions[0])
        session.end_current_question()

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/2, 2/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[1].question_text)

    def test_select_set_endpoint_can_switch_away_from_incomplete_set(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Switch Incomplete Set Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(quiz=quiz)
        session.send_question(self.questions[0])
        session.end_current_question()

        select_response = self.client.post(
            reverse('admin_dashboard:select_blackjack_set', args=[quiz.room_code]),
            data=json.dumps({'set_number': 2}),
            content_type='application/json',
        )

        self.assertEqual(select_response.status_code, 200)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertContains(response, 'Select Next Question (Set 2/2, 1/2)')
        self.assertContains(response, self.questions[2].question_text)

    def test_blackjack_stats_endpoint_reports_missing_answers_for_early_end_warning(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Stats Warning Quiz',
            status='active',
            total_questions=2,
            current_question=self.questions[0],
            current_question_number=1,
        )
        participant_one = BlackJackParticipant.objects.create(quiz=quiz, name='Alice')
        participant_two = BlackJackParticipant.objects.create(quiz=quiz, name='Bob')
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant_one,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['active_participants'], 2)
        self.assertEqual(payload['stats']['current_question_responses'], 1)

    def test_blackjack_stats_endpoint_excludes_busted_participants_from_early_end_warning(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Busted Warning Quiz',
            status='active',
            total_questions=3,
            current_question=self.questions[2],
            current_question_number=3,
            question_order=[[self.questions[0].id, self.questions[1].id, self.questions[2].id]],
        )
        busted = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            is_active=True,
            is_busted=True,
            final_score=25,
        )
        active = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Bob',
            is_active=True,
            is_busted=False,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=active,
            question=self.questions[2],
            user_answer=31,
            question_number=3,
        )

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['participant_count'], 2)
        self.assertEqual(payload['stats']['active_participants'], 1)
        self.assertEqual(payload['stats']['current_question_responses'], 1)

    def test_blackjack_stats_endpoint_scopes_early_end_warning_to_current_hub_session(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Scoped Warning Quiz',
            status='active',
            total_questions=2,
            current_question=self.questions[0],
            current_question_number=1,
        )
        current_participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        stale_participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Bob',
            hub_session_code='HUB2',
            is_active=True,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=current_participant,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=stale_participant,
            question=self.questions[0],
            user_answer=12,
            question_number=1,
        )

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code]),
            {'hub_session': 'HUB1'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['participant_count'], 1)
        self.assertEqual(payload['stats']['active_participants'], 1)
        self.assertEqual(payload['stats']['current_question_responses'], 1)

    def test_blackjack_stats_endpoint_falls_back_to_active_hub_session_for_warning_counts(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fallback Scoped Warning Quiz',
            status='active',
            total_questions=2,
            current_question=self.questions[0],
            current_question_number=1,
        )
        hub_session = HubSession.objects.create(
            code='BJCUR',
            name='BJCUR',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=1,
            game_key='blackjack',
            room_code=quiz.room_code,
        )
        current_participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code=hub_session.code,
            is_active=True,
        )
        BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Old Bob',
            hub_session_code='OLDHUB',
            is_active=True,
        )
        BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Lobby Carol',
            hub_session_code=hub_session.code,
            is_active=False,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=current_participant,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['participant_count'], 2)
        self.assertEqual(payload['stats']['active_participants'], 1)
        self.assertEqual(payload['stats']['current_question_responses'], 1)

    def test_blackjack_stats_endpoint_reports_single_unanswered_active_player(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Unanswered Single Player Warning Quiz',
            status='active',
            total_questions=2,
            current_question=self.questions[0],
            current_question_number=1,
        )
        BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Lobby Bob',
            hub_session_code='HUB1',
            is_active=False,
        )

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code]),
            {'hub_session': 'HUB1'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['participant_count'], 2)
        self.assertEqual(payload['stats']['active_participants'], 1)
        self.assertEqual(payload['stats']['current_question_responses'], 0)

    def test_blackjack_stats_endpoint_keeps_current_answerer_relevant_after_busting_on_current_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Current Question Bust Warning Quiz',
            status='active',
            total_questions=3,
            current_question=self.questions[0],
            current_question_number=1,
            question_order=[[self.questions[0].id, self.questions[1].id, self.questions[2].id]],
        )
        quiz.selected_questions.set(self.questions[:3])
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=self.questions[0].correct_answer + 25,
            question_number=1,
        )
        participant.refresh_from_db()
        self.assertTrue(participant.is_busted)

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code]),
            {'hub_session': 'HUB1'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['active_participants'], 1)
        self.assertEqual(payload['stats']['current_question_responses'], 1)

    def test_blackjack_stats_endpoint_ignores_answers_before_current_run(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Current Run Warning Quiz',
            status='active',
            started_at=timezone.now(),
            total_questions=2,
            current_question=self.questions[0],
            current_question_number=1,
        )
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        stale_answer = BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )
        BlackJackAnswer.objects.filter(pk=stale_answer.pk).update(
            submitted_at=quiz.started_at - timedelta(days=1)
        )

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code]),
            {'hub_session': 'HUB1'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['active_participants'], 1)
        self.assertEqual(payload['stats']['current_question_responses'], 0)
        self.assertEqual(payload['stats']['total_answers'], 0)

    def test_blackjack_stats_endpoint_excludes_no_answer_eliminated_participant_on_follow_up_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='No Answer Follow Up Warning Quiz',
            status='active',
            total_questions=3,
            question_order=[[self.questions[0].id, self.questions[1].id, self.questions[2].id]],
        )
        quiz.selected_questions.set(self.questions[:3])
        session = BlackJackSession.objects.create(quiz=quiz)
        eliminated = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        active = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Bob',
            hub_session_code='HUB1',
            is_active=True,
        )

        session.send_question(self.questions[0])
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=active,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )
        session.end_current_question()
        eliminated.refresh_from_db()
        self.assertTrue(eliminated.is_busted)

        session.send_question(self.questions[1])
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=active,
            question=self.questions[1],
            user_answer=21,
            question_number=2,
        )

        response = self.client.get(
            reverse('admin_dashboard:api_blackjack_quiz_stats', args=[quiz.room_code]),
            {'hub_session': 'HUB1'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['stats']['participant_count'], 2)
        self.assertEqual(payload['stats']['active_participants'], 1)
        self.assertEqual(payload['stats']['current_question_responses'], 1)

    def test_monitor_contains_early_end_warning_and_set_switch_warning_hooks(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Warning Hooks Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question=self.questions[0],
            current_question_number=1,
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(quiz=quiz)
        BlackJackParticipant.objects.create(quiz=quiz, name='Alice')
        BlackJackParticipant.objects.create(quiz=quiz, name='Bob')
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=quiz.participants.get(name='Alice'),
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'confirmEarlyQuestionEnd()')
        self.assertContains(response, 'data-stats-url=')
        self.assertContains(response, 'selectedSetHasRemainingQuestions')
        self.assertContains(response, 'select-set-btn')
        self.assertContains(response, "scopedUrl.searchParams.set('hub_session', hubSession);")
        self.assertContains(response, "scopedStatsUrl.searchParams.set('hub_session', hubSession);")
        self.assertContains(response, "rowText.includes('bust')")
        self.assertContains(response, 'response.is_busted')
        html = response.content.decode('utf-8')
        self.assertIn('</script>\n\n<!-- Score Edit Modal -->', html)

    def test_websocket_send_checks_fallback_question_pool(self):
        quiz = self._create_quiz(
            total_questions=2,
            ordered_questions=self.questions[:2],
            selected_questions=self.questions[:1],
        )
        consumer = BlackJackConsumer()

        self.assertTrue(async_to_sync(consumer.quiz_has_selected_questions)(quiz.id))
        self.assertTrue(async_to_sync(consumer.is_question_available_for_next_turn)(quiz.id, self.questions[1].id))

    def test_send_question_blocks_future_set_question_when_explicit_sets_are_defined(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Explicit Guard Quiz',
            status='active',
            total_questions=5,
            question_order=[[self.questions[0].id], [self.questions[1].id, self.questions[2].id]],
        )
        quiz.selected_questions.set(self.questions[:3])
        BlackJackSession.objects.create(quiz=quiz)

        response = self.client.post(
            reverse('admin_dashboard:send_blackjack_question', args=[quiz.room_code]),
            data=json.dumps({'question_id': self.questions[2].id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error'],
            'This question is not part of the current active set for this quiz.'
        )

    def test_send_question_blocks_after_all_questions_have_been_asked_without_configured_pool(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Unconfigured Pool Quiz',
            status='active',
            total_questions=1,
            current_question_number=1,
        )
        BlackJackSession.objects.create(quiz=quiz)

        response = self.client.post(
            reverse('admin_dashboard:send_blackjack_question', args=[quiz.room_code]),
            data=json.dumps({'question_id': self.questions[0].id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error'],
            'All questions in this quiz have already been asked.'
        )

    def test_consumer_guard_blocks_after_all_questions_have_been_asked_without_configured_pool(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Consumer Guard Quiz',
            status='active',
            total_questions=1,
            current_question_number=1,
        )
        BlackJackSession.objects.create(quiz=quiz)
        consumer = BlackJackConsumer()

        self.assertEqual(
            async_to_sync(consumer.get_next_question_send_error)(quiz.id, self.questions[0].id),
            'All questions in this quiz have already been asked.'
        )

    def test_send_question_blocks_when_another_question_is_still_active(self):
        quiz = self._create_quiz(
            total_questions=2,
            ordered_questions=self.questions[:2],
            selected_questions=self.questions[:2],
        )
        quiz.session.send_question(self.questions[0])

        response = self.client.post(
            reverse('admin_dashboard:send_blackjack_question', args=[quiz.room_code]),
            data=json.dumps({'question_id': self.questions[1].id}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error'],
            'A question is already active for this quiz.'
        )

    def test_monitor_uses_active_questions_for_set_overview_and_next_turn(self):
        inactive_question = self.questions[1]
        inactive_question.is_active = False
        inactive_question.save(update_fields=['is_active'])

        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Inactive Question Quiz',
            status='active',
            total_questions=2,
            question_order=[[self.questions[0].id, inactive_question.id], [self.questions[2].id]],
        )
        quiz.selected_questions.set([self.questions[0], inactive_question, self.questions[2]])
        BlackJackSession.objects.create(quiz=quiz)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.questions[0].question_text)
        self.assertContains(response, self.questions[2].question_text)
        self.assertNotContains(response, inactive_question.question_text)
        self.assertContains(response, 'Set 1')
        self.assertContains(response, 'Set 2')

    def test_monitor_does_not_mark_next_active_set_complete_early_when_prior_set_lost_inactive_question(self):
        inactive_question = self.questions[1]
        inactive_question.is_active = False
        inactive_question.save(update_fields=['is_active'])

        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Inactive Set Transition Quiz',
            status='active',
            total_questions=2,
            question_order=[[self.questions[0].id, inactive_question.id], [self.questions[2].id]],
            current_question_number=1,
        )
        quiz.selected_questions.set([self.questions[0], inactive_question, self.questions[2]])
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            selected_set_number=2,
            asked_question_ids=[self.questions[0].id],
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 2/2, 1/1)')
        self.assertNotContains(response, 'All 2 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[2].question_text)

    def test_waiting_quiz_with_stale_progress_renders_clean_initial_state(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Stale Waiting Quiz',
            status='waiting',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=4,
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/2, 1/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[0].question_text)
        self.assertContains(response, self.questions[1].question_text)
        self.assertEqual(session.get_normalized_selected_set_number(active_only=True), 1)
        self.assertEqual(session.get_asked_question_ids(), [])

    def test_starting_waiting_quiz_resets_stale_runtime_progress(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Reset Waiting Quiz',
            status='waiting',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=4,
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        quiz.start_quiz()

        quiz.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(quiz.status, 'active')
        self.assertEqual(quiz.current_question_number, 0)
        self.assertIsNone(quiz.current_question_id)
        self.assertEqual(session.current_question_number, 0)
        self.assertEqual(session.total_questions_sent, 0)
        self.assertEqual(session.completed_sets_count, 0)
        self.assertEqual(session.selected_set_number, 1)
        self.assertEqual(session.asked_question_ids, [])
        self.assertEqual(session.finalized_set_numbers, [])

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/2, 1/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')

    def test_starting_waiting_quiz_clears_stale_answers_and_participant_state(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Reset Answer State Quiz',
            status='waiting',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=4,
        )
        quiz.selected_questions.set(self.questions)
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            total_points=8,
            overall_points=13,
            questions_answered=2,
            is_busted=True,
            final_score=24,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=4,
            question_number=1,
        )
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        quiz.start_quiz()

        participant.refresh_from_db()
        self.assertEqual(BlackJackAnswer.objects.filter(quiz=quiz).count(), 0)
        self.assertEqual(participant.total_points, 0)
        self.assertEqual(participant.overall_points, 0)
        self.assertEqual(participant.questions_answered, 0)
        self.assertFalse(participant.is_busted)
        self.assertEqual(participant.final_score, 0)

    def test_play_view_starts_with_neutral_scorebox_after_waiting_quiz_restart(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Restart Neutral Scorebox Quiz',
            status='waiting',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=4,
        )
        quiz.selected_questions.set(self.questions)
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            total_points=11,
            overall_points=17,
            questions_answered=2,
            is_busted=False,
            final_score=10,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=4,
            question_number=1,
        )
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        quiz.start_quiz()

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['set_scoreboard'],
            [
                {'set_number': 1, 'earned_points': None, 'max_points': 21, 'status': 'current'},
                {'set_number': 2, 'earned_points': None, 'max_points': 21, 'status': 'upcoming'},
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 0)
        self.assertEqual(response.context['score_total_max'], 0)
        self.assertContains(response, 'id="participantStarsText">0 stars this set', html=False)
        self.assertContains(response, 'id="participantOverallPointsText">0 points total', html=False)
        self.assertContains(response, 'blackjack-score-empty')
        self.assertNotContains(response, 'id="participantOverallPointsText">17 points total', html=False)

    def test_waiting_quiz_with_stale_progress_can_still_select_second_set(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Selectable Stale Waiting Quiz',
            status='waiting',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=4,
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        select_response = self.client.post(
            reverse('admin_dashboard:select_blackjack_set', args=[quiz.room_code]),
            data=json.dumps({'set_number': 2}),
            content_type='application/json',
        )

        self.assertEqual(select_response.status_code, 200)
        self.assertEqual(select_response.json()['selected_set_number'], 2)

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 2/2, 1/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertContains(response, self.questions[2].question_text)
        self.assertContains(response, self.questions[3].question_text)

    def test_question_control_ignores_stale_quiz_counter_before_any_question_is_sent(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Stale Quiz Counter',
            status='active',
            total_questions=4,
            question_order=[question.id for question in self.questions],
            current_question_number=4,
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=0,
            total_questions_sent=0,
            asked_question_ids=[],
        )
        participant = BlackJackParticipant.objects.create(quiz=quiz, name='Alice')
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertContains(response, 'Select Next Question (Set 1/1, 1/4)')

    def test_monitor_ignores_stale_session_progress_when_active_quiz_has_not_sent_first_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Active Start Monitor Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=0,
        )
        quiz.selected_questions.set(self.questions)
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['quiz_complete'])
        self.assertEqual(response.context['selected_set_number'], 1)
        self.assertEqual(
            [question.id for question in response.context['available_questions']],
            [self.questions[0].id, self.questions[1].id],
        )
        self.assertContains(response, 'Select Next Question (Set 1/2, 1/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')

    def test_play_view_starts_neutral_when_active_quiz_has_not_sent_first_question(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Active Start Play Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=0,
        )
        quiz.selected_questions.set(self.questions)
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
        )
        BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['set_scoreboard'],
            [
                {'set_number': 1, 'earned_points': None, 'max_points': 21, 'status': 'current'},
                {'set_number': 2, 'earned_points': None, 'max_points': 21, 'status': 'upcoming'},
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 0)
        self.assertEqual(response.context['score_total_max'], 0)
        self.assertFalse(response.context['show_initial_set_end_state'])
        self.assertContains(response, 'blackjack-score-empty')
        self.assertNotContains(response, 'data-earned-points="0"')

    def test_monitor_resets_stale_runtime_for_unstarted_hub_session_before_game_start(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Session Monitor Reset Quiz',
            status='waiting',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(quiz=quiz)
        self._seed_stale_runtime_for_new_session(quiz, session)
        hub_session = self._create_hub_session('BJMONNEW')
        HubGameStep.objects.create(
            session=hub_session,
            order=1,
            game_key='blackjack',
            room_code=quiz.room_code,
            title=quiz.title,
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        quiz.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(quiz.status, 'waiting')
        self.assertIsNone(quiz.started_at)
        self.assertIsNone(quiz.current_question_id)
        self.assertEqual(quiz.current_question_number, 0)
        self.assertIsNone(quiz.question_start_time)
        self.assertEqual(session.current_question_number, 0)
        self.assertEqual(session.total_questions_sent, 0)
        self.assertEqual(session.completed_sets_count, 0)
        self.assertEqual(session.selected_set_number, 1)
        self.assertEqual(session.asked_question_ids, [])
        self.assertEqual(session.finalized_set_numbers, [])
        self.assertFalse(session.is_question_active)
        self.assertIsNone(session.question_end_time)
        self.assertFalse(response.context['quiz_complete'])
        self.assertEqual(response.context['selected_set_number'], 1)
        self.assertContains(response, 'Spiel starten')
        self.assertContains(response, 'Select Next Question (Set 1/2, 1/2)')
        self.assertContains(response, 'this.quizStartTime = null;', html=False)
        self.assertContains(response, 'data-question-active="0"', html=False)

    def test_play_view_resets_stale_runtime_for_unstarted_hub_session_before_game_start(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Session Play Reset Quiz',
            status='waiting',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
        )
        quiz.selected_questions.set(self.questions)
        session = BlackJackSession.objects.create(quiz=quiz)
        self._seed_stale_runtime_for_new_session(quiz, session)
        hub_session = self._create_hub_session('BJPLAYNEW')
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code=hub_session.code,
        )

        response = self.client.get(
            reverse('black_jack_quiz:play', args=[quiz.room_code, participant.name]),
            {'hub_session': hub_session.code},
        )

        quiz.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(quiz.status, 'waiting')
        self.assertIsNone(quiz.started_at)
        self.assertIsNone(quiz.current_question_id)
        self.assertEqual(quiz.current_question_number, 0)
        self.assertIsNone(quiz.question_start_time)
        self.assertEqual(session.current_question_number, 0)
        self.assertEqual(session.total_questions_sent, 0)
        self.assertEqual(session.completed_sets_count, 0)
        self.assertEqual(session.selected_set_number, 1)
        self.assertEqual(session.asked_question_ids, [])
        self.assertEqual(session.finalized_set_numbers, [])
        self.assertFalse(session.is_question_active)
        self.assertIsNone(session.question_end_time)
        self.assertEqual(
            response.context['set_scoreboard'],
            [
                {'set_number': 1, 'earned_points': None, 'max_points': 21, 'status': 'current'},
                {'set_number': 2, 'earned_points': None, 'max_points': 21, 'status': 'upcoming'},
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 0)
        self.assertEqual(response.context['score_total_max'], 0)
        self.assertFalse(response.context['show_initial_set_end_state'])
        self.assertContains(response, 'Waiting for BlackJack Quiz to Start')
        self.assertContains(response, 'blackjack-score-empty')
        self.assertContains(response, 'this.hasActiveQuestionAtLoad = false;', html=False)

    def test_starting_completed_quiz_resets_stale_runtime_progress(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Completed Restart Quiz',
            status='completed',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=4,
        )
        quiz.selected_questions.set(self.questions)
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            total_points=8,
            overall_points=15,
            questions_answered=2,
            is_busted=True,
            final_score=24,
        )
        BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )
        session = BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        quiz.start_quiz()

        quiz.refresh_from_db()
        participant.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(quiz.status, 'active')
        self.assertEqual(quiz.current_question_number, 0)
        self.assertIsNone(quiz.current_question_id)
        self.assertEqual(BlackJackAnswer.objects.filter(quiz=quiz).count(), 0)
        self.assertEqual(participant.total_points, 0)
        self.assertEqual(participant.overall_points, 0)
        self.assertEqual(participant.questions_answered, 0)
        self.assertFalse(participant.is_busted)
        self.assertEqual(participant.final_score, 0)
        self.assertEqual(session.current_question_number, 0)
        self.assertEqual(session.total_questions_sent, 0)
        self.assertEqual(session.completed_sets_count, 0)
        self.assertEqual(session.selected_set_number, 1)
        self.assertEqual(session.asked_question_ids, [])
        self.assertEqual(session.finalized_set_numbers, [])

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select Next Question (Set 1/2, 1/2)')
        self.assertNotContains(response, 'All 4 questions have been asked. The quiz is finished.')

    def test_fresh_active_start_ignores_stale_answers_from_previous_run(self):
        started_at = timezone.now()
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Active Start With Stale Answers Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=0,
            started_at=started_at,
        )
        quiz.selected_questions.set(self.questions)
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
        )
        stale_answer = BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )
        BlackJackAnswer.objects.filter(pk=stale_answer.pk).update(
            submitted_at=started_at - timedelta(minutes=5)
        )
        BlackJackParticipant.objects.filter(pk=participant.pk).update(
            total_points=0,
            overall_points=0,
            questions_answered=0,
            is_busted=False,
            final_score=0,
        )
        session = BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        self.assertEqual(session.get_asked_question_ids(), [])
        self.assertEqual(session.get_finalized_set_numbers(), [])

        monitor_response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )

        self.assertEqual(monitor_response.status_code, 200)
        self.assertFalse(monitor_response.context['quiz_complete'])
        self.assertEqual(monitor_response.context['selected_set_number'], 1)
        self.assertContains(monitor_response, 'Select Next Question (Set 1/2, 1/2)')
        self.assertNotContains(monitor_response, 'All 4 questions have been asked. The quiz is finished.')
        self.assertFalse(monitor_response.context['blackjack_set_overview'][0]['is_completed'])
        self.assertFalse(monitor_response.context['blackjack_set_overview'][1]['is_completed'])

        play_response = self.client.get(
            reverse('black_jack_quiz:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(play_response.status_code, 200)
        self.assertEqual(
            play_response.context['set_scoreboard'],
            [
                {'set_number': 1, 'earned_points': None, 'max_points': 21, 'status': 'current'},
                {'set_number': 2, 'earned_points': None, 'max_points': 21, 'status': 'upcoming'},
            ],
        )
        self.assertEqual(play_response.context['score_total_earned'], 0)
        self.assertEqual(play_response.context['score_total_max'], 0)
        self.assertContains(play_response, 'blackjack-score-empty')

    def test_starting_fresh_active_quiz_clears_stale_previous_run_state(self):
        started_at = timezone.now()
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Active Restart Cleanup Quiz',
            status='active',
            total_questions=2,
            question_order=[
                [self.questions[0].id, self.questions[1].id],
                [self.questions[2].id, self.questions[3].id],
            ],
            current_question_number=0,
            started_at=started_at - timedelta(minutes=1),
        )
        quiz.selected_questions.set(self.questions)
        participant = BlackJackParticipant.objects.create(
            quiz=quiz,
            name='Alice',
        )
        stale_answer = BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.questions[0],
            user_answer=11,
            question_number=1,
        )
        BlackJackAnswer.objects.filter(pk=stale_answer.pk).update(
            submitted_at=started_at - timedelta(minutes=5)
        )
        session = BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=4,
            total_questions_sent=4,
            completed_sets_count=2,
            selected_set_number=2,
            asked_question_ids=[question.id for question in self.questions],
            finalized_set_numbers=[1, 2],
        )

        quiz.start_quiz()

        quiz.refresh_from_db()
        participant.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(quiz.status, 'active')
        self.assertEqual(quiz.current_question_number, 0)
        self.assertEqual(BlackJackAnswer.objects.filter(quiz=quiz).count(), 0)
        self.assertEqual(participant.total_points, 0)
        self.assertEqual(participant.overall_points, 0)
        self.assertEqual(participant.questions_answered, 0)
        self.assertFalse(participant.is_busted)
        self.assertEqual(participant.final_score, 0)
        self.assertEqual(session.current_question_number, 0)
        self.assertEqual(session.total_questions_sent, 0)
        self.assertEqual(session.completed_sets_count, 0)
        self.assertEqual(session.selected_set_number, 1)
        self.assertEqual(session.asked_question_ids, [])
        self.assertEqual(session.finalized_set_numbers, [])

    def test_question_control_uses_session_send_progress_for_three_question_set(self):
        quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Three Question Set Quiz',
            status='active',
            total_questions=3,
            question_order=[[self.questions[0].id, self.questions[1].id, self.questions[2].id]],
            current_question_number=3,
        )
        quiz.selected_questions.set(self.questions[:3])
        session = BlackJackSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            asked_question_ids=[],
        )

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'All 3 questions have been asked. The quiz is finished.')
        self.assertContains(response, 'Select Next Question (Set 1/1, 2/3)')

        session.current_question_number = 2
        session.total_questions_sent = 2
        session.save(update_fields=['current_question_number', 'total_questions_sent'])

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'All 3 questions have been asked. The quiz is finished.')
        self.assertContains(response, 'Select Next Question (Set 1/1, 3/3)')

        session.current_question_number = 3
        session.total_questions_sent = 3
        session.save(update_fields=['current_question_number', 'total_questions_sent'])

        response = self.client.get(
            reverse('admin_dashboard:blackjack_monitor', args=[quiz.room_code])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'All 3 questions have been asked. The quiz is finished.')


class BlackJackTutorialRuntimeTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='blackjack-tutorial-user', password='pass')
        self.quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Black Jack Tutorial Quiz',
            room_code='BJTUT',
            tutorial_enabled=True,
            tutorial_title='Before the first set',
            tutorial_text='Closest to 21 wins.',
            total_questions=1,
        )
        BlackJackSession.objects.create(quiz=self.quiz)
        self.consumer = BlackJackConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'blackjack_{self.quiz.room_code}'
        self.consumer.channel_layer = FakeChannelLayer()
        self.consumer.channel_name = 'blackjack-tutorial-channel'
        self.direct_messages = []

        async def _capture_send(*args, **kwargs):
            text_data = kwargs.get('text_data')
            if text_data is None and args:
                text_data = args[0]
            if text_data:
                self.direct_messages.append(json.loads(text_data))

        self.consumer.send = _capture_send

    @patch('black_jack_quiz.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('black_jack_quiz.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_admin_start_quiz_with_tutorial_sets_runtime_state_and_broadcasts_tutorial(self, _ready_mock, _activation_mock):
        async_to_sync(self.consumer.handle_admin_start_quiz)({'show_tutorial': True})

        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.tutorial_active)
        messages = [message for _, message in self.consumer.channel_layer.group_messages]
        message_types = [message['type'] for message in messages]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)
        quiz_started = next(message for message in messages if message['type'] == 'quiz_started')
        self.assertEqual(quiz_started['status'], 'active')
        self.assertEqual(quiz_started['started_at'], self.quiz.started_at.isoformat())

    def test_first_question_start_clears_tutorial_active(self):
        question = BlackJackQuestion.objects.create(
            question_text='How many?',
            correct_answer=21,
            created_by=self.user,
        )
        self.quiz.status = 'active'
        self.quiz.tutorial_active = True
        self.quiz.question_order = [[question.id]]
        self.quiz.save(update_fields=['status', 'tutorial_active', 'question_order'])
        self.quiz.selected_questions.set([question])

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': question.id,
            'selected_set_number': 1,
        })

        self.quiz.refresh_from_db()
        self.assertFalse(self.quiz.tutorial_active)

    def test_participant_rejoin_during_active_tutorial_receives_tutorial_start(self):
        hub_session = HubSession.objects.create(
            code='BJHUB1',
            name='Black Jack Tutorial Rejoin',
            is_active=True,
            started_at=timezone.now(),
        )
        HubParticipant.objects.create(session=hub_session, nickname='Alice')
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='blackjack',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=hub_session.code,
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.started_at = timezone.now()
        self.quiz.save(update_fields=['status', 'started_at'])
        activate_game_tutorial_runtime('blackjack', self.quiz.room_code, hub_session.code, self.quiz, True)

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })

        message_types = [message['type'] for message in self.direct_messages]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)

    def test_admin_end_question_broadcasts_question_ending_before_question_ended(self):
        question = BlackJackQuestion.objects.create(
            question_text='How many?',
            correct_answer=21,
            created_by=self.user,
        )
        self.quiz.status = 'active'
        self.quiz.question_order = [[question.id]]
        self.quiz.save(update_fields=['status', 'question_order'])
        self.quiz.selected_questions.set([question])
        self.quiz.session.send_question(question)

        async_to_sync(self.consumer.handle_admin_end_question)({})

        message_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]
        self.assertIn('question_ending', message_types)
        self.assertIn('question_ended', message_types)
        self.assertLess(message_types.index('question_ending'), message_types.index('question_ended'))

    def test_tutorial_set_answer_does_not_award_points_or_bust_player(self):
        hub_session = HubSession.objects.create(
            code='BJT1',
            name='Black Jack Tutorial Set Session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='blackjack',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        tutorial_question = BlackJackQuestion.objects.create(
            question_text='Tutorial question',
            correct_answer=10,
            created_by=self.user,
        )
        normal_question = BlackJackQuestion.objects.create(
            question_text='Scored question',
            correct_answer=21,
            created_by=self.user,
        )
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=hub_session.code,
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.started_at = timezone.now()
        self.quiz.total_questions = 1
        self.quiz.question_order = [[tutorial_question.id], [normal_question.id]]
        self.quiz.tutorial_set_number = 1
        self.quiz.save(update_fields=[
            'status',
            'started_at',
            'total_questions',
            'question_order',
            'tutorial_set_number',
        ])
        self.quiz.selected_questions.set([tutorial_question, normal_question])
        prepare_unit_tutorial_runtime('blackjack', self.quiz.room_code, hub_session.code, True)

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': normal_question.id,
            'selected_set_number': 2,
            'hub_session_code': hub_session.code,
        })

        question_started = [
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        ][-1]
        self.assertEqual(question_started['question']['id'], tutorial_question.id)
        self.assertTrue(question_started['question']['is_tutorial_round'])
        self.assertTrue(get_unit_tutorial_state('blackjack', self.quiz.room_code, hub_session.code)['current_unit_is_tutorial'])

        play_response = self.client.get(
            reverse('black_jack_quiz:play', args=[self.quiz.room_code, participant.name]),
            {'hub_session': hub_session.code},
        )
        self.assertEqual(play_response.status_code, 200)
        self.assertTrue(play_response.context['current_unit_is_tutorial'])
        self.assertEqual(
            [entry['set_number'] for entry in play_response.context['set_scoreboard']],
            [2],
        )
        self.assertContains(play_response, 'Tutorialset - keine Wertung')

        tutorial_result = async_to_sync(self.consumer.save_participant_answer)(
            participant.name,
            hub_session.code,
            100,
            1.0,
            question_id=tutorial_question.id,
        )

        participant.refresh_from_db()
        answer = BlackJackAnswer.objects.get(
            quiz=self.quiz,
            participant=participant,
            question=tutorial_question,
        )
        self.assertTrue(tutorial_result['is_tutorial_round'])
        self.assertEqual(tutorial_result['points_earned'], 0)
        self.assertEqual(answer.points_earned, 0)
        self.assertEqual(participant.total_points, 0)
        self.assertEqual(participant.overall_points, 0)
        self.assertEqual(participant.questions_answered, 0)
        self.assertFalse(participant.is_busted)

        async_to_sync(self.consumer.handle_admin_end_question)({'hub_session_code': hub_session.code})
        tutorial_state = get_unit_tutorial_state('blackjack', self.quiz.room_code, hub_session.code)
        self.assertTrue(tutorial_state['tutorial_has_been_played'])
        self.assertFalse(tutorial_state['current_unit_is_tutorial'])

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': normal_question.id,
            'selected_set_number': 2,
            'hub_session_code': hub_session.code,
        })

        scored_question_started = [
            message for _, message in self.consumer.channel_layer.group_messages
            if message['type'] == 'question_started'
        ][-1]
        self.assertEqual(scored_question_started['question']['id'], normal_question.id)
        self.assertFalse(scored_question_started['question']['is_tutorial_round'])

    def test_save_participant_answer_rejects_recently_ended_no_answer_bust(self):
        question_one = BlackJackQuestion.objects.create(
            question_text='Question 1',
            correct_answer=10,
            created_by=self.user,
        )
        question_two = BlackJackQuestion.objects.create(
            question_text='Question 2',
            correct_answer=20,
            created_by=self.user,
        )
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.total_questions = 2
        self.quiz.question_order = [[question_one.id, question_two.id]]
        self.quiz.save(update_fields=['status', 'total_questions', 'question_order'])
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.session.send_question(question_one)
        self.quiz.session.end_current_question()

        result = async_to_sync(self.consumer.save_participant_answer)(
            participant.name,
            participant.hub_session_code,
            11,
            1.2,
            question_id=question_one.id,
        )

        participant.refresh_from_db()
        self.assertIsNone(result)
        self.assertTrue(participant.is_busted)
        self.assertEqual(
            BlackJackAnswer.objects.filter(
                quiz=self.quiz,
                participant=participant,
                question=question_one,
            ).count(),
            0,
        )


class BlackJackConsumerSetProgressTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(username='blackjack-consumer-set-user', password='pass')
        self.quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Black Jack Consumer Set Quiz',
            room_code='BJSET',
            status='active',
            total_questions=4,
        )
        BlackJackSession.objects.create(quiz=self.quiz)
        self.consumer = BlackJackConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'blackjack_{self.quiz.room_code}'
        self.consumer.channel_layer = FakeChannelLayer()
        self.consumer.channel_name = 'blackjack-consumer-set-channel'

        async def _capture_send(*args, **kwargs):
            return None

        self.consumer.send = _capture_send

    def _create_set_questions(self, prefix, correct_answer):
        questions = [
            BlackJackQuestion.objects.create(
                question_text=f'{prefix} {index}',
                correct_answer=correct_answer(index),
                created_by=self.user,
            )
            for index in range(1, 5)
        ]
        self.quiz.question_order = [[question.id for question in questions]]
        self.quiz.save(update_fields=['question_order'])
        self.quiz.selected_questions.set(questions)
        return questions

    def _get_question_ended_message(self):
        return [
            message
            for _, message in self.consumer.channel_layer.group_messages
            if message.get('type') == 'question_ended'
        ][-1]

    def test_admin_end_question_keeps_four_question_set_open_after_first_question(self):
        questions = self._create_set_questions('Set Question', lambda index: index * 10)
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.session.send_question(questions[0])
        BlackJackAnswer.objects.create(
            quiz=self.quiz,
            participant=participant,
            question=self.quiz.current_question,
            user_answer=questions[0].correct_answer + 1,
        )

        async_to_sync(self.consumer.handle_admin_end_question)({})

        self.quiz.refresh_from_db()
        self.quiz.session.refresh_from_db()
        question_ended = self._get_question_ended_message()

        self.assertFalse(question_ended['set_complete'])
        self.assertFalse(self.quiz.session.is_set_complete(1))
        self.assertEqual(
            self.quiz.session.get_remaining_question_ids_for_set(1, active_only=True),
            [question.id for question in questions[1:]],
        )
        self.assertIsNone(
            self.quiz.get_next_question_send_error(
                questions[1].id,
                selected_set_number=1,
            )
        )

    def test_admin_end_question_keeps_set_open_after_first_bust(self):
        questions = self._create_set_questions('Bust Question', lambda index: 0)
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.session.send_question(questions[0])
        BlackJackAnswer.objects.create(
            quiz=self.quiz,
            participant=participant,
            question=self.quiz.current_question,
            user_answer=30,
        )

        async_to_sync(self.consumer.handle_admin_end_question)({})

        participant.refresh_from_db()
        self.quiz.refresh_from_db()
        self.quiz.session.refresh_from_db()
        question_ended = self._get_question_ended_message()

        self.assertFalse(question_ended['set_complete'])
        self.assertTrue(participant.is_busted)
        self.assertFalse(self.quiz.session.is_set_complete(1))
        self.assertEqual(len(self.quiz.session.get_remaining_question_ids_for_set(1, active_only=True)), 3)
        self.assertIsNone(
            self.quiz.get_next_question_send_error(
                questions[1].id,
                selected_set_number=1,
            )
        )

    def test_current_question_payload_uses_played_set_order_for_out_of_order_questions(self):
        questions = self._create_set_questions('Counter Question', lambda index: index * 10)

        self.quiz.session.send_question(questions[2])
        first_payload = async_to_sync(self.consumer.get_current_question_data)()

        self.assertEqual(first_payload['question_in_set'], 1)
        self.assertEqual(first_payload['set_question_count'], 4)

        self.quiz.session.end_current_question()
        self.quiz.session.send_question(questions[0])
        second_payload = async_to_sync(self.consumer.get_current_question_data)()

        self.assertEqual(second_payload['question_in_set'], 2)
        self.assertEqual(second_payload['set_question_count'], 4)

        self.quiz.session.end_current_question()
        self.quiz.session.send_question(questions[3])
        third_payload = async_to_sync(self.consumer.get_current_question_data)()

        self.assertEqual(third_payload['question_in_set'], 3)
        self.assertEqual(third_payload['set_question_count'], 4)

        self.quiz.session.end_current_question()
        self.quiz.session.send_question(questions[1])
        fourth_payload = async_to_sync(self.consumer.get_current_question_data)()

        self.assertEqual(fourth_payload['question_in_set'], 4)
        self.assertEqual(fourth_payload['set_question_count'], 4)

    def test_admin_end_question_broadcasts_no_answer_elimination(self):
        questions = self._create_set_questions('No Answer Question', lambda index: index * 10)
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.session.send_question(questions[0])

        async_to_sync(self.consumer.handle_admin_end_question)({})

        participant.refresh_from_db()
        question_ended = self._get_question_ended_message()

        self.assertTrue(participant.is_busted)
        self.assertEqual(question_ended['no_answer_bust_participants'][0]['name'], 'Alice')
        self.assertEqual(question_ended['no_answer_bust_participants'][0]['reason'], 'no_answer')
        self.assertFalse(question_ended['set_complete'])

    def test_participant_timeout_after_server_expiry_ends_question_and_busts_no_answer(self):
        questions = self._create_set_questions('Participant Timeout Question', lambda index: index * 10)
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.session.send_question(questions[0])
        self.quiz.session.question_end_time = timezone.now() - timedelta(seconds=1)
        self.quiz.session.save(update_fields=['question_end_time'])

        async_to_sync(self.consumer.handle_participant_question_timeout)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
            'question_id': questions[0].id,
        })

        participant.refresh_from_db()
        self.quiz.refresh_from_db()
        self.quiz.session.refresh_from_db()
        message_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]
        question_ended = self._get_question_ended_message()

        self.assertIn('question_ending', message_types)
        self.assertIn('question_ended', message_types)
        self.assertIsNone(self.quiz.current_question_id)
        self.assertFalse(self.quiz.session.is_question_active)
        self.assertIsNone(self.quiz.session.question_end_time)
        self.assertTrue(participant.is_busted)
        self.assertEqual(question_ended['no_answer_bust_participants'][0]['name'], 'Alice')
        self.assertEqual(question_ended['no_answer_bust_participants'][0]['reason'], 'no_answer')

    def test_participant_timeout_before_server_expiry_does_not_end_question(self):
        questions = self._create_set_questions('Early Participant Timeout Question', lambda index: index * 10)
        participant = BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.session.send_question(questions[0])
        self.quiz.session.question_end_time = timezone.now() + timedelta(seconds=30)
        self.quiz.session.save(update_fields=['question_end_time'])

        async_to_sync(self.consumer.handle_participant_question_timeout)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
            'question_id': questions[0].id,
        })

        participant.refresh_from_db()
        self.quiz.refresh_from_db()
        self.quiz.session.refresh_from_db()
        message_types = [message['type'] for _, message in self.consumer.channel_layer.group_messages]

        self.assertNotIn('question_ending', message_types)
        self.assertNotIn('question_ended', message_types)
        self.assertEqual(self.quiz.current_question_id, questions[0].id)
        self.assertTrue(self.quiz.session.is_question_active)
        self.assertFalse(participant.is_busted)

    def test_participant_join_ignores_stale_runtime_for_unstarted_hub_session(self):
        questions = self._create_set_questions('Join Question', lambda index: index * 10)
        stale_started_at = timezone.now() - timedelta(days=1, minutes=9)
        self.quiz.started_at = stale_started_at
        self.quiz.current_question = questions[0]
        self.quiz.current_question_number = 1
        self.quiz.question_start_time = stale_started_at
        self.quiz.save(update_fields=[
            'started_at',
            'current_question',
            'current_question_number',
            'question_start_time',
        ])
        self.quiz.session.current_question_number = 1
        self.quiz.session.total_questions_sent = 1
        self.quiz.session.completed_sets_count = 1
        self.quiz.session.selected_set_number = 1
        self.quiz.session.asked_question_ids = [questions[0].id]
        self.quiz.session.finalized_set_numbers = [1]
        self.quiz.session.is_question_active = True
        self.quiz.session.question_end_time = timezone.now() + timedelta(seconds=30)
        self.quiz.session.save(update_fields=[
            'current_question_number',
            'total_questions_sent',
            'completed_sets_count',
            'selected_set_number',
            'asked_question_ids',
            'finalized_set_numbers',
            'is_question_active',
            'question_end_time',
        ])
        hub_session = HubSession.objects.create(
            code='BJJOINNEW',
            name='BJJOINNEW',
            is_active=True,
        )
        BlackJackParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=hub_session.code,
            is_active=True,
        )
        sent_messages = []

        async def _capture_send(text_data=None, bytes_data=None):
            if text_data:
                sent_messages.append(json.loads(text_data))

        self.consumer.send = _capture_send

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': 'Alice',
            'hub_session': hub_session.code,
        })

        self.quiz.refresh_from_db()
        self.quiz.session.refresh_from_db()
        sent_types = [message.get('type') for message in sent_messages]

        self.assertEqual(self.quiz.status, 'waiting')
        self.assertIsNone(self.quiz.started_at)
        self.assertIsNone(self.quiz.current_question_id)
        self.assertEqual(self.quiz.current_question_number, 0)
        self.assertIsNone(self.quiz.question_start_time)
        self.assertEqual(self.quiz.session.current_question_number, 0)
        self.assertEqual(self.quiz.session.total_questions_sent, 0)
        self.assertEqual(self.quiz.session.completed_sets_count, 0)
        self.assertEqual(self.quiz.session.asked_question_ids, [])
        self.assertEqual(self.quiz.session.finalized_set_numbers, [])
        self.assertFalse(self.quiz.session.is_question_active)
        self.assertIsNone(self.quiz.session.question_end_time)
        self.assertNotIn('quiz_started', sent_types)
        self.assertNotIn('question_started', sent_types)
