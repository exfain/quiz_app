import json, uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from wer_weiss_mehr import tests as fixtures, models as m, services as s
from wer_weiss_mehr.consumers import WerWeissMehrConsumer
from games_hub.models import HubSession, HubGameStep
from games_hub.authoritative_state import reset_question_flow, current_snapshot
from wer_weiss_mehr.identity import issue_player_token

class WwmPlayabilityTests(TransactionTestCase):

    def setUp(self):
        fixtures.WerWeissMehrRuntimeTests.setUp(self)
        self.user.is_staff = True
        self.user.save()
        self.game.started_at = timezone.now()
        self.game.save()
        self.hub = HubSession.objects.create(code='ABC', creator=self.user, is_active=True, started_at=timezone.now(), check_in_status='completed', locked_participant_count=2)
        HubGameStep.objects.create(session=self.hub, order=0, game_key='wer_weiss_mehr', room_code=self.game.room_code)
        self.q2 = m.WerWeissMehrQuestion.objects.create(created_by=self.user, question_text='Andere Frage')
        m.WerWeissMehrAnswerOption.objects.create(question=self.q2, canonical_text='Zwei')
        self.game.selected_questions.add(self.q2)
        self.game.question_order = [self.question.pk, self.q2.pk]
        self.game.save()
        self.consumer = WerWeissMehrConsumer()
        self.consumer.room_code = self.game.room_code
        self.consumer.room_group_name = 'werweissmehr_' + self.game.room_code
        self.consumer.scope = {'user': self.user}
        self.consumer.send_json = AsyncMock()
        self.consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
        self.consumer._authoritative_session = self.hub.code

    def start(self, q=None):
        s.start_set(self.game, (q or self.question).pk, self.hub.code)
        self.game.refresh_from_db()
        self.session = m.WerWeissMehrSession.objects.get(quiz=self.game)

    def end(self):
        s.end_current_round(self.game)
        self.session.refresh_from_db()

    def complete(self):
        s.submit_answer(self.game, self.p1, 'Bayern')
        s.submit_answer(self.game, self.p2, 'Bayern')
        self.end()
        s.finish_set(self.game)
        s.clear_current_set(self.game)
        self.game.refresh_from_db()
        self.session.refresh_from_db()

    def context(self):
        state = s.build_game_state(self.game, self.hub.code, self.p1.name)
        return {key: state.get(field) for key, field in [('game_id', 'game_id'), ('question_id', 'current_question_id'), ('round_id', 'current_round_id'), ('set_id', 'current_set_id'), ('state_revision', 'state_revision')]}

    def post(self, name, participant=None, **data):
        payload = {'hub_session': self.hub.code, **self.context(), **data}
        if participant:
            payload['participant_name'] = participant.name
            payload.setdefault('player_token', issue_player_token(participant))
        return self.client.post(reverse('wer_weiss_mehr:' + name, args=[self.game.room_code]), json.dumps(payload), content_type='application/json')

    def test_two_players_same_answer_is_valid_but_previously_revealed_answer_is_not(self):
        self.start()
        s.submit_answer(self.game, self.p1, 'Bayern')
        s.submit_answer(self.game, self.p2, 'Bayern')
        self.end()
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual([self.p1.total_score, self.p2.total_score], [1, 1])
        s.next_round_or_finish(self.game)
        self.assertFalse(s.submit_answer(self.game, self.p1, 'Bayern').is_correct)

    def test_pause_preserves_points_answers_and_freezes_deadline(self):
        from wer_weiss_mehr.runtime import set_paused
        self.start()
        s.submit_answer(self.game, self.p1, 'Bayern')
        s.submit_answer(self.game, self.p2, 'Bayern')
        self.end()
        s.next_round_or_finish(self.game)
        self.session.refresh_from_db()
        s.store_pending_input(self.game, self.p1, 'Saarland')
        before = self.session.round_end_time
        at = timezone.now()
        started = self.game.started_at
        set_paused(self.game, True, at=at)
        self.game.refresh_from_db()
        self.assertIsNone(s.store_pending_input(self.game, self.p1, 'late'))
        set_paused(self.game, False, at=at + timedelta(seconds=60))
        self.session.refresh_from_db()
        self.game.refresh_from_db()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.total_score, 1)
        self.assertEqual(self.p1.responses.count(), 1)
        self.assertEqual(self.session.round_end_time, before + timedelta(seconds=60))
        self.assertEqual(self.game.started_at, started)
        self.assertEqual(self.session.current_round, 2)
        self.assertEqual(self.p1.quiz.pending_inputs.get(participant=self.p1).answer_text, 'Saarland')

    def test_http_resume_preserves_score_without_lobby_gate(self):
        self.start()
        self.complete()
        self.start(self.q2)
        self.game.set_inactive()
        self.client.force_login(self.user)
        with patch('wer_weiss_mehr.views.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': False}) as gate:
            response = self.post('start_game')
            gate.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.game.refresh_from_db()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.total_score, 1)
        self.assertEqual(self.game.current_question_id, self.q2.pk)

    def test_duplicate_start_does_not_clear_prompt_or_current_set(self):
        reset_question_flow(game_key='wer_weiss_mehr', room_code=self.game.room_code, session_code=self.hub.code, mode='manual_three_phase')
        snap = current_snapshot('wer_weiss_mehr', self.game.room_code, self.hub.code)
        decision = s.present_set_round(self.game, self.question.pk, hub_session_code=self.hub.code, action={'client_action_id': str(uuid.uuid4()), 'state_revision': snap['state_revision'], 'game_id': snap['game_id'], 'question_id': self.question.pk})
        self.game.refresh_from_db()
        self.client.force_login(self.user)
        response = self.post('start_game')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(current_snapshot('wer_weiss_mehr', self.game.room_code, self.hub.code)['question_phase'], decision.snapshot['question_phase'])
        async_to_sync(self.consumer.handle_admin_start_quiz)({'hub_session_code': self.hub.code})
        self.assertEqual(current_snapshot('wer_weiss_mehr', self.game.room_code, self.hub.code)['question_phase'], 'prompt_visible')

    def test_repeat_is_additional_set_with_own_points_and_history(self):
        self.start()
        self.complete()
        self.start()
        self.complete()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.total_score, 2)
        self.assertEqual(self.p1.responses.count(), 2)
        state = s.build_game_state(self.game, self.hub.code, self.p1.name)
        self.assertEqual(state['set_total'], 3)
        self.assertEqual([row['scores'][0]['points'] for row in state['scorebox'][:2]], [1, 1])
        self.assertEqual(len({row['set_number'] for row in state['scorebox'][:2]}), 2)

    def test_replay_button_remains_available_after_completed_set(self):
        self.start()
        self.complete()
        state = s.build_game_state(self.game, self.hub.code)
        entry = next((q for q in state['available_questions'] if q['id'] == self.question.pk))
        self.assertTrue(entry['is_completed'])
        self.assertFalse(entry['is_start_disabled'])

    def test_expired_round_rejects_draft_and_submit_and_disables_snapshot(self):
        self.start()
        s.store_pending_input(self.game, self.p1, 'Bayern')
        self.session.round_end_time = timezone.now() - timedelta(seconds=1)
        self.session.save()
        self.assertIsNone(s.store_pending_input(self.game, self.p1, 'Saarland'))
        state = s.build_game_state(self.game, self.hub.code, self.p1.name)
        self.assertFalse(state['participant_state']['can_answer'])
        with self.assertRaises(ValueError):
            s.submit_answer(self.game, self.p1, 'Saarland')
        self.end()
        self.assertEqual(self.p1.responses.get().answer_text, 'Bayern')

    def test_old_draft_cannot_be_rebound_to_new_round(self):
        self.start()
        old = self.context()
        s.submit_answer(self.game, self.p1, 'Bayern')
        s.submit_answer(self.game, self.p2, 'Bayern')
        self.end()
        s.next_round_or_finish(self.game)
        response = self.post('participant_pending_input', participant=self.p1, **old, answer_text='OLD')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['pending_saved'])
        self.assertFalse(m.WerWeissMehrPendingInput.objects.filter(participant=self.p1, round_number=2).exists())

    def test_old_host_end_does_not_close_new_round(self):
        self.start()
        old = self.context()
        s.submit_answer(self.game, self.p1, 'Bayern')
        s.submit_answer(self.game, self.p2, 'Bayern')
        self.end()
        s.next_round_or_finish(self.game)
        self.client.force_login(self.user)
        response = self.post('end_round', **old)
        self.assertEqual(response.status_code, 400)
        self.session.refresh_from_db()
        self.assertEqual(self.session.phase, 'round_active')
        with self.assertRaises(ValueError):
            async_to_sync(self.consumer.handle_admin_end_round)({'hub_session_code': self.hub.code, **old})

    def test_public_endpoint_never_returns_host_solutions(self):
        self.start()
        response = self.client.get(reverse('wer_weiss_mehr:state', args=[self.game.room_code]), {'hub_session': self.hub.code})
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.user)
        response = self.client.get(reverse('wer_weiss_mehr:state', args=[self.game.room_code]), {'hub_session': self.hub.code})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['target_answers'])

    def test_capability_must_match_claimed_player(self):
        self.start()
        response = self.post('participant_pending_input', participant=self.p1, player_token=issue_player_token(self.p2), answer_text='FORGED')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(m.WerWeissMehrPendingInput.objects.exists())

    def test_socket_bound_player_cannot_modify_foreign_draft(self):
        self.start()
        self.consumer._authoritative_participant = self.p1.name
        self.consumer._player_token = issue_player_token(self.p1)
        async_to_sync(self.consumer.handle_participant_input_changed)({'name': self.p2.name, 'hub_session_code': self.hub.code, 'answer_text': 'FORGED', **self.context()})
        self.assertFalse(m.WerWeissMehrPendingInput.objects.exists())

    def test_anonymous_socket_snapshot_and_unknown_join_do_not_expose_or_create(self):
        self.start()
        self.consumer.scope = {'user': None}
        async_to_sync(self.consumer.send_state)({})
        self.assertEqual(self.consumer.send_json.call_args.args[0]['code'], 'unauthorized')
        before = self.game.participants.count()
        async_to_sync(self.consumer.handle_participant_join)({'name': 'Ghost', 'hub_session_code': self.hub.code})
        self.assertEqual(self.game.participants.count(), before)

    def test_live_edit_cannot_change_grading_or_remove_existing_answers(self):
        self.start()
        s.submit_answer(self.game, self.p1, 'Bayern')
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:update_wer_weiss_mehr_question'), json.dumps({'question_id': self.question.pk, 'answers': ['NEU']}), content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.end()
        self.assertTrue(self.p1.responses.get().is_correct)

    def test_other_pool_is_not_deleted_eliminated_or_scored(self):
        foreign = m.WerWeissMehrParticipant.objects.create(quiz=self.game, name='Other', hub_session_code='OTHER')
        state = m.WerWeissMehrParticipantState.objects.create(quiz=self.game, participant=foreign, question=self.question, survived_rounds=4)
        self.start()
        self.end()
        state.refresh_from_db()
        self.assertFalse(state.is_eliminated)
        self.assertEqual(state.survived_rounds, 4)
        self.assertFalse(foreign.responses.exists())

    def test_out_of_order_progress_matches_spectator(self):
        from games_hub.spectator import _serialize_wer_weiss_mehr
        self.start(self.q2)
        state = s.build_game_state(self.game, self.hub.code, self.p1.name)
        spectator = _serialize_wer_weiss_mehr(self.game, self.hub)
        self.assertEqual(state['current_set_number'], 1)
        self.assertEqual(spectator['progress']['set'], 1)
        self.assertEqual(spectator['progress']['set_total'], 2)

    def test_archive_keeps_historical_points_column(self):
        self.start()
        self.complete()
        self.question.is_active = False
        self.question.save()
        state = s.build_game_state(self.game, self.hub.code, self.p1.name)
        self.assertEqual(len(state['scorebox']), 2)
        self.assertEqual(state['scorebox'][0]['scores'][0]['points'], 1)

    def test_late_join_waits_until_next_set(self):
        self.start()
        late = m.WerWeissMehrParticipant.objects.create(quiz=self.game, name='Late <Team', hub_session_code=self.hub.code)
        state = s.build_game_state(self.game, self.hub.code, late.name)
        self.assertFalse(state['participant_state']['can_answer'])
        self.assertTrue(state['participant_state']['waiting_for_next_set'])
        self.complete()
        self.start(self.q2)
        self.assertTrue(s.build_game_state(self.game, self.hub.code, late.name)['participant_state']['can_answer'])

    def test_end_keeps_completed_points_discards_open_round_and_reveals_all(self):
        self.start()
        s.submit_answer(self.game, self.p1, 'Bayern')
        s.submit_answer(self.game, self.p2, 'Bayern')
        self.end()
        s.next_round_or_finish(self.game)
        s.store_pending_input(self.game, self.p1, 'Saarland')
        self.game.end_quiz()
        self.p1.refresh_from_db()
        state = s.build_game_state(self.game, self.hub.code, self.p1.name)
        self.assertEqual(self.p1.total_score, 1)
        self.assertEqual(self.p1.responses.count(), 1)
        self.assertFalse(self.game.pending_inputs.exists())
        self.assertTrue(all((tile['revealed'] for tile in state['question']['tiles'])))

    def test_selection_context_does_not_change_when_socket_reobserves_snapshot(self):
        from games_hub.authoritative_state import observe_snapshot, get_runtime_state
        self.start()
        self.complete()
        first = s.build_game_state(self.game, self.hub.code)
        runtime = get_runtime_state('wer_weiss_mehr', self.game.room_code, self.hub.code)
        revision = runtime.context_revision
        observe_snapshot('wer_weiss_mehr', self.game.room_code, {'type': 'state', **first}, self.hub.code)
        s.build_game_state(self.game, self.hub.code, self.p1.name)
        runtime.refresh_from_db()
        self.assertEqual(runtime.context_revision, revision)

    def test_inactive_snapshot_reports_frozen_remaining_time(self):
        from wer_weiss_mehr.runtime import set_paused
        self.start()
        at = timezone.now()
        set_paused(self.game, True, at=at)
        with patch('wer_weiss_mehr.services.timezone.now', return_value=at + timedelta(seconds=120)):
            state = s.build_game_state(self.game, self.hub.code, self.p1.name)
        self.assertEqual(state['timer']['remaining_seconds'], state['remaining_answer_time'])
        self.assertGreater(state['timer']['remaining_seconds'], 25)
        self.assertFalse(state['participant_state']['can_answer'])
