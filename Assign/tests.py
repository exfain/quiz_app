import json
import os
import time
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from asgiref.sync import async_to_sync
from .models import (
    AssignAnswer,
    AssignParticipant,
    AssignQuestion,
    AssignQuiz,
    AssignRoundParticipantState,
    AssignSession,
    AssignSetRuntime,
)
from .consumers import AssignConsumer
from .runtime import (
    ASSIGN_REVEAL_ANIMATION_MS,
    ASSIGN_REVEAL_STAGGER_MS,
    assign_reveal_ready_at,
    current_set_runtime,
    evaluate_and_advance,
    round_status,
    start_set_runtime,
    store_round_selection,
)
from games_hub.models import GameRuntimeState, HubGameStep, HubParticipant, HubSession
from games_hub.authoritative_state import (
    QUESTION_PRESENTATION_DELAY_MS,
    current_snapshot,
    observe_snapshot,
    reset_question_flow,
    validate_and_reserve_action,
)
from games_hub.lobby_join import issue_rejoin_token
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserLiveServerTestCase
except ImportError:
    from django.test import LiveServerTestCase as _BrowserLiveServerTestCase


class DummyChannelLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group_name, message):
        self.sent.append((group_name, message))

    async def group_discard(self, group_name, channel_name):
        return None


class CheckRoundAnswerTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='pass')
        self.quiz = AssignQuiz.objects.create(
            title='Test Quiz',
            creator=self.user,
            room_code='1234',
            status='active',
        )
        # Frage: 3 linke Items, 3 rechte Items, correct_matches = {"0": 2, "1": 0, "2": 1}
        self.question = AssignQuestion.objects.create(
            question_text='Match items',
            points=10,
            time_limit=60,
            left_items=['A', 'B', 'C'],
            right_items=['X', 'Y', 'Z'],
            correct_matches={'0': 2, '1': 0, '2': 1},
            created_by=self.user,
        )
        self.participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess1'
        )
        self.consumer = AssignConsumer()
        self.consumer.room_code = '1234'
        self.consumer.room_group_name = 'assign_1234'
        self.consumer.channel_layer = DummyChannelLayer()
        self.consumer.channel_name = 'test-channel'
        self.consumer._authoritative_participant = self.participant.name
        self.consumer._authoritative_session = self.participant.hub_session_code
        async def _noop_send(*args, **kwargs):
            return None
        self.consumer.send = _noop_send

    def _get_shuffled_pos_for_original(self, original_idx, question=None):
        """Hilfsmethode: ermittelt shuffled Position für einen Original-Index."""
        question = question or self.question
        randomized = question.get_randomized_items(room_code='1234')
        for shuffled_pos, orig in randomized['position_to_original'].items():
            if orig == original_idx:
                return shuffled_pos
        return None

    def _register_active_participant_channel(self, channel_name='channel-a'):
        self.consumer.channel_name = channel_name
        self.consumer._authoritative_participant = self.participant.name
        self.consumer._authoritative_session = self.participant.hub_session_code
        return channel_name

    def _start_runtime(self, question=None, time_limit=None):
        question = question or self.question
        runtime, _ = start_set_runtime(
            quiz_id=self.quiz.id,
            question_id=question.id,
            hub_session_code=self.participant.hub_session_code,
            effective_time_limit=time_limit or question.time_limit,
        )
        self.quiz.refresh_from_db()
        return runtime

    def _store_selection(
        self,
        runtime,
        *,
        round_index,
        left_item_index,
        user_match,
        lock=True,
    ):
        return store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=runtime.question_id,
            round_index=round_index,
            left_item_index=left_item_index,
            user_match=user_match,
            lock_selection=lock,
        )

    def _capture_consumer_send(self):
        payloads = []

        async def _capture_send(*args, **kwargs):
            payloads.append(json.loads(kwargs['text_data']))

        self.consumer.send = _capture_send
        return payloads

    def _create_four_round_question(self):
        return AssignQuestion.objects.create(
            question_text='Four round flow',
            points=10,
            time_limit=45,
            left_items=['A', 'B', 'C', 'D'],
            right_items=['W', 'X', 'Y', 'Z'],
            correct_matches={'0': 0, '1': 1, '2': 2, '3': 3},
            created_by=self.user,
        )

    def _create_capitals_question(self):
        return AssignQuestion.objects.create(
            question_text='European capitals',
            points=10,
            time_limit=45,
            left_items=['Brüssel', 'Berlin', 'Paris', 'Amsterdam'],
            right_items=['Deutschland', 'Frankreich', 'Belgien', 'Niederlande'],
            correct_matches={'0': 2, '1': 0, '2': 1, '3': 3},
            created_by=self.user,
        )

    def _create_left_distractor_question(self):
        return AssignQuestion.objects.create(
            question_text='Left distractor flow',
            points=10,
            time_limit=45,
            left_items=['A', 'B', 'C', 'D', 'Distractor'],
            right_items=['W', 'X', 'Y', 'Z'],
            correct_matches={'0': 0, '1': 1, '2': 2, '3': 3},
            created_by=self.user,
        )

    def _create_right_distractor_question(self):
        return AssignQuestion.objects.create(
            question_text='Right distractor flow',
            points=10,
            time_limit=45,
            left_items=['A', 'B', 'C', 'D'],
            right_items=['W', 'X', 'Y', 'Z', 'Distractor'],
            correct_matches={'0': 0, '1': 1, '2': 2, '3': 3},
            created_by=self.user,
        )

    def test_rejoin_active_quiz_uses_existing_question_started_event(self):
        runtime = self._start_runtime()

        payloads = self._capture_consumer_send()

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
        })

        state = next(payload for payload in payloads if payload['type'] == 'assign_state')
        self.assertEqual(state['current_question_id'], self.question.id)
        self.assertEqual(state['current_round_id'], 0)
        self.assertEqual(state['ends_at'], runtime.round_ends_at.isoformat())
        self.assertFalse(any(payload['type'] == 'quiz_started' for payload in payloads))
        self.assertFalse(any(payload['type'] == 'question_started' for payload in payloads))

    def test_rejoin_completed_question_does_not_send_question_started(self):
        self.quiz.current_question = self.question
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['current_question', 'status'])
        AssignSession.objects.create(quiz=self.quiz, is_question_active=False)

        payloads = self._capture_consumer_send()

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
        })

        self.assertTrue(any(payload['type'] == 'assign_state' for payload in payloads))
        self.assertFalse(any(payload['type'] == 'question_started' for payload in payloads))

    def test_rejoin_completed_quiz_does_not_reassert_old_endscreen(self):
        self.quiz.status = 'completed'
        self.quiz.current_question = None
        self.quiz.save(update_fields=['status', 'current_question'])

        payloads = self._capture_consumer_send()

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
        })

        self.assertFalse(any(payload['type'] in {'quiz_ended', 'assign_state'} for payload in payloads))

    def test_question_lifecycle_updates_session_active_flag(self):
        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': self.question.id,
            'hub_session': self.participant.hub_session_code,
        })

        session = AssignSession.objects.get(quiz=self.quiz)
        self.assertTrue(session.is_question_active)

        async_to_sync(self.consumer.handle_admin_end_question)({
            'hub_session': self.participant.hub_session_code,
        })
        session.refresh_from_db()
        self.assertFalse(session.is_question_active)

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': self.question.id,
            'hub_session': self.participant.hub_session_code,
        })
        session.refresh_from_db()
        self.assertTrue(session.is_question_active)

        async_to_sync(self.consumer.handle_admin_end_quiz)({})
        session.refresh_from_db()
        self.assertFalse(session.is_question_active)

    def test_correct_answer_round_0(self):
        """Richtige Zuordnung für Runde 0 ergibt True."""
        # correct für Runde 0 ist original_idx 2 (right_items[2] = 'Z')
        shuffled_pos = self._get_shuffled_pos_for_original(2)
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 0, {'0': shuffled_pos}
        )
        self.assertEqual(result, (True, 2))

    def test_wrong_answer_round_0(self):
        """Falsche Zuordnung für Runde 0 ergibt False."""
        # correct für Runde 0 ist original_idx 2; wir nehmen original_idx 0 (falsch)
        shuffled_pos = self._get_shuffled_pos_for_original(0)
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 0, {'0': shuffled_pos}
        )
        self.assertEqual(result, (False, 0))

    def test_empty_match_is_wrong(self):
        """Leeres user_match (kein Drop) zählt als falsch."""
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 0, {}
        )
        self.assertEqual(result, (False, None))

    def test_distractor_round_is_always_wrong(self):
        """Eine Runde ohne Eintrag in correct_matches (Distractor) ergibt immer False."""
        # round_index 99 existiert nicht in correct_matches → immer False
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 99, {'99': 0}
        )
        self.assertEqual(result, (False, None))

    def test_save_participant_answer_correct_conversion(self):
        """save_participant_answer konvertiert shuffled→original korrekt und speichert AssignAnswer."""
        # current_question auf dem Quiz setzen (notwendig für save_participant_answer)
        self.quiz.current_question = self.question
        self.quiz.save()

        # Shuffled Matches für alle 3 korrekten Runden bauen
        shuffled_matches = {}
        for left_idx_str, correct_orig in self.question.correct_matches.items():
            shuffled_pos = self._get_shuffled_pos_for_original(correct_orig)
            shuffled_matches[left_idx_str] = shuffled_pos

        result = async_to_sync(self.consumer.save_participant_answer)(
            'Alice', 'sess1', shuffled_matches, 12.0, self.question.id
        )

        self.assertIsNotNone(result, "save_participant_answer sollte ein Ergebnis-Dict zurückgeben")
        self.assertEqual(result['correct_matches'], 3)
        self.assertEqual(result['points_earned'], 3)
        self.assertEqual(result['accuracy'], 100.0)

        # Sicherstellen dass AssignAnswer in der DB gespeichert wurde
        answer = AssignAnswer.objects.get(
            quiz=self.quiz, participant=self.participant, question=self.question
        )
        self.assertEqual(answer.user_matches, {'0': 2, '1': 0, '2': 1})

    def test_assign_answer_score_calculation(self):
        """AssignAnswer berechnet den Score korrekt: 3 richtige Matches = 3."""
        answer = AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            user_matches={'0': 2, '1': 0, '2': 1},  # alle korrekt (original indices)
            time_taken=15.0
        )
        self.assertEqual(answer.points_earned, 3)
        self.assertEqual(answer.get_correct_matches_count(), 3)
        self.assertEqual(answer.get_accuracy_percentage(), 100.0)

    def test_assign_answer_partial_score(self):
        """AssignAnswer berechnet Teilscores korrekt: 1 richtiges Match = 1."""
        answer = AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            user_matches={'0': 2},  # nur Runde 0 korrekt
            time_taken=5.0
        )
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(answer.get_correct_matches_count(), 1)
        self.assertEqual(answer.get_total_matches_count(), 1)
        self.assertAlmostEqual(answer.get_accuracy_percentage(), 33.3, places=1)

    def test_evaluate_current_round_persists_solved_pair(self):
        """Eine korrekt evaluierte Runde speichert linkes und rechtes Element gemeinsam."""
        runtime = self._start_runtime()
        shuffled_pos = self._get_shuffled_pos_for_original(2)
        self._store_selection(
            runtime,
            round_index=0,
            left_item_index=0,
            user_match={'0': shuffled_pos},
        )

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })

        runtime.refresh_from_db()
        self.assertEqual(runtime.solved_matches, {'0': 2})
        state = AssignRoundParticipantState.objects.get(
            set_runtime=runtime,
            participant=self.participant,
            round_index=0,
        )
        self.assertTrue(state.is_correct)
        self.assertEqual(state.original_right_index, 2)
        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertFalse(round_checked['eliminated'])
        self.assertIsNone(round_checked['elimination_reason'])

    def test_wrong_assignment_emits_authoritative_elimination_reason(self):
        runtime = self._start_runtime()
        wrong_target = self._get_shuffled_pos_for_original(0)
        self._store_selection(
            runtime,
            round_index=0,
            left_item_index=0,
            user_match={'0': wrong_target},
        )

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })

        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertEqual(round_checked['elimination_reason'], 'incorrect_assignment')
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.elimination_reason, 'incorrect_assignment')

    def test_missing_assignment_emits_timeout_elimination_reason(self):
        runtime = self._start_runtime()

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })

        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertEqual(round_checked['elimination_reason'], 'no_assignment')
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.elimination_reason, 'no_assignment')

    def test_rejoin_restores_authoritative_elimination_reason(self):
        runtime = self._start_runtime()
        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })

        self.consumer.channel_name = 'channel-rejoin'
        payloads = self._capture_consumer_send()
        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
        })

        restored = next(payload for payload in payloads if payload['type'] == 'assign_state')
        self.assertEqual(restored['participant_state']['elimination_reason'], 'no_assignment')
        self.assertTrue(restored['participant_state']['eliminated'])
        self.assertFalse(any(payload['type'] == 'question_started' for payload in payloads))

    def test_elimination_survives_cache_loss_and_later_rounds_in_same_set(self):
        runtime = self._start_runtime()
        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.eliminated_set_number, runtime.set_number)
        self.assertEqual(self.participant.elimination_reason, 'no_assignment')

        payloads = self._capture_consumer_send()
        async_to_sync(self.consumer.handle_participant_update_selection)({
            'question_id': self.question.id,
            'round_index': 1,
            'left_item_index': 2,
            'user_match': {'2': self._get_shuffled_pos_for_original(1)},
        })

        self.assertEqual(payloads[-1]['type'], 'round_checked')
        self.assertEqual(payloads[-1]['set_number'], runtime.set_number)
        self.assertTrue(payloads[-1]['eliminated'])

    def test_eliminated_participant_cannot_submit_final_answer(self):
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        AssignSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            is_question_active=True,
        )
        self.participant.eliminated_set_number = 1
        self.participant.elimination_reason = 'incorrect_assignment'
        self.participant.save(update_fields=['eliminated_set_number', 'elimination_reason'])
        self._register_active_participant_channel()
        payloads = self._capture_consumer_send()
        user_matches = {
            left_index: self._get_shuffled_pos_for_original(original_index)
            for left_index, original_index in self.question.correct_matches.items()
        }

        async_to_sync(self.consumer.handle_participant_submit_answer)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
            'question_id': self.question.id,
            'user_matches': user_matches,
            'time_taken': 12,
        })

        self.assertFalse(AssignAnswer.objects.filter(participant=self.participant).exists())
        self.assertEqual(payloads[-1]['type'], 'round_checked')
        self.assertEqual(payloads[-1]['elimination_reason'], 'incorrect_assignment')

    def test_new_set_resets_persisted_elimination_once(self):
        session = AssignSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            current_round_index=2,
            is_question_active=False,
        )
        self.participant.eliminated_set_number = 1
        self.participant.elimination_reason = 'incorrect_assignment'
        self.participant.save(update_fields=['eliminated_set_number', 'elimination_reason'])

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': self.question.id,
            'hub_session': self.participant.hub_session_code,
        })

        session.refresh_from_db()
        self.participant.refresh_from_db()
        self.assertEqual(session.current_question_number, 2)
        self.assertEqual(session.current_round_index, 0)
        self.assertIsNone(self.participant.eliminated_set_number)
        self.assertEqual(self.participant.elimination_reason, '')
        started = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'question_started'
        )
        self.assertEqual(started['question']['set_number'], 2)

    def test_duplicate_set_start_does_not_reactivate_eliminated_participant(self):
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        session = AssignSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            current_round_index=2,
            is_question_active=True,
        )
        self.participant.eliminated_set_number = 1
        self.participant.elimination_reason = 'incorrect_assignment'
        self.participant.save(update_fields=['eliminated_set_number', 'elimination_reason'])

        async_to_sync(self.consumer.handle_admin_send_question)({
            'question_id': self.question.id,
            'hub_session': self.participant.hub_session_code,
        })

        session.refresh_from_db()
        self.participant.refresh_from_db()
        self.assertEqual(session.current_question_number, 1)
        self.assertEqual(session.current_round_index, 2)
        self.assertEqual(self.participant.eliminated_set_number, 1)
        self.assertEqual(self.participant.elimination_reason, 'incorrect_assignment')
        self.assertFalse(any(
            message.get('type') == 'question_started'
            for _, message in self.consumer.channel_layer.sent
        ))

    def test_stale_previous_set_elimination_cannot_affect_new_set(self):
        next_question = self._create_four_round_question()
        self.quiz.current_question = next_question
        self.quiz.save(update_fields=['current_question'])
        AssignSession.objects.create(
            quiz=self.quiz,
            current_question_number=2,
            current_round_index=0,
            is_question_active=True,
        )

        persisted = async_to_sync(self.consumer.mark_participant_eliminated_for_set)(
            self.quiz.id,
            self.question.id,
            1,
            self.participant.name,
            self.participant.hub_session_code,
            'incorrect_assignment',
        )

        self.participant.refresh_from_db()
        self.assertFalse(persisted)
        self.assertIsNone(self.participant.eliminated_set_number)
        self.assertEqual(self.participant.elimination_reason, '')

    def test_only_eliminated_participant_is_filtered_from_current_set(self):
        bob = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code='sess1',
        )
        runtime = self._start_runtime()
        wrong_target = self._get_shuffled_pos_for_original(0)
        correct_target = self._get_shuffled_pos_for_original(2)
        self._store_selection(
            runtime,
            round_index=0,
            left_item_index=0,
            user_match={'0': wrong_target},
        )
        store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=bob.name,
            hub_session_code=bob.hub_session_code,
            question_id=runtime.question_id,
            round_index=0,
            left_item_index=0,
            user_match={'0': correct_target},
            lock_selection=True,
        )
        evaluate_and_advance(
            room_code=self.quiz.room_code,
            hub_session_code=self.participant.hub_session_code,
            expected_round=0,
            set_number=runtime.set_number,
        )

        active_participants = async_to_sync(self.consumer.get_relevant_active_channels)()

        self.assertNotIn(f'{self.participant.name}::{self.participant.id}', active_participants)
        self.assertIn(f'{bob.name}::{bob.id}', active_participants)
        runtime.refresh_from_db()
        self.assertEqual(
            runtime.round_participant_ids['1'],
            [bob.id],
        )

    def test_build_round_payload_keeps_completed_pair_visible_and_open_items_filtered(self):
        """Der nächste Rundenpayload enthält gelöste Paare und nur noch offene rechte Items."""
        shuffled_pos = self._get_shuffled_pos_for_original(2)
        payload = self.consumer.build_round_payload(
            self.question,
            1,
            45,
            solved_matches={0: 2},
        )

        self.assertEqual(payload['total_rounds'], 3)
        self.assertEqual(payload['current_left_item'], {'id': 1, 'text': 'B'})
        self.assertEqual(len(payload['all_right_items']), 3)
        self.assertEqual(
            payload['solved_pairs'],
            [{
                'left_index': 0,
                'left_text': 'A',
                'right_original_index': 2,
                'right_position': shuffled_pos,
                'right_text': 'Z',
            }],
        )
        remaining_ids = {item['id'] for item in payload['right_items']}
        self.assertNotIn(shuffled_pos, remaining_ids)
        self.assertEqual(len(payload['right_items']), 2)

    def test_build_round_payload_uses_correct_match_count_with_left_distractor(self):
        """Beispiel 1: 5 linke Items, 4 Matches -> Rundenzahl bleibt 4."""
        question = self._create_left_distractor_question()

        payload = self.consumer.build_round_payload(question, 3, 45)

        self.assertEqual(payload['total_rounds'], 4)
        self.assertEqual(payload['current_left_item'], {'id': 3, 'text': 'D'})

    def test_build_round_payload_keeps_four_rounds_with_right_distractor(self):
        """Beispiel 2: 4 linke Items, 5 rechte Items, 4 Matches -> Rundenzahl bleibt 4."""
        question = self._create_right_distractor_question()

        payload = self.consumer.build_round_payload(question, 3, 45)

        self.assertEqual(payload['total_rounds'], 4)
        self.assertEqual(payload['current_left_item'], {'id': 3, 'text': 'D'})
        self.assertEqual(len(payload['all_right_items']), 5)

    def test_participant_selection_preserves_chosen_open_left_item(self):
        """Die freie Auswahl eines offenen Items bleibt bis zur Auswertung erhalten."""
        question = self._create_four_round_question()
        runtime = self._start_runtime(question)

        chosen_left_index = 3
        chosen_right_pos = self._get_shuffled_pos_for_original(chosen_left_index, question=question)
        async_to_sync(self.consumer.handle_participant_log_round)({
            'question_id': question.id,
            'round_index': 0,
            'left_item_index': chosen_left_index,
            'user_match': {'3': chosen_right_pos},
        })

        stored = AssignRoundParticipantState.objects.get(
            set_runtime=runtime,
            participant=self.participant,
            round_index=0,
        )
        self.assertEqual(stored.left_item_index, chosen_left_index)
        self.assertEqual(stored.user_match, {'3': chosen_right_pos})
        runtime.refresh_from_db()
        self.assertEqual(runtime.current_round_index, 0)
        self.assertEqual(runtime.solved_matches, {})

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })
        runtime.refresh_from_db()
        self.assertEqual(runtime.solved_matches, {'3': 3})
        self.participant.refresh_from_db()
        self.assertIsNone(self.participant.eliminated_set_number)

    def test_evaluation_prefers_actual_user_match_key_over_stale_left_item_index(self):
        """Paris -> Frankreich bleibt korrekt, auch wenn left_item_index noch stale/current ist."""
        question = self._create_capitals_question()
        runtime = self._start_runtime(question)

        paris_left_index = 2
        france_right_original_index = 1
        france_shuffled_pos = self._get_shuffled_pos_for_original(france_right_original_index, question=question)

        async_to_sync(self.consumer.handle_participant_log_round)({
            'question_id': question.id,
            'round_index': 0,
            'left_item_index': 0,
            'user_match': {'2': france_shuffled_pos},
        })

        stored = AssignRoundParticipantState.objects.get(
            set_runtime=runtime,
            participant=self.participant,
            round_index=0,
        )
        self.assertEqual(stored.left_item_index, paris_left_index)
        self.assertEqual(stored.user_match, {'2': france_shuffled_pos})
        runtime.refresh_from_db()
        self.assertEqual(runtime.current_round_index, 0)
        self.assertEqual(runtime.solved_matches, {})

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })
        runtime.refresh_from_db()
        self.assertEqual(runtime.solved_matches, {'2': 1})
        self.participant.refresh_from_db()
        self.assertIsNone(self.participant.eliminated_set_number)

    def test_host_early_end_evaluates_pending_unlogged_selection(self):
        """Host-Ende wertet eine vorhandene, aber nicht eingeloggte Zuordnung als Paar aus."""
        question = self._create_four_round_question()
        runtime = self._start_runtime(question)

        chosen_left_index = 3
        shuffled_pos = self._get_shuffled_pos_for_original(chosen_left_index, question=question)
        async_to_sync(self.consumer.handle_participant_update_selection)({
            'question_id': question.id,
            'round_index': 0,
            'left_item_index': chosen_left_index,
            'user_match': {'3': shuffled_pos},
        })

        previous_count = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })
        new_messages = [message for _, message in self.consumer.channel_layer.sent[previous_count:]]

        round_checked = next(
            (message for message in new_messages if message.get('type') == 'round_checked'),
            None,
        )
        round_advanced = next(
            (message for message in new_messages if message.get('type') == 'round_advanced'),
            None,
        )
        participant_answered = next(
            (message for message in new_messages if message.get('type') == 'participant_answered'),
            None,
        )

        self.assertIsNotNone(round_checked)
        self.assertTrue(round_checked['is_correct'])
        self.assertFalse(round_checked['eliminated'])

        self.assertIsNotNone(participant_answered)
        self.assertEqual(participant_answered['answer']['participant_name'], self.participant.name)
        self.assertFalse(participant_answered['answer']['logged'])

        self.assertIsNotNone(round_advanced)
        self.assertEqual(round_advanced['round_index'], 1)
        self.assertEqual(
            round_advanced['solved_pairs'],
            [{
                'left_index': 3,
                'left_text': 'D',
                'right_original_index': 3,
                'right_position': shuffled_pos,
                'right_text': 'Z',
            }],
        )
        remaining_ids = {item['id'] for item in round_advanced['right_items']}
        self.assertNotIn(shuffled_pos, remaining_ids)
        runtime.refresh_from_db()
        self.assertEqual(runtime.solved_matches, {'3': 3})

    def test_host_early_end_without_selection_uses_no_assignment_reason(self):
        question = self._create_four_round_question()
        runtime = self._start_runtime(question)

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })

        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertEqual(round_checked['elimination_reason'], 'no_assignment')

    def test_four_round_sequence_advances_cleanly(self):
        """Vier aufeinanderfolgende Runden bleiben sequentiell und enden erst nach Runde 4."""
        question = self._create_four_round_question()
        runtime = self._start_runtime(question)
        session = AssignSession.objects.get(quiz=self.quiz)

        expected_events = ['round_advanced', 'round_advanced', 'round_advanced', 'question_rounds_complete']
        observed_events = []

        chosen_order = [3, 1, 0, 2]

        for round_index, left_item_index in enumerate(chosen_order):
            shuffled_pos = self._get_shuffled_pos_for_original(left_item_index, question=question)
            self._store_selection(
                runtime,
                round_index=round_index,
                left_item_index=left_item_index,
                user_match={str(left_item_index): shuffled_pos},
            )

            previous_count = len(self.consumer.channel_layer.sent)
            async_to_sync(self.consumer.handle_admin_next_round)({
                'expected_round': round_index,
                'expected_set': runtime.set_number,
            })
            new_messages = [message for _, message in self.consumer.channel_layer.sent[previous_count:]]

            flow_event = next(
                (
                    message for message in new_messages
                    if message.get('type') in ('round_advanced', 'question_rounds_complete')
                ),
                None,
            )
            self.assertIsNotNone(flow_event)
            observed_events.append(flow_event['type'])

            if round_index < 3:
                session.refresh_from_db()
                self.assertTrue(session.is_question_active)
                self.assertEqual(flow_event['round_index'], round_index + 1)
                self.assertEqual(flow_event['total_rounds'], 4)
                self.assertEqual(len(flow_event['right_items']), 3 - round_index)
                self.assertEqual(len(flow_event['solved_pairs']), round_index + 1)
            else:
                session.refresh_from_db()
                self.assertFalse(session.is_question_active)
                self.assertEqual(len(flow_event['solved_pairs']), 4)

        self.assertEqual(observed_events, expected_events)
        runtime.refresh_from_db()
        self.assertEqual(runtime.solved_matches, {'0': 0, '1': 1, '2': 2, '3': 3})
        self.assertEqual(async_to_sync(self.consumer.get_current_round_index)(self.quiz.id), 3)

    def test_progress_history_uses_correct_match_count_for_max_rounds(self):
        """Fortschrittshistory verwendet die Anzahl lösbarer Paare statt left_items."""
        question = self._create_left_distractor_question()
        self.quiz.current_question = question
        self.quiz.save()

        shuffled_matches = {}
        for left_idx_str, correct_orig in question.correct_matches.items():
            shuffled_matches[left_idx_str] = self._get_shuffled_pos_for_original(correct_orig, question=question)

        result = async_to_sync(self.consumer.save_participant_answer)(
            'Alice', 'sess1', shuffled_matches, 12.0, question.id
        )

        self.assertEqual(result['progress_history'], [{
            'question_id': question.id,
            'question_number': 1,
            'survived_rounds': 4,
            'max_rounds': 4,
        }])

    def test_progress_history_ignores_stale_answers_from_other_hub_session(self):
        question_one = AssignQuestion.objects.create(
            question_text='Question one',
            points=10,
            time_limit=45,
            left_items=['A', 'B'],
            right_items=['X', 'Y'],
            correct_matches={'0': 0, '1': 1},
            created_by=self.user,
        )
        question_two = AssignQuestion.objects.create(
            question_text='Question two',
            points=10,
            time_limit=45,
            left_items=['C', 'D'],
            right_items=['U', 'V'],
            correct_matches={'0': 0, '1': 1},
            created_by=self.user,
        )
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_one.id, question_two.id]
        self.quiz.current_question = question_two
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['question_order', 'current_question', 'status'])

        stale_participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code='sess2',
        )
        AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=stale_participant,
            question=question_one,
            user_matches={'0': 0},
            time_taken=3.0,
        )

        shuffled_matches = {}
        for left_idx_str, correct_orig in question_two.correct_matches.items():
            shuffled_matches[left_idx_str] = self._get_shuffled_pos_for_original(correct_orig, question=question_two)

        result = async_to_sync(self.consumer.save_participant_answer)(
            'Alice',
            'sess1',
            shuffled_matches,
            12.0,
            question_two.id,
        )

        self.assertEqual(result['progress_history'], [{
            'question_id': question_two.id,
            'question_number': 1,
            'survived_rounds': 2,
            'max_rounds': 2,
        }])

    def test_assign_monitor_uses_correct_match_count_for_rounds_and_preview(self):
        """Admin-Monitor zeigt fachliche Rundenzahl und Match-Preview über correct_matches."""
        question = self._create_left_distractor_question()
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        response = self.client.get(reverse('admin_dashboard:assign_monitor', args=[self.quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '4 matches')
        self.assertNotContains(response, 'pts per match')

        self.quiz.current_question = question
        self.quiz.save()

        response = self.client.get(reverse('admin_dashboard:assign_monitor', args=[self.quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_rounds_current'], 4)
        self.assertContains(response, 'Runde 1 von 4')
        self.assertNotContains(response, 'pts per match')

    def test_assign_monitor_uses_persisted_round_deadline_after_reload(self):
        question = self._create_left_distractor_question()
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])
        participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Monitor participant',
            hub_session_code='monitor-session',
        )
        runtime, started = start_set_runtime(
            quiz_id=self.quiz.id,
            question_id=question.id,
            hub_session_code=participant.hub_session_code,
            effective_time_limit=45,
        )
        self.assertTrue(started)
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        halfway = runtime.round_started_at + timezone.timedelta(seconds=22)
        with patch('admin_dashboard.views.timezone.now', return_value=halfway):
            response = self.client.get(
                reverse('admin_dashboard:assign_monitor', args=[self.quiz.room_code]),
                {'hub_session': participant.hub_session_code},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_round_ends_at'], runtime.round_ends_at)
        self.assertEqual(response.context['current_round_time_left'], 23)
        self.assertContains(response, 'this.roundEndsAt = ')
        self.assertNotContains(response, 'override_time_${this.roomCode}')
        self.assertContains(response, '<span id="questionTimeLeft">23</span>', html=True)

    def test_add_assign_question_ignores_legacy_points_field(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('admin_dashboard:add_assign_question'),
            data={
                'question_text': 'Assign without legacy points',
                'points': 99,
                'time_limit': 60,
                'left_items': ['A', 'B'],
                'right_items': ['X', 'Y'],
                'correct_matches': {'0': 0, '1': 1},
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        created = AssignQuestion.objects.get(question_text='Assign without legacy points')
        self.assertEqual(created.points, 1)
        self.assertEqual(created.get_total_possible_points(), 2)

    def test_manage_games_assign_form_hides_legacy_points_field(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        response = self.client.get(reverse('admin_dashboard:create_game'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="asgn-points"', html=False)
        self.assertNotContains(response, 'Points per Match')

    def test_manage_games_overview_assign_editor_hides_legacy_points_field(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        response = self.client.get(reverse('admin_dashboard:manage_games'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="qba-points"', html=False)

    def test_get_assign_selected_questions_omits_legacy_points_field(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)
        self.quiz.selected_questions.set([self.question])

        response = self.client.get(
            reverse('admin_dashboard:get_assign_selected_questions', args=[self.quiz.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['questions']), 1)
        self.assertNotIn('points', payload['questions'][0])

    def test_assign_round_payload_omits_legacy_points_field(self):
        payload = self.consumer.build_round_payload(self.question, round_index=0, time_limit=45)

        self.assertNotIn('points', payload)
        self.assertEqual(payload['total_possible_points'], 3)


class AssignPersistentRuntimeTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='assign-runtime')
        self.quiz = AssignQuiz.objects.create(
            title='Persistent Assign',
            creator=self.user,
            room_code='6199',
            status='active',
        )
        self.question = AssignQuestion.objects.create(
            question_text='Persist the match',
            points=4,
            time_limit=40,
            left_items=['A', 'B'],
            right_items=['X', 'Y'],
            correct_matches={'0': 0, '1': 1},
            created_by=self.user,
        )
        self.participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='runtime-session',
        )
        self.runtime, started = start_set_runtime(
            quiz_id=self.quiz.id,
            question_id=self.question.id,
            hub_session_code=self.participant.hub_session_code,
            effective_time_limit=40,
        )
        self.assertTrue(started)

    def _consumer(self, channel_name):
        consumer = AssignConsumer()
        consumer.room_code = self.quiz.room_code
        consumer.room_group_name = f'assign_{self.quiz.room_code}'
        consumer.channel_name = channel_name
        consumer.channel_layer = DummyChannelLayer()
        consumer._authoritative_participant = self.participant.name
        consumer._authoritative_session = self.participant.hub_session_code
        return consumer

    def _correct_position(self, original_index):
        randomized = self.question.get_randomized_items(room_code=self.quiz.room_code)
        return next(
            position
            for position, original in randomized['position_to_original'].items()
            if original == original_index
        )

    def test_start_runtime_preserves_legacy_question_counters(self):
        session = AssignSession.objects.get(quiz=self.quiz)

        self.assertEqual(session.current_question_number, 1)
        self.assertEqual(session.total_questions_sent, 1)

    def test_refresh_keeps_absolute_deadline_at_25_50_and_90_percent(self):
        consumer = self._consumer('refresh-a')
        duration = self.runtime.round_ends_at - self.runtime.round_started_at
        snapshots = []
        for fraction in (0.25, 0.5, 0.9):
            at = self.runtime.round_started_at + (duration * fraction)
            with patch('Assign.consumers.timezone.now', return_value=at):
                snapshots.append(async_to_sync(consumer.get_rejoin_snapshot)(
                    self.participant.name,
                    self.participant.hub_session_code,
                ))

        self.assertEqual(
            {snapshot['starts_at'] for snapshot in snapshots},
            {self.runtime.round_started_at.isoformat()},
        )
        self.assertEqual(
            {snapshot['ends_at'] for snapshot in snapshots},
            {self.runtime.round_ends_at.isoformat()},
        )

    def test_selection_and_lock_are_shared_by_tabs_and_survive_worker_restart(self):
        position = self._correct_position(0)
        first = store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=self.question.id,
            round_index=0,
            left_item_index=0,
            user_match={'0': position},
            lock_selection=False,
        )
        self.assertTrue(first.accepted)

        second_worker = self._consumer('tab-b')
        snapshot = async_to_sync(second_worker.get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            snapshot['participant_state']['selection']['user_match'],
            {'0': position},
        )
        self.assertFalse(snapshot['participant_state']['answer_locked'])

        locked = store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=self.question.id,
            round_index=0,
            left_item_index=0,
            user_match={'0': position},
            lock_selection=True,
        )
        self.assertTrue(locked.accepted)
        restarted_worker = self._consumer('worker-after-restart')
        restarted_snapshot = async_to_sync(restarted_worker.get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertTrue(restarted_snapshot['participant_state']['answer_locked'])
        self.assertEqual(
            AssignRoundParticipantState.objects.filter(
                set_runtime=self.runtime,
                participant=self.participant,
                round_index=0,
            ).count(),
            1,
        )

    def test_stale_context_deadline_and_duplicate_submit_are_rejected(self):
        position = self._correct_position(0)
        stale = store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=self.question.id + 1,
            round_index=0,
            left_item_index=0,
            user_match={'0': position},
            lock_selection=True,
        )
        self.assertEqual(stale.code, 'stale_action')

        with patch(
            'Assign.runtime.timezone.now',
            return_value=self.runtime.round_ends_at,
        ):
            expired = store_round_selection(
                room_code=self.quiz.room_code,
                participant_name=self.participant.name,
                hub_session_code=self.participant.hub_session_code,
                question_id=self.question.id,
                round_index=0,
                left_item_index=0,
                user_match={'0': position},
                lock_selection=True,
            )
        self.assertEqual(expired.code, 'deadline_expired')

        accepted = store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=self.question.id,
            round_index=0,
            left_item_index=0,
            user_match={'0': position},
            lock_selection=True,
        )
        duplicate = store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=self.question.id,
            round_index=0,
            left_item_index=0,
            user_match={'0': position},
            lock_selection=True,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(duplicate.code, 'already_submitted')

    def test_duplicate_client_action_id_is_reserved_once_across_tabs(self):
        snapshot = observe_snapshot(
            game_key='assign',
            room_code=self.quiz.room_code,
            session_code=self.participant.hub_session_code,
            payload={
                'phase': 'active',
                'game': {'id': self.quiz.id, 'status': 'active'},
                'current_question_id': self.question.id,
                'current_round_id': 0,
                'current_set_id': self.runtime.set_number,
                'starts_at': self.runtime.round_started_at.isoformat(),
                'ends_at': self.runtime.round_ends_at.isoformat(),
            },
        )
        action = {
            'client_action_id': str(uuid.uuid4()),
            'state_revision': snapshot['state_revision'],
            'game_id': str(self.quiz.id),
            'question_id': str(self.question.id),
            'round_id': '0',
            'set_id': str(self.runtime.set_number),
        }

        first_tab = validate_and_reserve_action(
            game_key='assign',
            room_code=self.quiz.room_code,
            session_code=self.participant.hub_session_code,
            participant_name=self.participant.name,
            action_type='participant_log_round',
            action=action,
        )
        second_tab = validate_and_reserve_action(
            game_key='assign',
            room_code=self.quiz.room_code,
            session_code=self.participant.hub_session_code,
            participant_name=self.participant.name,
            action_type='participant_log_round',
            action=action,
        )

        self.assertTrue(first_tab.accepted)
        self.assertEqual(second_tab.code, 'already_submitted')

    def test_final_actions_always_require_authoritative_context(self):
        consumer = self._consumer('required-action-context')

        self.assertTrue(consumer._is_guarded_action('participant_log_round', {}))
        self.assertTrue(consumer._is_guarded_action('participant_submit_answer', {}))

    def test_connection_identity_cannot_be_rebound_by_a_later_action(self):
        consumer = self._consumer('immutable-identity')

        consumer._capture_identity({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
        })
        consumer._capture_identity({
            'participant_name': 'Mallory',
            'hub_session': 'other-session',
        })

        self.assertEqual(
            consumer._authoritative_participant_name(),
            self.participant.name,
        )
        self.assertEqual(
            consumer._authoritative_session_code(),
            self.participant.hub_session_code,
        )

    def test_evaluation_is_once_only_and_disconnect_does_not_advance(self):
        position = self._correct_position(0)
        store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=self.question.id,
            round_index=0,
            left_item_index=0,
            user_match={'0': position},
            lock_selection=False,
        )
        consumer = self._consumer('disconnecting-tab')
        async_to_sync(consumer.disconnect)(1000)
        self.runtime.refresh_from_db()
        self.assertEqual(self.runtime.current_round_index, 0)
        self.assertEqual(self.runtime.evaluated_rounds, [])

        first = evaluate_and_advance(
            room_code=self.quiz.room_code,
            hub_session_code=self.participant.hub_session_code,
            expected_round=0,
            set_number=self.runtime.set_number,
        )
        second = evaluate_and_advance(
            room_code=self.quiz.room_code,
            hub_session_code=self.participant.hub_session_code,
            expected_round=0,
            set_number=self.runtime.set_number,
        )
        self.assertTrue(first['advanced'])
        self.assertFalse(second['advanced'])
        self.assertIn(second['code'], {'stale_action', 'already_evaluated'})
        state = AssignRoundParticipantState.objects.get(
            set_runtime=self.runtime,
            participant=self.participant,
            round_index=0,
        )
        self.assertIsNotNone(state.evaluated_at)


class AssignManualQuestionPhaseTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='assign-phases')
        self.quiz = AssignQuiz.objects.create(
            title='Assign Phases',
            creator=self.user,
            room_code='6288',
            status='active',
        )
        self.question = AssignQuestion.objects.create(
            question_text='Ordne die Paare zu',
            time_limit=30,
            left_items=['Alpha', 'Beta', 'Gamma'],
            right_items=['Eins', 'Zwei', 'Drei'],
            correct_matches={'0': 0, '1': 1, '2': 2},
            created_by=self.user,
        )
        self.participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='assign-phase-session',
        )
        self.consumer = AssignConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'assign_{self.quiz.room_code}'
        self.consumer.channel_name = 'assign-phase-channel'
        self.consumer.channel_layer = DummyChannelLayer()
        self.consumer._authoritative_participant = self.participant.name
        self.consumer._authoritative_session = self.participant.hub_session_code
        self.sent_payloads = []

        async def capture_send(*args, **kwargs):
            self.sent_payloads.append(json.loads(kwargs['text_data']))

        self.consumer.send = capture_send
        self.runtime_snapshot = reset_question_flow(
            game_key='assign',
            room_code=self.quiz.room_code,
            session_code=self.participant.hub_session_code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def phase_action(self, snapshot, *, action_type, round_index=0, set_number=1):
        return {
            'type': action_type,
            'client_action_id': str(uuid.uuid4()),
            'state_revision': snapshot['state_revision'],
            'game_id': snapshot.get('game_id'),
            'question_id': self.question.id,
            'expected_round': round_index,
            'expected_set': set_number,
            'hub_session': self.participant.hub_session_code,
        }

    def send_first_round(self, at=None):
        action = self.phase_action(
            self.runtime_snapshot,
            action_type='admin_send_question',
        )
        patcher = (
            patch('games_hub.authoritative_state.timezone.now', return_value=at)
            if at
            else patch('games_hub.authoritative_state.timezone.now')
        )
        if at:
            with patcher:
                async_to_sync(self.consumer.handle_admin_send_question)(action)
        else:
            async_to_sync(self.consumer.handle_admin_send_question)(action)
        return current_snapshot(
            'assign',
            self.quiz.room_code,
            self.participant.hub_session_code,
        )

    def reveal_round(self, snapshot, at, *, round_index=0, set_number=1):
        action = self.phase_action(
            snapshot,
            action_type='admin_reveal_question_content',
            round_index=round_index,
            set_number=set_number,
        )
        with patch('games_hub.authoritative_state.timezone.now', return_value=at):
            async_to_sync(self.consumer.handle_admin_reveal_question_content)(action)
        return current_snapshot(
            'assign',
            self.quiz.room_code,
            self.participant.hub_session_code,
        )

    def open_round(self, snapshot, at, *, round_index=0, set_number=1, action=None):
        action = action or self.phase_action(
            snapshot,
            action_type='admin_open_answering',
            round_index=round_index,
            set_number=set_number,
        )
        with patch('Assign.consumers.timezone.now', return_value=at):
            async_to_sync(self.consumer.handle_admin_open_answering)(action)
        return current_snapshot(
            'assign',
            self.quiz.room_code,
            self.participant.hub_session_code,
        )

    def test_assign_reveal_automatically_opens_first_round_after_animation(self):
        presented_at = timezone.now()
        prompt = self.send_first_round(presented_at)
        runtime = current_set_runtime(
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        session = AssignSession.objects.get(quiz=self.quiz)

        self.assertEqual(prompt['question_phase'], 'prompt_visible')
        self.assertIsNone(prompt['answering_deadline_at'])
        self.assertIsNone(session.question_end_time)
        self.assertEqual(
            prompt['question_visible_at'],
            (
                presented_at
                + timezone.timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS)
            ).isoformat(),
        )
        blocked = store_round_selection(
            room_code=self.quiz.room_code,
            participant_name=self.participant.name,
            hub_session_code=self.participant.hub_session_code,
            question_id=self.question.id,
            round_index=0,
            left_item_index=0,
            user_match={'0': 0},
            lock_selection=False,
        )
        self.assertEqual(blocked.code, 'invalid_phase')

        visible_at = presented_at + timezone.timedelta(
            milliseconds=QUESTION_PRESENTATION_DELAY_MS
        )
        opened = self.reveal_round(prompt, visible_at)
        session.refresh_from_db()
        ready_at = assign_reveal_ready_at(runtime, visible_at)
        runtime.refresh_from_db()
        self.assertEqual(opened['question_phase'], 'answering_open')
        self.assertEqual(opened['answering_started_at'], ready_at.isoformat())
        self.assertEqual(
            opened['answering_deadline_at'],
            (ready_at + timezone.timedelta(seconds=30)).isoformat(),
        )
        self.assertEqual(session.question_end_time, runtime.round_ends_at)
        with patch('Assign.runtime.timezone.now', return_value=visible_at):
            early = store_round_selection(
                room_code=self.quiz.room_code,
                participant_name=self.participant.name,
                hub_session_code=self.participant.hub_session_code,
                question_id=self.question.id,
                round_index=0,
                left_item_index=0,
                user_match={'0': 0},
                lock_selection=False,
            )
        self.assertEqual(early.code, 'invalid_phase')
        with patch('Assign.runtime.timezone.now', return_value=ready_at):
            accepted = store_round_selection(
                room_code=self.quiz.room_code,
                participant_name=self.participant.name,
                hub_session_code=self.participant.hub_session_code,
                question_id=self.question.id,
                round_index=0,
                left_item_index=0,
                user_match={'0': 0},
                lock_selection=False,
            )
        self.assertTrue(accepted.accepted)

    def test_first_send_action_creates_and_broadcasts_the_set_once(self):
        action = self.phase_action(
            self.runtime_snapshot,
            action_type='admin_send_question',
        )

        async_to_sync(self.consumer.handle_admin_send_question)(action)
        async_to_sync(self.consumer.handle_admin_send_question)(action)

        session = AssignSession.objects.get(quiz=self.quiz)
        runtimes = AssignSetRuntime.objects.filter(
            quiz=self.quiz,
            question=self.question,
            hub_session_code=self.participant.hub_session_code,
        )
        event_types = [event['type'] for _, event in self.consumer.channel_layer.sent]
        self.assertEqual(session.current_question_number, 1)
        self.assertEqual(session.total_questions_sent, 1)
        self.assertEqual(runtimes.count(), 1)
        self.assertEqual(event_types.count('question_started'), 1)
        self.assertFalse(any(payload['type'] == 'action_rejected' for payload in self.sent_payloads))

    def test_duplicate_reveal_does_not_extend_deadline(self):
        presented_at = timezone.now()
        prompt = self.send_first_round(presented_at)
        action = self.phase_action(
            prompt,
            action_type='admin_reveal_question_content',
        )
        visible_at = (
            presented_at
            + timezone.timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS)
        )
        with patch('games_hub.authoritative_state.timezone.now', return_value=visible_at):
            async_to_sync(self.consumer.handle_admin_reveal_question_content)(action)
        opened = current_snapshot(
            'assign',
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        with patch(
            'games_hub.authoritative_state.timezone.now',
            return_value=visible_at + timezone.timedelta(seconds=5),
        ):
            async_to_sync(self.consumer.handle_admin_reveal_question_content)(action)
        duplicate = current_snapshot(
            'assign',
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        runtime = current_set_runtime(
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            duplicate['answering_deadline_at'],
            opened['answering_deadline_at'],
        )
        self.assertEqual(
            runtime.round_ends_at.isoformat(),
            opened['answering_deadline_at'],
        )

    def test_legacy_submit_is_rejected_before_open_and_accepted_after_open(self):
        presented_at = timezone.now()
        prompt = self.send_first_round(presented_at)
        payload = {
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
            'question_id': self.question.id,
            'user_matches': {'0': 0, '1': 1, '2': 2},
            'time_taken': 1,
        }

        async_to_sync(self.consumer.handle_participant_submit_answer)(payload)

        self.assertFalse(AssignAnswer.objects.filter(
            participant=self.participant,
            question=self.question,
        ).exists())
        self.assertEqual(self.sent_payloads[-2]['type'], 'action_rejected')
        self.assertEqual(self.sent_payloads[-2]['code'], 'invalid_phase')

        visible_at = presented_at + timezone.timedelta(
            milliseconds=QUESTION_PRESENTATION_DELAY_MS
        )
        self.reveal_round(prompt, visible_at)
        runtime = current_set_runtime(
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        opened_at = assign_reveal_ready_at(runtime, visible_at)
        randomized = self.question.get_randomized_items(room_code=self.quiz.room_code)
        payload['user_matches'] = {
            str(left_index): next(
                position
                for position, original in randomized['position_to_original'].items()
                if original == right_index
            )
            for left_index, right_index in self.question.correct_matches.items()
        }

        with patch('Assign.runtime.timezone.now', return_value=opened_at):
            async_to_sync(self.consumer.handle_participant_submit_answer)(payload)

        self.assertTrue(AssignAnswer.objects.filter(
            participant=self.participant,
            question=self.question,
        ).exists())
        self.assertEqual(self.sent_payloads[-1]['type'], 'answer_submitted')

    def test_next_internal_round_opens_directly_without_another_reveal(self):
        presented_at = timezone.now()
        prompt = self.send_first_round(presented_at)
        visible_at = presented_at + timezone.timedelta(
            milliseconds=QUESTION_PRESENTATION_DELAY_MS
        )
        opened = self.reveal_round(prompt, visible_at)
        runtime = current_set_runtime(
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        opened_at = assign_reveal_ready_at(runtime, visible_at)
        randomized = self.question.get_randomized_items(room_code=self.quiz.room_code)
        correct_position = next(
            position
            for position, original in randomized['position_to_original'].items()
            if original == 0
        )
        with patch('Assign.runtime.timezone.now', return_value=opened_at):
            stored = store_round_selection(
                room_code=self.quiz.room_code,
                participant_name=self.participant.name,
                hub_session_code=self.participant.hub_session_code,
                question_id=self.question.id,
                round_index=0,
                left_item_index=0,
                user_match={'0': correct_position},
                lock_selection=True,
            )
        self.assertTrue(stored.accepted)

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })

        next_snapshot = current_snapshot(
            'assign',
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        runtime.refresh_from_db()
        session = AssignSession.objects.get(quiz=self.quiz)
        self.assertEqual(runtime.current_round_index, 1)
        self.assertEqual(next_snapshot['question_phase'], 'answering_open')
        self.assertIsNotNone(next_snapshot['answering_deadline_at'])
        self.assertEqual(
            session.question_end_time.isoformat(),
            next_snapshot['answering_deadline_at'],
        )
        self.assertEqual(runtime.solved_matches, {'0': 0})

    def test_participant_round_log_never_advances_without_host_action(self):
        second_participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Bea',
            hub_session_code=self.participant.hub_session_code,
        )
        presented_at = timezone.now()
        prompt = self.send_first_round(presented_at)
        visible_at = presented_at + timezone.timedelta(
            milliseconds=QUESTION_PRESENTATION_DELAY_MS
        )
        self.reveal_round(prompt, visible_at)
        runtime = current_set_runtime(
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        opened_at = assign_reveal_ready_at(runtime, visible_at)
        randomized = self.question.get_randomized_items(room_code=self.quiz.room_code)
        correct_position = next(
            position
            for position, original in randomized['position_to_original'].items()
            if original == 0
        )

        def log_round(participant):
            self.consumer._authoritative_participant = participant.name
            with patch('Assign.runtime.timezone.now', return_value=opened_at):
                async_to_sync(self.consumer.handle_participant_log_round)({
                    'question_id': self.question.id,
                    'round_index': 0,
                    'left_item_index': 0,
                    'user_match': {'0': correct_position},
                })

        log_round(self.participant)
        runtime.refresh_from_db()
        self.assertEqual(runtime.current_round_index, 0)
        self.assertEqual(runtime.evaluated_rounds, [])

        log_round(second_participant)
        runtime.refresh_from_db()
        self.assertEqual(runtime.current_round_index, 0)
        self.assertEqual(runtime.evaluated_rounds, [])
        self.assertFalse(any(
            event['type'] == 'round_advanced'
            for _, event in self.consumer.channel_layer.sent
        ))
        statuses = round_status(
            self.quiz.room_code,
            self.participant.hub_session_code,
            0,
        )
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(status['logged'] for status in statuses))

        log_round(second_participant)
        runtime.refresh_from_db()
        self.assertEqual(runtime.current_round_index, 0)

        async_to_sync(self.consumer.handle_admin_next_round)({
            'expected_round': 0,
            'expected_set': runtime.set_number,
        })
        runtime.refresh_from_db()
        self.assertEqual(runtime.current_round_index, 1)
        self.assertEqual(runtime.evaluated_rounds, [0])
        self.assertEqual(sum(
            event['type'] == 'round_advanced'
            for _, event in self.consumer.channel_layer.sent
        ), 1)

    def test_three_internal_rounds_need_only_the_initial_reveal(self):
        snapshot = self.send_first_round(timezone.now())

        for round_index in range(3):
            if round_index == 0:
                visible_at = parse_datetime(snapshot['question_visible_at'])
                snapshot = self.reveal_round(snapshot, visible_at)
            self.assertEqual(snapshot['question_phase'], 'answering_open')
            runtime = current_set_runtime(
                self.quiz.room_code,
                self.participant.hub_session_code,
            )
            randomized = self.question.get_randomized_items(
                room_code=self.quiz.room_code
            )
            correct_position = next(
                position
                for position, original in randomized['position_to_original'].items()
                if original == round_index
            )
            round_started_at = parse_datetime(snapshot['answering_started_at'])
            with patch('Assign.runtime.timezone.now', return_value=round_started_at):
                stored = store_round_selection(
                    room_code=self.quiz.room_code,
                    participant_name=self.participant.name,
                    hub_session_code=self.participant.hub_session_code,
                    question_id=self.question.id,
                    round_index=round_index,
                    left_item_index=round_index,
                    user_match={str(round_index): correct_position},
                    lock_selection=True,
                )
            self.assertTrue(stored.accepted)

            async_to_sync(self.consumer.handle_admin_next_round)({
                'expected_round': round_index,
                'expected_set': runtime.set_number,
            })
            snapshot = current_snapshot(
                'assign',
                self.quiz.room_code,
                self.participant.hub_session_code,
            )
            if round_index < 2:
                self.assertEqual(snapshot['question_phase'], 'answering_open')
                self.assertIsNotNone(snapshot['answering_deadline_at'])

        runtime.refresh_from_db()
        self.assertEqual(runtime.solved_matches, {'0': 0, '1': 1, '2': 2})
        self.assertEqual(runtime.phase, AssignSetRuntime.PHASE_WAITING_REVEAL)
        self.assertIsNone(snapshot['question_phase'])

    def test_rejoin_reconstructs_prompt_reveal_progress_and_open_round(self):
        presented_at = timezone.now()
        prompt = self.send_first_round(presented_at)
        prompt_rejoin = async_to_sync(self.consumer.get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(prompt_rejoin['question_phase'], 'prompt_visible')
        self.assertEqual(
            prompt_rejoin['question_visible_at'],
            prompt['question_visible_at'],
        )
        self.assertIsNone(prompt_rejoin['ends_at'])

        visible_at = parse_datetime(prompt['question_visible_at'])
        opened = self.reveal_round(prompt, visible_at)
        reveal_midpoint = visible_at + timezone.timedelta(milliseconds=250)
        with patch('Assign.consumers.timezone.now', return_value=reveal_midpoint):
            content_rejoin = async_to_sync(self.consumer.get_rejoin_snapshot)(
                self.participant.name,
                self.participant.hub_session_code,
            )
        self.assertEqual(content_rejoin['question_phase'], 'answering_open')
        self.assertEqual(content_rejoin['server_now'], reveal_midpoint.isoformat())
        self.assertEqual(content_rejoin['assign_target_reveal_count'], 3)
        self.assertEqual(content_rejoin['assign_element_reveal_count'], 3)
        self.assertEqual(content_rejoin['assign_reveal_stagger_ms'], 120)
        self.assertEqual(
            content_rejoin['assign_reveal_ready_at'],
            (
                visible_at
                + timezone.timedelta(
                    milliseconds=(5 * ASSIGN_REVEAL_STAGGER_MS)
                    + ASSIGN_REVEAL_ANIMATION_MS
                )
            ).isoformat(),
        )
        self.assertEqual(
            content_rejoin['ends_at'],
            opened['answering_deadline_at'],
        )

        runtime = current_set_runtime(
            self.quiz.room_code,
            self.participant.hub_session_code,
        )
        opened_at = assign_reveal_ready_at(runtime, visible_at)
        with patch('Assign.consumers.timezone.now', return_value=opened_at):
            open_rejoin = async_to_sync(self.consumer.get_rejoin_snapshot)(
                self.participant.name,
                self.participant.hub_session_code,
            )
        self.assertEqual(open_rejoin['question_phase'], 'answering_open')
        self.assertEqual(open_rejoin['starts_at'], opened_at.isoformat())
        self.assertEqual(
            open_rejoin['ends_at'],
            (opened_at + timezone.timedelta(seconds=30)).isoformat(),
        )

    def test_assign_templates_use_target_then_element_reveal_and_phase_controls(self):
        project_root = Path(__file__).resolve().parent.parent
        participant_source = (
            project_root / 'templates' / 'assign' / 'play.html'
        ).read_text(encoding='utf-8')
        host_source = (
            project_root / 'templates' / 'admin_dashboard' / 'assign_monitor.html'
        ).read_text(encoding='utf-8')

        self.assertIn('const targets = Array.from(', participant_source)
        self.assertIn('const elements = Array.from(', participant_source)
        self.assertIn('return [...targets, ...elements];', participant_source)
        self.assertIn("['content_visible', 'answering_open'].includes", participant_source)
        self.assertIn("this.questionPhase === 'answering_open'", participant_source)
        self.assertIn(
            'this.estimatedPhaseServerNow() - revealedAt',
            participant_source,
        )
        self.assertIn(
            'const remaining = (index * this.assignRevealStaggerMs) - elapsed;',
            participant_source,
        )
        self.assertIn('ELEMENTE UND ZIELE ENTHÜLLEN', host_source)
        self.assertNotIn('RUNDE FREIGEBEN', host_source)
        self.assertIn('this.pendingQuestionId = null;', host_source)
        self.assertIn('this.sendSocketReady = false;', host_source)
        self.assertIn('this.pendingQuestionId !== null) return;', host_source)
        self.assertIn('this.websocket.readyState === WebSocket.OPEN', host_source)
        self.assertIn('this.updateSendQuestionAvailability();', host_source)
        self.assertIn('data-question-id="{{ question.id }}" disabled', host_source)
        self.assertEqual(ASSIGN_REVEAL_STAGGER_MS, 120)
        self.assertEqual(ASSIGN_REVEAL_ANIMATION_MS, 160)


class AssignPlayScoreboardViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='playerview', password='pass')
        self.quiz = AssignQuiz.objects.create(
            title='Scoreboard Quiz',
            creator=self.user,
            room_code='5678',
        )
        self.participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess1',
        )

    def _create_question(self, question_text, left_items, right_items, correct_matches):
        return AssignQuestion.objects.create(
            question_text=question_text,
            points=10,
            time_limit=45,
            left_items=left_items,
            right_items=right_items,
            correct_matches=correct_matches,
            created_by=self.user,
        )

    def test_assign_play_context_builds_full_question_scoreboard_from_start(self):
        """Die Spielerseite kennt von Beginn an alle Fragen und markiert die erste als aktuell."""
        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        question_two = self._create_question(
            'Question two',
            ['C', 'D', 'E'],
            ['U', 'V', 'W'],
            {'0': 0, '1': 1, '2': 2},
        )
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_two.id, question_one.id]
        self.quiz.current_question = None
        self.quiz.save(update_fields=['question_order', 'current_question'])

        response = self.client.get(
            reverse('assign:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['current_question_id'])
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_two.id,
                    'number': 1,
                    'earned_points': None,
                    'max_points': None,
                    'status': 'current',
                },
                {
                    'id': question_one.id,
                    'number': 2,
                    'earned_points': None,
                    'max_points': None,
                    'status': 'upcoming',
                },
            ],
        )
        self.assertContains(response, 'Punkte')
        self.assertContains(response, 'assignScoreList')
        self.assertContains(response, 'assign-score-empty score-box__empty')

    def test_assign_play_renders_points_box_with_played_current_and_upcoming_rows(self):
        """Die Punkte-Box rendert Platzhalter und Statusfarben für gespielt/aktuell/offen."""
        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        question_two = self._create_question(
            'Question two',
            ['C', 'D', 'E'],
            ['U', 'V', 'W'],
            {'0': 0, '1': 1, '2': 2},
        )
        question_three = self._create_question(
            'Question three',
            ['F'],
            ['Z'],
            {'0': 0},
        )
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_two
        self.quiz.save(update_fields=['question_order', 'current_question'])

        AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_matches={'0': 0},
            time_taken=5.0,
        )

        response = self.client.get(
            reverse('assign:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_one.id,
                    'number': 1,
                    'earned_points': 1,
                    'max_points': 2,
                    'status': 'played',
                },
                {
                    'id': question_two.id,
                    'number': 2,
                    'earned_points': None,
                    'max_points': 3,
                    'status': 'current',
                },
                {
                    'id': question_three.id,
                    'number': 3,
                    'earned_points': None,
                    'max_points': None,
                    'status': 'upcoming',
                },
            ],
        )
        self.assertContains(response, 'assign-score-row score-box__row is-played')
        self.assertContains(response, 'assign-score-row score-box__row is-current')
        self.assertContains(response, 'assign-score-row score-box__row is-upcoming')
        self.assertContains(response, 'assign-score-empty')
        self.assertContains(response, 'assign-score-badge')

    def test_assign_play_uses_actual_send_order_for_out_of_order_current_question(self):
        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        question_two = self._create_question(
            'Question two',
            ['C', 'D'],
            ['U', 'V'],
            {'0': 0, '1': 1},
        )
        question_three = self._create_question(
            'Question three',
            ['E', 'F', 'G'],
            ['L', 'M', 'N'],
            {'0': 0, '1': 1, '2': 2},
        )
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_three
        self.quiz.save(update_fields=['question_order', 'current_question'])

        AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_matches={'0': 0},
            time_taken=5.0,
        )

        response = self.client.get(
            reverse('assign:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry['id'] for entry in response.context['question_scoreboard']],
            [question_one.id, question_three.id, question_two.id],
        )
        self.assertEqual(response.context['question_scoreboard'][1]['status'], 'current')
        self.assertContains(response, 'moveQuestionToNextFreeScoreSlot(question.id, question.total_possible_points);')

    def test_assign_play_ignores_stale_other_session_answers_for_out_of_order_current_question(self):
        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        question_two = self._create_question(
            'Question two',
            ['C', 'D'],
            ['U', 'V'],
            {'0': 0, '1': 1},
        )
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_one.id, question_two.id]
        self.quiz.current_question = question_two
        self.quiz.save(update_fields=['question_order', 'current_question'])

        stale_participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code='sess2',
        )
        AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=stale_participant,
            question=question_one,
            user_matches={'0': 0},
            time_taken=4.0,
        )

        response = self.client.get(
            reverse('assign:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_two.id,
                    'number': 1,
                    'earned_points': None,
                    'max_points': 2,
                    'status': 'current',
                },
                {
                    'id': question_one.id,
                    'number': 2,
                    'earned_points': None,
                    'max_points': None,
                    'status': 'upcoming',
                },
            ],
        )

    def test_assign_play_uses_question_id_primary_score_mapping_in_client(self):
        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        self.quiz.selected_questions.set([question_one])

        response = self.client.get(
            reverse('assign:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'getProgressEntryForScoreRow(question, questionNumber')
        self.assertContains(response, 'return byQuestionId.get(normalizedQuestionId) || null;')
        self.assertContains(response, 'scoreboard_questions_updated')
        self.assertContains(response, 'setProgressHistory(data.history, data.scoreboard_questions);')

    def test_assign_play_renders_cumulative_score_total_for_played_sets(self):
        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        question_two = self._create_question(
            'Question two',
            ['C', 'D', 'E'],
            ['U', 'V', 'W'],
            {'0': 0, '1': 1, '2': 2},
        )
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_one.id, question_two.id]
        self.quiz.current_question = question_two
        self.quiz.save(update_fields=['question_order', 'current_question'])

        AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_matches={'0': 0},
            time_taken=5.0,
        )

        response = self.client.get(
            reverse('assign:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['score_total_earned'], 1)
        self.assertEqual(response.context['score_total_max'], 2)
        self.assertContains(response, 'id="assignScoreTotal"')
        self.assertContains(response, '1/2')

    def test_assign_play_uses_shared_score_box_foundation_classes(self):
        """Der aktive Referenzpfad bindet die gemeinsame Score-Box-Basis sichtbar ein."""
        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        self.quiz.selected_questions.set([question_one])
        self.quiz.question_order = [question_one.id]
        self.quiz.current_question = None
        self.quiz.save(update_fields=['question_order', 'current_question'])

        response = self.client.get(
            reverse('assign:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="assign-score-box score-box"')
        self.assertContains(response, 'class="assign-score-list score-box__list"')
        self.assertContains(response, 'assign-score-row score-box__row')
        self.assertContains(response, 'assign-score-badge score-box__badge')
        self.assertContains(response, 'assign-score-value score-box__value')
        self.assertContains(response, 'assign-score-empty score-box__empty')
        self.assertContains(response, 'assign-score-total')

    def test_question_bank_add_extends_live_scoreboard_schema(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        question_one = self._create_question(
            'Question one',
            ['A', 'B'],
            ['X', 'Y'],
            {'0': 0, '1': 1},
        )
        question_two = self._create_question(
            'Question two',
            ['C', 'D', 'E'],
            ['U', 'V', 'W'],
            {'0': 0, '1': 1, '2': 2},
        )
        self.quiz.selected_questions.set([question_one])
        self.quiz.question_order = [question_one.id]
        self.quiz.save(update_fields=['question_order'])
        AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_matches={'0': 0},
            time_taken=5.0,
        )
        channel_layer = DummyChannelLayer()

        with patch('admin_dashboard.views.get_channel_layer', return_value=channel_layer):
            response = self.client.post(
                reverse('admin_dashboard:add_question_from_bank'),
                data={
                    'game_key': 'assign',
                    'room_code': self.quiz.room_code,
                    'question_id': question_two.id,
                    'hub_session': self.participant.hub_session_code,
                },
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.question_order, [question_one.id, question_two.id])
        self.assertEqual(
            [entry['id'] for entry in payload['scoreboard_questions']],
            [question_one.id, question_two.id],
        )
        self.assertEqual(payload['scoreboard_questions'][1]['status'], 'upcoming')
        self.assertEqual(channel_layer.sent[-1][0], f'assign_{self.quiz.room_code}')
        self.assertEqual(channel_layer.sent[-1][1]['type'], 'scoreboard_questions_updated')
        self.assertEqual(
            [entry['id'] for entry in channel_layer.sent[-1][1]['scoreboard_questions']],
            [question_one.id, question_two.id],
        )


class AssignTouchDragTemplateTests(TestCase):
    def test_vhs_elimination_copy_and_reason_styles_are_scoped(self):
        project_root = Path(__file__).resolve().parent.parent
        template_source = (project_root / 'templates' / 'assign' / 'play.html').read_text(encoding='utf-8')
        vhs_css = (project_root / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')

        self.assertIn("'Die vorherige Zuweisung war falsch.'", template_source)
        self.assertIn("'Die Zeit ist abgelaufen.'", template_source)
        self.assertNotIn('Du bist leider ausgeschieden.', template_source)
        self.assertIn("data.elimination_reason || null", template_source)
        self.assertIn("showEliminatedState(this.lastEliminationReason)", template_source)
        self.assertIn('#eliminatedState .assign-elimination-message--vhs', vhs_css)
        self.assertIn('color: #b96f65;', vhs_css)
        self.assertIn('color: var(--vhs-muted, #9ba19c);', vhs_css)

    def test_player_template_uses_standard_round_events_and_clamped_single_timer(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'assign' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn("case 'assign_state':", template_source)
        self.assertIn('this.applyAuthoritativeState(data);', template_source)
        self.assertNotIn('roundIsActive', template_source)
        self.assertNotIn('allowRoundInteraction', template_source)
        self.assertIn("case 'question_started':", template_source)
        self.assertIn("case 'round_advanced':", template_source)
        self.assertIn('clearInterval(this.questionTimer);', template_source)
        self.assertIn("const endMs = Date.parse(timing?.ends_at || '');", template_source)
        self.assertIn(
            'Math.ceil((endMs - (Date.now() + clockOffset)) / 1000)',
            template_source,
        )
        self.assertIn('div.draggable = true;', template_source)
        self.assertIn("dropZone.addEventListener('drop'", template_source)

    def test_player_template_rejects_stale_same_set_reactivation(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'assign' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn('acceptSetEvent(setNumber', template_source)
        self.assertIn('normalizedSetNumber < this.currentSetNumber', template_source)
        self.assertIn('normalizedSetNumber === this.currentSetNumber', template_source)
        self.assertIn('if (!this.acceptSetEvent(question?.set_number)) return;', template_source)

    def test_player_template_supports_pointer_drag_for_touch_devices(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'assign' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn('touch-action: none;', template_source)
        self.assertIn("item.addEventListener('pointerdown'", template_source)
        self.assertIn("window.addEventListener('pointermove'", template_source)
        self.assertIn("window.addEventListener('pointerup'", template_source)
        self.assertIn("window.addEventListener('pointercancel'", template_source)
        self.assertIn('requestAnimationFrame(() => this.renderAssignPointerDrag())', template_source)
        self.assertIn('translate3d(${deltaX}px, ${deltaY}px, 0)', template_source)
        self.assertIn('releasePointerCapture(drag.pointerId)', template_source)
        self.assertIn('transition: none;', template_source)
        self.assertIn('this.handleRoundDrop(drag.leftIndex, rightIndex, dropZone, draggedText);', template_source)
        self.assertIn("if (event.pointerType === 'mouse') return;", template_source)


class AssignReloadBrowserLiveFlowTests(_BrowserLiveServerTestCase):
    """Real-browser diagnosis for rejoining after the final Assign round."""

    TIMEOUT = 15_000
    LONG_TIMEOUT = 35_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls._pw, cls._browser = start_chromium_browser(headless=True)
            cls._playwright_available = True
        except Exception as exc:
            cls._playwright_available = False
            cls._playwright_error = exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, '_playwright_available', False):
            cls._browser.close()
            cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._playwright_available:
            self.skipTest(f'Playwright/Chromium nicht verfuegbar: {self._playwright_error}')

        self.password = 'testpass123'
        self.user = User.objects.create_superuser(
            username='assign-reload-browser-host',
            password=self.password,
            email='',
        )
        self.first_quiz = self._create_quiz('Assign Reload 1', '8901', status='active')
        self.completed_round_question = self._create_question(
            self.first_quiz,
            'Single round for reload diagnosis',
            ['Alpha'],
            ['One'],
            {'0': 0},
        )
        self.followup_question = self._create_question(
            self.first_quiz,
            'Three rounds remain usable',
            ['Beta', 'Gamma', 'Theta'],
            ['Two', 'Three', 'Five'],
            {'0': 0, '1': 1, '2': 2},
        )
        self.first_quiz.question_order = [
            self.completed_round_question.id,
            self.followup_question.id,
        ]
        self.first_quiz.save(update_fields=['question_order', 'updated_at'])

        self.second_quiz = self._create_quiz('Assign Reload 2', '8902', status='waiting')
        second_question = self._create_question(
            self.second_quiz,
            'Second game question',
            ['Delta'],
            ['Four'],
            {'0': 0},
        )
        self.second_quiz.question_order = [second_question.id]
        self.second_quiz.save(update_fields=['question_order', 'updated_at'])

        self.session = HubSession.objects.create(
            code='ASGRELOAD',
            name='Assign Reload Browser Session',
            is_active=True,
            started_at=timezone.now(),
            current_step_index=0,
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        self.first_quiz.started_at = self.session.started_at
        self.first_quiz.save(update_fields=['started_at', 'updated_at'])
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='assign',
            room_code=self.first_quiz.room_code,
            title=self.first_quiz.title,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='assign',
            room_code=self.second_quiz.room_code,
            title=self.second_quiz.title,
        )
        hub_participant = HubParticipant.objects.create(
            session=self.session,
            nickname='Alice',
            checked_in_at=timezone.now(),
            scoring_eligible=True,
        )
        self.participant = AssignParticipant.objects.create(
            quiz=self.first_quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        second_hub_participant = HubParticipant.objects.create(
            session=self.session,
            nickname='Bob',
            checked_in_at=timezone.now(),
            scoring_eligible=True,
        )
        self.second_participant = AssignParticipant.objects.create(
            quiz=self.first_quiz,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )

        self.host_context = self._browser.new_context()
        self.player_context = self._browser.new_context()
        self.second_player_context = self._browser.new_context()
        self.spectator_context = self._browser.new_context()
        install_browser_test_stubs(self.host_context)
        install_browser_test_stubs(self.player_context)
        install_browser_test_stubs(self.second_player_context)
        install_browser_test_stubs(self.spectator_context)
        rejoin_credential = json.dumps({
            'nickname': hub_participant.nickname,
            'token': issue_rejoin_token(hub_participant),
        })
        self.player_context.add_init_script(
            f"localStorage.setItem('hub_session_code', {json.dumps(self.session.code)});"
            f"localStorage.setItem('hub_lobby_rejoin:{self.session.code}', "
            f"{json.dumps(rejoin_credential)});"
            "localStorage.setItem('participant_interface_theme', 'vhs');"
        )
        second_rejoin_credential = json.dumps({
            'nickname': second_hub_participant.nickname,
            'token': issue_rejoin_token(second_hub_participant),
        })
        self.second_player_context.add_init_script(
            f"localStorage.setItem('hub_session_code', {json.dumps(self.session.code)});"
            f"localStorage.setItem('hub_lobby_rejoin:{self.session.code}', "
            f"{json.dumps(second_rejoin_credential)});"
            "localStorage.setItem('participant_interface_theme', 'vhs');"
        )
        self.host_page = self.host_context.new_page()
        self.player_page = self.player_context.new_page()
        self.second_player_page = self.second_player_context.new_page()
        self.spectator_page = self.spectator_context.new_page()
        self.browser_errors = []
        self.websocket_urls = {'host': [], 'player': [], 'player_two': [], 'spectator': []}
        self.websocket_frames = {
            'host': {'sent': [], 'received': []},
            'player': {'sent': [], 'received': []},
            'player_two': {'sent': [], 'received': []},
            'spectator': {'sent': [], 'received': []},
        }
        self._instrument_page('host', self.host_page)
        self._instrument_page('player', self.player_page)
        self._instrument_page('player_two', self.second_player_page)
        self._instrument_page('spectator', self.spectator_page)
        self._admin_login()

    def tearDown(self):
        for page in (
            getattr(self, 'host_page', None),
            getattr(self, 'player_page', None),
            getattr(self, 'second_player_page', None),
            getattr(self, 'spectator_page', None),
        ):
            if page:
                page.close()
        for context in (
            getattr(self, 'host_context', None),
            getattr(self, 'player_context', None),
            getattr(self, 'second_player_context', None),
            getattr(self, 'spectator_context', None),
        ):
            if context:
                context.close()

    def _create_quiz(self, title, room_code, status):
        return AssignQuiz.objects.create(
            title=title,
            creator=self.user,
            room_code=room_code,
            status=status,
            started_at=timezone.now() if status == 'active' else None,
        )

    def _create_question(self, quiz, text, left_items, right_items, correct_matches):
        question = AssignQuestion.objects.create(
            question_text=text,
            points=10,
            time_limit=30,
            left_items=left_items,
            right_items=right_items,
            correct_matches=correct_matches,
            created_by=self.user,
        )
        quiz.selected_questions.add(question)
        return question

    def _instrument_page(self, label, page):
        page.on('console', lambda msg: self._record_console(label, msg))
        page.on('pageerror', lambda exc: self.browser_errors.append(f'{label} pageerror: {exc}'))
        page.on('websocket', lambda ws: self._record_websocket(label, ws))

    def _record_console(self, label, msg):
        if msg.type == 'error':
            self.browser_errors.append(f'{label} console error: {msg.text}')

    def _record_websocket(self, label, websocket):
        self.websocket_urls[label].append(websocket.url)
        websocket.on('framesent', lambda payload: self.websocket_frames[label]['sent'].append(str(payload)))
        websocket.on(
            'framereceived',
            lambda payload: self.websocket_frames[label]['received'].append(str(payload)),
        )

    def _admin_login(self):
        self.host_page.goto(f'{self.live_server_url}{reverse("admin_dashboard:login")}')
        self.host_page.fill("input[name='username']", self.user.username)
        self.host_page.fill("input[name='password']", self.password)
        self.host_page.click("button[type='submit']")
        self.host_page.wait_for_url(f'**{reverse("admin_dashboard:home")}**', timeout=self.TIMEOUT)

    def _wait_for_ws_url(self, label, path):
        deadline = time.time() + (self.TIMEOUT / 1000)
        while time.time() < deadline:
            if any(path in url for url in self.websocket_urls[label]):
                return
            self.player_page.wait_for_timeout(100)
        self.fail(f'{label} did not open WebSocket {path}. URLs: {self.websocket_urls[label]}')

    def _wait_for_frame(self, label, direction, needle, start_index=0):
        deadline = time.time() + (self.LONG_TIMEOUT / 1000)
        while time.time() < deadline:
            frames = self.websocket_frames[label][direction][start_index:]
            if any(needle in frame for frame in frames):
                return
            self.player_page.wait_for_timeout(100)
        self.fail(
            f'{label} did not receive {needle!r} in {direction} frames. '
            f'Frames: {self.websocket_frames[label][direction][start_index:]}'
        )

    def _received_types_since(self, label, start_index):
        event_types = []
        for raw_frame in self.websocket_frames[label]['received'][start_index:]:
            try:
                payload = json.loads(raw_frame)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get('type'):
                event_types.append(payload['type'])
        return event_types

    def _play_url(self, quiz, participant_name='Alice'):
        return (
            f'{self.live_server_url}'
            f'{reverse("assign:play", args=[quiz.room_code, participant_name])}'
            f'?hub_session={self.session.code}'
        )

    def _monitor_url(self, quiz):
        return (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:assign_monitor", args=[quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )

    def _correct_drop_target(self, question, left_index):
        quiz = question.quizzes.first()
        randomized = question.get_randomized_items(room_code=quiz.room_code)
        correct_original = int(question.correct_matches[str(left_index)])
        for shuffled_position, original_position in randomized['position_to_original'].items():
            if int(original_position) == correct_original:
                return int(shuffled_position)
        self.fail(f'No shuffled target for question={question.id}, left={left_index}')

    def _drag_and_log(self, question, left_index=0, page=None):
        page = page or self.player_page
        target_index = self._correct_drop_target(question, left_index)
        source = page.locator(f'.draggable-item[data-left-index="{left_index}"]')
        target = page.locator(f'.drop-zone[data-right-index="{target_index}"]')
        source.drag_to(target)
        page.wait_for_selector('#logRoundBtn:not(.d-none)', timeout=self.TIMEOUT)
        page.click('#logRoundBtn')

    def _assert_vhs_workspace_layout(self):
        self.assertEqual(
            self.player_page.locator('html').get_attribute('data-participant-theme'),
            'vhs',
        )
        self.player_page.locator('#questionState').evaluate(
            """(element) => Promise.allSettled(
                element.getAnimations({ subtree: true }).map((animation) => animation.finished)
            )"""
        )
        source_panel = self.player_page.locator('.assign-source-panel').bounding_box()
        target_panel = self.player_page.locator('.assign-target-panel').bounding_box()
        self.assertLess(source_panel['x'], target_panel['x'])
        self.assertAlmostEqual(source_panel['y'], target_panel['y'], delta=0.5)
        self.assertAlmostEqual(source_panel['width'], target_panel['width'], delta=0.5)
        self.assertEqual(
            self.player_page.locator('#leftItemsList').evaluate(
                'element => getComputedStyle(element).display'
            ),
            'grid',
        )
        self.assertEqual(
            self.player_page.locator('#zonesList').evaluate(
                'element => getComputedStyle(element).display'
            ),
            'grid',
        )

    def _assert_no_browser_errors(self):
        relevant_errors = [
            error for error in self.browser_errors
            if (
                'favicon.ico' not in error
                and 'ERR_NETWORK_ACCESS_DENIED' not in error
                and '404' not in error
                and 'Join failed, falling back to play URL' not in error
            )
        ]
        self.assertEqual(relevant_errors, [])

    def test_reload_after_completed_round_does_not_reactivate_it(self):
        self.host_page.goto(self._monitor_url(self.first_quiz))
        self.host_page.wait_for_function('() => !!window.adminGameMonitor', timeout=self.TIMEOUT)
        self.player_page.goto(self._play_url(self.first_quiz))
        self.second_player_page.goto(self._play_url(self.first_quiz, 'Bob'))
        self.spectator_page.goto(
            f'{self.live_server_url}'
            f'{reverse("games_hub:spectate_session", args=[self.session.code])}'
        )
        self.player_page.wait_for_selector('#questionState', state='attached', timeout=self.TIMEOUT)
        self.second_player_page.wait_for_selector('#questionState', state='attached', timeout=self.TIMEOUT)
        self.spectator_page.wait_for_selector(
            '[data-spectator-game="assign"]',
            timeout=self.LONG_TIMEOUT,
        )
        self._wait_for_ws_url('host', f'/ws/assign/{self.first_quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/assign/{self.first_quiz.room_code}/')
        self._wait_for_ws_url('player_two', f'/ws/assign/{self.first_quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/hub/{self.session.code}/')

        send_selector = (
            f'.send-question-btn[data-question-id="{self.completed_round_question.id}"]'
        )
        self.host_page.click(send_selector)
        self.host_page.click(send_selector)
        self._wait_for_frame('player', 'received', 'question_started')
        send_actions = []
        for raw_frame in self.websocket_frames['host']['sent']:
            try:
                payload = json.loads(raw_frame)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get('type') == 'admin_send_question':
                send_actions.append(payload)
        self.assertEqual(len(send_actions), 1)
        self.assertTrue(send_actions[0].get('client_action_id'))
        self.player_page.wait_for_selector('#questionState:not(.d-none)', timeout=self.TIMEOUT)
        self.player_page.wait_for_selector('.draggable-item[draggable="true"]', timeout=self.TIMEOUT)
        self.second_player_page.wait_for_selector(
            '.draggable-item[draggable="true"]',
            timeout=self.TIMEOUT,
        )
        self._assert_vhs_workspace_layout()
        active_timer = int(self.player_page.locator('#playerTimeLeft').inner_text())
        self.assertGreater(active_timer, 0)

        self._drag_and_log(self.completed_round_question)
        self._drag_and_log(self.completed_round_question, page=self.second_player_page)
        self._wait_for_frame('player', 'received', 'round_logged')
        self.player_page.wait_for_timeout(500)
        self.assertNotIn(
            'question_rounds_complete',
            self._received_types_since('player', 0),
        )
        self.host_page.click('#endQuestionBtn')
        self._wait_for_frame('player', 'received', 'question_rounds_complete')
        self.first_quiz.session.refresh_from_db()
        self.assertFalse(self.first_quiz.session.is_question_active)
        completed_runtime = AssignSetRuntime.objects.get(
            quiz=self.first_quiz,
            question=self.completed_round_question,
            set_number=1,
        )
        locked_item = self.player_page.wait_for_selector(
            '.draggable-item[draggable="false"]',
            state='attached',
            timeout=self.TIMEOUT,
        )
        self.assertEqual(locked_item.get_attribute('draggable'), 'false')
        stopped_timer_before = int(self.player_page.locator('#playerTimeLeft').inner_text())
        self.player_page.wait_for_timeout(1200)
        stopped_timer_after = int(self.player_page.locator('#playerTimeLeft').inner_text())
        self.assertEqual(stopped_timer_before, stopped_timer_after)

        reload_frame_start = len(self.websocket_frames['player']['received'])
        submit_count_before_reload = sum(
            'participant_log_round' in frame
            for frame in self.websocket_frames['player']['sent']
        )
        self.player_page.reload()
        self._wait_for_frame('player', 'received', 'connection_established', reload_frame_start)
        self.player_page.wait_for_timeout(1500)

        reload_types = self._received_types_since('player', reload_frame_start)
        self.assertIn('assign_state', reload_types)
        self.assertNotIn('quiz_started', reload_types)
        self.assertNotIn('question_started', reload_types)
        reload_snapshots = []
        for raw_frame in self.websocket_frames['player']['received'][reload_frame_start:]:
            try:
                payload = json.loads(raw_frame)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get('type') == 'assign_state':
                reload_snapshots.append(payload)
        self.assertEqual(len(reload_snapshots), 1)
        self.assertEqual(
            reload_snapshots[0]['phase'],
            AssignSetRuntime.PHASE_WAITING_REVEAL,
        )
        self.assertEqual(
            reload_snapshots[0]['current_question_id'],
            str(self.completed_round_question.id),
        )
        self.assertEqual(reload_snapshots[0]['current_round_id'], '0')
        self.assertEqual(
            reload_snapshots[0]['ends_at'],
            completed_runtime.round_ends_at.isoformat(),
        )
        self.assertTrue(reload_snapshots[0]['participant_state']['answer_locked'])
        self.assertGreaterEqual(reload_snapshots[0]['state_revision'], 1)
        question_restarted = 'question_started' in reload_types
        question_visible = self.player_page.locator('#questionState:not(.d-none)').count() == 1
        drag_reenabled = self.player_page.locator('.draggable-item[draggable="true"]').count() > 0
        timer_after_reload = int(self.player_page.locator('#playerTimeLeft').inner_text())
        self.player_page.wait_for_timeout(1200)
        timer_later = int(self.player_page.locator('#playerTimeLeft').inner_text())
        timer_restarted = timer_after_reload > 0 and timer_later < timer_after_reload
        submit_count_after_reload = sum(
            'participant_log_round' in frame
            for frame in self.websocket_frames['player']['sent']
        )
        self.assertEqual(submit_count_before_reload, submit_count_after_reload)

        end_question_start = len(self.websocket_frames['player']['received'])
        self.host_page.click('#endQuestionBtn')
        self._wait_for_frame('player', 'received', 'question_ended', end_question_start)

        followup_start = len(self.websocket_frames['player']['received'])
        send_selector = f'.send-question-btn[data-question-id="{self.followup_question.id}"]'
        self.host_page.click(send_selector)
        self.host_page.click(send_selector)
        self._wait_for_frame('player', 'received', 'question_started', followup_start)
        self.first_quiz.session.refresh_from_db()
        self.assertTrue(self.first_quiz.session.is_question_active)
        self.player_page.wait_for_selector('.draggable-item[draggable="true"]', timeout=self.TIMEOUT)
        self.second_player_page.wait_for_selector(
            '.draggable-item[draggable="true"]',
            timeout=self.TIMEOUT,
        )
        self._assert_vhs_workspace_layout()
        self.assertGreater(int(self.player_page.locator('#playerTimeLeft').inner_text()), 0)

        next_round_start = len(self.websocket_frames['player']['received'])
        self._drag_and_log(self.followup_question, left_index=0)
        self._drag_and_log(
            self.followup_question,
            left_index=0,
            page=self.second_player_page,
        )
        self._wait_for_frame('player', 'received', 'round_logged', next_round_start)
        self.player_page.wait_for_timeout(500)
        self.assertNotIn(
            'round_advanced',
            self._received_types_since('player', next_round_start),
        )
        self.host_page.click('#nextRoundBtn')
        self._wait_for_frame('player', 'received', 'round_advanced', next_round_start)
        self.first_quiz.session.refresh_from_db()
        self.assertTrue(self.first_quiz.session.is_question_active)
        self.player_page.wait_for_selector(
            '.draggable-item[data-left-index="1"][draggable="true"]',
            timeout=self.TIMEOUT,
        )
        self.second_player_page.wait_for_selector(
            '.draggable-item[data-left-index="1"][draggable="true"]',
            timeout=self.TIMEOUT,
        )
        self.assertEqual(self.player_page.locator('.drop-zone').count(), 3)
        self.assertEqual(self.player_page.locator('.drop-zone.completed-round').count(), 1)
        self.assertEqual(self.player_page.locator('.draggable-item').count(), 2)
        self.assertGreater(int(self.player_page.locator('#playerTimeLeft').inner_text()), 0)

        third_round_start = len(self.websocket_frames['player']['received'])
        self._drag_and_log(self.followup_question, left_index=1)
        self._drag_and_log(
            self.followup_question,
            left_index=1,
            page=self.second_player_page,
        )
        self._wait_for_frame('player', 'received', 'round_logged', third_round_start)
        self.player_page.wait_for_timeout(500)
        self.assertNotIn(
            'round_advanced',
            self._received_types_since('player', third_round_start),
        )
        self.host_page.click('#nextRoundBtn')
        self._wait_for_frame('player', 'received', 'round_advanced', third_round_start)
        self.player_page.wait_for_selector(
            '.draggable-item[data-left-index="2"][draggable="true"]',
            timeout=self.TIMEOUT,
        )
        self.second_player_page.wait_for_selector(
            '.draggable-item[data-left-index="2"][draggable="true"]',
            timeout=self.TIMEOUT,
        )
        self.assertEqual(self.player_page.locator('.drop-zone').count(), 3)
        self.assertEqual(self.player_page.locator('.drop-zone.completed-round').count(), 2)
        self.assertEqual(self.player_page.locator('.draggable-item').count(), 1)

        completed_start = len(self.websocket_frames['player']['received'])
        self._drag_and_log(self.followup_question, left_index=2)
        self._drag_and_log(
            self.followup_question,
            left_index=2,
            page=self.second_player_page,
        )
        self._wait_for_frame('player', 'received', 'round_logged', completed_start)
        self.player_page.wait_for_timeout(500)
        self.assertNotIn(
            'question_rounds_complete',
            self._received_types_since('player', completed_start),
        )
        self.host_page.click('#endQuestionBtn')
        self._wait_for_frame(
            'player', 'received', 'question_rounds_complete', completed_start
        )

        self.host_page.once('dialog', lambda dialog: dialog.accept())
        self.host_page.click('#endQuizBtn')
        self._wait_for_frame('player', 'received', 'quiz_ended')
        self.first_quiz.session.refresh_from_db()
        self.assertFalse(self.first_quiz.session.is_question_active)
        self.player_page.wait_for_selector('#quizEndedState:not(.d-none)', timeout=self.TIMEOUT)

        self.host_page.goto(f'{self.live_server_url}{reverse("games_hub:monitor", args=[self.session.code])}')
        self.host_page.click('[data-session-panel-target="checkInPanel"]')
        self.host_page.wait_for_selector('#recallLobbyBtn', timeout=self.TIMEOUT)
        recall_frame_start = len(self.websocket_frames['player']['received'])
        self.host_page.click('#recallLobbyBtn')
        self.host_page.wait_for_function(
            "() => document.querySelector('#lobbyReturnGuardNames')?.textContent.includes('Alice')"
            " && document.querySelector('#lobbyReturnGuardNames')?.textContent.includes('Bob')",
            timeout=self.TIMEOUT,
        )
        recall_primary = self.host_page.locator('#lobbyReturnGuardPrimaryBtn')
        recall_primary.dispatch_event('click')
        self._wait_for_frame(
            'player', 'received', 'lobby_return_countdown_started', recall_frame_start
        )
        recall_primary.dispatch_event('click')
        self._wait_for_frame('player', 'received', 'players_recalled_to_lobby', recall_frame_start)
        self.player_page.wait_for_url(f'**/hub/lobby/{self.session.code}/**', timeout=self.LONG_TIMEOUT)
        self.second_player_page.wait_for_url(
            f'**/hub/lobby/{self.session.code}/**',
            timeout=self.LONG_TIMEOUT,
        )

        self.host_page.goto(self._monitor_url(self.second_quiz))
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#startQuizBtn')
        self.player_page.wait_for_url(
            f'**/assign/play/{self.second_quiz.room_code}/Alice/**',
            timeout=self.LONG_TIMEOUT,
        )
        self.second_player_page.wait_for_url(
            f'**/assign/play/{self.second_quiz.room_code}/Bob/**',
            timeout=self.LONG_TIMEOUT,
        )
        self.spectator_page.wait_for_selector(
            '[data-spectator-game="assign"]',
            timeout=self.LONG_TIMEOUT,
        )
        self.player_page.reload()
        self.player_page.wait_for_url(
            f'**/assign/play/{self.second_quiz.room_code}/Alice/**',
            timeout=self.TIMEOUT,
        )
        self._assert_no_browser_errors()

        diagnostics = {
            'reload_event_types': reload_types,
            'question_visible': question_visible,
            'timer_after_reload': timer_after_reload,
            'timer_later': timer_later,
            'drag_reenabled': drag_reenabled,
        }
        self.assertTrue(question_visible)
        self.assertFalse(
            question_restarted or timer_restarted or drag_reenabled,
            f'Completed Assign round was reactivated after reload: {diagnostics}',
        )
