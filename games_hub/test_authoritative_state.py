import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import OperationalError
from django.contrib.staticfiles import finders
from django.test import TestCase, override_settings
from django.utils import timezone

from buzzer.models import BuzzerGame

from .authoritative_state import (
    QUESTION_PRESENTATION_DELAY_MS,
    _retry_sqlite_locked,
    attach_snapshot_metadata,
    configure_question_flow,
    connected_participant_ids,
    current_snapshot,
    disconnect_socket_connection,
    expire_stale_connections,
    get_runtime_state,
    observe_snapshot,
    open_answering,
    participant_is_connected,
    present_question,
    register_socket_connection,
    reveal_question_content,
    validate_and_reserve_action,
)
from .lobby_return_flow import get_session_lobby_presence
from .models import (
    GameRuntimeState,
    HubGameStep,
    HubParticipant,
    HubSession,
    HubSocketConnection,
    ProcessedClientAction,
)


class AuthoritativeStateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('state-host', password='pw')
        self.game = BuzzerGame.objects.create(title='State game', creator=self.user)
        self.session = HubSession.objects.create(
            code='STATE1',
            name='State session',
            started_at=timezone.now(),
        )
        self.participant = HubParticipant.objects.create(
            session=self.session,
            nickname='Alice',
        )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )

    def snapshot(self, *, phase='active', question_id=10, round_id=1, ends_at=None):
        starts_at = timezone.now()
        ends_at = ends_at or starts_at + timedelta(seconds=30)
        return attach_snapshot_metadata(
            {
                'game': {'id': self.game.id, 'status': phase},
                'phase': phase,
                'question': {
                    'id': question_id,
                    'text': 'Server question',
                    'options': [{'id': 1, 'text': 'A'}],
                },
                'round': {'id': round_id, 'number': round_id},
                'timer': {
                    'started_at': starts_at.isoformat(),
                    'ends_at': ends_at.isoformat(),
                    'remaining_seconds': 30,
                },
                'participant_state': {
                    'answer': None,
                    'answer_locked': False,
                    'next_state': 'answer',
                },
                'revealed': False,
            },
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
        )

    def test_snapshot_contains_standard_context_and_absolute_times(self):
        payload = self.snapshot()

        self.assertEqual(payload['game_id'], str(self.game.id))
        self.assertEqual(payload['session_id'], self.session.id)
        self.assertEqual(payload['current_question_id'], '10')
        self.assertEqual(payload['current_round_id'], '1')
        self.assertEqual(payload['phase'], 'active')
        self.assertGreaterEqual(payload['state_revision'], 1)
        self.assertIn('+00:00', payload['server_now'])
        self.assertIn('+00:00', payload['starts_at'])
        self.assertIn('+00:00', payload['ends_at'])

        runtime = GameRuntimeState.objects.get(game_step=self.step)
        self.assertEqual(runtime.public_snapshot['question']['text'], 'Server question')
        self.assertEqual(runtime.public_snapshot['answer_options'], [{'id': 1, 'text': 'A'}])
        self.assertNotIn('own_answer', runtime.public_snapshot)
        self.assertNotIn('answer_locked', runtime.public_snapshot)
        self.assertNotIn('result', runtime.public_snapshot)
        self.assertNotIn('next_participant_state', runtime.public_snapshot)

    def test_revision_only_advances_for_changed_display_state(self):
        first = self.snapshot()
        duplicate = attach_snapshot_metadata(
            first,
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
        )
        next_round = self.snapshot(round_id=2)

        self.assertEqual(duplicate['state_revision'], first['state_revision'])
        self.assertGreater(next_round['state_revision'], duplicate['state_revision'])
        runtime = GameRuntimeState.objects.get(game_step=self.step)
        self.assertEqual(runtime.context_revision, next_round['state_revision'])

    def test_snapshot_preserves_zero_as_a_valid_own_answer(self):
        initial = self.snapshot()
        standard = observe_snapshot(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            payload={
                'type': 'answer_submitted',
                'participant_state': {'answer': 0, 'answer_locked': True},
            },
        )

        self.assertEqual(standard['own_answer'], 0)
        self.assertEqual(standard['state_revision'], initial['state_revision'])
        runtime = GameRuntimeState.objects.get(game_step=self.step)
        self.assertNotIn('own_answer', runtime.public_snapshot)
        self.assertEqual(runtime.public_snapshot['question']['text'], 'Server question')

    def test_partial_confirmation_does_not_erase_public_question(self):
        initial = self.snapshot()

        confirmation = observe_snapshot(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            payload={
                'type': 'answer_submitted',
                'participant_state': {
                    'answer': 'A',
                    'answer_locked': True,
                },
            },
        )

        runtime = GameRuntimeState.objects.get(game_step=self.step)
        self.assertEqual(confirmation['state_revision'], initial['state_revision'])
        self.assertEqual(runtime.public_snapshot['question']['text'], 'Server question')
        self.assertEqual(runtime.public_snapshot['answer_options'], [{'id': 1, 'text': 'A'}])
        self.assertNotIn('own_answer', runtime.public_snapshot)
        self.assertNotIn('answer_locked', runtime.public_snapshot)

    def test_rejoin_quiz_started_replay_does_not_regress_active_question_phase(self):
        initial = self.snapshot(phase='question_active')

        replay = observe_snapshot(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            payload={'type': 'quiz_started'},
        )

        self.assertEqual(replay['state_revision'], initial['state_revision'])
        self.assertEqual(replay['phase'], 'question_active')

    def test_full_snapshot_can_clear_previous_question_and_deadline(self):
        initial = self.snapshot()

        cleared = observe_snapshot(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            payload={
                'phase': 'active',
                'question': None,
                'round': None,
                'timer': {
                    'started_at': None,
                    'ends_at': None,
                },
            },
        )

        self.assertGreater(cleared['state_revision'], initial['state_revision'])
        self.assertIsNone(cleared['current_question_id'])
        self.assertIsNone(cleared['current_round_id'])
        self.assertIsNone(cleared['starts_at'])
        self.assertIsNone(cleared['ends_at'])

    def test_old_question_and_round_actions_are_rejected(self):
        payload = self.snapshot(question_id=10, round_id=2)
        base_action = {
            'client_action_id': str(uuid.uuid4()),
            'state_revision': payload['state_revision'],
            'game_id': str(self.game.id),
            'question_id': '9',
            'round_id': '2',
        }
        stale_question = validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_submit_answer',
            action=base_action,
        )
        base_action.update({
            'client_action_id': str(uuid.uuid4()),
            'question_id': '10',
            'round_id': '1',
        })
        stale_round = validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_submit_answer',
            action=base_action,
        )

        self.assertEqual(stale_question.code, 'stale_action')
        self.assertEqual(stale_round.code, 'stale_action')
        self.assertFalse(ProcessedClientAction.objects.exists())

    def test_duplicate_client_action_is_reserved_once(self):
        payload = self.snapshot()
        action_id = str(uuid.uuid4())
        action = {
            'client_action_id': action_id,
            'state_revision': payload['state_revision'],
            'game_id': str(self.game.id),
            'question_id': '10',
            'round_id': '1',
        }
        first = validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_submit_answer',
            action=action,
        )
        duplicate = validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_submit_answer',
            action=action,
        )

        self.assertTrue(first.accepted)
        self.assertEqual(duplicate.code, 'already_submitted')
        self.assertEqual(ProcessedClientAction.objects.count(), 1)

    def test_action_with_unknown_future_revision_is_rejected(self):
        payload = self.snapshot()
        decision = validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_buzz',
            action={
                'client_action_id': str(uuid.uuid4()),
                'state_revision': payload['state_revision'] + 1,
                'game_id': str(self.game.id),
                'question_id': '10',
                'round_id': '1',
            },
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, 'stale_action')
        self.assertFalse(ProcessedClientAction.objects.exists())

    def test_deadline_is_enforced_by_action_guard(self):
        payload = self.snapshot(ends_at=timezone.now() - timedelta(seconds=1))
        decision = validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_submit_answer',
            action={
                'client_action_id': str(uuid.uuid4()),
                'state_revision': payload['state_revision'],
                'game_id': str(self.game.id),
                'question_id': '10',
                'round_id': '1',
            },
        )

        self.assertEqual(decision.code, 'deadline_expired')
        self.assertFalse(ProcessedClientAction.objects.exists())

    def test_action_guard_supplies_server_received_time_and_elapsed_time(self):
        payload = self.snapshot()
        runtime = GameRuntimeState.objects.get(game_step=self.step)
        GameRuntimeState.objects.filter(pk=runtime.pk).update(
            starts_at=timezone.now() - timedelta(seconds=7),
        )

        decision = validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_submit_answer',
            action={
                'client_action_id': str(uuid.uuid4()),
                'state_revision': payload['state_revision'],
                'game_id': str(self.game.id),
                'question_id': '10',
                'round_id': '1',
            },
        )

        self.assertTrue(decision.accepted)
        self.assertIsNotNone(decision.received_at)
        self.assertGreaterEqual(decision.time_taken, 7)
        self.assertLess(decision.time_taken, 9)

    def test_runtime_state_is_database_shared_not_process_local(self):
        self.snapshot()
        first_worker_view = get_runtime_state('buzzer', self.game.room_code, self.session.code)
        second_worker_view = GameRuntimeState.objects.get(identity_key=first_worker_view.identity_key)

        self.assertEqual(first_worker_view.pk, second_worker_view.pk)
        self.assertEqual(first_worker_view.state_revision, second_worker_view.state_revision)


class QuestionPhaseStateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('phase-host', password='pw')
        self.game = BuzzerGame.objects.create(title='Phase game', creator=self.user)
        self.session = HubSession.objects.create(
            code='PHASE1',
            name='Phase session',
            started_at=timezone.now(),
        )
        self.participant = HubParticipant.objects.create(
            session=self.session,
            nickname='Alice',
        )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )

    def enable_manual_flow(self):
        return configure_question_flow(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def host_action(self, revision, *, question_id='10', action_id=None):
        return {
            'client_action_id': action_id or str(uuid.uuid4()),
            'state_revision': revision,
            'game_id': str(self.game.id),
            'question_id': question_id,
        }

    def present(self, snapshot, *, at=None, action_id=None):
        return present_question(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            action=self.host_action(
                snapshot['state_revision'],
                action_id=action_id,
            ),
            at=at,
        )

    def reveal(self, snapshot, *, at=None, action_id=None):
        if at is None and snapshot.get('question_visible_at'):
            at = datetime.fromisoformat(snapshot['question_visible_at'])
        return reveal_question_content(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            action=self.host_action(
                snapshot['state_revision'],
                action_id=action_id,
            ),
            at=at,
        )

    def open(self, snapshot, *, at=None, action_id=None, duration=30):
        return open_answering(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            action=self.host_action(
                snapshot['state_revision'],
                action_id=action_id,
            ),
            answer_duration_seconds=duration,
            at=at,
        )

    def participant_action(self, snapshot):
        action = {
            'client_action_id': str(uuid.uuid4()),
            'state_revision': snapshot['state_revision'],
            'game_id': str(self.game.id),
            'question_id': '10',
        }
        if snapshot.get('current_round_id') is not None:
            action['round_id'] = snapshot['current_round_id']
        if snapshot.get('current_set_id') is not None:
            action['set_id'] = snapshot['current_set_id']
        return validate_and_reserve_action(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            participant_name='Alice',
            action_type='participant_buzz',
            action=action,
        )

    def test_present_question_persists_prompt_without_deadline_and_blocks_answers(self):
        configured = self.enable_manual_flow()
        presented_at = timezone.now()
        decision = self.present(configured, at=presented_at)

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.snapshot['question_phase'], 'prompt_visible')
        self.assertEqual(
            decision.snapshot['question_presented_at'],
            presented_at.isoformat(),
        )
        self.assertEqual(
            decision.snapshot['question_visible_at'],
            (
                presented_at
                + timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS)
            ).isoformat(),
        )
        self.assertIsNone(decision.snapshot['content_revealed_at'])
        self.assertIsNone(decision.snapshot['answering_started_at'])
        self.assertIsNone(decision.snapshot['answering_deadline_at'])
        self.assertFalse(decision.snapshot['answering_allowed'])
        self.assertFalse(decision.snapshot['timer_running'])
        rejected = self.participant_action(decision.snapshot)
        self.assertEqual(rejected.code, 'invalid_phase')
        self.assertEqual(
            ProcessedClientAction.objects.filter(participant=self.participant).count(),
            0,
        )

    def test_display_revision_does_not_reject_current_question_context(self):
        configured = self.enable_manual_flow()
        runtime = GameRuntimeState.objects.get(game_step=self.step)
        self.assertEqual(configured['state_revision'], runtime.context_revision)
        runtime.state_revision += 1
        runtime.save(update_fields=['state_revision', 'updated_at'])

        decision = self.present(configured)

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.snapshot['question_phase'], 'prompt_visible')

    def test_content_reveal_persists_without_deadline_and_blocks_answers(self):
        presented = self.present(self.enable_manual_flow())
        revealed_at = datetime.fromisoformat(presented.snapshot['question_visible_at'])
        revealed = self.reveal(presented.snapshot, at=revealed_at)

        self.assertTrue(revealed.accepted)
        self.assertEqual(revealed.snapshot['question_phase'], 'content_visible')
        self.assertEqual(
            revealed.snapshot['content_revealed_at'],
            revealed_at.isoformat(),
        )
        self.assertIsNone(revealed.snapshot['answering_started_at'])
        self.assertIsNone(revealed.snapshot['answering_deadline_at'])
        self.assertEqual(self.participant_action(revealed.snapshot).code, 'invalid_phase')

    def test_content_reveal_is_rejected_until_question_is_visible(self):
        presented_at = timezone.now()
        presented = self.present(self.enable_manual_flow(), at=presented_at)
        early = self.reveal(
            presented.snapshot,
            at=(
                presented_at
                + timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS - 1)
            ),
        )
        visible_at = presented_at + timedelta(
            milliseconds=QUESTION_PRESENTATION_DELAY_MS
        )
        revealed = self.reveal(presented.snapshot, at=visible_at)

        self.assertFalse(early.accepted)
        self.assertEqual(early.code, 'question_not_visible')
        self.assertEqual(early.snapshot['question_phase'], 'prompt_visible')
        self.assertIsNone(early.snapshot['answering_deadline_at'])
        self.assertTrue(revealed.accepted)
        self.assertEqual(revealed.snapshot['question_phase'], 'content_visible')
        self.assertIsNone(revealed.snapshot['answering_deadline_at'])

    def test_open_answering_creates_deadline_and_accepts_answers(self):
        presented = self.present(self.enable_manual_flow())
        revealed = self.reveal(presented.snapshot)
        opened_at = timezone.now()
        opened = self.open(revealed.snapshot, at=opened_at, duration=25)

        self.assertTrue(opened.accepted)
        self.assertEqual(opened.snapshot['question_phase'], 'answering_open')
        self.assertEqual(opened.snapshot['answering_started_at'], opened_at.isoformat())
        self.assertEqual(
            opened.snapshot['answering_deadline_at'],
            (opened_at + timedelta(seconds=25)).isoformat(),
        )
        self.assertTrue(opened.snapshot['answering_allowed'])
        self.assertTrue(opened.snapshot['timer_running'])
        self.assertTrue(self.participant_action(opened.snapshot).accepted)

    def test_duplicate_open_answering_never_extends_deadline(self):
        presented = self.present(self.enable_manual_flow())
        revealed = self.reveal(presented.snapshot)
        opened_at = timezone.now()
        action_id = str(uuid.uuid4())
        first = self.open(
            revealed.snapshot,
            at=opened_at,
            action_id=action_id,
            duration=20,
        )
        duplicate = self.open(
            revealed.snapshot,
            at=opened_at + timedelta(seconds=5),
            action_id=action_id,
            duration=90,
        )
        repeated = self.open(
            first.snapshot,
            at=opened_at + timedelta(seconds=10),
            duration=90,
        )

        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertTrue(repeated.accepted)
        self.assertTrue(repeated.duplicate)
        expected_deadline = (opened_at + timedelta(seconds=20)).isoformat()
        self.assertEqual(duplicate.snapshot['answering_deadline_at'], expected_deadline)
        self.assertEqual(repeated.snapshot['answering_deadline_at'], expected_deadline)

    def test_invalid_and_stale_phase_transitions_are_rejected(self):
        configured = self.enable_manual_flow()
        skipped = self.open(configured)
        presented = self.present(configured)
        stale_reveal = self.reveal(configured)
        skipped_open = self.open(presented.snapshot)

        self.assertEqual(skipped.code, 'stale_action')
        self.assertEqual(stale_reveal.code, 'stale_action')
        self.assertEqual(skipped_open.code, 'invalid_phase')
        runtime = GameRuntimeState.objects.get(game_step=self.step)
        self.assertEqual(runtime.question_phase, 'prompt_visible')
        self.assertIsNone(runtime.ends_at)

    def test_snapshots_reconstruct_each_persisted_phase(self):
        configured = self.enable_manual_flow()
        presented = self.present(configured)
        prompt_snapshot = current_snapshot('buzzer', self.game.room_code, self.session.code)
        revealed = self.reveal(prompt_snapshot)
        content_snapshot = current_snapshot('buzzer', self.game.room_code, self.session.code)
        opened = self.open(content_snapshot)
        answering_snapshot = current_snapshot('buzzer', self.game.room_code, self.session.code)

        self.assertEqual(prompt_snapshot['question_phase'], 'prompt_visible')
        self.assertFalse(prompt_snapshot['answering_allowed'])
        self.assertEqual(content_snapshot['question_phase'], 'content_visible')
        self.assertFalse(content_snapshot['answering_allowed'])
        self.assertEqual(answering_snapshot['question_phase'], 'answering_open')
        self.assertTrue(answering_snapshot['answering_allowed'])
        self.assertEqual(
            answering_snapshot['answering_deadline_at'],
            opened.snapshot['answering_deadline_at'],
        )
        self.assertGreater(
            answering_snapshot['state_revision'],
            revealed.snapshot['state_revision'],
        )

    def test_manual_snapshot_observation_cannot_start_timer_early(self):
        presented = self.present(self.enable_manual_flow())
        forged_end = timezone.now() + timedelta(minutes=5)
        observed = observe_snapshot(
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
            payload={
                'phase': 'question_active',
                'question': {'id': 10, 'text': 'Prompt', 'options': ['A', 'B']},
                'timer': {
                    'started_at': timezone.now().isoformat(),
                    'ends_at': forged_end.isoformat(),
                },
            },
        )

        self.assertEqual(observed['question_phase'], 'prompt_visible')
        self.assertIsNone(observed['answering_started_at'])
        self.assertIsNone(observed['answering_deadline_at'])
        self.assertIsNone(observed['answer_options'])
        self.assertNotIn('options', observed['question'])
        runtime = GameRuntimeState.objects.get(game_step=self.step)
        self.assertIsNone(runtime.starts_at)
        self.assertIsNone(runtime.ends_at)

    def test_phase_actions_use_one_action_id_reservation_each(self):
        configured = self.enable_manual_flow()
        present_id = str(uuid.uuid4())
        presented = self.present(configured, action_id=present_id)
        duplicate_present = self.present(configured, action_id=present_id)
        reveal_id = str(uuid.uuid4())
        revealed = self.reveal(presented.snapshot, action_id=reveal_id)
        duplicate_reveal = self.reveal(presented.snapshot, action_id=reveal_id)
        open_id = str(uuid.uuid4())
        opened = self.open(revealed.snapshot, action_id=open_id)
        duplicate_open = self.open(revealed.snapshot, action_id=open_id)

        self.assertTrue(duplicate_present.duplicate)
        self.assertTrue(duplicate_reveal.duplicate)
        self.assertTrue(duplicate_open.duplicate)
        phase_actions = ProcessedClientAction.objects.filter(
            participant_key='__host_question_phase__',
        )
        self.assertEqual(phase_actions.count(), 3)
        self.assertEqual(
            set(phase_actions.values_list('action_type', flat=True)),
            {'present_question', 'reveal_question_content', 'open_answering'},
        )
        self.assertTrue(opened.accepted)

    def test_stale_action_cannot_regress_open_phase_or_remove_deadline(self):
        configured = self.enable_manual_flow()
        presented = self.present(configured)
        revealed = self.reveal(presented.snapshot)
        opened = self.open(revealed.snapshot)
        deadline = opened.snapshot['answering_deadline_at']

        stale = self.reveal(presented.snapshot)
        reconstructed = current_snapshot(
            'buzzer',
            self.game.room_code,
            self.session.code,
        )

        self.assertEqual(stale.code, 'stale_action')
        self.assertEqual(reconstructed['question_phase'], 'answering_open')
        self.assertEqual(reconstructed['answering_deadline_at'], deadline)

    def test_legacy_mode_keeps_immediate_deadline_and_answer_behavior(self):
        starts_at = timezone.now()
        snapshot = attach_snapshot_metadata(
            {
                'phase': 'question_active',
                'question': {'id': 10, 'text': 'Legacy prompt', 'options': ['A']},
                'timer': {
                    'started_at': starts_at.isoformat(),
                    'ends_at': (starts_at + timedelta(seconds=30)).isoformat(),
                },
            },
            game_key='buzzer',
            room_code=self.game.room_code,
            session_code=self.session.code,
        )

        self.assertEqual(snapshot['question_flow_mode'], 'legacy_immediate')
        self.assertEqual(snapshot['question_phase'], 'answering_open')
        self.assertIsNotNone(snapshot['answering_deadline_at'])
        decision = self.participant_action(snapshot)
        self.assertTrue(decision.accepted, decision)

    def test_snapshot_observation_retries_a_transient_sqlite_write_lock(self):
        original_observe_snapshot = observe_snapshot
        attempts = []

        def flaky_observe_snapshot(*args, **kwargs):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise OperationalError('database is locked')
            return original_observe_snapshot(*args, **kwargs)

        with (
            patch('games_hub.authoritative_state.connection.vendor', 'sqlite'),
            patch(
                'games_hub.authoritative_state.observe_snapshot',
                side_effect=flaky_observe_snapshot,
            ),
            patch('games_hub.authoritative_state.time.sleep') as sleep,
        ):
            snapshot = attach_snapshot_metadata(
                {'phase': 'waiting'},
                game_key='buzzer',
                room_code=self.game.room_code,
                session_code=self.session.code,
            )

        self.assertEqual(attempts, [1, 2])
        sleep.assert_called_once_with(0.02)
        self.assertEqual(snapshot['game_key'], 'buzzer')


