import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from Assign import models as m
from Assign.consumers import AssignConsumer
from Assign.runtime import start_set_runtime, store_round_selection, evaluate_and_advance, persist_final_answers, mark_revealed, mark_ended
from games_hub.models import HubSession, HubGameStep
from games_hub.authoritative_state import current_snapshot, reset_question_flow
from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime, start_unit_tutorial_if_needed

class AssignPlayabilityTests(TransactionTestCase):

    def setUp(self):
        self.user = User.objects.create_user('assign-audit', is_staff=True)
        self.hub = HubSession.objects.create(code='ASSAUDIT', creator=self.user, is_active=True, started_at=timezone.now(), check_in_status='completed', locked_participant_count=2)
        self.quiz = m.AssignQuiz.objects.create(creator=self.user, status='active', started_at=timezone.now())
        self.questions = []
        for n in range(2):
            self.questions.append(m.AssignQuestion.objects.create(created_by=self.user, question_text='Match ' + str(n), left_items=['A', 'B', 'C'], right_items=['X', 'Y', 'Z'], correct_matches={'0': 0, '1': 1, '2': 2}, time_limit=30))
        self.question = self.questions[0]
        self.quiz.selected_questions.set(self.questions)
        self.session = m.AssignSession.objects.create(quiz=self.quiz)
        HubGameStep.objects.create(session=self.hub, order=0, game_key='assign', room_code=self.quiz.room_code)
        self.alice = m.AssignParticipant.objects.create(quiz=self.quiz, name='Alice', hub_session_code=self.hub.code)
        self.bob = m.AssignParticipant.objects.create(quiz=self.quiz, name='Bob', hub_session_code=self.hub.code)
        self.consumer = AssignConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = 'assign_' + self.quiz.room_code
        self.consumer.scope = {'user': self.user}
        self.consumer.send = AsyncMock()
        self.consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
        self.consumer._authoritative_session = self.hub.code
        self.consumer._authoritative_participant = 'Alice'

    def start(self, q=None):
        self.runtime, _ = start_set_runtime(quiz_id=self.quiz.pk, question_id=(q or self.question).pk, hub_session_code=self.hub.code, effective_time_limit=30)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()

    def select(self, participant, left, lock=True):
        mapping = self.runtime.question.get_randomized_items(self.quiz.room_code)['position_to_original']
        right = next((k for k, v in mapping.items() if v == left))
        return store_round_selection(room_code=self.quiz.room_code, participant_name=participant.name, hub_session_code=self.hub.code, question_id=self.runtime.question_id, round_index=self.runtime.current_round_index, left_item_index=left, user_match={str(left): right}, lock_selection=lock)

    def advance(self):
        result = evaluate_and_advance(room_code=self.quiz.room_code, hub_session_code=self.hub.code, expected_round=self.runtime.current_round_index, set_number=self.runtime.set_number)
        self.runtime.refresh_from_db()
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        return result

    def finish(self):
        persist_final_answers(self.runtime)
        mark_revealed(self.quiz.room_code, self.hub.code)
        mark_ended(self.quiz.room_code, self.hub.code)
        self.quiz.current_question = None
        self.quiz.save()
        self.session.is_question_active = False
        self.session.save()

    def complete(self):
        for index in range(3):
            self.select(self.alice, index)
            self.select(self.bob, index)
            self.advance()
        self.finish()

    def test_independent_pairs_survive_all_three_rounds(self):
        self.start()
        for left_a, left_b in [(0, 1), (1, 2), (2, 0)]:
            self.assertTrue(self.select(self.alice, left_a).accepted)
            self.assertTrue(self.select(self.bob, left_b).accepted)
            result = self.advance()
            self.assertTrue(all((row['is_correct'] for row in result['outcomes'])))
            if not result['completed']:
                for participant in (self.alice, self.bob):
                    state = async_to_sync(self.consumer.get_rejoin_snapshot)(participant.name, self.hub.code)
                    solved = state['solved_pairs']
                    self.assertEqual(len(solved), self.runtime.current_round_index)
                    self.assertEqual(len(state['question']['right_items']), 3 - len(solved))
        persist_final_answers(self.runtime)
        self.assertEqual(list(self.quiz.participants.values_list('total_score', flat=True)), [3, 3])

    def test_solved_pair_cannot_be_reused(self):
        self.start()
        self.select(self.alice, 0)
        self.advance()
        result = self.select(self.alice, 0)
        self.assertFalse(result.accepted)

    def test_repeated_question_is_separate_scored_set(self):
        from Assign.scoreboard import build_participant_progress_history
        from games_hub.set_progress import set_progress
        self.start()
        self.complete()
        self.start()
        self.complete()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 6)
        self.assertEqual(m.AssignAnswer.objects.filter(participant=self.alice).count(), 2)
        history = build_participant_progress_history(self.quiz, self.alice, self.hub.code)
        self.assertEqual([row['survived_rounds'] for row in history], [3, 3])
        self.assertEqual(set_progress(self.quiz, 'assign', self.hub.code)['total'], 3)

    def test_manual_total_is_preserved_when_new_points_arrive(self):
        self.start()
        self.complete()
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:set_assign_participant_score'), json.dumps({'participant_id': self.alice.pk, 'score': 7}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.start(self.questions[1])
        self.complete()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 10)

    def test_tutorial_does_not_award_official_points(self):
        self.quiz.tutorial_question = self.question
        self.quiz.save()
        prepare_unit_tutorial_runtime('assign', self.quiz.room_code, self.hub.code, True, validate=False)
        start_unit_tutorial_if_needed('assign', self.quiz.room_code, self.hub.code)
        self.start()
        self.complete()
        self.assertEqual(list(self.quiz.participants.values_list('total_score', flat=True)), [0, 0])

    def test_pause_freezes_deadline_selection_and_round(self):
        from games_hub.matching_runtime import set_paused
        self.start()
        self.select(self.alice, 0, False)
        deadline = self.runtime.round_ends_at
        at = timezone.now()
        started = self.quiz.started_at
        self.assertTrue(set_paused(self.quiz, 'assign', True, at=at))
        self.assertFalse(self.advance()['advanced'])
        self.assertTrue(set_paused(self.quiz, 'assign', False, at=at + timedelta(seconds=60)))
        self.runtime.refresh_from_db()
        self.quiz.refresh_from_db()
        self.assertEqual(self.runtime.round_ends_at, deadline + timedelta(seconds=60))
        self.assertEqual(self.runtime.current_round_index, 0)
        self.assertEqual(self.quiz.started_at, started)
        state = async_to_sync(self.consumer.get_rejoin_snapshot)('Alice', self.hub.code)
        self.assertTrue(state['participant_state']['selection'])

    def test_stale_set_action_does_not_advance_new_set(self):
        self.start()
        stale = {'question_id': self.question.pk, 'expected_set': self.runtime.set_number, 'expected_round': 0}
        self.complete()
        self.start(self.questions[1])
        async_to_sync(self.consumer.handle_admin_next_round)(stale)
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.current_round_index, 0)

    def test_legacy_full_set_http_submission_is_rejected_without_solution(self):
        self.start()
        response = self.client.post(reverse('assign:submit_answer', args=[self.quiz.room_code, 'Alice']) + '?hub_session=' + self.hub.code, json.dumps({'user_matches': {'0': 0, '1': 1, '2': 2}}), content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('correct_matches', response.json())
        self.assertFalse(m.AssignAnswer.objects.exists())

    def test_legacy_full_set_socket_submission_does_not_award_points(self):
        self.start()
        async_to_sync(self.consumer.handle_participant_submit_answer)({'participant_name': 'Alice', 'hub_session': self.hub.code, 'user_matches': {'0': 0}})
        self.assertFalse(m.AssignAnswer.objects.exists())

    def test_end_keeps_completed_round_and_discards_open_selection(self):
        from games_hub.matching_runtime import end_game
        self.start()
        self.select(self.alice, 0)
        self.select(self.bob, 1)
        self.advance()
        self.select(self.alice, 1, False)
        end_game(self.quiz, 'assign')
        self.runtime.refresh_from_db()
        self.session.refresh_from_db()
        self.alice.refresh_from_db()
        self.quiz.refresh_from_db()
        self.assertEqual(self.alice.total_score, 1)
        self.assertEqual(self.runtime.phase, 'ended')
        self.assertFalse(self.session.is_question_active)
        self.assertIsNone(self.quiz.current_question_id)
        self.assertFalse(self.runtime.participant_round_states.filter(evaluated_at__isnull=True).exclude(user_match={}).exists())
        end_game(self.quiz, 'assign')
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 1)

    def test_eliminated_player_can_reload_shared_solution(self):
        self.start()
        for _ in range(3):
            self.advance()
        persist_final_answers(self.runtime)
        mark_revealed(self.quiz.room_code, self.hub.code)
        state = async_to_sync(self.consumer.get_rejoin_snapshot)('Alice', self.hub.code)
        self.assertTrue(state['participant_state']['eliminated'])
        self.assertTrue(state['solution'])
        self.assertEqual(state['phase'], 'revealed')

    def test_live_question_edit_is_blocked_before_grading(self):
        self.start()
        self.select(self.alice, 0)
        self.client.force_login(self.user)
        r = self.client.post(reverse('admin_dashboard:update_assign_question'), json.dumps({'question_id': self.question.pk, 'correct_matches': {'0': 1, '1': 0, '2': 2}}), content_type='application/json')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(next((row for row in self.advance()['outcomes'] if row['participant_name'] == 'Alice'))['is_correct'])

    def test_spectator_and_player_share_actual_set_number(self):
        from games_hub.spectator import _serialize_assign
        from Assign.scoreboard import build_question_scoreboard
        self.quiz.question_order = [q.pk for q in self.questions]
        self.quiz.save()
        self.start(self.questions[1])
        state = _serialize_assign(self.quiz, self.hub)
        board = build_question_scoreboard(self.quiz, self.alice, self.hub.code)
        self.assertEqual(state['progress'], {'kind': 'set', 'current': 1, 'total': 2})
        self.assertEqual(board[0]['id'], self.questions[1].pk)

    def test_score_migration_preserves_existing_manual_total_and_answers(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        self.start()
        self.complete()
        m.AssignParticipant.objects.filter(pk=self.alice.pk).update(total_score=7)
        before = list(m.AssignAnswer.objects.values_list('pk', 'user_matches', 'points_earned'))
        importlib.import_module('Assign.migrations.0019_set_answers_and_score_adjustment').preserve_scores(apps, SimpleNamespace(connection=connection))
        self.alice.refresh_from_db()
        self.alice.calculate_score()
        self.assertEqual(self.alice.total_score, 7)
        self.assertEqual(before, list(m.AssignAnswer.objects.values_list('pk', 'user_matches', 'points_earned')))

    def test_http_end_discards_draft_and_persists_completed_round(self):
        self.start()
        self.select(self.alice, 0)
        self.advance()
        self.select(self.alice, 1, False)
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:end_assign_quiz_by_room_code', args=[self.quiz.room_code]) + '?hub_session=' + self.hub.code)
        self.assertEqual(response.status_code, 200)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 1)
        self.quiz.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)

    def test_shared_guard_pause_and_resume_preserve_runtime(self):
        from games_hub.active_game_guard import _set_game_status
        self.start()
        self.select(self.alice, 0, False)
        _set_game_status(self.quiz, 'inactive')
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.current_round_index, 0)
        _set_game_status(self.quiz, 'active')
        self.assertTrue(self.select(self.alice, 0).accepted)

    def test_deleting_current_question_cannot_destroy_running_round(self):
        self.start()
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:delete_assign_question'), json.dumps({'question_id': self.question.pk}), content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.runtime.refresh_from_db()
        self.assertTrue(self.select(self.alice, 0).accepted)

    def test_host_snapshot_does_not_publish_unavailable_participant_state(self):
        self.start()
        self.consumer._authoritative_participant = ''
        async_to_sync(self.consumer.send_state)({})
        state = json.loads(self.consumer.send.call_args.kwargs['text_data'])
        self.assertNotEqual(state['phase'], 'unavailable')

    def test_inactive_game_cannot_start_a_new_question(self):
        self.quiz.status = 'inactive'
        self.quiz.save()
        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': self.question.pk, 'hub_session': self.hub.code})
        self.quiz.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)
        self.assertEqual(json.loads(self.consumer.send.call_args.kwargs['text_data'])['code'], 'invalid_phase')
