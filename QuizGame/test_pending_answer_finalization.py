import json
from datetime import timedelta
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .consumers import QuizConsumer
from .models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion, QuizSession


User = get_user_model()


class QuizAnswerDeadlineTest(TransactionTestCase):
    def setUp(self):
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
        self.quiz.question_start_time = timezone.now() - timedelta(seconds=5)
        self.quiz.save(update_fields=['current_question', 'question_start_time'])
        self.session = QuizSession.objects.create(
            quiz=self.quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timedelta(seconds=25),
        )
        self.participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            is_active=True,
        )
        self.consumer = QuizConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'quiz_{self.quiz.room_code}'
        self.consumer.send = AsyncMock()
        self.consumer.channel_layer = type('ChannelLayerStub', (), {'group_send': AsyncMock()})()

    def submit(self, question_id=None):
        async_to_sync(self.consumer.handle_participant_submit_answer)({
            'participant_name': self.participant.name,
            'hub_session': None,
            'question_id': self.question.id if question_id is None else question_id,
            'answer': 'Berlin',
            'time_taken': 4.2,
        })

    def select(self, answer='Berlin'):
        async_to_sync(self.consumer.handle_participant_update_pending_answer)({
            'participant_name': self.participant.name,
            'hub_session': None,
            'question_id': self.question.id,
            'answer': answer,
        })

    def end_question(self):
        async_to_sync(self.consumer.handle_admin_end_question)({})

    def question_ended_event(self):
        return next(
            call.args[1]
            for call in self.consumer.channel_layer.group_send.await_args_list
            if call.args[1].get('type') == 'question_ended'
        )

    def direct_messages(self):
        return [
            json.loads(call.kwargs['text_data'])
            for call in self.consumer.send.await_args_list
        ]

    def test_answer_before_authoritative_deadline_is_accepted(self):
        self.select()
        self.submit()

        self.assertEqual(QuizAnswer.objects.filter(quiz=self.quiz).count(), 1)
        self.assertEqual(self.direct_messages()[0]['type'], 'answer_submitted')
        self.assertEqual(self.direct_messages()[0]['question_id'], self.question.id)
        self.assertEqual(self.direct_messages()[0]['display_answer'], 'Berlin')
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_answers, {})

        self.end_question()
        self.assertEqual(self.question_ended_event()['auto_finalized_answers'], [])

    def test_answer_after_authoritative_deadline_is_rejected(self):
        self.session.question_end_time = timezone.now() - timedelta(milliseconds=1)
        self.session.save(update_fields=['question_end_time'])

        self.submit()

        self.assertFalse(QuizAnswer.objects.filter(quiz=self.quiz).exists())
        self.assertEqual(self.direct_messages()[0]['type'], 'answer_rejected')

    def test_manual_submit_just_before_deadline_is_accepted_and_not_auto_finalized(self):
        self.select()
        self.session.question_end_time = timezone.now() + timedelta(seconds=1)
        self.session.save(update_fields=['question_end_time'])

        self.submit()
        self.end_question()

        self.assertEqual(self.direct_messages()[0]['type'], 'answer_submitted')
        self.assertEqual(QuizAnswer.objects.filter(quiz=self.quiz).count(), 1)
        self.assertEqual(self.question_ended_event()['auto_finalized_answers'], [])

    def test_selected_answer_is_finalized_silently_when_timer_ends(self):
        self.select()

        self.end_question()

        answer = QuizAnswer.objects.get(quiz=self.quiz, participant=self.participant, question=self.question)
        event = self.question_ended_event()
        self.assertEqual(answer.answer_text, 'Berlin')
        self.assertTrue(answer.is_correct)
        self.assertEqual(len(event['auto_finalized_answers']), 1)
        self.assertEqual(event['auto_finalized_answers'][0]['display_answer'], 'Berlin')
        self.assertEqual(self.direct_messages(), [])

    def test_selected_answer_is_finalized_silently_on_early_host_end(self):
        self.session.question_end_time = timezone.now() + timedelta(minutes=5)
        self.session.save(update_fields=['question_end_time'])
        self.select()

        self.end_question()

        self.assertEqual(QuizAnswer.objects.filter(quiz=self.quiz).count(), 1)
        self.assertEqual(len(self.question_ended_event()['auto_finalized_answers']), 1)
        self.assertEqual(self.direct_messages(), [])

    def test_no_selection_remains_unanswered_at_question_end(self):
        self.end_question()

        self.assertFalse(QuizAnswer.objects.filter(quiz=self.quiz).exists())
        event = self.question_ended_event()
        self.assertEqual(event['auto_finalized_answers'], [])
        self.assertEqual(event['answer_results'], [])
        self.assertEqual(self.direct_messages(), [])

    def test_no_selection_remains_unanswered_on_early_host_end(self):
        self.session.question_end_time = timezone.now() + timedelta(minutes=5)
        self.session.save(update_fields=['question_end_time'])

        self.end_question()

        self.assertFalse(QuizAnswer.objects.filter(quiz=self.quiz).exists())
        self.assertEqual(self.question_ended_event()['auto_finalized_answers'], [])

    def test_late_manual_submit_falls_back_to_previous_server_side_selection(self):
        self.select()
        self.session.question_end_time = timezone.now() - timedelta(milliseconds=1)
        self.session.save(update_fields=['question_end_time'])

        self.submit()
        self.end_question()

        self.assertEqual(self.direct_messages()[0]['type'], 'answer_rejected')
        self.assertEqual(QuizAnswer.objects.filter(quiz=self.quiz).count(), 1)
        self.assertEqual(len(self.question_ended_event()['auto_finalized_answers']), 1)

    def test_answer_after_question_end_is_rejected(self):
        self.session.is_question_active = False
        self.session.question_end_time = None
        self.session.save(update_fields=['is_question_active', 'question_end_time'])
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        self.quiz.save(update_fields=['current_question', 'question_start_time'])

        self.submit()

        self.assertFalse(QuizAnswer.objects.filter(quiz=self.quiz).exists())
        self.assertEqual(self.direct_messages()[0]['type'], 'answer_rejected')

    def test_duplicate_submit_creates_one_answer_and_one_confirmation(self):
        self.select()
        self.submit()
        self.submit()

        self.assertEqual(QuizAnswer.objects.filter(quiz=self.quiz).count(), 1)
        self.assertEqual(
            [message['type'] for message in self.direct_messages()],
            ['answer_submitted', 'answer_rejected'],
        )
        self.assertEqual(self.consumer.channel_layer.group_send.await_count, 1)

        self.end_question()
        self.participant.refresh_from_db()
        self.assertEqual(QuizAnswer.objects.filter(quiz=self.quiz).count(), 1)
        self.assertEqual(self.participant.total_score, 1)
        self.assertEqual(self.question_ended_event()['auto_finalized_answers'], [])

    def test_submit_for_previous_question_is_rejected(self):
        next_question = QuizQuestion.objects.create(
            question_text='Next question?',
            question_type='short_answer',
            correct_answer='Next',
            created_by=self.user,
        )
        self.quiz.current_question = next_question
        self.quiz.question_start_time = timezone.now()
        self.quiz.save(update_fields=['current_question', 'question_start_time'])

        self.submit(question_id=self.question.id)

        self.assertFalse(QuizAnswer.objects.filter(quiz=self.quiz).exists())
        self.assertEqual(self.direct_messages()[0]['type'], 'answer_rejected')

    def test_rejoin_restores_only_the_server_confirmed_submitted_state(self):
        self.submit()
        self.consumer.send.reset_mock()
        self.consumer.channel_layer.group_send.reset_mock()

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': None,
        })

        messages = self.direct_messages()
        self.assertEqual(
            [message['type'] for message in messages],
            ['quiz_started', 'question_started', 'answer_submitted'],
        )
        self.assertEqual(messages[-1]['question_id'], self.question.id)
        self.assertEqual(messages[-1]['display_answer'], 'Berlin')
        self.assertEqual(QuizAnswer.objects.filter(quiz=self.quiz).count(), 1)

    def test_rejoin_after_automatic_finalization_restores_reveal_not_submitted(self):
        self.select()
        self.end_question()
        self.consumer.send.reset_mock()
        self.consumer.channel_layer.group_send.reset_mock()

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': None,
        })

        messages = self.direct_messages()
        self.assertEqual([message['type'] for message in messages], ['quiz_started', 'question_ended'])
        self.assertEqual(messages[-1]['correct_answer']['question_id'], self.question.id)
        self.assertEqual(messages[-1]['answer_results'][0]['display_answer'], 'Berlin')

    def test_quiz_play_requires_server_confirmation_before_submitted_screen(self):
        response = self.client.get(reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        submit_section = content.split('submitAnswer() {', 1)[1].split('onTimeUp() {', 1)[0]
        timeout_section = content.split('onTimeUp() {', 1)[1].split('showWaitingForNextQuestion() {', 1)[0]
        confirmation_section = content.split('onAnswerSubmitted(data) {', 1)[1].split('onAnswerRejected(data) {', 1)[0]

        self.assertIn('question_id: questionId', submit_section)
        self.assertIn('this.submissionPending = true;', submit_section)
        self.assertNotIn("this.showState('answerSubmittedState')", submit_section)
        self.assertNotIn('setTimeout(', submit_section)
        self.assertNotIn('this.submitAnswer()', timeout_section)
        self.assertIn("this.showState('answerSubmittedState')", confirmation_section)
        self.assertContains(response, "type: 'participant_update_pending_answer'")
        self.assertContains(response, 'this.syncPendingAnswer();')

    def test_quiz_play_gives_reveal_priority_over_late_confirmation(self):
        response = self.client.get(reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        confirmation_section = content.split('onAnswerSubmitted(data) {', 1)[1].split('onAnswerRejected(data) {', 1)[0]
        question_end_section = content.split('onQuestionEnded(data) {', 1)[1].split('onQuizEnded(data) {', 1)[0]

        self.assertIn('if (this.revealedQuestionId === questionId)', confirmation_section)
        self.assertIn('this.revealedQuestionId = this.recentlyEndedQuestionId;', question_end_section)
        self.assertNotIn("this.showState('answerSubmittedState')", question_end_section)
        self.assertNotIn('finalizePendingAnswerOnQuestionEnd', content)
        self.assertNotIn('participant_finalize_answer', content)

    def test_quiz_play_keeps_answer_review_visible_after_question_end(self):
        response = self.client.get(reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="revealedQuestionText"')
        content = response.content.decode()
        question_end_section = content.split('onQuestionEnded(data) {', 1)[1].split('onQuizEnded(data) {', 1)[0]

        self.assertIn('this.showCorrectAnswer(data.correct_answer);', question_end_section)
        self.assertNotIn('this.showWaitingForNextQuestion();', question_end_section)