@override_settings(SOCKET_PRESENCE_TTL_SECONDS=45)
class SocketPresenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('presence-host', password='pw')
        self.session = HubSession.objects.create(code='SOCKET', name='Socket session')
        self.alice = HubParticipant.objects.create(session=self.session, nickname='Alice')
        self.bob = HubParticipant.objects.create(session=self.session, nickname='Bob')

    def connect(self, channel, participant='Alice', scope='game'):
        return register_socket_connection(
            channel_name=channel,
            session_code=self.session.code,
            participant_name=participant,
            scope_kind=scope,
            game_key='buzzer' if scope == 'game' else '',
            room_code='ROOM' if scope == 'game' else '',
        )

    def test_sqlite_presence_write_retries_the_complete_operation_after_lock(self):
        attempts = []

        def operation():
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise OperationalError('database is locked')
            return 'persisted'

        with (
            patch('games_hub.authoritative_state.connection.vendor', 'sqlite'),
            patch('games_hub.authoritative_state.time.sleep') as sleep,
        ):
            result = _retry_sqlite_locked(operation)

        self.assertEqual(result, 'persisted')
        self.assertEqual(attempts, [1, 2])
        sleep.assert_called_once_with(0.02)

    def test_two_tabs_remain_connected_when_one_disconnects(self):
        self.connect('channel.one')
        self.connect('channel.two')

        disconnect_socket_connection('channel.one')

        self.assertTrue(participant_is_connected(self.alice))
        self.assertEqual(connected_participant_ids(self.session), {self.alice.id})
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_active)

    def test_hard_disconnect_expires_after_ttl_without_changing_membership(self):
        self.connect('channel.dead')
        HubSocketConnection.objects.filter(channel_name='channel.dead').update(
            last_seen=timezone.now() - timedelta(seconds=46),
        )

        expired = expire_stale_connections()

        self.assertGreaterEqual(expired, 1)
        self.assertIsNotNone(
            HubSocketConnection.objects.get(channel_name='channel.dead').disconnected_at
        )
        self.assertFalse(participant_is_connected(self.alice))
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_active)

    def test_lobby_presence_uses_live_connections_after_rollout(self):
        self.connect('alice.lobby', scope='lobby')
        self.connect('bob.game', participant='Bob')
        from buzzer.models import BuzzerParticipant

        game = BuzzerGame.objects.create(title='Presence game', creator=self.user)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=game.room_code,
            title=game.title,
        )
        BuzzerParticipant.objects.create(
            quiz=game,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )

        presence = get_session_lobby_presence(self.session.code)

        self.assertEqual(presence['total_participants'], 2)
        self.assertEqual(presence['participants_in_lobby'], [{'name': 'Alice'}])
        self.assertEqual(presence['participants_not_in_lobby'][0]['name'], 'Bob')


