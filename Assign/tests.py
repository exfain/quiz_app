import json
import os
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from asgiref.sync import async_to_sync
from .models import AssignQuiz, AssignQuestion, AssignParticipant, AssignAnswer, AssignSession
from .consumers import AssignConsumer
from games_hub.models import HubGameStep, HubParticipant, HubSession
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


class CheckRoundAnswerTest(TransactionTestCase):
    def setUp(self):
        AssignConsumer._round_submissions = {}
        AssignConsumer._round_logged = {}
        AssignConsumer._round_selections = {}
        AssignConsumer._auto_advancing = set()
        AssignConsumer._participant_channels = {}
        AssignConsumer._channel_participants = {}
        AssignConsumer._channel_hub_sessions = {}
        AssignConsumer._effective_time_limits = {}
        AssignConsumer._eliminated_participants = {}
        AssignConsumer._elimination_reasons = {}
        AssignConsumer._room_matched_originals = {}
        AssignConsumer._room_solved_matches = {}

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
        AssignConsumer._participant_channels[self.quiz.room_code] = {channel_name}
        AssignConsumer._channel_participants[channel_name] = self.participant.name
        AssignConsumer._channel_hub_sessions[channel_name] = self.participant.hub_session_code
        return channel_name

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
        self.quiz.current_question = self.question
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['current_question', 'status'])
        AssignSession.objects.create(quiz=self.quiz, is_question_active=True)

        payloads = self._capture_consumer_send()

        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
        })

        self.assertTrue(any(payload['type'] == 'question_started' for payload in payloads))
        self.assertFalse(any(payload['type'] == 'assign_state' for payload in payloads))

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

        self.assertTrue(any(payload['type'] == 'quiz_started' for payload in payloads))
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
            'Alice', 'sess1', shuffled_matches, 12.0
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
        self.quiz.current_question = self.question
        self.quiz.save()

        shuffled_pos = self._get_shuffled_pos_for_original(2)
        AssignConsumer._participant_channels[self.quiz.room_code] = {'channel-a'}
        AssignConsumer._channel_participants['channel-a'] = self.participant.name
        AssignConsumer._channel_hub_sessions['channel-a'] = self.participant.hub_session_code
        AssignConsumer._round_selections[(self.quiz.room_code, 0)] = {
            'channel-a': {
                'left_item_index': 0,
                'user_match': {'0': shuffled_pos},
            }
        }
        AssignConsumer._round_logged[(self.quiz.room_code, 0)] = {'channel-a'}

        async_to_sync(self.consumer.evaluate_current_round)(self.quiz, 0)

        self.assertEqual(AssignConsumer._room_solved_matches[self.quiz.room_code], {0: 2})
        self.assertEqual(AssignConsumer._room_matched_originals[self.quiz.room_code], {2})
        self.assertNotIn((self.quiz.room_code, 0), AssignConsumer._round_selections)
        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertFalse(round_checked['eliminated'])
        self.assertIsNone(round_checked['elimination_reason'])

    def test_wrong_assignment_emits_authoritative_elimination_reason(self):
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        channel_name = self._register_active_participant_channel()
        wrong_target = self._get_shuffled_pos_for_original(0)
        AssignConsumer._round_selections[(self.quiz.room_code, 0)] = {
            channel_name: {
                'left_item_index': 0,
                'user_match': {'0': wrong_target},
            }
        }
        AssignConsumer._round_logged[(self.quiz.room_code, 0)] = {channel_name}

        async_to_sync(self.consumer.evaluate_current_round)(self.quiz, 0)

        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertEqual(round_checked['elimination_reason'], 'incorrect_assignment')
        self.assertEqual(
            AssignConsumer._elimination_reasons[self.quiz.room_code]['sess1::alice'],
            'incorrect_assignment',
        )

    def test_missing_assignment_emits_timeout_elimination_reason(self):
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        self._register_active_participant_channel()

        async_to_sync(self.consumer.evaluate_current_round)(self.quiz, 0)

        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertEqual(round_checked['elimination_reason'], 'no_assignment')
        self.assertEqual(
            AssignConsumer._elimination_reasons[self.quiz.room_code]['sess1::alice'],
            'no_assignment',
        )

    def test_rejoin_restores_authoritative_elimination_reason(self):
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        self._register_active_participant_channel()
        AssignSession.objects.create(quiz=self.quiz, is_question_active=True)
        async_to_sync(self.consumer.evaluate_current_round)(self.quiz, 0)

        AssignConsumer._eliminated_participants = {}
        AssignConsumer._elimination_reasons = {}

        self.consumer.channel_name = 'channel-rejoin'
        payloads = self._capture_consumer_send()
        async_to_sync(self.consumer.handle_participant_join)({
            'participant_name': self.participant.name,
            'hub_session': self.participant.hub_session_code,
        })

        restored = next(payload for payload in payloads if payload['type'] == 'round_checked')
        self.assertEqual(restored['elimination_reason'], 'no_assignment')
        self.assertTrue(restored['eliminated'])
        self.assertFalse(any(payload['type'] == 'question_started' for payload in payloads))

    def test_elimination_survives_cache_loss_and_later_rounds_in_same_set(self):
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        session = AssignSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            current_round_index=0,
            is_question_active=True,
        )
        channel_name = self._register_active_participant_channel()

        async_to_sync(self.consumer.evaluate_current_round)(self.quiz, 0, 1)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.eliminated_set_number, 1)
        self.assertEqual(self.participant.elimination_reason, 'no_assignment')

        AssignConsumer._eliminated_participants = {}
        AssignConsumer._elimination_reasons = {}
        session.current_round_index = 3
        session.save(update_fields=['current_round_index'])

        active_channels = async_to_sync(self.consumer.get_relevant_active_channels)()
        self.assertNotIn(channel_name, active_channels)

        payloads = self._capture_consumer_send()
        async_to_sync(self.consumer.handle_participant_update_selection)({
            'round_index': 3,
            'left_item_index': 2,
            'user_match': {'2': self._get_shuffled_pos_for_original(1)},
        })

        self.assertNotIn((self.quiz.room_code, 3), AssignConsumer._round_selections)
        self.assertEqual(payloads[-1]['type'], 'round_checked')
        self.assertEqual(payloads[-1]['set_number'], 1)
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
        self.assertEqual([payload['type'] for payload in payloads], ['round_checked'])
        self.assertEqual(payloads[0]['elimination_reason'], 'incorrect_assignment')

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
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        AssignSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            is_question_active=True,
        )
        bob = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code='sess1',
        )
        alice_channel = self._register_active_participant_channel('channel-alice')
        bob_channel = 'channel-bob'
        AssignConsumer._participant_channels[self.quiz.room_code].add(bob_channel)
        AssignConsumer._channel_participants[bob_channel] = bob.name
        AssignConsumer._channel_hub_sessions[bob_channel] = bob.hub_session_code
        self.participant.eliminated_set_number = 1
        self.participant.elimination_reason = 'incorrect_assignment'
        self.participant.save(update_fields=['eliminated_set_number', 'elimination_reason'])

        active_channels = async_to_sync(self.consumer.get_relevant_active_channels)()

        self.assertNotIn(alice_channel, active_channels)
        self.assertIn(bob_channel, active_channels)

    def test_build_round_payload_keeps_completed_pair_visible_and_open_items_filtered(self):
        """Der nächste Rundenpayload enthält gelöste Paare und nur noch offene rechte Items."""
        shuffled_pos = self._get_shuffled_pos_for_original(2)
        AssignConsumer._room_solved_matches[self.quiz.room_code] = {0: 2}

        payload = self.consumer.build_round_payload(self.question, 1, 45)

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
        self.quiz.current_question = question
        self.quiz.save()
        channel_name = self._register_active_participant_channel()

        chosen_left_index = 3
        chosen_right_pos = self._get_shuffled_pos_for_original(chosen_left_index, question=question)
        async_to_sync(self.consumer.handle_participant_log_round)({
            'round_index': 0,
            'left_item_index': chosen_left_index,
            'user_match': {'3': chosen_right_pos},
        })

        stored = AssignConsumer._round_selections[(self.quiz.room_code, 0)][channel_name]
        self.assertEqual(stored['left_item_index'], chosen_left_index)
        self.assertEqual(stored['user_match'], {'3': chosen_right_pos})

        async_to_sync(self.consumer.evaluate_current_round)(self.quiz, 0)

        self.assertEqual(AssignConsumer._room_solved_matches[self.quiz.room_code], {3: 3})
        self.assertEqual(AssignConsumer._room_matched_originals[self.quiz.room_code], {3})
        self.assertNotIn('sess1::alice', AssignConsumer._eliminated_participants.get(self.quiz.room_code, set()))

    def test_evaluation_prefers_actual_user_match_key_over_stale_left_item_index(self):
        """Paris -> Frankreich bleibt korrekt, auch wenn left_item_index noch stale/current ist."""
        question = self._create_capitals_question()
        self.quiz.current_question = question
        self.quiz.save()
        channel_name = self._register_active_participant_channel()

        paris_left_index = 2
        france_right_original_index = 1
        france_shuffled_pos = self._get_shuffled_pos_for_original(france_right_original_index, question=question)

        async_to_sync(self.consumer.handle_participant_log_round)({
            'round_index': 0,
            'left_item_index': 0,
            'user_match': {'2': france_shuffled_pos},
        })

        stored = AssignConsumer._round_selections[(self.quiz.room_code, 0)][channel_name]
        self.assertEqual(stored['left_item_index'], paris_left_index)
        self.assertEqual(stored['user_match'], {'2': france_shuffled_pos})

        async_to_sync(self.consumer.evaluate_current_round)(self.quiz, 0)

        self.assertEqual(AssignConsumer._room_solved_matches[self.quiz.room_code], {2: 1})
        self.assertEqual(AssignConsumer._room_matched_originals[self.quiz.room_code], {1})
        self.assertNotIn('sess1::alice', AssignConsumer._eliminated_participants.get(self.quiz.room_code, set()))

    def test_host_early_end_evaluates_pending_unlogged_selection(self):
        """Host-Ende wertet eine vorhandene, aber nicht eingeloggte Zuordnung als Paar aus."""
        question = self._create_four_round_question()
        self.quiz.current_question = question
        self.quiz.save()
        channel_name = self._register_active_participant_channel()

        chosen_left_index = 3
        shuffled_pos = self._get_shuffled_pos_for_original(chosen_left_index, question=question)
        async_to_sync(self.consumer.handle_participant_update_selection)({
            'round_index': 0,
            'left_item_index': chosen_left_index,
            'user_match': {'3': shuffled_pos},
        })

        previous_count = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_next_round)({'expected_round': 0})
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
        self.assertEqual(AssignConsumer._room_solved_matches[self.quiz.room_code], {3: 3})
        self.assertNotIn((self.quiz.room_code, 0), AssignConsumer._round_selections)
        self.assertNotIn(channel_name, AssignConsumer._round_logged.get((self.quiz.room_code, 0), set()))

    def test_host_early_end_without_selection_uses_no_assignment_reason(self):
        question = self._create_four_round_question()
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])
        self._register_active_participant_channel()

        async_to_sync(self.consumer.handle_admin_next_round)({'expected_round': 0})

        round_checked = next(
            message for _, message in self.consumer.channel_layer.sent
            if message.get('type') == 'round_checked'
        )
        self.assertEqual(round_checked['elimination_reason'], 'no_assignment')

    def test_four_round_sequence_advances_cleanly(self):
        """Vier aufeinanderfolgende Runden bleiben sequentiell und enden erst nach Runde 4."""
        question = self._create_four_round_question()
        self.quiz.current_question = question
        self.quiz.save()
        session = AssignSession.objects.create(quiz=self.quiz, is_question_active=True)
        channel_name = self._register_active_participant_channel()

        expected_events = ['round_advanced', 'round_advanced', 'round_advanced', 'question_rounds_complete']
        observed_events = []

        chosen_order = [3, 1, 0, 2]

        for round_index, left_item_index in enumerate(chosen_order):
            shuffled_pos = self._get_shuffled_pos_for_original(left_item_index, question=question)
            AssignConsumer._round_selections[(self.quiz.room_code, round_index)] = {
                channel_name: {
                    'left_item_index': left_item_index,
                    'user_match': {str(left_item_index): shuffled_pos},
                }
            }
            AssignConsumer._round_logged[(self.quiz.room_code, round_index)] = {channel_name}

            previous_count = len(self.consumer.channel_layer.sent)
            async_to_sync(self.consumer.handle_admin_next_round)({'expected_round': round_index})
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
        self.assertEqual(AssignConsumer._room_solved_matches[self.quiz.room_code], {0: 0, 1: 1, 2: 2, 3: 3})
        self.assertEqual(async_to_sync(self.consumer.get_current_round_index)(self.quiz.id), 0)

    def test_progress_history_uses_correct_match_count_for_max_rounds(self):
        """Fortschrittshistory verwendet die Anzahl lösbarer Paare statt left_items."""
        question = self._create_left_distractor_question()
        self.quiz.current_question = question
        self.quiz.save()

        shuffled_matches = {}
        for left_idx_str, correct_orig in question.correct_matches.items():
            shuffled_matches[left_idx_str] = self._get_shuffled_pos_for_original(correct_orig, question=question)

        result = async_to_sync(self.consumer.save_participant_answer)(
            'Alice', 'sess1', shuffled_matches, 12.0
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

        self.assertNotIn("case 'assign_state':", template_source)
        self.assertNotIn('roundIsActive', template_source)
        self.assertNotIn('allowRoundInteraction', template_source)
        self.assertIn("case 'question_started':", template_source)
        self.assertIn("case 'round_advanced':", template_source)
        self.assertIn('clearInterval(this.questionTimer);', template_source)
        self.assertIn('timeLeft = Math.max(0, timeLeft - 1);', template_source)
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
            'Two rounds remain usable',
            ['Beta', 'Gamma'],
            ['Two', 'Three'],
            {'0': 0, '1': 1},
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
            locked_participant_count=1,
        )
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
        HubParticipant.objects.create(
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

        self.host_context = self._browser.new_context()
        self.player_context = self._browser.new_context()
        install_browser_test_stubs(self.host_context)
        install_browser_test_stubs(self.player_context)
        self.player_context.add_init_script(
            f"localStorage.setItem('hub_session_code', {json.dumps(self.session.code)});"
        )
        self.host_page = self.host_context.new_page()
        self.player_page = self.player_context.new_page()
        self.browser_errors = []
        self.websocket_urls = {'host': [], 'player': []}
        self.websocket_frames = {
            'host': {'sent': [], 'received': []},
            'player': {'sent': [], 'received': []},
        }
        self._instrument_page('host', self.host_page)
        self._instrument_page('player', self.player_page)
        self._admin_login()

    def tearDown(self):
        for page in (getattr(self, 'host_page', None), getattr(self, 'player_page', None)):
            if page:
                page.close()
        for context in (getattr(self, 'host_context', None), getattr(self, 'player_context', None)):
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

    def _drag_and_log(self, question, left_index=0):
        target_index = self._correct_drop_target(question, left_index)
        source = self.player_page.locator(f'.draggable-item[data-left-index="{left_index}"]')
        target = self.player_page.locator(f'.drop-zone[data-right-index="{target_index}"]')
        source.drag_to(target)
        self.player_page.wait_for_selector('#logRoundBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.player_page.click('#logRoundBtn')

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
        self.player_page.wait_for_selector('#questionState', state='attached', timeout=self.TIMEOUT)
        self._wait_for_ws_url('host', f'/ws/assign/{self.first_quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/assign/{self.first_quiz.room_code}/')
        self._wait_for_ws_url('player', f'/ws/hub/{self.session.code}/')

        self.host_page.click(
            f'.send-question-btn[data-question-id="{self.completed_round_question.id}"]'
        )
        self._wait_for_frame('player', 'received', 'question_started')
        self.player_page.wait_for_selector('#questionState:not(.d-none)', timeout=self.TIMEOUT)
        self.player_page.wait_for_selector('.draggable-item[draggable="true"]', timeout=self.TIMEOUT)
        active_timer = int(self.player_page.locator('#playerTimeLeft').inner_text())
        self.assertGreater(active_timer, 0)

        self._drag_and_log(self.completed_round_question)
        self._wait_for_frame('player', 'received', 'question_rounds_complete')
        self.first_quiz.session.refresh_from_db()
        self.assertFalse(self.first_quiz.session.is_question_active)
        self.player_page.wait_for_selector('.draggable-item[draggable="false"]', timeout=self.TIMEOUT)
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
        self.assertIn('quiz_started', reload_types)
        self.assertNotIn('question_started', reload_types)
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
        self.host_page.click(f'.send-question-btn[data-question-id="{self.followup_question.id}"]')
        self._wait_for_frame('player', 'received', 'question_started', followup_start)
        self.first_quiz.session.refresh_from_db()
        self.assertTrue(self.first_quiz.session.is_question_active)
        self.player_page.wait_for_selector('.draggable-item[draggable="true"]', timeout=self.TIMEOUT)
        self.assertGreater(int(self.player_page.locator('#playerTimeLeft').inner_text()), 0)

        next_round_start = len(self.websocket_frames['player']['received'])
        self._drag_and_log(self.followup_question, left_index=0)
        self._wait_for_frame('player', 'received', 'round_advanced', next_round_start)
        self.first_quiz.session.refresh_from_db()
        self.assertTrue(self.first_quiz.session.is_question_active)
        self.player_page.wait_for_selector(
            '.draggable-item[data-left-index="1"][draggable="true"]',
            timeout=self.TIMEOUT,
        )
        self.assertGreater(int(self.player_page.locator('#playerTimeLeft').inner_text()), 0)

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
            "() => document.querySelector('#lobbyReturnGuardNames')?.textContent.includes('Alice')",
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

        self.host_page.goto(self._monitor_url(self.second_quiz))
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#startQuizBtn')
        self.player_page.wait_for_url(
            f'**/assign/play/{self.second_quiz.room_code}/Alice/**',
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
        self.assertFalse(
            question_restarted or question_visible or timer_restarted or drag_reenabled,
            f'Completed Assign round was reactivated after reload: {diagnostics}',
        )
