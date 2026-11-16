import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from sorting_ladder import models as m
from sorting_ladder.consumers import SortingLadderGameConsumer, _set_number_for_game
from games_hub.models import HubSession, HubGameStep
from games_hub.authoritative_state import current_snapshot
from games_hub.spectator import _serialize_sorting_ladder

class SortingPlayabilityTests(TransactionTestCase):

    def setUp(self):
        self.user = User.objects.create_user('sorting-audit', is_staff=True)
        self.hub = HubSession.objects.create(code='SORTAUDIT', creator=self.user, is_active=True, started_at=timezone.now(), check_in_status='completed', locked_participant_count=2)
        self.quiz = m.SortingLadderGame.objects.create(creator=self.user, status='active', started_at=timezone.now())
        self.questions = []
        for n in range(2):
            q = m.SortingQuestion.objects.create(created_by=self.user, question_text='Sort ' + str(n), round_time_limit=30)
            items = [m.SortingItem.objects.create(topic=q, text='Item ' + str(i), correct_rank=i) for i in range(4)]
            q.starting_item = items[0]
            q.save()
            self.questions.append(q)
        self.question = self.questions[0]
        self.items = list(self.question.elements.all())
        self.quiz.selected_questions.set(self.questions)
        self.session = m.SortingLadderSession.objects.create(quiz=self.quiz)
        HubGameStep.objects.create(session=self.hub, order=0, game_key='sorting_ladder', room_code=self.quiz.room_code)
        self.alice = m.SortingLadderParticipant.objects.create(quiz=self.quiz, name='Alice', hub_session_code=self.hub.code)
        self.bob = m.SortingLadderParticipant.objects.create(quiz=self.quiz, name='Bob', hub_session_code=self.hub.code)
        self.consumer = SortingLadderGameConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = 'sortingladder_' + self.quiz.room_code
        self.consumer.scope = {'user': self.user}
        self.consumer.send = AsyncMock()
        self.consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
        self.consumer._authoritative_session = self.hub.code
        self.consumer._authoritative_participant = 'Alice'

    def start(self, q=None):
        payload = async_to_sync(self.consumer.initialize_question_for_quiz)(self.quiz.pk, (q or self.question).pk, 30, self.hub.code)
        self.assertTrue(payload)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.items = list((q or self.question).elements.all())

    def save(self, p=None, ids=None):
        p = p or self.alice
        ids = ids or self.visible_order()
        return async_to_sync(self.consumer.save_round_full_order)(p.name, self.hub.code, ids)

    def visible_order(self):
        visible = [int(pk) for pk in self.session.shuffled_item_ids.split(',')][:self.session.current_round + 1]
        return list(m.SortingItem.objects.filter(pk__in=visible).order_by('correct_rank').values_list('pk', flat=True))

    def pending(self, p=None):
        p = p or self.alice
        return async_to_sync(self.consumer.update_pending_round_selection_db)(p.name, p.hub_session_code, self.visible_order())

    def next(self):
        result = async_to_sync(self.consumer.start_next_round_db)(self.quiz.pk)
        self.session.refresh_from_db()
        return result

    def complete(self):
        for index in range(3):
            self.save()
            self.save(self.bob)
            if index < 2:
                self.next()
        async_to_sync(self.consumer.end_question_db)(self.quiz.pk)
        async_to_sync(self.consumer.reveal_solution_db)(self.quiz.pk)
        async_to_sync(self.consumer.clear_revealed_question_db)(self.quiz.pk)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()

    def test_repeat_keeps_previous_answers_and_scores(self):
        self.start()
        self.complete()
        self.start()
        self.complete()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 6)
        self.assertEqual(self.alice.submissions.count(), 6)
        history = async_to_sync(self.consumer.get_participant_progress_history)('Alice', self.hub.code)
        self.assertEqual([row['question_number'] for row in history], [1, 2])
        self.assertEqual([row['survived_rounds'] for row in history], [3, 3])

    def test_manual_total_survives_next_round_and_final_standings(self):
        self.start()
        self.save()
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:set_sorting_ladder_participant_score'), json.dumps({'participant_id': self.alice.pk, 'score': 7}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.next()
        self.save()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 8)
        scores = async_to_sync(self.consumer.get_final_scores)()
        self.assertEqual(next((row for row in scores if row['name'] == 'Alice'))['total_score'], 8)

    def test_pause_freezes_timers_and_pending_order(self):
        from games_hub.matching_runtime import set_paused
        self.start()
        self.pending()
        deadline = self.session.round_end_time
        at = timezone.now()
        started = self.quiz.started_at
        self.assertTrue(set_paused(self.quiz, 'sorting_ladder', True, at=at))
        self.assertIsNone(self.next())
        self.assertIsNone(self.save())
        snapshot = async_to_sync(self.consumer.get_rejoin_snapshot)('Alice', self.hub.code)
        self.assertTrue(snapshot['paused'])
        self.assertTrue(snapshot['own_selection'])
        self.assertTrue(set_paused(self.quiz, 'sorting_ladder', False, at=at + timedelta(seconds=60)))
        self.session.refresh_from_db()
        self.quiz.refresh_from_db()
        self.assertEqual(self.session.round_end_time, deadline + timedelta(seconds=60))
        self.assertEqual(self.session.current_round, 1)
        self.assertEqual(self.quiz.started_at, started)

    def test_end_keeps_completed_round_and_discards_pending(self):
        from games_hub.matching_runtime import end_game
        self.start()
        self.save()
        self.next()
        self.pending()
        end_game(self.quiz, 'sorting_ladder')
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 1)
        self.assertFalse(m.SortingPendingRoundSelection.objects.exists())
        self.assertIsNone(self.quiz.current_question_id)
        self.assertFalse(self.session.is_round_active)
        self.assertIsNone(self.session.round_end_time)

    def test_foreign_result_does_not_reveal_order(self):
        self.start()
        result = self.save()
        other = SortingLadderGameConsumer()
        other.room_code = self.quiz.room_code
        other.send = AsyncMock()
        other._authoritative_participant = 'Bob'
        other._authoritative_session = self.hub.code
        async_to_sync(other.round_result)({'participant_name': 'Alice', 'hub_session_code': self.hub.code, **result})
        other.send.assert_not_called()

    def test_other_pool_is_not_eliminated_or_scored(self):
        self.start()
        foreign = m.SortingLadderParticipant.objects.create(quiz=self.quiz, name='OtherPool', hub_session_code='OTHER')
        self.save()
        self.save(self.bob)
        self.next()
        foreign.refresh_from_db()
        self.assertFalse(foreign.is_eliminated)
        self.assertFalse(foreign.submissions.exists())

    def test_stale_host_cannot_close_next_question(self):
        self.start()
        stale = {'question_id': self.question.pk, 'expected_set': self.session.current_set_number, 'expected_round': 3}
        self.complete()
        self.start(self.questions[1])
        self.next()
        self.next()
        async_to_sync(self.consumer.handle_admin_end_question)(stale)
        self.session.refresh_from_db()
        self.assertEqual(self.session.reveal_state, 'active')
        self.assertTrue(self.session.is_round_active)

    def test_legacy_end_evaluates_pending_before_closing(self):
        self.start()
        self.pending()
        async_to_sync(self.consumer.end_round_db)(self.quiz.pk)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 1)
        self.assertFalse(self.alice.is_eliminated)

    def test_pending_and_locked_answer_recovered_from_snapshot(self):
        self.start()
        self.pending()
        state = async_to_sync(self.consumer.get_rejoin_snapshot)('Alice', self.hub.code)
        self.assertEqual(state['own_selection'], self.visible_order())
        self.save()
        state = async_to_sync(self.consumer.get_rejoin_snapshot)('Alice', self.hub.code)
        self.assertTrue(state['answer_locked'])

    def test_snapshot_fallback_uses_existing_identity(self):
        self.start()
        async_to_sync(self.consumer.send_state)({'participant_name': 'Alice', 'hub_session': self.hub.code})
        payload = json.loads(self.consumer.send.call_args.kwargs['text_data'])
        self.assertEqual(payload['type'], 'sorting_state')
        self.assertIn('question_payload', payload)
        async_to_sync(self.consumer.handle_participant_join)({'name': 'Ghost', 'hub_session_code': self.hub.code})
        self.assertFalse(self.quiz.participants.filter(name='Ghost').exists())

    def test_spectator_anchor_uses_actual_shuffled_first_item(self):
        self.question.starting_item = None
        self.question.save()
        self.start()
        self.save()
        state = _serialize_sorting_ladder(self.quiz, self.hub)
        anchor = int(self.session.shuffled_item_ids.split(',')[0])
        self.assertEqual(state['question']['fixed_element']['id'], anchor)
        self.assertNotIn(anchor, [row['id'] for row in state['question']['items']])
        self.assertEqual(state['response_count'], 1)

    def test_out_of_order_first_set_displays_one_everywhere(self):
        self.start(self.questions[1])
        state = async_to_sync(self.consumer.get_rejoin_snapshot)('Alice', self.hub.code)
        spectator = _serialize_sorting_ladder(self.quiz, self.hub)
        self.assertEqual(state['question_payload']['question']['display_set_number'], 1)
        self.assertEqual(spectator['progress'], {'kind': 'set', 'current': 1, 'total': 2})

    def test_live_item_edit_cannot_invalidate_current_schedule(self):
        self.start()
        oldids = list(self.question.elements.values_list('id', flat=True))
        self.client.force_login(self.user)
        r = self.client.post(reverse('admin_dashboard:update_sorting_topic'), {'topic_id': self.question.pk, 'points': '10', 'round_time_limit': '30', 'items_json': json.dumps([{'text': 'New ' + str(i), 'order': i} for i in range(4)])})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(oldids, list(self.question.elements.values_list('id', flat=True)))

    def test_public_participants_scope(self):
        m.SortingLadderParticipant.objects.create(quiz=self.quiz, name='OtherPool', hub_session_code='OTHER')
        r = self.client.get(reverse('sorting_ladder:api_participants', args=[self.quiz.room_code]), {'hub_session': self.hub.code})
        self.assertEqual({p['name'] for p in r.json()['participants']}, {'Alice', 'Bob'})

    def test_drafts_cannot_overwrite_confirmed_or_expired_round(self):
        self.start()
        self.save()
        self.assertFalse(self.pending())
        self.session.round_end_time = timezone.now() - timedelta(seconds=1)
        self.session.save()
        self.assertFalse(self.pending(self.bob))

    def test_standalone_submission_accepts_null_hub(self):
        self.start()
        solo = m.SortingLadderParticipant.objects.create(quiz=self.quiz, name='Solo')
        async_to_sync(self.consumer.handle_participant_submit_round)({'participant_name': 'Solo', 'hub_session_code': None, 'ordered_item_ids': self.visible_order()})
        self.assertEqual(solo.submissions.count(), 1)

    def test_score_migration_preserves_existing_total_and_answers(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        self.start()
        self.save()
        m.SortingLadderParticipant.objects.filter(pk=self.alice.pk).update(total_score=7)
        before = list(m.RoundSubmission.objects.values_list('pk', 'all_elements', 'is_correct'))
        importlib.import_module('sorting_ladder.migrations.0016_sets_and_score_adjustment').preserve_scores(apps, SimpleNamespace(connection=connection))
        self.alice.refresh_from_db()
        self.alice.calculate_total_score()
        self.assertEqual(self.alice.total_score, 7)
        self.assertEqual(before, list(m.RoundSubmission.objects.values_list('pk', 'all_elements', 'is_correct')))

    def test_shared_events_contain_no_private_ordering(self):
        self.start()
        async_to_sync(self.consumer.hub_mirror_event)('round_result', {'correct_order_ids': [1, 2], 'participant_name': 'Alice', 'progress_history': []})
        event = self.consumer.channel_layer.group_send.call_args.args[1]
        self.assertNotIn('correct_order_ids', event)
        self.assertNotIn('progress_history', event)
        self.pending()
        context = {'question_id': self.question.pk, 'expected_set': self.session.current_set_number, 'expected_round': self.session.current_round}
        async_to_sync(self.consumer.handle_admin_start_round)(context)
        events = [call.args[1] for call in self.consumer.channel_layer.group_send.call_args_list]
        started = next(event for event in events if event.get('type') == 'round_started')
        self.assertNotIn('auto_results', started['round'])

    def test_tutorial_has_physical_set_identity_without_official_counter_or_points(self):
        from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime, start_unit_tutorial_if_needed
        self.quiz.tutorial_question = self.question
        self.quiz.save()
        prepare_unit_tutorial_runtime('sorting_ladder', self.quiz.room_code, self.hub.code, True, validate=False)
        start_unit_tutorial_if_needed('sorting_ladder', self.quiz.room_code, self.hub.code)
        self.start()
        context = {'question_id': self.question.pk, 'expected_set': self.session.current_set_number, 'expected_round': 1}
        self.assertTrue(async_to_sync(self.consumer.validate_host_set_action)(context))
        self.complete()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 0)

    def test_http_send_uses_question_flow_and_http_end_discards_draft(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:send_sorting_ladder_topic', args=[self.quiz.room_code]) + '?hub_session=' + self.hub.code, json.dumps({'topic_id': self.question.pk}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertTrue(self.session.shuffled_item_ids)
        self.assertEqual(self.session.current_set_number, 1)
        self.save()
        self.next()
        self.pending()
        response = self.client.post(reverse('admin_dashboard:end_sorting_ladder_game_by_room_code', args=[self.quiz.room_code]) + '?hub_session=' + self.hub.code)
        self.assertEqual(response.status_code, 200)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 1)
        self.assertFalse(m.SortingPendingRoundSelection.objects.exists())

    def test_inactive_game_cannot_start_a_new_question(self):
        self.quiz.status = 'inactive'
        self.quiz.save()
        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': self.question.pk, 'hub_session': self.hub.code})
        self.quiz.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)
        self.assertEqual(json.loads(self.consumer.send.call_args.kwargs['text_data'])['code'], 'invalid_phase')