class AuthoritativeClientSafeguardTests(TestCase):
    def test_production_settings_default_to_shared_redis_channel_layer(self):
        settings_source = Path('games_website/settings.py').read_text(encoding='utf-8')

        self.assertIn("os.environ.get('DJANGO_DEBUG', 'true')", settings_source)
        self.assertIn("'memory' if DEBUG else 'redis'", settings_source)
        self.assertIn("channels_redis.core.RedisChannelLayer", settings_source)
        self.assertIn("'CHANNEL_REDIS_URL'", settings_source)

    def test_shared_client_guards_revisions_http_races_and_actions(self):
        script_path = finders.find('js/authoritative-game-state.js')
        self.assertIsNotNone(script_path)
        script = Path(script_path).read_text(encoding='utf-8')
        consumer_source = Path('games_hub/authoritative_consumer.py').read_text(encoding='utf-8')

        self.assertIn('incomingRevision < Number(state.state_revision', script)
        self.assertIn('incomingRevision === Number(state.state_revision)', script)
        self.assertIn('estimatedServerNow(previous) >= previousEnd', script)
        self.assertIn('event.stopImmediatePropagation()', script)
        self.assertIn('client_action_id', script)
        self.assertIn("nativeSend(JSON.stringify({type: 'snapshot_request'}))", script)
        self.assertIn('createActionId: makeActionId', script)
        self.assertIn("type: 'heartbeat'", script)
        self.assertIn('performance.now()', script)
        self.assertIn('latestByIdentity.set(identity, context)', script)
        self.assertIn("data['participant_name'] = participant_name", consumer_source)
        self.assertIn("data['hub_session'] = session_code", consumer_source)

        template = Path('templates/wer_weiss_mehr/play.html').read_text(encoding='utf-8')
        self.assertIn('stateAbortController.abort()', template)
        self.assertIn('requestSequence !== stateRequestSequence', template)
        self.assertIn('AuthoritativeGameState.acceptSnapshot(state)', template)
        blackjack_template = Path('templates/black_jack_quiz/play.html').read_text(encoding='utf-8')
        self.assertIn('timeoutStatusAbortController?.abort()', blackjack_template)
        self.assertIn('requestSequence !== this.timeoutStatusRequestSequence', blackjack_template)
        self.assertIn('AuthoritativeGameState.acceptSnapshot(data)', blackjack_template)

    def test_every_participant_game_loads_the_shared_protocol(self):
        templates = [
            'assign',
            'black_jack_quiz',
            'buzzer',
            'clue_rush',
            'estimation',
            'host_points',
            'quiz',
            'sorting_ladder',
            'wann_war_das',
            'wer_weiss_mehr',
            'where_is_this',
            'who_is_lying',
            'who_is_that',
        ]
        for game_template in templates:
            with self.subTest(game_template=game_template):
                source = Path(f'templates/{game_template}/play.html').read_text(encoding='utf-8')
                self.assertIn("js/authoritative-game-state.js", source)

    def test_timed_participant_clients_do_not_submit_client_calculated_time_taken(self):
        templates = [
            'black_jack_quiz',
            'buzzer',
            'clue_rush',
            'estimation',
            'quiz',
            'sorting_ladder',
            'wann_war_das',
            'wer_weiss_mehr',
            'where_is_this',
            'who_is_lying',
            'who_is_that',
        ]
        for game_template in templates:
            with self.subTest(game_template=game_template):
                source = Path(f'templates/{game_template}/play.html').read_text(encoding='utf-8')
                self.assertNotIn('time_taken:', source)
                self.assertNotIn('questionStartTime', source)

    def test_legacy_http_submit_paths_use_server_deadlines_and_server_time(self):
        view_paths = [
            'QuizGame/views.py',
            'where_is_this/views.py',
            'Estimation/views.py',
            'black_jack_quiz/views.py',
            'who_is_lying/views.py',
            'who_is_that/views.py',
        ]
        for view_path in view_paths:
            with self.subTest(view_path=view_path):
                source = Path(view_path).read_text(encoding='utf-8')
                self.assertNotIn("time_taken = data.get('time_taken'", source)
                self.assertIn('select_for_update()', source)
                self.assertIn('question_end_time', source)
                self.assertIn('received_at >= locked_session.question_end_time', source)
                self.assertIn('.objects.get_or_create(', source)
                self.assertIn('validate_and_reserve_action(', source)

    def test_final_participant_actions_are_required_to_use_the_common_guard(self):
        consumers = {
            'QuizGame': 'participant_submit_answer',
            'where_is_this': 'participant_submit_answer',
            'sorting_ladder': 'participant_submit_round',
            'Estimation': 'participant_submit_answer',
            'black_jack_quiz': 'participant_submit_answer',
            'who_is_lying': 'participant_submit_answer',
            'who_is_that': 'participant_submit_answer',
            'wer_weiss_mehr': 'participant_submit_answer',
            'wann_war_das': 'participant_submit_answer',
            'buzzer': 'participant_buzz',
        }
        for app_name, action_type in consumers.items():
            with self.subTest(app_name=app_name):
                source = Path(f'{app_name}/consumers.py').read_text(encoding='utf-8')
                self.assertRegex(
                    source,
                    rf"authoritative_required_actions\s*=\s*frozenset\(\{{[^}}]*'{action_type}'",
                )
