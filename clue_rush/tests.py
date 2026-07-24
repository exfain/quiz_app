from pathlib import Path

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

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

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code

        first = async_to_sync(consumer.advance_next_clue)()
        second = async_to_sync(consumer.advance_next_clue)()
        third = async_to_sync(consumer.advance_next_clue)()

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
        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code

        async_to_sync(consumer.update_quiz_question)(quiz, question, 9)
        first = async_to_sync(consumer.advance_next_clue)()
        second = async_to_sync(consumer.advance_next_clue)()

        quiz.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(first['duration'], 9)
        self.assertEqual(second['duration'], 9)
        self.assertTrue(first['has_next_clue'])
        self.assertTrue(first['end_time'])
        self.assertTrue(first['sequence_end_time'])
        self.assertEqual(first['sequence_duration'], 18)
        self.assertTrue(first['server_now'])
        self.assertIsNotNone(session.question_end_time)
        self.assertEqual(int((session.question_end_time - quiz.question_start_time).total_seconds()), 9)

    def test_runtime_without_override_keeps_original_clue_durations(self):
        user = User.objects.create_user(username='clue-runtime-default')
        question = self._create_question_with_durations(user, durations=[5, 8, 11], time_limit=90)
        quiz, session = self._create_active_quiz(user, question, room_code='8422')
        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code

        async_to_sync(consumer.update_quiz_question)(quiz, question, None)
        first = async_to_sync(consumer.advance_next_clue)()
        second = async_to_sync(consumer.advance_next_clue)()
        third = async_to_sync(consumer.advance_next_clue)()

        session.refresh_from_db()
        self.assertEqual(first['duration'], 5)
        self.assertEqual(second['duration'], 8)
        self.assertEqual(third['duration'], 11)
        self.assertEqual(first['sequence_duration'], 13)
        self.assertEqual(second['sequence_duration'], 13)
        self.assertEqual(third['sequence_duration'], 13)
        self.assertIsNone(session.question_end_time)

    def test_rejoin_snapshot_restores_authoritative_clue_and_sequence_deadlines(self):
        user = User.objects.create_user(username='clue-timer-rejoin')
        question = self._create_question_with_durations(user, durations=[5, 8, 11], time_limit=90)
        quiz, session = self._create_active_quiz(user, question, room_code='8435')
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )
        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code

        async_to_sync(consumer.update_quiz_question)(quiz, question, None)
        live_clue = async_to_sync(consumer.advance_next_clue)()
        snapshot = async_to_sync(consumer.get_rejoin_snapshot)(
            participant.name,
            participant.hub_session_code,
        )

        self.assertTrue(snapshot['question']['question_started_at'])
        self.assertTrue(snapshot['question']['server_now'])
        self.assertEqual(len(snapshot['revealed_clues']), 1)
        rejoined_clue = snapshot['revealed_clues'][0]
        self.assertEqual(rejoined_clue['question_id'], question.id)
        self.assertEqual(rejoined_clue['end_time'], live_clue['end_time'])
        self.assertEqual(rejoined_clue['sequence_end_time'], live_clue['sequence_end_time'])
        self.assertEqual(rejoined_clue['sequence_duration'], 13)

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

        late_submit = async_to_sync(consumer.save_participant_answer)(empty_player.name, None, 'Brazil', 1.0)
        self.assertIsNone(late_submit)
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


class ClueRushVhsParticipantSafeguardTests(SimpleTestCase):
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
