import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_state import current_snapshot
from games_hub.models import HubParticipant, HubSession
from games_hub.quiz_pause import pause, resume
from games_hub.quiz_rounds import current_round, progress
from games_hub.spectator import _serialize_who_that
from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime, start_unit_tutorial_if_needed
from .models import WhoThatAnswer, WhoThatParticipant, WhoThatQuestion
from .runtime import end_question, present, reveal_state, save_answer, save_pending
from . import tests as fixtures
from .views import _build_question_status_board


class WhoThatRepairTests(TransactionTestCase):
    setUp = fixtures.WhoThatAutomaticQuestionStartTests.setUp

    def start(self, question=None, age=3):
        question = question or self.question
        snapshot = current_snapshot('who_that', self.quiz.room_code, self.hub.code)
        action = {'question_id': question.pk, 'game_id': str(self.quiz.pk),
                  'state_revision': snapshot['state_revision'], 'client_action_id': str(uuid.uuid4())}
        result = present(self.quiz.room_code, self.quiz.pk, question.pk, self.hub.code,
                         action, question.time_limit, at=timezone.now() - timedelta(seconds=age))
        self.assertTrue(result.accepted, result.message)
        self.quiz.refresh_from_db()
        self.quiz_session.refresh_from_db()
        return result

    def save(self, value=None, participant=None):
        participant = participant or self.participant
        return save_answer(self.quiz.room_code, participant.name, participant.hub_session_code,
                           value or self.question.correct_answer, 0, self.quiz.current_question_id)

    def test_expired_host_recovers_once_and_has_return_button(self):
        self.start(age=50)
        self.client.force_login(self.host)
        url = reverse('admin_dashboard:who_that_monitor', args=[self.quiz.room_code])
        response = self.client.get(url, {'hub_session': self.hub.code})
        self.assertContains(response, 'id="backToQuestionOverviewBtn"')
        self.quiz.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)
        self.assertIsNone(end_question(self.quiz.pk, self.hub.code))
        self.assertContains(self.client.get(url, {'hub_session': self.hub.code}), 'id="questionSelection"')

    def test_player_snapshot_does_not_advance_expired_question(self):
        self.start(age=50)
        self.consumer.send = AsyncMock()
        async_to_sync(self.consumer.send_current_question_state)(self.hub.code, self.participant.pk)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.question.pk)

    def test_repeat_has_own_answer_score_and_progress(self):
        self.start()
        self.assertIsNotNone(self.save())
        end_question(self.quiz.pk, self.hub.code)
        self.start()
        self.assertIsNotNone(self.save())
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, 2)
        self.assertEqual(list(WhoThatAnswer.objects.order_by('round_number').values_list('round_number', flat=True)), [1, 2])
        self.assertEqual(progress(self.quiz, 'who_that', self.hub.code, question_id=self.question.pk),
                         {'kind': 'question', 'current': 2, 'total': 2})
        board, number = _build_question_status_board(self.quiz, self.participant, self.hub.code)
        self.assertEqual(number, 2)
        self.assertEqual([row['result'] for row in board], ['correct', None])

    def test_old_round_end_and_old_draft_cannot_affect_repeat(self):
        self.start()
        first_round = current_round(self.quiz)
        end_question(self.quiz.pk, self.hub.code)
        self.start()
        self.assertIsNone(end_question(self.quiz.pk, self.hub.code,
            question_id=self.question.pk, round_number=first_round))
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.question.pk)
        self.quiz_session.pending_answers = {str(self.participant.pk): {
            'question_id': self.question.pk, 'round_number': first_round,
            'user_answer': self.question.correct_answer, 'hub_session_code': self.hub.code,
            'updated_at': timezone.now().isoformat(),
        }}
        self.quiz_session.save()
        end_question(self.quiz.pk, self.hub.code)
        self.assertFalse(WhoThatAnswer.objects.exists())

    def test_unanswered_out_of_order_rounds_have_consistent_counters(self):
        questions = [WhoThatQuestion.objects.create(created_by=self.host, correct_answer=str(i), image='test.png') for i in range(4)]
        self.quiz.selected_questions.add(*questions)
        for question in [questions[2], questions[0]]:
            self.start(question)
            end_question(self.quiz.pk, self.hub.code)
        self.start(questions[3])
        board, number = _build_question_status_board(self.quiz, self.participant, self.hub.code)
        payload = async_to_sync(self.consumer.get_current_question_state)(self.hub.code)
        spectator = _serialize_who_that(self.quiz, self.hub)
        self.assertEqual(number, 3)
        self.assertEqual(next(row['number'] for row in board if row['is_current']), 3)
        self.assertEqual(payload['question']['question_number'], 3)
        self.assertEqual(spectator['progress'], {'kind': 'question', 'current': 3, 'total': 5})

    def tutorial(self):
        self.quiz.tutorial_question = self.question
        self.quiz.save(update_fields=['tutorial_question'])
        prepare_unit_tutorial_runtime('who_that', self.quiz.room_code, self.hub.code, True)
        start_unit_tutorial_if_needed('who_that', self.quiz.room_code, self.hub.code)
        self.start()

    def test_tutorial_confirmed_answer_has_no_points_even_after_correction_save(self):
        self.tutorial()
        self.assertEqual(self.save()['points_earned'], 0)
        answer = WhoThatAnswer.objects.get()
        answer.save()
        self.participant.refresh_from_db()
        self.assertEqual(answer.points_earned, 0)
        self.assertEqual(self.participant.total_score, 0)

    def test_tutorial_draft_has_no_points_and_first_real_question_is_one(self):
        self.tutorial()
        self.assertTrue(save_pending(self.quiz.room_code, 'Alice', self.hub.code, self.question.correct_answer, self.question.pk))
        end_question(self.quiz.pk, self.hub.code)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, 0)
        question = WhoThatQuestion.objects.create(created_by=self.host, correct_answer='Grace', image='test.png')
        self.quiz.selected_questions.add(question)
        self.start(question)
        self.assertEqual(progress(self.quiz, 'who_that', self.hub.code, question_id=question.pk)['current'], 1)

    def test_manual_total_survives_later_answers(self):
        self.start()
        self.save()
        self.client.force_login(self.host)
        response = self.client.post(reverse('admin_dashboard:set_who_that_participant_score'),
            json.dumps({'participant_id': self.participant.pk, 'score': 7}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        end_question(self.quiz.pk, self.hub.code)
        self.start()
        self.save()
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, 8)

    def test_pause_freezes_deadline_and_preserves_draft_on_resume(self):
        self.start()
        save_pending(self.quiz.room_code, 'Alice', self.hub.code, self.question.correct_answer, self.question.pk)
        now = timezone.now()
        old_deadline = self.quiz_session.question_end_time
        self.assertTrue(pause(self.quiz, 'who_that', self.hub.code, at=now))
        self.assertFalse(current_snapshot('who_that', self.quiz.room_code, self.hub.code)['answering_allowed'])
        self.assertIsNone(self.save())
        self.assertTrue(resume(self.quiz, 'who_that', self.hub.code, at=now + timedelta(seconds=60)))
        self.quiz.refresh_from_db()
        self.quiz_session.refresh_from_db()
        self.assertEqual(self.quiz_session.question_end_time, old_deadline + timedelta(seconds=60))
        self.assertEqual(current_round(self.quiz), 1)
        with patch('who_is_that.runtime.timezone.now', return_value=now + timedelta(seconds=61)):
            end_question(self.quiz.pk, self.hub.code)
        self.assertEqual(WhoThatAnswer.objects.get().points_earned, 1)

    def test_pause_before_presentation_keeps_content_hidden_after_visibility_time(self):
        self.start(age=0)
        now = timezone.now()
        self.assertTrue(pause(self.quiz, 'who_that', self.hub.code, at=now))
        from games_hub.authoritative_state import question_content_is_visible
        snapshot = current_snapshot('who_that', self.quiz.room_code, self.hub.code)
        self.assertFalse(question_content_is_visible(snapshot, at=now + timedelta(minutes=1)))
        state = async_to_sync(self.consumer.get_current_question_state)(self.hub.code)
        self.assertTrue(state['question']['paused'])
        self.assertNotIn('image_url', state['question'])

    def test_http_send_opens_persisted_answer_window_and_end_scores_draft(self):
        self.client.force_login(self.host)
        response = self.client.post(reverse('admin_dashboard:send_who_that_question', args=[self.quiz.room_code]),
            json.dumps({'question_id': self.question.pk, 'hub_session': self.hub.code}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.quiz.refresh_from_db()
        self.quiz_session.refresh_from_db()
        self.assertTrue(self.quiz_session.is_question_active)
        self.assertIsNotNone(self.quiz_session.question_end_time)
        with patch('who_is_that.runtime.timezone.now', return_value=self.quiz.question_start_time + timedelta(seconds=1)):
            self.assertTrue(save_pending(self.quiz.room_code, 'Alice', self.hub.code, self.question.correct_answer, self.question.pk))
            response = self.client.post(reverse('admin_dashboard:end_who_that_question', args=[self.quiz.room_code]),
                json.dumps({'hub_session': self.hub.code}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(WhoThatAnswer.objects.get().points_earned, 1)
        self.assertIsNone(current_snapshot('who_that', self.quiz.room_code, self.hub.code)['current_question_id'])

    def test_direct_game_end_discards_open_drafts(self):
        self.start()
        save_pending(self.quiz.room_code, 'Alice', self.hub.code, self.question.correct_answer, self.question.pk)
        self.quiz.end_quiz()
        self.assertFalse(WhoThatAnswer.objects.exists())
        self.quiz_session.refresh_from_db()
        self.assertEqual(self.quiz_session.pending_answers, {})
        self.assertIsNone(current_snapshot('who_that', self.quiz.room_code, self.hub.code)['current_question_id'])

    def test_duplicate_names_are_scoped_for_results_rank_and_public_leaderboard(self):
        WhoThatParticipant.objects.create(quiz=self.quiz, name='Alice', hub_session_code='OTHER', total_score=100)
        result = self.client.get(reverse('who_is_that:result', args=[self.quiz.room_code, 'Alice']), {'hub_session': self.hub.code})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.context['participant_rank'], 1)
        self.assertEqual(len(result.context['leaderboard']), 1)
        self.assertEqual(self.participant.get_rank(), 1)

    def test_strict_names_and_explicit_aliases(self):
        self.question.correct_answer = 'Miley Cyrus'
        self.question.alternative_answers = ['Hannah Montana']
        for value in ['Billy Cyrus', 'Cyrus', 'Miley', 'Miley Cyruss', '']:
            self.assertFalse(self.question.check_answer(value), value)
        for value in ['miley CYRUS', ' Miley   Cyrus ', ' Hannah Montana ']:
            self.assertTrue(self.question.check_answer(value), value)

    def test_draft_restore_is_private_and_answer_confirmation_has_no_evaluation(self):
        self.start()
        save_pending(self.quiz.room_code, 'Alice', self.hub.code, self.question.correct_answer, self.question.pk)
        self.assertEqual(async_to_sync(self.consumer.get_pending_answer)(self.participant.pk, self.question.pk), self.question.correct_answer)
        bob = WhoThatParticipant.objects.create(quiz=self.quiz, name='Bob', hub_session_code=self.hub.code)
        self.assertIsNone(async_to_sync(self.consumer.get_pending_answer)(bob.pk, self.question.pk))
        self.consumer.send = AsyncMock()
        self.consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
        self.consumer.room_group_name = 'who_that_' + self.quiz.room_code
        async_to_sync(self.consumer.handle_participant_submit_answer)({'participant_name': 'Alice',
            'hub_session': self.hub.code, 'user_answer': self.question.correct_answer, 'question_id': self.question.pk})
        confirmation = json.loads(self.consumer.send.call_args.kwargs['text_data'])
        self.assertEqual(confirmation['type'], 'answer_submitted')
        self.assertNotIn('is_correct', confirmation)
        self.consumer.send.reset_mock()
        async_to_sync(self.consumer.participant_answered)({'answer': {'user_answer': 'secret'}})
        self.consumer.send.assert_not_called()
        end_question(self.quiz.pk, self.hub.code)
        self.assertEqual(reveal_state(self.quiz.room_code, self.hub.code, self.participant.pk)['evaluated_pending_answers'][0]['points_earned'], 1)
        self.assertEqual(reveal_state(self.quiz.room_code, self.hub.code, bob.pk)['evaluated_pending_answers'], [])

    def test_rejections_and_pending_updates_are_acknowledged(self):
        self.start(age=50)
        self.consumer.send = AsyncMock()
        async_to_sync(self.consumer.handle_participant_submit_answer)({'participant_name': 'Alice',
            'hub_session': self.hub.code, 'user_answer': 'Ada', 'question_id': self.question.pk})
        self.assertEqual(json.loads(self.consumer.send.call_args.kwargs['text_data'])['type'], 'answer_rejected')
        async_to_sync(self.consumer.handle_participant_update_pending_answer)({'participant_name': 'Alice',
            'hub_session': self.hub.code, 'user_answer': 'Ada', 'question_id': self.question.pk})
        self.assertEqual(json.loads(self.consumer.send.call_args.kwargs['text_data'])['type'], 'pending_answer_rejected')

    def test_fresh_hub_activation_resets_stale_session(self):
        self.hub.check_in_status = HubSession.CHECK_IN_COMPLETED
        self.hub.locked_participant_count = 1
        self.hub.save()
        HubParticipant.objects.create(session=self.hub, nickname='Alice')
        self.quiz.status = 'waiting'
        self.quiz.save()
        self.quiz_session.current_question_number = 9
        self.quiz_session.is_question_active = True
        self.quiz_session.save()
        result = resolve_session_game_activation_for_room('who_that', self.quiz.room_code)
        self.assertTrue(result['success'], result)
        self.quiz_session.refresh_from_db()
        self.assertEqual(self.quiz_session.current_question_number, 0)
        self.assertFalse(self.quiz_session.is_question_active)
