import asyncio
from pathlib import Path
import uuid
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .consumers import ClueRushGameConsumer
from .models import (
    Clue,
    ClueAnswer,
    CluePendingInput,
    ClueQuestion,
    ClueRushGame,
    ClueRushParticipant,
    ClueRushSession,
)
from .runtime import reconcile_clue_schedule, start_question_schedule
from games_hub.authoritative_state import (
    current_snapshot,
    finish_question_flow,
    get_question_flow_capabilities,
    observe_snapshot,
    reset_question_flow,
    validate_and_reserve_action,
)
from games_hub.models import GameRuntimeState, HubGameStep, HubSession


class DummyClueChannelLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group_name, message):
        self.sent.append((group_name, message))

    async def group_discard(self, group_name, channel_name):
        return None


class ClueRushScoreBoxTests(TransactionTestCase):
    def _create_question(self, user, answer='Brazil', clue_orders=None, points=12):
        question = ClueQuestion.objects.create(
            question_text='Guess the country',
            answer=answer,
            points=points,
            time_limit=30,
            created_by=user,
        )
        orders = clue_orders or [1, 2, 3]
        for index, order in enumerate(orders, start=1):
            Clue.objects.create(
                clue_question=question,
                order=order,
                clue_text=f'Legacy clue {index}',
                duration=10,
            )
        return question

    def _create_question_with_durations(self, user, durations, answer='Brazil', points=12, time_limit=90, clue_orders=None):
        question = ClueQuestion.objects.create(
            question_text='Guess the country',
            answer=answer,
            points=points,
            time_limit=time_limit,
            created_by=user,
        )
        orders = clue_orders or list(range(1, len(durations) + 1))
        for index, (order, duration) in enumerate(zip(orders, durations), start=1):
            Clue.objects.create(
                clue_question=question,
                order=order,
                clue_text=f'Duration clue {index}',
                duration=duration,
            )
        return question

    def _create_active_quiz(self, user, question, room_code):
        quiz = ClueRushGame.objects.create(
            title='Clue History',
            room_code=room_code,
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now(),
        )
        session = ClueRushSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=10),
        )
        return quiz, session

    def _set_visible_clue(self, quiz, session, clue):
        quiz.current_clue = clue
        quiz.save(update_fields=['current_clue'])
        session.current_clue_number = clue.clue_question.get_revealed_clue_count(current_clue=clue)
        session.is_clue_active = True
        session.save(update_fields=['current_clue_number', 'is_clue_active'])

    def _start_persistent_schedule(self, quiz, session, question, duration_override=None):
        session.is_question_active = False
        session.save(update_fields=['is_question_active'])
        return start_question_schedule(
            quiz_id=quiz.id,
            question_id=question.id,
            duration_override=duration_override,
        )

    def test_correct_answer_with_last_visible_clue_awards_one_point(self):
        user = User.objects.create_user(username='clue-last-visible')
        question = self._create_question(user, clue_orders=[0, 1, 2], points=12)
        quiz, session = self._create_active_quiz(user, question, room_code='8411')
        last_clue = question.clues.order_by('order', 'id').last()
        self._set_visible_clue(quiz, session, last_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)

        answer = ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Brazil',
            time_taken=1.5,
        )

        participant.refresh_from_db()
        self.assertTrue(answer.auto_is_correct)
        self.assertFalse(answer.is_manually_corrected)
        self.assertEqual(answer.submitted_clue_number, 3)
        self.assertEqual(answer.total_clues_at_submission, 3)
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)

    def test_correct_answer_with_penultimate_visible_clue_awards_two_points(self):
        user = User.objects.create_user(username='clue-penultimate-visible')
        question = self._create_question(user, clue_orders=[0, 1, 2], points=12)
        quiz, session = self._create_active_quiz(user, question, room_code='8412')
        penultimate_clue = question.clues.order_by('order', 'id')[1]
        self._set_visible_clue(quiz, session, penultimate_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)

        answer = ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Brazil',
            time_taken=1.5,
        )

        participant.refresh_from_db()
        self.assertEqual(answer.points_earned, 2)
        self.assertEqual(participant.total_score, 2)

    def test_wrong_answer_awards_zero_points(self):
        user = User.objects.create_user(username='clue-wrong-visible')
        question = self._create_question(user, clue_orders=[0, 1, 2], points=12)
        quiz, session = self._create_active_quiz(user, question, room_code='8413')
        first_clue = question.clues.order_by('order', 'id').first()
        self._set_visible_clue(quiz, session, first_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)

        answer = ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Argentina',
            time_taken=1.5,
        )

        participant.refresh_from_db()
        self.assertEqual(answer.points_earned, 0)
        self.assertEqual(participant.total_score, 0)

    def test_advance_next_clue_uses_one_based_visible_order_for_zero_based_clues(self):
        user = User.objects.create_user(username='clue-visible-order')
        question = self._create_question(user, clue_orders=[0, 1, 2], points=12)
        quiz, session = self._create_active_quiz(user, question, room_code='8414')

        runtime = self._start_persistent_schedule(quiz, session, question)
        schedule = runtime['schedule']
        first = reconcile_clue_schedule(quiz.room_code, at=parse_datetime(schedule[0]['starts_at']))['new_clues'][0]
        second = reconcile_clue_schedule(quiz.room_code, at=parse_datetime(schedule[1]['starts_at']))['new_clues'][0]
        third = reconcile_clue_schedule(quiz.room_code, at=parse_datetime(schedule[2]['starts_at']))['new_clues'][0]

        session.refresh_from_db()
        self.assertEqual(first['order'], 1)
        self.assertTrue(first['has_next_clue'])
        self.assertEqual(second['order'], 2)
        self.assertTrue(second['has_next_clue'])
        self.assertEqual(third['order'], 3)
        self.assertFalse(third['has_next_clue'])
        self.assertEqual(session.current_clue_number, 3)

    def test_close_answer_approval_uses_last_visible_clue_score(self):
        user = User.objects.create_user(username='clue-close-approval')
        question = self._create_question(user, clue_orders=[0, 1, 2], points=12)
        quiz, session = self._create_active_quiz(user, question, room_code='8415')
        last_clue = question.clues.order_by('order', 'id').last()
        self._set_visible_clue(quiz, session, last_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)
        answer = ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Argentina',
            time_taken=1.5,
        )

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        result = async_to_sync(consumer.approve_close_answer_db)(participant.name)

        answer.refresh_from_db()
        participant.refresh_from_db()
        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)
        self.assertEqual(result['points_earned'], 1)

    def test_progress_history_includes_solution_text_and_round_score(self):
        user = User.objects.create_user(username='clue-score-history')
        question = self._create_question(user, answer='Brazil')
        quiz, session = self._create_active_quiz(user, question, room_code='8416')
        session.current_clue_number = 2
        session.save(update_fields=['current_clue_number'])
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )
        ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Brazil',
            time_taken=1.5,
        )

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        history = async_to_sync(consumer.get_participant_score_history)(
            participant.name,
            participant.hub_session_code,
        )

        self.assertEqual(session.current_clue_number, 2)
        self.assertEqual(
            history,
            [{
                'question_number': 1,
                'correct_answer': 'Brazil',
                'achieved_points': 2,
                'max_points': 3,
            }],
        )

    def test_play_template_contains_score_total_and_solution_hooks(self):
        user = User.objects.create_user(username='clue-score-template')
        quiz = ClueRushGame.objects.create(
            title='Clue Template',
            room_code='8417',
            creator=user,
            status='active',
        )
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )

        response = self.client.get(
            reverse('clue_rush:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="clueHistoryTotal"', html=False)
        self.assertContains(response, 'clue-history-solution')
        self.assertContains(response, 'score-box__row')
        self.assertContains(response, 'score-box__title')

    def test_game_status_returns_one_based_current_round_for_zero_based_first_clue(self):
        user = User.objects.create_user(username='clue-status-round')
        question = self._create_question(user, clue_orders=[0, 1, 2], points=12)
        quiz, session = self._create_active_quiz(user, question, room_code='8418')
        first_clue = question.clues.order_by('order', 'id').first()
        self._set_visible_clue(quiz, session, first_clue)
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )

        response = self.client.get(
            reverse('clue_rush:game_status', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['current_round'], 1)

    def test_monitor_renders_one_based_clue_numbers_for_zero_based_orders(self):
        admin_user = User.objects.create_user(
            username='clue-monitor-admin',
            password='secret',
            is_staff=True,
        )
        question = self._create_question(admin_user, clue_orders=[0, 1, 2], points=12)
        quiz, _session = self._create_active_quiz(admin_user, question, room_code='8419')
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin_dashboard:clue_rush_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-clue-order="1"', html=False)
        self.assertContains(response, '<strong>#1</strong> Legacy clue 1', html=False)
        self.assertNotContains(response, 'data-clue-order="0"', html=False)
        self.assertNotContains(response, '<strong>#0</strong> Legacy clue 1', html=False)

    def test_monitor_prefills_host_timer_with_first_clue_duration(self):
        admin_user = User.objects.create_user(
            username='clue-monitor-duration-admin',
            password='secret',
            is_staff=True,
        )
        quiz = ClueRushGame.objects.create(
            title='Clue Timer',
            room_code='8420',
            creator=admin_user,
            status='active',
        )
        ClueRushSession.objects.create(quiz=quiz)
        self._create_question_with_durations(admin_user, durations=[7, 11], time_limit=90)
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin_dashboard:clue_rush_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'class="form-control form-control-sm question-time-input" placeholder="Time (s)" min="5" step="1" style="max-width: 120px;" value="7"',
            html=False,
        )
        self.assertNotContains(response, 'value="90"', html=False)

    def test_runtime_override_before_start_controls_clue_duration(self):
        user = User.objects.create_user(username='clue-runtime-override')
        question = self._create_question_with_durations(user, durations=[5, 5, 5], time_limit=90)
        quiz, session = self._create_active_quiz(user, question, room_code='8421')
        runtime = self._start_persistent_schedule(quiz, session, question, 9)
        first, second = runtime['schedule'][:2]

        quiz.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(first['duration'], 9)
        self.assertEqual(second['duration'], 9)
        self.assertTrue(first['has_next_clue'])
        self.assertTrue(first['end_time'])
        self.assertTrue(first['sequence_end_time'])
        self.assertEqual(first['sequence_duration'], 27)
        self.assertTrue(first['starts_at'])
        self.assertIsNotNone(session.question_end_time)
        self.assertEqual(int((session.question_end_time - quiz.question_start_time).total_seconds()), 27)

    def test_runtime_without_override_keeps_original_clue_durations(self):
        user = User.objects.create_user(username='clue-runtime-default')
        question = self._create_question_with_durations(user, durations=[5, 8, 11], time_limit=90)
        quiz, session = self._create_active_quiz(user, question, room_code='8422')
        runtime = self._start_persistent_schedule(quiz, session, question)
        first, second, third = runtime['schedule']

        session.refresh_from_db()
        self.assertEqual(first['duration'], 5)
        self.assertEqual(second['duration'], 8)
        self.assertEqual(third['duration'], 11)
        self.assertEqual(first['sequence_duration'], 24)
        self.assertEqual(second['sequence_duration'], 24)
        self.assertEqual(third['sequence_duration'], 24)
        self.assertIsNotNone(session.question_end_time)

    def test_rejoin_snapshot_restores_authoritative_clue_and_sequence_deadlines(self):
        user = User.objects.create_user(username='clue-timer-rejoin')
        question = self._create_question_with_durations(user, durations=[5, 8, 11], time_limit=90)
        quiz, session = self._create_active_quiz(user, question, room_code='8435')
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )
        runtime = self._start_persistent_schedule(quiz, session, question)
        live_clue = reconcile_clue_schedule(
            quiz.room_code,
            at=parse_datetime(runtime['schedule'][0]['starts_at']),
        )['new_clues'][0]
        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        snapshot = async_to_sync(consumer.get_state_snapshot)(
            participant.name,
            participant.hub_session_code,
        )

        self.assertTrue(snapshot['question']['question_started_at'])
        self.assertTrue(snapshot['server_now'])
        self.assertEqual(len(snapshot['revealed_clues']), 1)
        rejoined_clue = snapshot['revealed_clues'][0]
        self.assertEqual(rejoined_clue['question_id'], question.id)
        self.assertEqual(rejoined_clue['end_time'], live_clue['end_time'])
        self.assertEqual(rejoined_clue['sequence_end_time'], live_clue['sequence_end_time'])
        self.assertEqual(rejoined_clue['sequence_duration'], 24)

    def test_monitor_uses_clue_end_time_for_running_timer_and_hides_editor(self):
        admin_user = User.objects.create_user(
            username='clue-running-timer-admin',
            password='secret',
            is_staff=True,
        )
        question = self._create_question_with_durations(admin_user, durations=[12, 12, 12], time_limit=90)
        quiz, session = self._create_active_quiz(admin_user, question, room_code='8423')
        first_clue = question.clues.order_by('order', 'id').first()
        self._set_visible_clue(quiz, session, first_clue)
        session.clue_end_time = timezone.now() + timezone.timedelta(seconds=30)
        session.save(update_fields=['clue_end_time'])
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin_dashboard:clue_rush_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'this.currentClueTimeLeft = 30;', html=False)
        self.assertContains(response, 'this.currentClueHasNext = true;', html=False)
        self.assertNotContains(
            response,
            'class="form-control form-control-sm question-time-input" placeholder="Time (s)"',
            html=False,
        )

    def test_start_quiz_resets_stale_clue_progress_for_waiting_game(self):
        user = User.objects.create_user(username='clue-start-reset')
        question = self._create_question(user, clue_orders=[1, 2, 3], points=12)
        last_clue = question.clues.order_by('order', 'id').last()
        quiz = ClueRushGame.objects.create(
            title='Clue Reset',
            room_code='8424',
            creator=user,
            status='waiting',
            current_question=question,
            question_start_time=timezone.now(),
            current_clue=last_clue,
            clue_start_time=timezone.now(),
        )
        session = ClueRushSession.objects.create(
            quiz=quiz,
            total_questions_sent=1,
            current_question_number=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=10),
            current_clue_number=3,
            is_clue_active=True,
            clue_end_time=timezone.now() + timezone.timedelta(seconds=10),
            total_responses_current_question=2,
            correct_responses_current_question=1,
        )

        quiz.start_quiz()

        quiz.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(quiz.status, 'active')
        self.assertIsNone(quiz.current_question_id)
        self.assertIsNone(quiz.current_clue_id)
        self.assertIsNone(quiz.question_start_time)
        self.assertIsNone(quiz.clue_start_time)
        self.assertEqual(session.total_questions_sent, 0)
        self.assertEqual(session.current_question_number, 0)
        self.assertFalse(session.is_question_active)
        self.assertIsNone(session.question_end_time)
        self.assertEqual(session.current_clue_number, 0)
        self.assertFalse(session.is_clue_active)
        self.assertIsNone(session.clue_end_time)

    def test_next_question_starts_with_first_clue_after_previous_question_finished(self):
        user = User.objects.create_user(username='clue-next-question-start')
        first_question = self._create_question(user, answer='Brazil', clue_orders=[1, 2, 3], points=12)
        second_question = self._create_question(user, answer='Chile', clue_orders=[1, 2, 3], points=12)
        quiz, session = self._create_active_quiz(user, first_question, room_code='8425')
        last_clue = first_question.clues.order_by('order', 'id').last()
        self._set_visible_clue(quiz, session, last_clue)

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code

        async_to_sync(consumer.finalize_current_question_for_end)(None, first_question.id)
        async_to_sync(consumer.update_quiz_question)(quiz, second_question, None)
        quiz.refresh_from_db()
        session.refresh_from_db()
        self.assertIsNone(quiz.current_clue_id)
        self.assertEqual(session.current_clue_number, 0)
        self.assertFalse(session.is_clue_active)

        first_new_clue = async_to_sync(consumer.advance_next_clue)()

        session.refresh_from_db()
        self.assertEqual(first_new_clue['order'], 1)
        self.assertEqual(first_new_clue['clue_text'], second_question.clues.order_by('order', 'id').first().clue_text)
        self.assertEqual(session.current_clue_number, 1)

    def test_rejoin_snapshot_at_question_start_does_not_reveal_all_clues_from_stale_progress(self):
        user = User.objects.create_user(username='clue-rejoin-clean-start')
        first_question = self._create_question(user, answer='Brazil', clue_orders=[1, 2, 3], points=12)
        second_question = self._create_question(user, answer='Chile', clue_orders=[1, 2, 3], points=12)
        quiz, session = self._create_active_quiz(user, second_question, room_code='8426')
        stale_clue = first_question.clues.order_by('order', 'id').last()
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )
        quiz.current_clue = stale_clue
        quiz.clue_start_time = timezone.now()
        quiz.save(update_fields=['current_clue', 'clue_start_time'])
        session.current_clue_number = 3
        session.is_clue_active = False
        session.save(update_fields=['current_clue_number', 'is_clue_active'])

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        snapshot = async_to_sync(consumer.get_rejoin_snapshot)(participant.name, participant.hub_session_code)

        self.assertEqual(snapshot['question']['id'], second_question.id)
        self.assertEqual(snapshot['revealed_clues'], [])

    def test_monitor_keeps_all_clues_pending_at_question_start_with_stale_progress(self):
        admin_user = User.objects.create_user(
            username='clue-monitor-clean-start',
            password='secret',
            is_staff=True,
        )
        first_question = self._create_question(admin_user, answer='Brazil', clue_orders=[1, 2, 3], points=12)
        second_question = self._create_question(admin_user, answer='Chile', clue_orders=[1, 2, 3], points=12)
        quiz, session = self._create_active_quiz(admin_user, second_question, room_code='8427')
        quiz.current_clue = first_question.clues.order_by('order', 'id').last()
        quiz.save(update_fields=['current_clue'])
        session.current_clue_number = 3
        session.is_clue_active = False
        session.save(update_fields=['current_clue_number', 'is_clue_active'])
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin_dashboard:clue_rush_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_revealed_clue_count'], 0)
        self.assertNotContains(response, '<span class="badge bg-success clue-status">Sent</span>', html=False)

    def test_wrong_answer_persists_visible_clue_number_for_manual_mark_correct(self):
        user = User.objects.create_user(username='clue-mark-correct-store')
        question = self._create_question(user, clue_orders=[1, 2, 3, 4], points=12)
        quiz, session = self._create_active_quiz(user, question, room_code='8428')
        second_clue = question.clues.order_by('order', 'id')[1]
        self._set_visible_clue(quiz, session, second_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)

        answer = ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Argentina',
            time_taken=2.25,
        )

        self.assertFalse(answer.auto_is_correct)
        self.assertFalse(answer.is_correct)
        self.assertFalse(answer.is_manually_corrected)
        self.assertEqual(answer.submitted_clue_number, 2)
        self.assertEqual(answer.total_clues_at_submission, 4)
        self.assertEqual(answer.points_earned, 0)

    def test_promote_answer_correct_uses_stored_submission_clue_after_question_end(self):
        admin_user = User.objects.create_user(
            username='clue-mark-correct-admin',
            password='secret',
            is_staff=True,
        )
        question = self._create_question(admin_user, clue_orders=[1, 2, 3, 4], points=12)
        quiz, session = self._create_active_quiz(admin_user, question, room_code='8429')
        second_clue = question.clues.order_by('order', 'id')[1]
        self._set_visible_clue(quiz, session, second_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)
        answer = ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Argentina',
            time_taken=2.25,
        )

        quiz.current_question = None
        quiz.question_start_time = None
        quiz.current_clue = None
        quiz.clue_start_time = None
        quiz.save(update_fields=['current_question', 'question_start_time', 'current_clue', 'clue_start_time'])
        session.current_clue_number = 0
        session.is_question_active = False
        session.is_clue_active = False
        session.question_end_time = None
        session.clue_end_time = None
        session.save(update_fields=['current_clue_number', 'is_question_active', 'is_clue_active', 'question_end_time', 'clue_end_time'])

        self.client.force_login(admin_user)
        response = self.client.post(
            reverse('admin_dashboard:promote_clue_rush_answer_correct', args=[quiz.room_code]),
            data='{"answer_id": %d}' % answer.id,
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        answer.refresh_from_db()
        participant.refresh_from_db()

        self.assertTrue(payload['success'])
        self.assertTrue(answer.is_correct)
        self.assertTrue(answer.is_manually_corrected)
        self.assertEqual(answer.points_earned, 3)
        self.assertEqual(participant.total_score, 3)
        self.assertEqual(payload['points_earned'], 3)
        self.assertEqual(payload['submitted_clue_number'], 2)
        self.assertEqual(
            payload['progress_history'],
            [{
                'question_number': 1,
                'correct_answer': 'Brazil',
                'achieved_points': 3,
                'max_points': 4,
            }],
        )

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        history = async_to_sync(consumer.get_participant_score_history)(
            participant.name,
            participant.hub_session_code,
        )
        self.assertEqual(payload['progress_history'], history)

    def test_monitor_hydrates_mark_correct_candidates_for_latest_answered_question(self):
        admin_user = User.objects.create_user(
            username='clue-mark-correct-monitor',
            password='secret',
            is_staff=True,
        )
        question = self._create_question(admin_user, clue_orders=[1, 2, 3, 4], points=12)
        quiz, session = self._create_active_quiz(admin_user, question, room_code='8430')
        second_clue = question.clues.order_by('order', 'id')[1]
        self._set_visible_clue(quiz, session, second_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)
        ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Argentina',
            time_taken=2.25,
        )
        quiz.current_question = None
        quiz.current_clue = None
        quiz.save(update_fields=['current_question', 'current_clue'])

        self.client.force_login(admin_user)
        response = self.client.get(reverse('admin_dashboard:clue_rush_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'initialClueRushResponsesData', html=False)
        self.assertContains(response, f'/admin-dashboard/clue-rush/{quiz.room_code}/promote-answer-correct/', html=False)
        initial_live_responses = response.context['initial_live_responses']
        self.assertEqual(len(initial_live_responses), 1)
        self.assertEqual(initial_live_responses[0]['participant_name'], 'Ada')
        self.assertEqual(initial_live_responses[0]['submitted_clue_number'], 2)
        self.assertTrue(initial_live_responses[0]['can_mark_correct'])

    def test_play_template_contains_manual_mark_correct_runtime_hooks(self):
        user = User.objects.create_user(username='clue-mark-correct-play')
        quiz = ClueRushGame.objects.create(
            title='Clue Template Hooks',
            room_code='8431',
            creator=user,
            status='active',
        )
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )

        response = self.client.get(
            reverse('clue_rush:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "case 'answer_corrected':", html=False)
        self.assertContains(response, "this.onAnswerCorrected(data);", html=False)
        self.assertContains(response, "case 'participant_rehydrated':", html=False)
        self.assertContains(response, "this.onParticipantRehydrated(data.answer);", html=False)

    def test_host_end_question_evaluates_pending_and_empty_answers(self):
        user = User.objects.create_user(username='clue-host-end-pending')
        question = self._create_question(user, clue_orders=[1, 2, 3, 4], answer='Brazil')
        quiz, session = self._create_active_quiz(user, question, room_code='8432')
        fourth_clue = question.clues.order_by('order', 'id')[3]
        self._set_visible_clue(quiz, session, fourth_clue)
        pending_player = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)
        locked_player = ClueRushParticipant.objects.create(quiz=quiz, name='Ben', hub_session_code=None)
        empty_player = ClueRushParticipant.objects.create(quiz=quiz, name='Cal', hub_session_code=None)
        locked_answer = ClueAnswer.objects.create(
            quiz=quiz,
            participant=locked_player,
            question=question,
            answer_text='Brazil',
            submitted_clue_number=2,
            total_clues_at_submission=4,
            time_taken=1.2,
        )
        CluePendingInput.objects.create(
            quiz=quiz,
            participant=pending_player,
            question=question,
            answer_text='Brazil',
            submitted_clue_number=4,
            total_clues_at_input=4,
            time_taken=3.4,
        )

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        payload = async_to_sync(consumer.finalize_current_question_for_end)(None)

        quiz.refresh_from_db()
        session.refresh_from_db()
        pending_player.refresh_from_db()
        locked_player.refresh_from_db()
        empty_player.refresh_from_db()

        self.assertIsNone(quiz.current_question_id)
        self.assertIsNone(quiz.current_clue_id)
        self.assertFalse(session.is_question_active)
        self.assertFalse(session.is_clue_active)
        self.assertIsNone(session.question_end_time)
        self.assertIsNone(session.clue_end_time)
        self.assertEqual(session.total_responses_current_question, 3)
        self.assertEqual(session.correct_responses_current_question, 2)
        self.assertFalse(CluePendingInput.objects.filter(quiz=quiz, question=question).exists())

        pending_answer = ClueAnswer.objects.get(quiz=quiz, participant=pending_player, question=question)
        empty_answer = ClueAnswer.objects.get(quiz=quiz, participant=empty_player, question=question)
        locked_answer.refresh_from_db()

        self.assertTrue(pending_answer.is_correct)
        self.assertEqual(pending_answer.submitted_clue_number, 4)
        self.assertEqual(pending_answer.points_earned, 1)
        self.assertEqual(pending_player.total_score, 1)
        self.assertTrue(locked_answer.is_correct)
        self.assertEqual(locked_answer.submitted_clue_number, 2)
        self.assertEqual(locked_answer.points_earned, 3)
        self.assertEqual(locked_player.total_score, 3)
        self.assertFalse(empty_answer.is_correct)
        self.assertEqual(empty_answer.answer_text, '')
        self.assertEqual(empty_answer.points_earned, 0)
        self.assertEqual(empty_player.total_score, 0)

        answers_by_name = {answer['participant_name']: answer for answer in payload['answers']}
        self.assertEqual(payload['correct_answer']['formatted_answer'], 'Brazil')
        self.assertEqual(answers_by_name['Ada']['answer_text'], 'Brazil')
        self.assertEqual(answers_by_name['Ada']['points_earned'], 1)
        self.assertEqual(
            answers_by_name['Ada']['progress_history'],
            [{
                'question_number': 1,
                'correct_answer': 'Brazil',
                'achieved_points': 1,
                'max_points': 4,
            }],
        )
        self.assertEqual(answers_by_name['Cal']['points_earned'], 0)

        late_submit = async_to_sync(consumer.save_participant_answer)(
            empty_player.name,
            None,
            'Brazil',
            question.id,
        )
        self.assertEqual(late_submit, {'_error': 'invalid_phase'})
        self.assertEqual(ClueAnswer.objects.filter(quiz=quiz, participant=empty_player, question=question).count(), 1)

    def test_rejoin_after_host_end_question_reconstructs_reveal_state(self):
        user = User.objects.create_user(username='clue-rejoin-after-host-end')
        question = self._create_question(user, clue_orders=[1, 2, 3], answer='Brazil')
        quiz, session = self._create_active_quiz(user, question, room_code='8433')
        third_clue = question.clues.order_by('order', 'id')[2]
        self._set_visible_clue(quiz, session, third_clue)
        participant = ClueRushParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code=None)
        CluePendingInput.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Brazil',
            submitted_clue_number=3,
            total_clues_at_input=3,
            time_taken=2.0,
        )
        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        async_to_sync(consumer.finalize_current_question_for_end)(None)

        snapshot = async_to_sync(consumer.get_rejoin_snapshot)(participant.name, participant.hub_session_code)

        self.assertIsNone(snapshot['question'])
        self.assertEqual(snapshot['correct_answer']['formatted_answer'], 'Brazil')
        self.assertEqual(snapshot['revealed_answer']['participant_name'], 'Ada')
        self.assertEqual(snapshot['revealed_answer']['answer_text'], 'Brazil')
        self.assertEqual(snapshot['revealed_answer']['progress_history'][0]['achieved_points'], 1)

    def test_play_template_sends_pending_input_and_reveals_on_question_end(self):
        user = User.objects.create_user(username='clue-pending-template')
        quiz = ClueRushGame.objects.create(
            title='Clue Pending Hooks',
            room_code='8434',
            creator=user,
            status='active',
        )
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )

        response = self.client.get(
            reverse('clue_rush:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "type: 'participant_input_changed'", html=False)
        self.assertContains(response, "const ownResult = (data?.answers || []).find", html=False)
        self.assertContains(response, "this.showCorrectAnswer(data.correct_answer);", html=False)


class ClueRushPersistentRuntimeTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='clue-runtime-persistent')
        self.question = ClueQuestion.objects.create(
            question_text='Persistent clue question',
            answer='Brazil',
            points=3,
            time_limit=60,
            created_by=self.user,
        )
        for order, duration in enumerate((5, 7, 9), start=1):
            Clue.objects.create(
                clue_question=self.question,
                order=order,
                clue_text=f'Clue {order}',
                duration=duration,
            )
        self.quiz = ClueRushGame.objects.create(
            title='Persistent Clue Rush',
            room_code='6299',
            creator=self.user,
            status='active',
        )
        self.quiz.selected_questions.add(self.question)
        self.quiz.question_order = [self.question.id]
        self.quiz.save(update_fields=['question_order'])
        self.session = ClueRushSession.objects.create(quiz=self.quiz)
        self.participant = ClueRushParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code=None,
        )
        self.started = start_question_schedule(
            quiz_id=self.quiz.id,
            question_id=self.question.id,
        )

    def _consumer(self):
        consumer = ClueRushGameConsumer()
        consumer.room_code = self.quiz.room_code
        consumer.room_group_name = f'cluerush_{self.quiz.room_code}'
        consumer.channel_layer = DummyClueChannelLayer()
        return consumer

    def test_worker_restart_reconstructs_missed_clues_idempotently(self):
        schedule = self.started['schedule']
        after_third_start = parse_datetime(schedule[2]['starts_at'])
        first_worker = reconcile_clue_schedule(
            self.quiz.room_code,
            at=after_third_start,
        )
        second_worker = reconcile_clue_schedule(
            self.quiz.room_code,
            at=after_third_start,
        )

        self.assertEqual([clue['order'] for clue in first_worker['new_clues']], [1, 2, 3])
        self.assertEqual(second_worker['new_clues'], [])
        restarted = self._consumer()
        snapshot = async_to_sync(restarted.get_state_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            [clue['order'] for clue in snapshot['revealed_clues']],
            [1, 2, 3],
        )
        self.assertEqual(
            snapshot['ends_at'],
            self.started['answer_deadline'].isoformat(),
        )

    def test_host_reload_after_first_clue_does_not_stop_following_clues(self):
        now = timezone.now()
        self.session.refresh_from_db()
        schedule = list(self.session.clue_schedule)
        for index, entry in enumerate(schedule):
            starts_at = now + timezone.timedelta(seconds=index)
            ends_at = starts_at + timezone.timedelta(seconds=1)
            entry['starts_at'] = starts_at.isoformat()
            entry['end_time'] = ends_at.isoformat()
            entry['sequence_end_time'] = (
                now + timezone.timedelta(seconds=len(schedule))
            ).isoformat()
            entry['duration'] = 1
        self.session.clue_schedule = schedule
        self.session.answer_deadline = now + timezone.timedelta(seconds=len(schedule))
        self.session.question_end_time = self.session.answer_deadline
        self.session.save(update_fields=[
            'clue_schedule',
            'answer_deadline',
            'question_end_time',
        ])

        async def run_sequence():
            consumer = self._consumer()
            consumer.channel_name = 'host-channel'
            consumer._ensure_clue_reconciliation_task(self.question.id)
            task = consumer.auto_clue_task
            await asyncio.sleep(0.15)
            await consumer.disconnect(1000)
            self.assertFalse(
                task.done(),
                repr(task.exception()) if task.done() and not task.cancelled() else None,
            )
            await asyncio.sleep(2.35)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return consumer.channel_layer.sent

        events = async_to_sync(run_sequence)()
        clue_events = [
            message
            for _, message in events
            if message.get('type') == 'clue_started'
        ]
        self.assertEqual(
            [message['clue']['order'] for message in clue_events],
            [1, 2, 3],
        )

    def test_rejoin_uses_persisted_schedule_without_recalculating_durations(self):
        schedule = self.started['schedule']
        second_start = parse_datetime(schedule[1]['starts_at'])
        reconcile_clue_schedule(self.quiz.room_code, at=second_start)

        snapshot = async_to_sync(self._consumer().get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )

        self.assertEqual(snapshot['question']['ends_at'], schedule[-1]['end_time'])
        self.assertEqual(snapshot['question']['question_number'], 1)
        self.assertEqual(
            [
                (
                    clue['order'],
                    clue['duration'],
                    clue['end_time'],
                    clue['sequence_end_time'],
                )
                for clue in snapshot['revealed_clues']
            ],
            [
                (
                    clue['order'],
                    clue['duration'],
                    clue['end_time'],
                    clue['sequence_end_time'],
                )
                for clue in schedule[:2]
            ],
        )

    def test_final_answer_always_requires_authoritative_context(self):
        self.assertTrue(
            self._consumer()._is_guarded_action('participant_submit_answer', {}),
        )

    def test_pending_input_cannot_write_for_another_participant(self):
        consumer = self._consumer()
        consumer._authoritative_participant = self.participant.name
        consumer._authoritative_session = ''
        consumer.send_action_error = AsyncMock()
        consumer.store_pending_input = AsyncMock()

        async_to_sync(consumer.handle_participant_input_changed)({
            'participant_name': 'Mallory',
            'hub_session': None,
            'question_id': self.question.id,
            'answer': 'spoofed',
        })

        consumer.send_action_error.assert_awaited_once_with('invalid_participant')
        consumer.store_pending_input.assert_not_awaited()

    def test_two_workers_broadcast_each_due_clue_only_once(self):
        layer = DummyClueChannelLayer()
        first = self._consumer()
        second = self._consumer()
        first.channel_layer = layer
        second.channel_layer = layer

        async_to_sync(first.reconcile_and_broadcast_clues)()
        async_to_sync(second.reconcile_and_broadcast_clues)()

        clue_events = [
            message
            for _, message in layer.sent
            if message.get('type') == 'clue_started'
        ]
        self.assertEqual(len(clue_events), 1)
        self.assertEqual(clue_events[0]['clue']['order'], 1)

    def test_parallel_question_start_is_idempotent_and_cannot_replace_active_question(self):
        duplicate = start_question_schedule(
            quiz_id=self.quiz.id,
            question_id=self.question.id,
        )
        other = ClueQuestion.objects.create(
            question_text='Other question',
            answer='Chile',
            points=2,
            time_limit=30,
            created_by=self.user,
        )
        replaced = start_question_schedule(
            quiz_id=self.quiz.id,
            question_id=other.id,
        )

        self.assertFalse(duplicate['started'])
        self.assertEqual(duplicate['code'], 'already_started')
        self.assertFalse(replaced['started'])
        self.assertEqual(replaced['code'], 'invalid_phase')
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.question.id)

    def test_stale_and_late_answers_are_rejected_server_side(self):
        consumer = self._consumer()
        stale = async_to_sync(consumer.save_participant_answer)(
            self.participant.name,
            self.participant.hub_session_code,
            'Brazil',
            self.question.id + 1,
        )
        self.assertEqual(stale, {'_error': 'stale_action'})

        self.session.refresh_from_db()
        with patch(
            'clue_rush.consumers.timezone.now',
            return_value=self.session.answer_deadline,
        ):
            late = async_to_sync(consumer.save_participant_answer)(
                self.participant.name,
                self.participant.hub_session_code,
                'Brazil',
                self.question.id,
            )
        self.assertEqual(late, {'_error': 'deadline_expired'})
        self.assertFalse(ClueAnswer.objects.filter(participant=self.participant).exists())

    def test_duplicate_client_action_id_is_reserved_once_across_workers(self):
        snapshot = observe_snapshot(
            game_key='clue_rush',
            room_code=self.quiz.room_code,
            payload={
                'phase': 'active',
                'game': {'id': self.quiz.id, 'status': 'active'},
                'current_question_id': self.question.id,
                'starts_at': self.started['question_started_at'].isoformat(),
                'ends_at': self.started['answer_deadline'].isoformat(),
            },
        )
        action = {
            'client_action_id': str(uuid.uuid4()),
            'state_revision': snapshot['state_revision'],
            'game_id': str(self.quiz.id),
            'question_id': str(self.question.id),
            'round_id': snapshot.get('current_round_id'),
            'set_id': snapshot.get('current_set_id'),
        }

        first_worker = validate_and_reserve_action(
            game_key='clue_rush',
            room_code=self.quiz.room_code,
            session_code=None,
            participant_name=self.participant.name,
            action_type='participant_submit_answer',
            action=action,
        )
        second_worker = validate_and_reserve_action(
            game_key='clue_rush',
            room_code=self.quiz.room_code,
            session_code=None,
            participant_name=self.participant.name,
            action_type='participant_submit_answer',
            action=action,
        )

        self.assertTrue(first_worker.accepted)
        self.assertEqual(second_worker.code, 'already_submitted')

    def test_own_answer_and_lock_are_reconstructed_after_reconnect(self):
        consumer = self._consumer()
        submitted = async_to_sync(consumer.save_participant_answer)(
            self.participant.name,
            self.participant.hub_session_code,
            'Brazil',
            self.question.id,
        )
        self.assertTrue(submitted['is_correct'])

        restarted = self._consumer()
        snapshot = async_to_sync(restarted.get_state_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertTrue(snapshot['participant_state']['answer_locked'])
        self.assertEqual(
            snapshot['participant_state']['answer']['answer_text'],
            'Brazil',
        )
        self.assertEqual(
            snapshot['participant_state']['answer']['time_taken'],
            submitted['time_taken'],
        )

    def test_timeout_finalization_runs_once_across_workers(self):
        self.session.answer_deadline = timezone.now() - timezone.timedelta(seconds=1)
        self.session.save(update_fields=['answer_deadline'])
        first_worker = self._consumer()
        second_worker = self._consumer()

        first = async_to_sync(first_worker.finalize_current_question_for_end)(
            None,
            self.question.id,
        )
        second = async_to_sync(second_worker.finalize_current_question_for_end)(
            None,
            self.question.id,
        )

        self.assertTrue(first['newly_finalized'])
        self.assertFalse(second['newly_finalized'])
        self.assertEqual(
            ClueAnswer.objects.filter(
                quiz=self.quiz,
                participant=self.participant,
                question=self.question,
            ).count(),
            1,
        )
        timeout_answer = ClueAnswer.objects.get(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
        )
        self.assertEqual(timeout_answer.answer_text, '')

    def test_reconcile_after_worker_restart_finalizes_expired_question_once(self):
        first_worker = reconcile_clue_schedule(
            self.quiz.room_code,
            at=self.started['answer_deadline'],
        )
        second_worker = reconcile_clue_schedule(
            self.quiz.room_code,
            at=self.started['answer_deadline'] + timezone.timedelta(seconds=1),
        )

        self.assertTrue(first_worker['deadline_reached'])
        self.assertTrue(first_worker['finalization']['newly_finalized'])
        self.assertNotIn('finalization', second_worker)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)
        self.assertEqual(self.session.finalized_question_id, self.question.id)
        self.assertEqual(
            ClueAnswer.objects.filter(
                quiz=self.quiz,
                participant=self.participant,
                question=self.question,
            ).count(),
            1,
        )


class ClueRushManualQuestionPhaseTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='clue-phase-host')
        self.questions = []
        for question_index in range(2):
            question = ClueQuestion.objects.create(
                question_text=f'Clue phase question {question_index + 1}',
                answer='Brazil',
                points=3,
                time_limit=30,
                created_by=self.user,
            )
            for order, duration in enumerate((2, 3, 4), start=1):
                Clue.objects.create(
                    clue_question=question,
                    order=order,
                    clue_text=f'Question {question_index + 1} clue {order}',
                    duration=duration,
                )
            self.questions.append(question)
        self.quiz = ClueRushGame.objects.create(
            title='Manual Clue Rush',
            room_code='CRPH',
            creator=self.user,
            status='active',
            question_order=[question.id for question in self.questions],
        )
        self.quiz.selected_questions.set(self.questions)
        self.session = ClueRushSession.objects.create(quiz=self.quiz)
        self.participant = ClueRushParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUBCLUE',
        )
        self.consumer = ClueRushGameConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'cluerush_{self.quiz.room_code}'
        self.consumer.channel_layer = DummyClueChannelLayer()
        reset_question_flow(
            game_key='clue_rush',
            room_code=self.quiz.room_code,
            session_code='HUBCLUE',
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def action_for(self, question, *, action_id=None):
        snapshot = current_snapshot(
            'clue_rush',
            self.quiz.room_code,
            'HUBCLUE',
        )
        return {
            'question_id': question.id,
            'state_revision': snapshot['state_revision'],
            'game_id': snapshot['game_id'],
            'client_action_id': str(action_id or uuid.uuid4()),
        }

    def present(self, question=None, *, at=None, duration_override=None):
        question = question or self.questions[0]
        return async_to_sync(self.consumer.present_clue_question)(
            self.quiz.id,
            question.id,
            'HUBCLUE',
            self.action_for(question),
            duration_override,
            at,
        )

    def open_answering(self, question=None, *, at=None, action_id=None):
        question = question or self.questions[0]
        return async_to_sync(self.consumer.open_clue_answering)(
            self.quiz.id,
            question.id,
            'HUBCLUE',
            self.action_for(question, action_id=action_id),
            at,
        )

    def test_send_question_prepares_prompt_without_clues_deadline_or_input(self):
        capabilities = get_question_flow_capabilities('clue_rush')
        self.assertTrue(capabilities.uses_prompt_phase)
        self.assertFalse(capabilities.uses_content_phase)

        presented_at = timezone.now()
        decision = self.present(at=presented_at)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.snapshot['question_phase'], 'prompt_visible')
        self.assertEqual(
            parse_datetime(decision.snapshot['question_visible_at']),
            presented_at + timezone.timedelta(seconds=1),
        )
        self.assertEqual(self.quiz.current_question_id, self.questions[0].id)
        self.assertIsNone(self.quiz.question_start_time)
        self.assertIsNone(self.quiz.current_clue_id)
        self.assertFalse(self.session.is_question_active)
        self.assertEqual(self.session.clue_schedule, [])
        self.assertIsNone(self.session.answer_deadline)
        self.assertIsNone(self.session.question_end_time)
        self.assertIsNone(async_to_sync(self.consumer.store_pending_input)(
            self.participant.name,
            self.participant.hub_session_code,
            'Brazil',
            self.questions[0].id,
        ))
        self.assertEqual(
            async_to_sync(self.consumer.save_participant_answer)(
                self.participant.name,
                self.participant.hub_session_code,
                'Brazil',
                self.questions[0].id,
            ),
            {'_error': 'invalid_phase'},
        )

        early = self.open_answering(
            at=presented_at + timezone.timedelta(milliseconds=999),
        )
        self.assertFalse(early.accepted)
        self.assertEqual(early.code, 'question_not_visible')

    def test_reactivated_game_clears_stale_question_before_first_send(self):
        stale_question = self.questions[0]
        next_question = self.questions[1]
        stale_start = timezone.now() - timezone.timedelta(seconds=30)
        self.quiz.status = 'inactive'
        self.quiz.started_at = stale_start
        self.quiz.current_question = stale_question
        self.quiz.question_start_time = stale_start
        self.quiz.current_clue = stale_question.clues.order_by('order', 'id').first()
        self.quiz.clue_start_time = stale_start
        self.quiz.save()
        self.session.is_question_active = True
        self.session.current_question_number = 1
        self.session.total_questions_sent = 1
        self.session.question_end_time = timezone.now() + timezone.timedelta(seconds=10)
        self.session.answer_deadline = self.session.question_end_time
        self.session.clue_schedule = [{'question_id': stale_question.id}]
        self.session.save()
        hub_session = HubSession.objects.create(
            code='HUBREACT',
            name='Clue reactivation',
            creator=self.user,
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            locked_participant_count=1,
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='clue_rush',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        self.consumer.send = AsyncMock()

        async_to_sync(self.consumer.handle_admin_start_quiz)({
            'show_tutorial': False,
            'play_tutorial': False,
        })

        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.assertIsNone(self.quiz.current_question_id)
        self.assertIsNone(self.quiz.question_start_time)
        self.assertIsNone(self.quiz.current_clue_id)
        self.assertFalse(self.session.is_question_active)
        self.assertEqual(self.session.clue_schedule, [])
        self.assertIsNone(self.session.answer_deadline)
        self.assertIsNone(self.session.question_end_time)

        snapshot = current_snapshot(
            'clue_rush',
            self.quiz.room_code,
            hub_session.code,
        )
        decision = async_to_sync(self.consumer.present_clue_question)(
            self.quiz.id,
            next_question.id,
            hub_session.code,
            {
                'question_id': next_question.id,
                'state_revision': snapshot['state_revision'],
                'game_id': snapshot['game_id'],
                'client_action_id': str(uuid.uuid4()),
            },
        )

        self.assertTrue(decision.accepted, decision.code)

    def test_prepare_failure_rolls_back_phase_and_allows_retry(self):
        question = self.questions[0]
        action = self.action_for(question, action_id=uuid.uuid4())

        with patch(
            'clue_rush.consumers.prepare_question_schedule',
            return_value={'prepared': False, 'code': 'invalid_phase'},
        ):
            failed = async_to_sync(self.consumer.present_clue_question)(
                self.quiz.id,
                question.id,
                'HUBCLUE',
                action,
            )

        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        after_failure = current_snapshot(
            'clue_rush',
            self.quiz.room_code,
            'HUBCLUE',
        )
        self.assertFalse(failed.accepted)
        self.assertEqual(failed.code, 'invalid_phase')
        self.assertIsNone(self.quiz.current_question_id)
        self.assertFalse(self.session.is_question_active)
        self.assertEqual(self.session.clue_schedule, [])
        self.assertIsNone(self.session.answer_deadline)
        self.assertIsNone(after_failure['question_phase'])

        retry = async_to_sync(self.consumer.present_clue_question)(
            self.quiz.id,
            question.id,
            'HUBCLUE',
            action,
        )

        self.assertTrue(retry.accepted, retry.code)
        self.assertFalse(retry.duplicate)

    def test_participant_reconnect_publishes_revision_before_host_notification(self):
        host_notifications = []
        second_participant = ClueRushParticipant.objects.create(
            quiz=self.quiz,
            name='Grace',
            hub_session_code='HUBCLUE',
        )

        async def record_group_send(group_name, message):
            if message.get('type') == 'participant_joined':
                host_notifications.append(
                    await database_sync_to_async(current_snapshot)(
                        'clue_rush',
                        self.quiz.room_code,
                        'HUBCLUE',
                    )
                )

        async def observe_participant_state(data):
            participant_snapshot = await self.consumer.get_state_snapshot(
                data['participant_name'],
                data['hub_session'],
            )
            await database_sync_to_async(observe_snapshot)(
                'clue_rush',
                self.quiz.room_code,
                participant_snapshot,
                'HUBCLUE',
            )

        self.consumer.channel_layer.group_send = record_group_send
        self.consumer.send_state = observe_participant_state
        self.consumer.send = AsyncMock()

        for participant in (self.participant, second_participant):
            async_to_sync(self.consumer.handle_participant_join)({
                'participant_name': participant.name,
                'hub_session': 'HUBCLUE',
            })

        self.assertEqual(len(host_notifications), 2)
        latest = current_snapshot(
            'clue_rush',
            self.quiz.room_code,
            'HUBCLUE',
        )
        self.assertTrue(all(
            notification['state_revision'] == latest['state_revision']
            for notification in host_notifications
        ))
        action = {
            'question_id': self.questions[0].id,
            'state_revision': host_notifications[0]['state_revision'],
            'game_id': host_notifications[0]['game_id'],
            'client_action_id': str(uuid.uuid4()),
        }
        decision = async_to_sync(self.consumer.present_clue_question)(
            self.quiz.id,
            self.questions[0].id,
            'HUBCLUE',
            action,
        )
        self.assertTrue(decision.accepted, decision.code)

    def test_real_stale_question_action_is_rejected_and_retry_succeeds(self):
        stale_snapshot = current_snapshot(
            'clue_rush',
            self.quiz.room_code,
            'HUBCLUE',
        )
        observe_snapshot(
            'clue_rush',
            self.quiz.room_code,
            {
                'type': 'clue_rush_state',
                'phase': 'active',
                'game': {'id': self.quiz.id, 'status': 'active'},
                'current_question_id': None,
            },
            'HUBCLUE',
        )
        stale_decision = async_to_sync(self.consumer.present_clue_question)(
            self.quiz.id,
            self.questions[0].id,
            'HUBCLUE',
            {
                'question_id': self.questions[0].id,
                'state_revision': stale_snapshot['state_revision'],
                'game_id': stale_snapshot['game_id'],
                'client_action_id': str(uuid.uuid4()),
            },
        )
        self.assertFalse(stale_decision.accepted)
        self.assertEqual(stale_decision.code, 'stale_action')
        self.quiz.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)

        retry = self.present(self.questions[0])
        self.assertTrue(retry.accepted)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.questions[0].id)

    def test_duplicate_question_action_id_prepares_question_once(self):
        question = self.questions[0]
        action = self.action_for(question, action_id=uuid.uuid4())

        first = async_to_sync(self.consumer.present_clue_question)(
            self.quiz.id,
            question.id,
            'HUBCLUE',
            action,
        )
        duplicate = async_to_sync(self.consumer.present_clue_question)(
            self.quiz.id,
            question.id,
            'HUBCLUE',
            action,
        )

        self.session.refresh_from_db()
        self.assertTrue(first.accepted)
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(self.session.total_questions_sent, 1)
        self.assertEqual(self.session.current_question_number, 1)
        self.assertEqual(self.session.clue_schedule, [])

    def test_retry_recovers_same_prepared_question_without_double_counting(self):
        question = self.questions[0]
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])
        self.session.current_question_number = 1
        self.session.total_questions_sent = 1
        self.session.save(update_fields=['current_question_number', 'total_questions_sent'])

        decision = self.present(question)

        self.session.refresh_from_db()
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.snapshot['question_phase'], 'prompt_visible')
        self.assertEqual(self.session.current_question_number, 1)
        self.assertEqual(self.session.total_questions_sent, 1)
        self.assertEqual(self.session.clue_schedule, [])
        self.assertIsNone(self.session.answer_deadline)

    def test_start_clues_opens_answering_and_anchors_first_clue_and_deadline(self):
        presented = self.present(at=timezone.now() - timezone.timedelta(seconds=2))
        opened_at = timezone.now()
        action_id = uuid.uuid4()
        opened = self.open_answering(at=opened_at, action_id=action_id)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()

        self.assertTrue(opened.accepted)
        self.assertEqual(opened.snapshot['question_phase'], 'answering_open')
        self.assertEqual(self.quiz.question_start_time, opened_at)
        self.assertTrue(self.session.is_question_active)
        self.assertEqual(len(self.session.clue_schedule), 3)
        first_start = parse_datetime(self.session.clue_schedule[0]['starts_at'])
        self.assertEqual(first_start, opened_at)
        self.assertEqual(
            self.session.answer_deadline,
            opened_at + timezone.timedelta(seconds=9),
        )
        self.assertEqual(
            parse_datetime(opened.snapshot['answering_deadline_at']),
            self.session.answer_deadline,
        )
        first_reconcile = reconcile_clue_schedule(
            self.quiz.room_code,
            at=opened_at,
        )
        self.assertEqual(
            [clue['order'] for clue in first_reconcile['new_clues']],
            [1],
        )
        self.assertIsNotNone(async_to_sync(self.consumer.store_pending_input)(
            self.participant.name,
            self.participant.hub_session_code,
            'Bra',
            self.questions[0].id,
        ))
        answer = async_to_sync(self.consumer.save_participant_answer)(
            self.participant.name,
            self.participant.hub_session_code,
            'Brazil',
            self.questions[0].id,
        )
        self.assertTrue(answer['is_correct'])

        duplicate = self.open_answering(
            at=opened_at + timezone.timedelta(seconds=1),
            action_id=action_id,
        )
        self.session.refresh_from_db()
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(
            self.session.answer_deadline,
            opened_at + timezone.timedelta(seconds=9),
        )
        self.assertIsNone(presented.snapshot['answering_deadline_at'])

    def test_start_clues_handler_broadcasts_first_clue_immediately(self):
        self.present(at=timezone.now() - timezone.timedelta(seconds=2))
        action = {
            'type': 'admin_start_clues',
            'hub_session': 'HUBCLUE',
            **self.action_for(self.questions[0]),
        }
        with patch.object(
            self.consumer,
            '_ensure_clue_reconciliation_task',
        ) as ensure_task:
            async_to_sync(self.consumer.handle_admin_start_clues)(action)

        event_types = [message['type'] for _, message in self.consumer.channel_layer.sent]
        self.assertEqual(event_types[:2], [
            'question_answering_opened',
            'clue_started',
        ])
        self.assertEqual(self.consumer.channel_layer.sent[1][1]['clue']['order'], 1)
        ensure_task.assert_called_once_with(self.questions[0].id)

    def test_reconnect_between_clues_uses_persisted_absolute_schedule(self):
        self.present(at=timezone.now() - timezone.timedelta(seconds=2))
        opened_at = timezone.now()
        opened = self.open_answering(at=opened_at)
        second_clue_at = opened_at + timezone.timedelta(seconds=2)
        reconcile_clue_schedule(self.quiz.room_code, at=second_clue_at)

        snapshot = async_to_sync(self.consumer.get_state_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        phase_snapshot = current_snapshot(
            'clue_rush',
            self.quiz.room_code,
            'HUBCLUE',
        )
        self.assertEqual(
            [clue['order'] for clue in snapshot['revealed_clues']],
            [1, 2],
        )
        self.assertEqual(snapshot['ends_at'], opened.snapshot['answering_deadline_at'])
        self.assertEqual(phase_snapshot['question_phase'], 'answering_open')
        self.assertEqual(
            phase_snapshot['answering_deadline_at'],
            opened.snapshot['answering_deadline_at'],
        )

    def test_second_question_has_no_old_schedule_answer_or_deadline(self):
        first = self.questions[0]
        second = self.questions[1]
        self.present(first, at=timezone.now() - timezone.timedelta(seconds=2))
        self.open_answering(first, at=timezone.now())
        async_to_sync(self.consumer.finalize_current_question_for_end)(
            'HUBCLUE',
            first.id,
        )
        finish_question_flow(
            game_key='clue_rush',
            room_code=self.quiz.room_code,
            session_code='HUBCLUE',
            question_id=first.id,
        )

        second_decision = self.present(second, at=timezone.now())
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertTrue(second_decision.accepted)
        self.assertEqual(second_decision.snapshot['question_phase'], 'prompt_visible')
        self.assertEqual(self.quiz.current_question_id, second.id)
        self.assertIsNone(self.quiz.question_start_time)
        self.assertEqual(self.session.clue_schedule, [])
        self.assertIsNone(self.session.answer_deadline)
        self.assertFalse(self.session.is_question_active)
        self.assertFalse(CluePendingInput.objects.filter(quiz=self.quiz).exists())

    def test_templates_expose_only_send_and_start_clues_host_steps(self):
        host_source = Path(
            'templates/admin_dashboard/clue_rush_monitor.html'
        ).read_text(encoding='utf-8')
        participant_source = Path('templates/clue_rush/play.html').read_text(
            encoding='utf-8'
        )

        self.assertIn('FRAGE SENDEN', host_source)
        self.assertIn('HINWEISE STARTEN', host_source)
        self.assertIn("type: 'admin_start_clues'", host_source)
        self.assertNotIn('Send Next Clue', host_source)
        self.assertNotIn('FRAGE FREIGEBEN', host_source)
        self.assertIn("this.questionPhase === 'prompt_visible'", participant_source)
        self.assertIn("this.questionPhase !== 'answering_open'", participant_source)
        self.assertIn('question.question_visible_at || this.questionVisibleAt', participant_source)

    def test_monitor_sends_each_question_once_while_action_is_pending(self):
        host_source = Path(
            'templates/admin_dashboard/clue_rush_monitor.html'
        ).read_text(encoding='utf-8')
        send_handler = host_source.split('sendQuestion(questionId) {', 1)[1].split(
            '\n        hostActionContext(questionId) {',
            1,
        )[0]

        self.assertIn('!this.pendingQuestionAction', send_handler)
        self.assertEqual(
            send_handler.count('this.websocket.send(JSON.stringify(payload));'),
            1,
        )
        self.assertEqual(
            host_source.count("btn.addEventListener('click', (e) => {"),
            1,
        )
        self.assertIn('btn.disabled = true;', send_handler)
        self.assertIn('this.clearPendingQuestionAction();', host_source)
        self.assertNotIn('fetchState()', host_source)


class ClueRushVhsParticipantSafeguardTests(SimpleTestCase):
    def test_monitor_waits_for_confirmed_start_before_reloading(self):
        monitor = (
            Path(settings.BASE_DIR)
            / 'templates'
            / 'admin_dashboard'
            / 'clue_rush_monitor.html'
        ).read_text(encoding='utf-8')
        start_handler = monitor.split('startQuiz() {', 1)[1].split(
            '\n        setStartPending(pending) {',
            1,
        )[0]
        quiz_started_handler = monitor.split("case 'quiz_started':", 1)[1].split(
            "case 'tutorial_start':",
            1,
        )[0]

        self.assertIn('this.pendingStartReload = true;', start_handler)
        self.assertIn('this.setStartPending(true);', start_handler)
        self.assertNotIn('location.reload()', start_handler)
        self.assertNotIn('setTimeout(() => { hubWs.close(); location.reload(); }, 150)', monitor)
        self.assertIn('if (this.pendingStartReload) {', quiz_started_handler)
        self.assertIn('location.reload();', quiz_started_handler)
        self.assertIn("this.runKey = '{{ quiz.started_at|date:\"c\"|escapejs }}' || 'not-started';", monitor)
        self.assertIn("${this.hubSession || 'no-session'}_${this.runKey}", monitor)

    def test_clue_rush_vhs_hooks_are_scoped_and_use_authoritative_timers(self):
        base_dir = Path(settings.BASE_DIR)
        template = (base_dir / 'templates' / 'clue_rush' / 'play.html').read_text(encoding='utf-8')
        who_template = (
            base_dir / 'templates' / 'who_is_lying' / 'play.html'
        ).read_text(encoding='utf-8')
        css = (base_dir / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        accessibility = (
            base_dir / 'templates' / 'includes' / 'accessibility_widget.html'
        ).read_text(encoding='utf-8')

        self.assertIn("index.className = 'clue-rush-clue__index option-key d-none';", template)
        self.assertIn('vhs-action-button clue-rush-submit', template)
        self.assertIn('row justify-content-center qa-participant-stage', template)
        self.assertIn('col-lg-8 qa-participant-stage__content', template)
        self.assertIn('id="clueRushNextClueTimer"', template)
        for shared_timer_class in (
            'who-person-switch-timer',
            'who-person-switch-timer__fill',
        ):
            self.assertIn(shared_timer_class, who_template)
            self.assertIn(shared_timer_class, template)
        clue_timer_start = template.index('id="clueRushNextClueTimer"')
        clue_timer_end = template.index('id="answerOptions"', clue_timer_start)
        clue_timer_markup = template[clue_timer_start:clue_timer_end]
        self.assertNotIn('who-person-name-panel', clue_timer_markup)
        self.assertNotIn('class="person-name"', clue_timer_markup)
        self.assertIn('class="visually-hidden"', clue_timer_markup)
        self.assertIn("this.parseServerTimestamp(clue?.end_time)", template)
        self.assertIn("this.parseServerTimestamp(clue?.sequence_end_time)", template)
        self.assertIn("nextProgressEl.style.setProperty('--who-person-progress', progress)", template)
        self.assertIn("nextProgressEl.classList.add('is-resetting')", template)
        self.assertIn("input.placeholder = 'Antwort eingeben...';", template)
        self.assertNotIn('Type your answer here...', template)
        self.assertIn("legacyIndex.textContent = `#${clue.order}`;", template)
        self.assertIn("badge.textContent = 'New';", template)
        self.assertNotIn('let timeLeft = duration;', template)
        self.assertNotIn('--clue-rush-next-progress', template)

        clue_scope = (
            'html[data-participant-theme="vhs"] body.clue-rush-play-page '
            '.vhs-theme-shell'
        )
        self.assertIn(f'{clue_scope}\n  #questionState', css)
        self.assertIn(
            'body.clue-rush-play-page .vhs-theme-shell #questionState '
            '.who-person-switch-timer',
            css,
        )
        self.assertIn('grid-template-columns: minmax(0, 1fr) !important;', css)
        self.assertIn('align-content: start !important;', css)
        self.assertIn('#questionState .question-content > *', css)
        self.assertIn('--clue-answer-width: 560px;', css)
        self.assertNotIn('--clue-stack-height:', css)
        self.assertNotIn('height: var(--clue-stack-height);', css)
        self.assertIn('align-content: start;', css)
        self.assertIn(
            'body.clue-rush-play-page:has(#questionState:not(.d-none))',
            css,
        )
        self.assertIn('width: min(100%, var(--clue-answer-width));', css)
        self.assertIn(
            '--clue-rush-score-safe-area: clamp(270px, 27vw, 290px);',
            css,
        )
        self.assertNotIn('.clue-rush-next-clue-timer__track', css)
        self.assertIn('.clue-rush-clue__index', css)
        self.assertIn('.clue-rush-clue__legacy-index, .badge', css)
        self.assertIn('#questionState:has(#playerCluesList > :nth-child(7))', css)
        self.assertNotIn(
            '#questionState:has(#playerCluesList > :nth-child(7))\n'
            '  #participantQuestionLabel',
            css,
        )
        self.assertIn('@media (max-height: 780px)', css)
        self.assertIn('#submitAnswerBtn:disabled', css)
        self.assertIn("document.body.classList.contains('clue-rush-play-page')", accessibility)
        self.assertIn("document.getElementById('playerSetTimeLeft')", accessibility)
