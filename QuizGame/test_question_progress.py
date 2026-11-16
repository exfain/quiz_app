import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from games_hub.authoritative_state import (
    current_snapshot, finish_question_flow, open_answering,
    reset_question_flow, reveal_question_content,
)
from games_hub.models import GameRuntimeState, HubGameStep, HubSession
from games_hub.unit_tutorial_runtime import (
    finish_current_unit_tutorial, prepare_unit_tutorial_runtime,
    start_unit_tutorial_if_needed,
)
from .consumers import QuizConsumer
from .models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion, QuizSession


class QuickQuizQuestionProgressTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='progress-host')
        self.hub = HubSession.objects.create(code='COUNT5', is_active=True, started_at=timezone.now())
        self.quiz = Quiz.objects.create(
            creator=self.user, title='Question progress', status='active', started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=self.hub, order=0, game_key='quiz', room_code=self.quiz.room_code,
        )
        self.questions = [QuizQuestion.objects.create(
            created_by=self.user, question_text=f'Question {index}',
            question_type='true_false', correct_answer='True',
        ) for index in range(1, 6)]
        self.quiz.selected_questions.set(self.questions)
        self.quiz.question_order = [question.id for question in self.questions]
        self.quiz.save(update_fields=['question_order'])
        self.participant = QuizParticipant.objects.create(
            quiz=self.quiz, name='Team', hub_session_code=self.hub.code,
        )
        self.session = QuizSession.objects.create(quiz=self.quiz)
        for question in self.questions[:2]:
            self.session.present_question(question)
            self.session.end_current_question()
        reset_question_flow(
            game_key='quiz', room_code=self.quiz.room_code, session_code=self.hub.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        self.consumer = QuizConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'quiz_{self.quiz.room_code}'
        self.consumer.send = AsyncMock()
        self.consumer.channel_layer = type('ChannelStub', (), {'group_send': AsyncMock()})()

    def _action(self, question):
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.hub.code)
        return {
            'question_id': question.id, 'hub_session': self.hub.code,
            'game_id': snapshot['game_id'], 'state_revision': snapshot['state_revision'],
            'client_action_id': str(uuid.uuid4()),
        }

    def _present(self, question):
        decision = async_to_sync(self.consumer.present_quiz_question)(
            self.quiz.id, question.id, self.hub.code, self._action(question),
        )
        self.assertTrue(decision.accepted)

    def _assert_views(self, current, *, phase='question'):
        game = self.client.get(reverse(
            'games_hub:spectate_session_state', args=[self.hub.code],
        )).json()['game']
        self.assertEqual(game['phase'], phase)
        self.assertEqual(game['progress'], {'kind': 'question', 'current': current, 'total': 5})
        response = self.client.get(
            reverse('quiz:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.hub.code},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_number'], current)
        self.assertEqual(response.context['total_question_count'], 5)
        label = f'{current}/5' if current else ''
        self.assertContains(response, f'<span id="vhsQuizProgress" hidden aria-hidden="true">{label}</span>', html=True)
        return game

    def test_third_sent_question_uses_three_of_five_even_without_previous_answers(self):
        self._present(self.questions[4])
        self._assert_views(3)
        payload = async_to_sync(self.consumer.get_current_question_data)(self.hub.code)
        self.assertEqual((payload['question_number'], payload['total_questions']), (3, 5))
        self.assertEqual(QuizAnswer.objects.count(), 0)

    def test_unanswered_resolution_and_rejoin_keep_third_question_number(self):
        question = self.questions[4]
        self._present(question)
        snapshot = current_snapshot('quiz', self.quiz.room_code, self.hub.code)
        visible_at = parse_datetime(snapshot['question_visible_at']) + timedelta(seconds=1)
        revealed = reveal_question_content(
            game_key='quiz', room_code=self.quiz.room_code, session_code=self.hub.code,
            action=self._action(question), at=visible_at,
        )
        self.assertTrue(revealed.accepted)
        opened = open_answering(
            game_key='quiz', room_code=self.quiz.room_code, session_code=self.hub.code,
            action=self._action(question), answer_duration_seconds=30, at=visible_at,
        )
        self.assertTrue(opened.accepted)
        self.session.refresh_from_db()
        self.session.open_answering(question, started_at=visible_at, answer_duration_seconds=30)
        result = async_to_sync(self.consumer.finalize_pending_answers_and_close_question)(self.quiz.id, self.hub.code)
        async_to_sync(self.consumer.question_ended)({'message': 'Question ended', **result})
        forwarded = json.loads(self.consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(forwarded['question_number'], 3)
        finish_question_flow(
            game_key='quiz', room_code=self.quiz.room_code, session_code=self.hub.code,
            question_id=question.id,
        )
        game = self._assert_views(3, phase='reveal')
        self.assertEqual(game['question']['id'], question.id)
        self.assertEqual(QuizAnswer.objects.count(), 0)
        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name, 'hub_session': self.hub.code,
        })
        messages = [json.loads(call.kwargs['text_data']) for call in self.consumer.send.await_args_list]
        started = next(message for message in messages if message['type'] == 'quiz_started')
        self.assertEqual(started['question_number'], 3)

    def test_prepared_question_and_rejoin_use_next_sent_number(self):
        question = self.questions[4]
        async_to_sync(self.consumer.handle_admin_prepare_question)(self._action(question))
        event = self.consumer.channel_layer.group_send.await_args.args[1]
        self.assertEqual((event['question_number'], event['total_questions']), (3, 5))
        async_to_sync(self.consumer.question_prepared)(event)
        forwarded = json.loads(self.consumer.send.await_args.kwargs['text_data'])
        self.assertEqual((forwarded['question_number'], forwarded['total_questions']), (3, 5))
        self._assert_views(3, phase='question_prepared')
        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name, 'hub_session': self.hub.code,
        })
        messages = [json.loads(call.kwargs['text_data']) for call in self.consumer.send.await_args_list]
        prepared = next(message for message in messages if message['type'] == 'question_prepared')
        self.assertEqual((prepared['question_number'], prepared['total_questions']), (3, 5))

    def test_cleared_preparation_removes_counter_in_both_views_and_on_rejoin(self):
        question = self.questions[4]
        async_to_sync(self.consumer.handle_admin_prepare_question)(self._action(question))
        async_to_sync(self.consumer.handle_admin_clear_prepared_question)(self._action(question))
        event = self.consumer.channel_layer.group_send.await_args.args[1]
        async_to_sync(self.consumer.question_preparation_cleared)(event)
        forwarded = json.loads(self.consumer.send.await_args.kwargs['text_data'])
        self.assertIsNone(forwarded['question_number'])
        self.assertEqual(forwarded['total_questions'], 5)
        self._assert_views(None, phase='question_prepared')
        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name, 'hub_session': self.hub.code,
        })
        started = next(json.loads(call.kwargs['text_data'])
                       for call in self.consumer.send.await_args_list
                       if json.loads(call.kwargs['text_data'])['type'] == 'quiz_started')
        self.assertIsNone(started['question_number'])

    def test_join_uses_current_counter_when_host_prepares_question_during_join(self):
        action = self._action(self.questions[4])

        async def prepare_during_join(_hub_session_code):
            await self.consumer.handle_admin_prepare_question(action)
            return None

        self.consumer.get_current_question_data = AsyncMock(side_effect=prepare_during_join)
        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name, 'hub_session': self.hub.code,
        })
        messages = [json.loads(call.kwargs['text_data']) for call in self.consumer.send.await_args_list]
        prepared = next(message for message in messages if message['type'] == 'question_prepared')
        self.assertEqual((prepared['question_number'], prepared['total_questions']), (3, 5))

    def test_tutorial_is_not_a_scored_question_or_part_of_total(self):
        tutorial = QuizQuestion.objects.create(
            created_by=self.user, question_text='Tutorial', question_type='true_false', correct_answer='True',
        )
        self.quiz.tutorial_question = tutorial
        self.quiz.save(update_fields=['tutorial_question'])
        self.quiz.selected_questions.add(tutorial)
        self.quiz.question_order.insert(0, tutorial.id)
        self.quiz.save(update_fields=['question_order'])
        self.session.current_question_number = 0
        self.session.total_questions_sent = 0
        self.session.save(update_fields=['current_question_number', 'total_questions_sent'])
        prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, self.hub.code, True)
        start_unit_tutorial_if_needed('quiz', self.quiz.room_code, self.hub.code)
        self._present(tutorial)
        self._assert_views(None)
        self.session.refresh_from_db()
        self.session.end_current_question()
        finish_current_unit_tutorial('quiz', self.quiz.room_code, self.hub.code)
        reset_question_flow(
            game_key='quiz', room_code=self.quiz.room_code, session_code=self.hub.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        for question in self.questions[:2]:
            self.session.present_question(question)
            self.session.end_current_question()
        self._present(self.questions[4])
        self._assert_views(3)

    def test_skipped_tutorial_does_not_offset_the_actual_sent_number(self):
        tutorial = QuizQuestion.objects.create(
            created_by=self.user, question_text='Skipped tutorial',
            question_type='true_false', correct_answer='True',
        )
        self.quiz.tutorial_question = tutorial
        self.quiz.save(update_fields=['tutorial_question'])
        self.quiz.selected_questions.add(tutorial)
        prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, self.hub.code, False)
        self._present(self.questions[4])
        self._assert_views(3)
