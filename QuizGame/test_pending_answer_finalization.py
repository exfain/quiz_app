from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .consumers import QuizConsumer
from .models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion


User = get_user_model()


class QuizPendingAnswerFinalizationTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='quick_quiz_player', password='testpass123')
        self.quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='active',
            started_at=timezone.now(),
        )
        self.question = QuizQuestion.objects.create(
            question_text='Capital of Germany?',
            question_type='short_answer',
            correct_answer='Berlin',
            points=10,
            created_by=self.user,
        )
        self.quiz.current_question = self.question
        self.quiz.question_start_time = timezone.now() - timezone.timedelta(seconds=5)
        self.quiz.save(update_fields=['current_question', 'question_start_time'])
        self.participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            is_active=True,
        )
        self.consumer = QuizConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'quiz_{self.quiz.room_code}'

    def test_recently_ended_typed_answer_is_evaluated_after_question_end(self):
        async_to_sync(self.consumer.mark_recently_ended_question)(self.question.id)
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        self.quiz.save(update_fields=['current_question', 'question_start_time'])

        result = async_to_sync(self.consumer.save_participant_answer)(
            'Alice',
            None,
            'Berlin',
            4.2,
            question_id=self.question.id,
            allow_recently_ended=True,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result['is_correct'])
        self.assertEqual(result['points_earned'], 10)
        self.assertTrue(
            QuizAnswer.objects.filter(
                quiz=self.quiz,
                participant=self.participant,
                question=self.question,
            ).exists()
        )
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, 10)

    def test_finalize_answer_handler_keeps_logged_answers_unchanged(self):
        initial = async_to_sync(self.consumer.save_participant_answer)(
            'Alice',
            None,
            'Berlin',
            2.0,
        )
        self.assertIsNotNone(initial)

        async_to_sync(self.consumer.mark_recently_ended_question)(self.question.id)
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        self.quiz.save(update_fields=['current_question', 'question_start_time'])

        self.consumer.send = AsyncMock()
        self.consumer.channel_layer = type('ChannelLayerStub', (), {'group_send': AsyncMock()})()

        async_to_sync(self.consumer.handle_participant_finalize_answer)({
            'participant_name': 'Alice',
            'hub_session': None,
            'question_id': self.question.id,
            'answer': 'Berlin',
            'time_taken': 4.5,
        })

        self.assertEqual(
            QuizAnswer.objects.filter(
                quiz=self.quiz,
                participant=self.participant,
                question=self.question,
            ).count(),
            1,
        )
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, 10)
        self.consumer.send.assert_not_awaited()
        self.consumer.channel_layer.group_send.assert_not_awaited()

    def test_quiz_play_page_contains_pending_answer_finalization_path(self):
        response = self.client.get(reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'participant_finalize_answer')
        self.assertContains(response, 'finalizePendingAnswerOnQuestionEnd')
        self.assertContains(response, 'answerSubmissionConfirmed')

    def test_quiz_play_page_keeps_answer_review_visible_after_question_end(self):
        response = self.client.get(reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="revealedQuestionText"')

        content = response.content.decode()
        show_correct_section = content.split('showCorrectAnswer(correctAnswerData) {', 1)[1].split('bindEvents() {', 1)[0]
        question_end_section = content.split('onQuestionEnded(data) {', 1)[1].split('onQuizEnded(data) {', 1)[0]
        on_time_up_section = content.split('onTimeUp() {', 1)[1].split('showWaitingForNextQuestion() {', 1)[0]

        self.assertIn('revealedQuestionText', show_correct_section)
        self.assertNotIn('setTimeout(() => {', show_correct_section)
        self.assertIn('this.showCorrectAnswer(data.correct_answer);', question_end_section)
        self.assertNotIn('this.showWaitingForNextQuestion();', question_end_section)
        self.assertNotIn('this.showWaitingForNextQuestion();', on_time_up_section)

    def test_quiz_play_page_resets_and_replaces_previous_pending_answer_display(self):
        response = self.client.get(reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)

        content = response.content.decode()
        show_correct_section = content.split('showCorrectAnswer(correctAnswerData) {', 1)[1].split('bindEvents() {', 1)[0]
        question_started_section = content.split('onQuestionStarted(question, timeLimit) {', 1)[1].split('onQuestionEnded(data) {', 1)[0]

        self.assertIn('const userAnswer = this.lastSubmittedAnswerDisplay', show_correct_section)
        self.assertIn("if (submittedAnswerText) submittedAnswerText.textContent = '';", question_started_section)
