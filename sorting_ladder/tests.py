import json
import re
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.authoritative_state import (
    current_snapshot,
    get_question_flow_capabilities,
    reset_question_flow,
)
from games_hub.models import GameRuntimeState
from games_hub.tutorial_runtime import activate_tutorial_runtime as activate_game_tutorial_runtime
from games_hub.unit_tutorial_runtime import (
    get_unit_tutorial_state,
    prepare_unit_tutorial_runtime,
    start_unit_tutorial_if_needed,
)
from games_hub.views import get_leaderboard_data, get_post_game_results

from .consumers import SortingLadderGameConsumer
from .models import (
    RoundSubmission,
    SortingItem,
    SortingLadderGame,
    SortingLadderParticipant,
    SortingPendingRoundSelection,
    SortingLadderSession,
    SortingQuestion,
)
from .runtime import (
    SORTING_LADDER_REVEAL_ANIMATION_MS,
    SORTING_LADDER_REVEAL_STAGGER_MS,
    sorting_ladder_reveal_counts,
    sorting_ladder_reveal_ready_at,
    sorting_ladder_reveal_step_count,
)


class DummyChannelLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group_name, message):
        self.sent.append((group_name, message))


class SortingLadderPendingSelectionTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sorting-user', password='pass')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Test',
            creator=self.user,
            room_code='6789',
            status='active',
        )
        self.participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess1',
            is_active=True,
        )
        self.consumer = SortingLadderGameConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'sortingladder_{self.quiz.room_code}'
        self.consumer.channel_layer = DummyChannelLayer()
        self.consumer.channel_name = 'sorting-test-channel'

        async def _noop_send(*args, **kwargs):
            return None

        self.consumer.send = _noop_send

    def _create_question_with_items(self, texts_and_ranks, starting_index):
        question = SortingQuestion.objects.create(
            question_text='Sort these items',
            description='Place the next item correctly.',
            upper_label='Highest',
            lower_label='Lowest',
            points=10,
            round_time_limit=30,
            created_by=self.user,
        )
        items = []
        for text, rank in texts_and_ranks:
            items.append(
                SortingItem.objects.create(
                    topic=question,
                    text=text,
                    correct_rank=rank,
                )
            )
        question.starting_item = items[starting_index]
        question.save(update_fields=['starting_item'])
        return question, items

    def _set_live_round(self, question, ordered_items, current_round=1):
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])
        session, _ = SortingLadderSession.objects.get_or_create(quiz=self.quiz)
        session.shuffled_item_ids = ",".join(str(item.id) for item in ordered_items)
        session.current_round = current_round
        session.is_round_active = True
        session.time_limit_seconds = 30
        session.round_start_time = timezone.now()
        session.round_end_time = timezone.now() + timezone.timedelta(seconds=30)
        session.save()
        return session

    def _update_pending_selection(self, ordered_item_ids):
        async_to_sync(self.consumer.handle_participant_update_selection)({
            'participant_name': self.participant.name,
            'hub_session_code': self.participant.hub_session_code,
            'ordered_item_ids': ordered_item_ids,
        })

    def test_host_early_next_round_evaluates_pending_selection_and_advances(self):
        """Manuelles Host-Weiterklicken wertet eine vorhandene, nicht eingeloggte Auswahl aus."""
        question, items = self._create_question_with_items(
            [('Small', 1), ('Medium', 2), ('Large', 3)],
            starting_index=1,
        )
        medium = items[1]
        small = items[0]
        self._set_live_round(question, [medium, small, items[2]], current_round=1)

        self._update_pending_selection([small.id, medium.id])

        status = async_to_sync(self.consumer.get_round_answer_status_db)(self.quiz.id)
        self.assertEqual(status['statuses'], [{
            'participant_name': self.participant.name,
            'has_answered': True,
            'has_selection': True,
            'is_logged': False,
            'answer_status': 'gegeben',
        }])

        previous_count = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_start_round)({})
        new_messages = [message for _, message in self.consumer.channel_layer.sent[previous_count:]]

        round_result = next(
            (message for message in new_messages if message.get('type') == 'round_result'),
            None,
        )
        round_started = next(
            (message for message in new_messages if message.get('type') == 'round_started'),
            None,
        )

        self.assertIsNotNone(round_result)
        self.assertEqual(round_result['participant_name'], self.participant.name)
        self.assertEqual(round_result['question_id'], question.id)
        self.assertEqual(round_result['round_number'], 1)
        self.assertTrue(round_result['is_correct'])
        self.assertFalse(round_result['is_eliminated'])
        self.assertTrue(round_result['has_more_rounds'])

        self.assertIsNotNone(round_started)
        self.assertEqual(round_started['round']['question_id'], question.id)
        self.assertEqual(round_started['round']['round_number'], 2)
        self.assertFalse(any(
            message.get('type') in {
                'question_rounds_complete',
                'show_solution',
                'question_ended',
            }
            for message in new_messages
        ))

        submission = RoundSubmission.objects.get(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
        )
        self.assertEqual(submission.all_elements, [small.id, medium.id])
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.rounds_survived, 1)
        self.assertFalse(self.participant.is_eliminated)

    def test_round_answer_status_marks_logged_submission_as_eingeloggt(self):
        question, items = self._create_question_with_items(
            [('Small', 1), ('Medium', 2), ('Large', 3)],
            starting_index=1,
        )
        self._set_live_round(question, [items[1], items[0], items[2]], current_round=1)
        RoundSubmission.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            round_number=1,
            all_elements=[items[0].id, items[1].id],
        )

        status = async_to_sync(self.consumer.get_round_answer_status_db)(self.quiz.id)

        self.assertEqual(status['statuses'], [{
            'participant_name': self.participant.name,
            'has_answered': True,
            'has_selection': False,
            'is_logged': True,
            'answer_status': 'eingeloggt',
        }])

    def test_final_round_completion_waits_for_idempotent_host_reveal(self):
        """Frühes Fragenende in der letzten Runde wertet die vorhandene Auswahl vor dem Reveal aus."""
        question, items = self._create_question_with_items(
            [('Earlier', 1), ('Later', 2)],
            starting_index=1,
        )
        later = items[1]
        earlier = items[0]
        self._set_live_round(question, [later, earlier], current_round=1)

        self._update_pending_selection([earlier.id, later.id])

        previous_count = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_end_question)({})
        new_messages = [message for _, message in self.consumer.channel_layer.sent[previous_count:]]

        round_result = next(
            (message for message in new_messages if message.get('type') == 'round_result'),
            None,
        )
        rounds_complete = next(
            (message for message in new_messages if message.get('type') == 'question_rounds_complete'),
            None,
        )

        self.assertIsNotNone(round_result)
        self.assertEqual(round_result['participant_name'], self.participant.name)
        self.assertEqual(round_result['round_number'], 1)
        self.assertTrue(round_result['is_correct'])
        self.assertFalse(round_result['is_eliminated'])
        self.assertFalse(round_result['has_more_rounds'])
        self.assertEqual(round_result['correct_order_ids'], [])

        self.assertIsNotNone(rounds_complete)
        self.assertNotIn('correct_order_ids', rounds_complete)
        self.assertFalse(any(
            message.get('type') in {'question_ended', 'show_solution'}
            for message in new_messages
        ))

        submission = RoundSubmission.objects.get(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
        )
        self.assertEqual(submission.all_elements, [earlier.id, later.id])
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.rounds_survived, 1)
        self.assertFalse(self.participant.is_eliminated)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, question.id)
        self.quiz.session.refresh_from_db()
        self.assertFalse(self.quiz.session.is_round_active)
        self.assertEqual(
            self.quiz.session.reveal_state,
            SortingLadderSession.REVEAL_AWAITING,
        )
        awaiting_snapshot = async_to_sync(self.consumer.get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            awaiting_snapshot['reveal_state'],
            SortingLadderSession.REVEAL_AWAITING,
        )
        self.assertEqual(awaiting_snapshot['reveal_order_ids'], [])
        self.assertEqual(
            awaiting_snapshot['latest_round_result']['correct_order_ids'],
            [],
        )

        reveal_start = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_show_solution)({})
        reveal_messages = [
            message for _, message in self.consumer.channel_layer.sent[reveal_start:]
        ]
        self.assertEqual(
            [
                message['correct_order_ids']
                for message in reveal_messages
                if message.get('type') == 'show_solution'
            ],
            [[earlier.id, later.id]],
        )

        duplicate_start = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_show_solution)({})
        self.assertEqual(self.consumer.channel_layer.sent[duplicate_start:], [])

        self.quiz.session.refresh_from_db()
        self.assertEqual(
            self.quiz.session.reveal_state,
            SortingLadderSession.REVEAL_REVEALED,
        )
        revealed_snapshot = async_to_sync(self.consumer.get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            revealed_snapshot['reveal_order_ids'],
            [earlier.id, later.id],
        )

        clear_start = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_end_question)({})
        clear_messages = [
            message for _, message in self.consumer.channel_layer.sent[clear_start:]
        ]
        self.assertEqual(
            len([
                message
                for message in clear_messages
                if message.get('type') == 'question_ended'
            ]),
            1,
        )
        self.quiz.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)

    def test_set_cannot_complete_before_its_last_round(self):
        question, items = self._create_question_with_items(
            [('Small', 1), ('Medium', 2), ('Large', 3)],
            starting_index=1,
        )
        self._set_live_round(question, [items[1], items[0], items[2]], current_round=1)

        previous_count = len(self.consumer.channel_layer.sent)
        async_to_sync(self.consumer.handle_admin_end_question)({})
        new_messages = [
            message for _, message in self.consumer.channel_layer.sent[previous_count:]
        ]

        self.assertFalse(any(
            message.get('type') in {
                'question_rounds_complete',
                'show_solution',
                'question_ended',
            }
            for message in new_messages
        ))
        self.quiz.refresh_from_db()
        self.quiz.session.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, question.id)
        self.assertTrue(self.quiz.session.is_round_active)
        self.assertEqual(
            self.quiz.session.reveal_state,
            SortingLadderSession.REVEAL_ACTIVE,
        )

    def test_starting_next_question_clears_pending_selection_state(self):
        """Neue Fragen starten ohne Pending-Auswahl aus dem vorherigen Set."""
        question_one, items_one = self._create_question_with_items(
            [('A', 1), ('B', 2), ('C', 3)],
            starting_index=1,
        )
        question_two, items_two = self._create_question_with_items(
            [('D', 1), ('E', 2), ('F', 3)],
            starting_index=1,
        )
        self._set_live_round(question_one, [items_one[1], items_one[0], items_one[2]], current_round=1)

        self._update_pending_selection([items_one[0].id, items_one[1].id])
        self.assertTrue(SortingPendingRoundSelection.objects.exists())

        payload = async_to_sync(self.consumer.initialize_question_for_quiz)(
            self.quiz.id,
            question_two.id,
            None,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload['question']['id'], question_two.id)
        self.assertFalse(SortingPendingRoundSelection.objects.exists())
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, question_two.id)

    def test_question_payload_and_rejoin_use_configured_set_number(self):
        question_one, _ = self._create_question_with_items(
            [('A', 1), ('B', 2), ('C', 3)],
            starting_index=1,
        )
        question_two, _ = self._create_question_with_items(
            [('D', 1), ('E', 2), ('F', 3)],
            starting_index=1,
        )
        self.quiz.selected_questions.add(question_one, question_two)
        self.quiz.question_order = [question_two.id, question_one.id]
        self.quiz.save(update_fields=['question_order'])

        payload = async_to_sync(self.consumer.initialize_question_for_quiz)(
            self.quiz.id,
            question_two.id,
            None,
            self.participant.hub_session_code,
        )
        rejoin = async_to_sync(self.consumer.get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )

        self.assertEqual(payload['set_number'], 1)
        self.assertEqual(payload['question']['set_number'], 1)
        self.assertEqual(rejoin['question_payload']['set_number'], 1)
        self.assertEqual(rejoin['question_payload']['question']['set_number'], 1)

    def test_multiple_correct_rounds_keep_active_participant_advancing(self):
        question, _items = self._create_question_with_items(
            [('One', 1), ('Two', 2), ('Three', 3), ('Four', 4)],
            starting_index=1,
        )

        payload = async_to_sync(self.consumer.initialize_question_for_quiz)(
            self.quiz.id,
            question.id,
            None,
        )

        self.assertIsNotNone(payload)
        shuffled_ids = [entry['id'] for entry in payload['items']]
        self.assertEqual(len(shuffled_ids), 4)
        rank_map = dict(question.elements.values_list('id', 'correct_rank'))

        for round_number in range(1, len(shuffled_ids)):
            visible_ids = shuffled_ids[:round_number + 1]
            ordered_ids = sorted(visible_ids, key=lambda item_id: rank_map[item_id])

            result = async_to_sync(self.consumer.save_round_full_order)(
                self.participant.name,
                self.participant.hub_session_code,
                ordered_ids,
                False,
            )

            self.assertIsNotNone(result)
            self.assertEqual(result['question_id'], question.id)
            self.assertEqual(result['round_number'], round_number)
            self.assertTrue(result['is_correct'])
            self.assertFalse(result['is_eliminated'])
            expected_visible_order = (
                ordered_ids if round_number < len(shuffled_ids) - 1 else []
            )
            self.assertEqual(result['correct_order_ids'], expected_visible_order)

            self.participant.refresh_from_db()
            self.assertFalse(self.participant.is_eliminated)
            self.assertEqual(self.participant.rounds_survived, round_number)

            if round_number < len(shuffled_ids) - 1:
                next_round = async_to_sync(self.consumer.start_next_round_db)(self.quiz.id)
                self.assertIsNotNone(next_round)
                self.assertEqual(next_round['question_id'], question.id)
                self.assertEqual(next_round['round_number'], round_number + 1)
                self.quiz.session.refresh_from_db()
                self.assertEqual(self.quiz.session.current_round, round_number + 1)
                self.assertTrue(self.quiz.session.is_round_active)

    def test_timer_timeout_evaluates_pending_selected_order_without_login(self):
        """Bei Timer-Ende wird eine vorhandene, nicht eingeloggte Auswahl korrekt ausgewertet."""
        question, items = self._create_question_with_items(
            [('Small', 1), ('Medium', 2), ('Large', 3)],
            starting_index=1,
        )
        medium = items[1]
        small = items[0]
        self._set_live_round(question, [medium, small, items[2]], current_round=1)

        self._update_pending_selection([small.id, medium.id])

        result = async_to_sync(self.consumer.save_round_full_order)(
            self.participant.name,
            self.participant.hub_session_code,
            [],
            True,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result['is_correct'])
        self.assertFalse(result['is_eliminated'])
        self.assertEqual(result['correct_order_ids'], [small.id, medium.id])

        submission = RoundSubmission.objects.get(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
        )
        self.assertEqual(submission.all_elements, [small.id, medium.id])
        self.assertTrue(submission.is_correct)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.rounds_survived, 1)
        self.assertFalse(self.participant.is_eliminated)

    def test_timer_timeout_uses_pending_selected_order_when_it_is_wrong(self):
        """Bei Timer-Ende wird auch eine falsche Pending-Auswahl als genau diese falsche Antwort gewertet."""
        question, items = self._create_question_with_items(
            [('Small', 1), ('Medium', 2), ('Large', 3)],
            starting_index=1,
        )
        medium = items[1]
        small = items[0]
        self._set_live_round(question, [medium, small, items[2]], current_round=1)

        self._update_pending_selection([medium.id, small.id])

        result = async_to_sync(self.consumer.save_round_full_order)(
            self.participant.name,
            self.participant.hub_session_code,
            [],
            True,
        )

        self.assertIsNotNone(result)
        self.assertFalse(result['is_correct'])
        self.assertTrue(result['is_eliminated'])
        self.assertTrue(result['set_has_more_rounds'])
        self.assertEqual(result['correct_order_ids'], [small.id, medium.id])

        submission = RoundSubmission.objects.get(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
        )
        self.assertEqual(submission.all_elements, [medium.id, small.id])
        self.assertFalse(submission.is_correct)
        self.participant.refresh_from_db()
        self.assertTrue(self.participant.is_eliminated)


class SortingLadderScoreBoxTimingTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sorting-score-timing', password='pass')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Timing',
            creator=self.user,
            room_code='6791',
            status='active',
        )
        self.participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess2',
            is_active=True,
        )
        self.consumer = SortingLadderGameConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'sortingladder_{self.quiz.room_code}'
        self.consumer.channel_layer = DummyChannelLayer()
        self.consumer.channel_name = 'sorting-score-timing-channel'

        async def _noop_send(*args, **kwargs):
            return None

        self.consumer.send = _noop_send

    def _create_question_with_items(self, texts_and_ranks, starting_index):
        question = SortingQuestion.objects.create(
            question_text='Sort these items',
            description='Timing test question.',
            upper_label='Highest',
            lower_label='Lowest',
            points=10,
            round_time_limit=30,
            created_by=self.user,
        )
        items = []
        for text, rank in texts_and_ranks:
            items.append(
                SortingItem.objects.create(
                    topic=question,
                    text=text,
                    correct_rank=rank,
                )
            )
        question.starting_item = items[starting_index]
        question.save(update_fields=['starting_item'])
        return question, items

    def _set_live_round(self, question, ordered_items, current_round=1):
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])
        session, _ = SortingLadderSession.objects.get_or_create(quiz=self.quiz)
        session.shuffled_item_ids = ",".join(str(item.id) for item in ordered_items)
        session.current_round = current_round
        session.is_round_active = True
        session.time_limit_seconds = 30
        session.round_start_time = timezone.now()
        session.round_end_time = timezone.now() + timezone.timedelta(seconds=30)
        session.save()
        return session

    def test_progress_history_hides_eliminated_current_set_until_next_round_starts(self):
        question, items = self._create_question_with_items(
            [('Small', 1), ('Medium', 2), ('Large', 3)],
            starting_index=1,
        )
        self._set_live_round(question, [items[1], items[0], items[2]], current_round=1)

        result = async_to_sync(self.consumer.save_round_full_order)(
            self.participant.name,
            self.participant.hub_session_code,
            [items[1].id, items[0].id],
            False,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result['is_eliminated'])
        result_history = async_to_sync(self.consumer.get_participant_progress_history_for_round_result)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            result_history,
            [{
                'question_number': 1,
                'survived_rounds': 0,
                'max_rounds': 2,
            }],
        )

        history_before_next_round = async_to_sync(self.consumer.get_participant_progress_history)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(history_before_next_round, [])

        snapshot = async_to_sync(self.consumer.get_rejoin_snapshot)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            snapshot['latest_round_result']['progress_history'],
            [{
                'question_number': 1,
                'survived_rounds': 0,
                'max_rounds': 2,
            }],
        )

        session = self.quiz.session
        session.current_round = 2
        session.save(update_fields=['current_round'])

        history_after_next_round = async_to_sync(self.consumer.get_participant_progress_history)(
            self.participant.name,
            self.participant.hub_session_code,
        )
        self.assertEqual(
            history_after_next_round,
            [{
                'question_number': 1,
                'survived_rounds': 0,
                'max_rounds': 2,
            }],
        )

    def test_eliminated_player_still_advances_to_the_next_visible_round(self):
        template_source = (
            Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        ).read_text(encoding='utf-8')
        handler = template_source.split('onRoundStarted(roundData) {', 1)[1].split(
            'onRoundEnded(_survivors) {}',
            1,
        )[0]

        self.assertNotIn('if (this.isEliminated) {\n                            return;', handler)
        self.assertIn('this.currentRound = nextRoundNumber;', handler)
        self.assertIn(
            "this.setInteractionLock(true, 'Du bist für dieses Set ausgeschieden",
            handler,
        )


class SortingLadderScoreBoxTimingTemplateTests(TestCase):
    def test_player_template_defers_elimination_history_until_round_or_question_progresses(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn('const shouldDeferProgressHistory = !!data.is_eliminated && resultRound >= this.currentRound;', template_source)
        self.assertIn('if (!shouldDeferProgressHistory) {', template_source)
        self.assertIn('this.setProgressHistory(pendingRoundResult.progress_history);', template_source)


class SortingLadderVhsLayoutTemplateTests(TestCase):
    def setUp(self):
        base_dir = Path(__file__).resolve().parent.parent
        self.template_source = (
            base_dir / 'templates' / 'sorting_ladder' / 'play.html'
        ).read_text(encoding='utf-8')
        self.vhs_css = (
            base_dir / 'static' / 'themes' / 'vhs' / 'vhs.css'
        ).read_text(encoding='utf-8')
        self.accessibility_source = (
            base_dir / 'templates' / 'includes' / 'accessibility_widget.html'
        ).read_text(encoding='utf-8')

    def test_vhs_layout_keeps_runtime_hooks_and_internal_item_ids(self):
        self.assertEqual(self.template_source.count('id="topicLayout"'), 1)
        self.assertEqual(self.template_source.count('id="ladderContainer"'), 1)
        self.assertEqual(self.template_source.count('id="itemPool"'), 1)
        self.assertEqual(self.template_source.count("submitBtn.id = 'submitRoundBtn';"), 1)
        self.assertIn(
            "submit-answer vhs-action-button sorting-ladder-submit",
            self.template_source,
        )
        self.assertIn("submitBtn.addEventListener('click', () => this.submitCurrentRound());", self.template_source)
        self.assertIn('card.dataset.itemId = item.id;', self.template_source)
        self.assertIn('<div class="option-key">${index + 1}</div>', self.template_source)

    def test_vhs_cards_hide_only_the_visible_pool_number(self):
        scope = (
            'html[data-participant-theme="vhs"] '
            'body.sorting-ladder-play-page .vhs-theme-shell'
        )
        self.assertIn(f'{scope}\n  #topicState .answer-card .option-key {{', self.vhs_css)
        number_rule = self.vhs_css.split(
            f'{scope}\n  #topicState .answer-card .option-key {{',
            1,
        )[1].split('}', 1)[0]
        self.assertIn('display: none !important;', number_rule)
        self.assertIn(
            f'{scope}\n  #topicState .answer-card .option-text {{',
            self.vhs_css,
        )
        self.assertIn(f'{scope}\n  #finalOrderState .final-order-item {{', self.vhs_css)
        self.assertIn('background: rgba(233, 223, 202, 0.9) !important;', self.vhs_css)
        self.assertIn('background: rgba(25, 31, 29, 0.72) !important;', self.vhs_css)

    def test_vhs_reveal_uses_vertical_ladder_with_authoritative_end_labels(self):
        scope = (
            'html[data-participant-theme="vhs"] '
            'body.sorting-ladder-play-page .vhs-theme-shell'
        )
        self.assertEqual(self.template_source.count('id="finalOrderItems"'), 1)
        self.assertEqual(self.template_source.count('id="finalOrderUpperLabel"'), 1)
        self.assertEqual(self.template_source.count('id="finalOrderLowerLabel"'), 1)
        self.assertIn(
            "upperLabelEl.textContent = this.currentQuestion?.upper_label || '';",
            self.template_source,
        )
        self.assertIn(
            "lowerLabelEl.textContent = this.currentQuestion?.lower_label || '';",
            self.template_source,
        )
        self.assertIn(
            'this.ladderItems.forEach((item, index) => {',
            self.template_source,
        )
        self.assertIn(
            f'{scope}\n  #finalOrderState .sorting-ladder-reveal-helper {{\n'
            '  display: none !important;',
            self.vhs_css,
        )
        self.assertIn(
            f'{scope}\n  #finalOrderState .final-order-list {{\n'
            '  display: grid !important;\n'
            '  grid-template-columns: minmax(0, 1fr);',
            self.vhs_css,
        )
        self.assertIn(
            f'{scope}\n  #finalOrderState .final-order-arrow {{\n'
            '  display: none !important;',
            self.vhs_css,
        )
        self.assertIn('min-height: 58px;', self.vhs_css)
        self.assertIn(
            '#finalOrderState .sorting-ladder-reveal-label {\n'
            '  display: block !important;',
            self.vhs_css,
        )

    def test_vhs_layout_centers_labels_on_card_column_and_is_responsive(self):
        scope = (
            'html[data-participant-theme="vhs"] '
            'body.sorting-ladder-play-page .vhs-theme-shell'
        )
        self.assertIn(
            'grid-template-columns: minmax(0, 1fr) var(--sorting-ladder-triangle-width);',
            self.vhs_css,
        )
        self.assertIn(
            'grid-template-columns: repeat(2, minmax(0, 1fr));',
            self.vhs_css,
        )
        self.assertIn(
            '#topicState .pool-column {\n'
            '  padding-inline-end: calc(',
            self.vhs_css,
        )
        self.assertIn(
            'var(--sorting-ladder-triangle-width) + var(--sorting-ladder-column-gap)',
            self.vhs_css,
        )
        self.assertIn(f'{scope}\n  #topicState .topic-layout {{', self.vhs_css)
        self.assertIn('width: min(100%, 760px);', self.vhs_css)
        self.assertIn('@media (max-width: 760px)', self.vhs_css)
        self.assertIn(
            '.vhs-theme-shell:has(.qa-score-widget:not(.is-collapsed))\n'
            '    #topicState .topic-layout',
            self.vhs_css,
        )
        self.assertIn(
            f'{scope}\n  #topicState .sorting-ladder-actions',
            self.vhs_css,
        )

    def test_position_markers_use_card_and_gap_geometry(self):
        self.assertIn('--sorting-ladder-card-height: 58px;', self.vhs_css)
        self.assertIn('--sorting-ladder-row-gap: 16px;', self.vhs_css)
        self.assertIn(
            'padding-block: calc(\n'
            '    (var(--sorting-ladder-triangle-width) + var(--sorting-ladder-row-gap)) / 2',
            self.vhs_css,
        )
        self.assertIn(
            'transform: translateY(calc(\n'
            '    -50% - (var(--sorting-ladder-row-gap) / 2)',
            self.vhs_css,
        )
        self.assertIn('#topicState .ladder-slot-row:last-child {', self.vhs_css)
        self.assertIn(
            '#topicState .triangle-container .number {',
            self.vhs_css,
        )
        self.assertIn(
            'font: 800 15px/1 "Courier New", Courier, monospace !important;',
            self.vhs_css,
        )

    def test_submit_uses_shared_vhs_button_states(self):
        self.assertIn(
            'body.sorting-ladder-play-page .vhs-theme-shell #topicState '
            '.vhs-action-button:not(:disabled):hover',
            self.vhs_css,
        )
        self.assertIn(
            'body.sorting-ladder-play-page .vhs-theme-shell #topicState '
            '.vhs-action-button:not(:disabled):focus-visible',
            self.vhs_css,
        )
        self.assertIn(
            'body.sorting-ladder-play-page .vhs-theme-shell #topicState '
            '.vhs-action-button:not(:disabled):active',
            self.vhs_css,
        )
        self.assertIn(
            '#topicState .sorting-ladder-submit:disabled',
            self.vhs_css,
        )
        self.assertIn('hasPlacedCurrentRoundItem() {', self.template_source)
        self.assertIn('updateSubmitAvailability() {', self.template_source)
        self.assertIn('|| !this.hasPlacedCurrentRoundItem();', self.template_source)
        self.assertIn('const hasPlacedRoundItem = this.hasPlacedCurrentRoundItem();', self.template_source)
        self.assertGreaterEqual(self.template_source.count('this.updateSubmitAvailability();'), 2)

    def test_logged_answer_keeps_one_stable_waiting_message(self):
        self.assertNotIn(
            'Warte darauf, dass der Host die nächste Runde startet.',
            self.template_source,
        )
        self.assertGreaterEqual(
            self.template_source.count(
                "this.setInteractionLock(true, 'Warte auf die nächste Runde.');"
            ),
            3,
        )
        timer_match = re.search(
            r"startPlayerTimer\(seconds(?:,\s*endsAt = null,\s*serverNow = null)?\)\s*\{(?P<body>.*?)\n\s*\}\n\n\s*renderCurrentRound",
            self.template_source,
            re.DOTALL,
        )
        self.assertIsNotNone(timer_match)
        self.assertNotIn('setInteractionLock(true,', timer_match.group('body'))

    def test_wait_banner_and_submit_align_to_compact_source_column(self):
        self.assertIn(
            '#topicState:not(.set-awaiting-reveal) #roundLockBanner {',
            self.vhs_css,
        )
        self.assertIn('width: fit-content;', self.vhs_css)
        self.assertIn('padding: 7px 12px !important;', self.vhs_css)
        self.assertIn('font: 700 11px/1.3 "Courier New", Courier, monospace !important;', self.vhs_css)
        self.assertIn(
            'padding: 0\n'
            '    calc(var(--sorting-ladder-triangle-width) + var(--sorting-ladder-column-gap))\n'
            '    0 0 !important;',
            self.vhs_css,
        )

    def test_dense_ladder_uses_shared_count_and_height_responsive_card_variables(self):
        self.assertIn('--sorting-ladder-card-padding-block: 8px;', self.vhs_css)
        self.assertIn(
            '--sorting-ladder-card-font-size: clamp(12px, 1.1vw, 15px);',
            self.vhs_css,
        )
        self.assertIn(
            '#topicState:has(.topic-layout .ladder-slot-row:nth-child(n + 8)) {',
            self.vhs_css,
        )
        self.assertIn('--sorting-ladder-card-height: 50px;', self.vhs_css)
        self.assertIn('@media (max-height: 820px) and (min-width: 761px)', self.vhs_css)
        self.assertIn(
            'padding: var(--sorting-ladder-card-padding-block) 36px !important;',
            self.vhs_css,
        )
        self.assertIn(
            'padding: var(--sorting-ladder-card-padding-block) 12px !important;',
            self.vhs_css,
        )
        self.assertGreaterEqual(
            self.vhs_css.count('height: var(--sorting-ladder-card-height);'),
            2,
        )

    def test_delete_control_timer_and_question_spacing_use_existing_vhs_system(self):
        self.assertIn('#topicState .slot-clear-btn {', self.vhs_css)
        self.assertIn('position: absolute;', self.vhs_css)
        self.assertIn('top: 50%;', self.vhs_css)
        self.assertIn('transform: translateY(-50%);', self.vhs_css)
        self.assertIn(
            "document.body.classList.contains('sorting-ladder-play-page')",
            self.accessibility_source,
        )
        self.assertIn(
            "getVisibleVhsElement('#topicState #playerTimeLeft')",
            self.accessibility_source,
        )
        self.assertIn(
            '#topicState .question-text .vhs-question-body {',
            self.vhs_css,
        )
        self.assertIn(
            '#topicState .round-indicators {',
            self.vhs_css,
        )
        self.assertIn(
            '#topicState .round-indicator.round-success {',
            self.vhs_css,
        )
        self.assertIn(
            '#topicState .round-indicator.round-fail {',
            self.vhs_css,
        )


class SortingLadderRoundInteractionScopeTemplateTests(TestCase):
    def test_player_template_resets_round_state_for_new_question_and_ignores_stale_round_events(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn('this.currentRoundItemId = null;', template_source)
        self.assertIn('this.pendingRoundResult = null;', template_source)
        self.assertIn('this.latestResolvedRound = 0;', template_source)
        self.assertIn('if (data.question_id && this.currentQuestion?.id && Number(data.question_id) !== Number(this.currentQuestion.id)) {', template_source)
        self.assertIn('if (roundData?.question_id && this.currentQuestion?.id && Number(roundData.question_id) !== Number(this.currentQuestion.id)) {', template_source)

    def test_set_completion_and_reveal_are_distinct_authoritative_events(self):
        base_dir = Path(__file__).resolve().parent.parent
        player_source = (
            base_dir / 'templates' / 'sorting_ladder' / 'play.html'
        ).read_text(encoding='utf-8')
        monitor_source = (
            base_dir / 'templates' / 'admin_dashboard' / 'sorting_ladder_monitor.html'
        ).read_text(encoding='utf-8')

        self.assertIn("case 'question_rounds_complete':", player_source)
        self.assertIn("case 'show_solution':", player_source)
        self.assertIn(
            "'Warte darauf, dass der Host die Reihenfolge zeigt.'",
            player_source,
        )
        self.assertIn("this.setPhase = 'awaiting_reveal';", player_source)
        self.assertIn("this.setPhase = 'revealed';", player_source)
        self.assertIn(
            "if (this.setPhase && this.setPhase !== 'active') {",
            player_source,
        )
        self.assertNotIn(
            'Waiting for host to end and reveal this question...',
            player_source,
        )
        self.assertIn('Reihenfolge zeigen', monitor_source)
        self.assertIn("type: 'admin_show_solution'", monitor_source)
        self.assertIn("this.onRoundsComplete();", monitor_source)
        self.assertIn("this.onSolutionShown();", monitor_source)

    def test_round_result_ui_keeps_timer_running_after_logged_answer(self):
        """Die Round-Result-UI darf den lokalen Countdown nach dem Einloggen nicht stoppen."""
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')
        match = re.search(
            r"onRoundResult\(data\)\s*\{(?P<body>.*?)\n\s*\}\n\n\s*onRoundStarted",
            template_source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group('body')

        self.assertIn("this.pendingRoundResult = data;", body)
        self.assertNotIn("this.roundTimerEnded = true;", body)
        self.assertNotIn("clearInterval(this.roundTimer);", body)

    def test_player_template_supports_pointer_drag_for_touch_devices(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn('touch-action: none;', template_source)
        self.assertIn("card.addEventListener('pointerdown'", template_source)
        self.assertIn("window.addEventListener('pointermove'", template_source)
        self.assertIn("window.addEventListener('pointerup'", template_source)
        self.assertIn("window.addEventListener('pointercancel'", template_source)
        self.assertIn('requestAnimationFrame(() => this.renderSortingPointerDrag())', template_source)
        self.assertIn('translate3d(${deltaX}px, ${deltaY}px, 0)', template_source)
        self.assertIn('releasePointerCapture(pointerDrag.pointerId)', template_source)
        self.assertIn('transition: none;', template_source)
        self.assertIn('this.placeSortingItemAtPosition(drag.itemId, position);', template_source)
        self.assertIn('this.placeSortingItemAtPosition(droppedId, position);', template_source)
        self.assertIn("if (event.pointerType === 'mouse') return;", template_source)

    def test_cancelled_drag_releases_transient_item_selection(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn('claimCurrentRoundDragItem(itemId) {', template_source)
        self.assertIn('cleanupSortingDragState(itemId, element = null, pointerDrag = null) {', template_source)
        self.assertIn('this.cleanupSortingDragState(id, card);', template_source)
        self.assertIn('if (!this.hasPlacedCurrentRoundItem()) {', template_source)
        self.assertIn('this.currentRoundItemId = null;', template_source)
        self.assertIn('this.updateSubmitAvailability();', template_source)

    def test_new_drag_replaces_only_an_unplaced_stale_selection(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')
        match = re.search(
            r"claimCurrentRoundDragItem\(itemId\)\s*\{(?P<body>.*?)\n\s*\}\n\n\s*cleanupSortingDragState",
            template_source,
            re.DOTALL,
        )

        self.assertIsNotNone(match)
        body = match.group('body')
        self.assertIn('this.hasPlacedCurrentRoundItem()', body)
        self.assertIn('Number(this.currentRoundItemId) !== Number(itemId)', body)
        self.assertIn('this.currentRoundItemId = itemId;', body)

    def test_pointer_cancel_and_stale_pointer_events_use_guarded_cleanup(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn(
            'cancelHandler = (cancelEvent) => this.finishSortingPointerDrag(cancelEvent, false, drag);',
            template_source,
        )
        self.assertIn('this.pointerDrag !== drag', template_source)
        self.assertIn('event.pointerId !== drag.pointerId', template_source)
        self.assertIn("window.removeEventListener('pointercancel', pointerDrag.cancelHandler);", template_source)
        self.assertIn("window.removeEventListener('keydown', pointerDrag.escapeHandler);", template_source)
        self.assertIn('releasePointerCapture(pointerDrag.pointerId)', template_source)
        self.assertIn("if (keyEvent.key !== 'Escape' || this.pointerDrag !== drag) return;", template_source)
        self.assertIn('this.cancelActiveSortingDrag();', template_source)


class SortingLadderConsumerEventPayloadTests(TestCase):
    def test_live_round_result_event_includes_question_id_for_client_side_stale_event_guards(self):
        consumer = SortingLadderGameConsumer()
        direct_messages = []

        async def _capture_send(*args, **kwargs):
            text_data = kwargs.get('text_data')
            if text_data is None and args:
                text_data = args[0]
            if text_data:
                direct_messages.append(json.loads(text_data))

        consumer.send = _capture_send

        async_to_sync(consumer.round_result)({
            'participant_name': 'Alice',
            'question_id': 42,
            'round_number': 2,
            'is_correct': True,
            'rounds_survived': 2,
            'is_eliminated': False,
            'points': 20,
            'has_more_rounds': True,
            'set_has_more_rounds': True,
            'per_question_rounds': 2,
            'correct_order_ids': [7, 8, 9],
            'progress_history': [],
        })

        self.assertEqual(direct_messages, [{
            'type': 'round_result',
            'participant_name': 'Alice',
            'question_id': 42,
            'round_number': 2,
            'is_correct': True,
            'rounds_survived': 2,
            'is_eliminated': False,
            'points': 20,
            'is_tutorial_round': False,
            'has_more_rounds': True,
            'set_has_more_rounds': True,
            'per_question_rounds': 2,
            'correct_order_ids': [7, 8, 9],
            'progress_history': [],
        }])


class SortingLadderMonitorTimerTest(TestCase):
    def test_monitor_view_renders_active_round_timer(self):
        """Der Host-Monitor rendert den laufenden Rundentimer aus dem Session-State."""
        admin = User.objects.create_user(
            username='sorting-admin',
            password='pass',
            is_staff=True,
        )
        quiz = SortingLadderGame.objects.create(
            title='Sorting Monitor Test',
            creator=admin,
            room_code='9123',
            status='active',
        )
        question = SortingQuestion.objects.create(
            question_text='Sort these items',
            description='Question for monitor timer rendering.',
            upper_label='High',
            lower_label='Low',
            points=10,
            round_time_limit=30,
            created_by=admin,
        )
        first = SortingItem.objects.create(topic=question, text='First', correct_rank=1)
        second = SortingItem.objects.create(topic=question, text='Second', correct_rank=2)
        question.starting_item = first
        question.save(update_fields=['starting_item'])
        quiz.current_question = question
        quiz.save(update_fields=['current_question'])
        SortingLadderSession.objects.create(
            quiz=quiz,
            current_round=1,
            is_round_active=True,
            time_limit_seconds=30,
            round_start_time=timezone.now(),
            round_end_time=timezone.now() + timezone.timedelta(seconds=25),
            shuffled_item_ids=f'{first.id},{second.id}',
        )

        self.client.force_login(admin)
        response = self.client.get(reverse('admin_dashboard:sorting_ladder_monitor', args=[quiz.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.context['current_round_time_left'], 0)
        self.assertContains(response, 'id="questionTimerWrapper"')
        self.assertContains(response, 'id="hostTimeLeft"')
        self.assertContains(response, f'data-time-left="{response.context["current_round_time_left"]}"')

    def test_monitor_template_starts_and_stops_round_timer_from_host_events(self):
        """Der Host-Monitor muss den Countdown initialisieren und an Round-Events koppeln."""
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'admin_dashboard' / 'sorting_ladder_monitor.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn('initializeRoundTimer()', template_source)
        self.assertIn('startRoundTimer(seconds)', template_source)
        self.assertIn('stopRoundTimer()', template_source)
        self.assertIn('renderRoundTimer()', template_source)
        self.assertIn("this.initializeRoundTimer();", template_source)
        self.assertIn("this.startRoundTimer(roundData?.time_limit_seconds || 0);", template_source)
        self.assertIn("case 'round_ended':", template_source)
        self.assertIn("this.stopRoundTimer();", template_source)

    def test_monitor_template_distinguishes_given_and_logged_answer_badges(self):
        template_path = Path(__file__).resolve().parent.parent / 'templates' / 'admin_dashboard' / 'sorting_ladder_monitor.html'
        template_source = template_path.read_text(encoding='utf-8')

        self.assertIn("const isLogged = !!entry.is_logged;", template_source)
        self.assertIn("const hasSelection = !!entry.has_selection || (!!entry.has_answered && !isLogged);", template_source)
        self.assertIn("'badge badge-success'", template_source)
        self.assertIn("'badge badge-warning'", template_source)
        self.assertIn("const label = isLogged ? 'eingeloggt' : (hasSelection ? 'gegeben' : 'offen');", template_source)


class SortingLadderRoundAnswerStatusScopeTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sorting-scope', password='pass')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Scope',
            creator=self.user,
            room_code='9441',
            status='active',
        )
        self.question = SortingQuestion.objects.create(
            question_text='Sort these scope items',
            description='Scope test',
            upper_label='High',
            lower_label='Low',
            points=10,
            round_time_limit=30,
            created_by=self.user,
        )
        self.first = SortingItem.objects.create(topic=self.question, text='First', correct_rank=1)
        self.second = SortingItem.objects.create(topic=self.question, text='Second', correct_rank=2)
        self.third = SortingItem.objects.create(topic=self.question, text='Third', correct_rank=3)
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])
        self.session = SortingLadderSession.objects.create(
            quiz=self.quiz,
            current_round=1,
            is_round_active=True,
            time_limit_seconds=30,
            round_start_time=timezone.now(),
            round_end_time=timezone.now() + timezone.timedelta(seconds=20),
            shuffled_item_ids=f'{self.first.id},{self.second.id},{self.third.id}',
        )
        self.current_hub_session = HubSession.objects.create(
            code='CURRSL1',
            name='Current Sorting Session',
            is_active=True,
            started_at=timezone.now(),
        )
        self.old_hub_session = HubSession.objects.create(
            code='OLDSL1',
            name='Old Sorting Session',
            is_active=False,
            started_at=timezone.now() - timezone.timedelta(hours=1),
            ended_at=timezone.now() - timezone.timedelta(minutes=30),
        )
        HubGameStep.objects.create(
            session=self.old_hub_session,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title='Sorting Ladder Old',
            order=1,
        )
        HubGameStep.objects.create(
            session=self.current_hub_session,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title='Sorting Ladder Current',
            order=1,
        )
        self.current_participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code=self.current_hub_session.code,
            is_active=True,
            is_eliminated=False,
        )
        self.old_participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code=self.old_hub_session.code,
            is_active=True,
            is_eliminated=False,
        )
        self.consumer = SortingLadderGameConsumer()
        self.consumer.room_code = self.quiz.room_code

    def test_round_answer_status_only_includes_current_hub_session_participants(self):
        status = async_to_sync(self.consumer.get_round_answer_status_db)(self.quiz.id)

        self.assertEqual(status['round_number'], 1)
        self.assertEqual(status['statuses'], [{
            'participant_name': self.current_participant.name,
            'has_answered': False,
            'has_selection': False,
            'is_logged': False,
            'answer_status': 'offen',
        }])


class SortingLadderScoreBoxTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sorting-score-user', password='pass')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Score Quiz',
            creator=self.user,
            room_code='9551',
        )
        self.participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess1',
            is_active=True,
        )

    def _create_question(self, title, ranks):
        question = SortingQuestion.objects.create(
            question_text=title,
            description='Score box test',
            upper_label='High',
            lower_label='Low',
            points=10,
            round_time_limit=30,
            created_by=self.user,
        )
        for index, rank in enumerate(ranks, start=1):
            SortingItem.objects.create(
                topic=question,
                text=f'Item {index}',
                correct_rank=rank,
            )
        return question

    def test_play_view_renders_score_box_total_hook_without_template_error(self):
        response = self.client.get(
            reverse('sorting_ladder:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="sorting-history-box score-box"')
        self.assertContains(response, 'class="sorting-history-list score-box__list"')
        self.assertContains(response, 'id="sortingHistoryTotal"')
        self.assertContains(response, 'sessionCode: \'sess1\'')

    def test_progress_history_groups_rounds_into_set_score_with_correct_total(self):
        question = self._create_question('Sort these items', [1, 2, 3])
        self.quiz.current_question = None
        self.quiz.save(update_fields=['current_question'])

        ordered_ids = list(question.elements.order_by('correct_rank').values_list('id', flat=True))
        RoundSubmission.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            round_number=1,
            all_elements=ordered_ids[:2],
        )
        RoundSubmission.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            round_number=2,
            all_elements=ordered_ids,
        )

        consumer = SortingLadderGameConsumer()
        consumer.room_code = self.quiz.room_code
        history = async_to_sync(consumer.get_participant_progress_history)(
            self.participant.name,
            self.participant.hub_session_code,
        )

        self.assertEqual(history, [{
            'question_number': 1,
            'survived_rounds': 2,
            'max_rounds': 2,
        }])


class SortingLadderFinalScoreTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sorting-final-score', password='pass')
        self.hub_session = HubSession.objects.create(
            code='SLFINAL',
            name='Sorting final score',
            is_active=False,
        )
        HubParticipant.objects.create(session=self.hub_session, nickname='Alice')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting final score',
            creator=self.user,
            room_code='SLFS',
            status='completed',
        )
        self.step = HubGameStep.objects.create(
            session=self.hub_session,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        self.participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.hub_session.code,
        )
        self.question = SortingQuestion.objects.create(
            question_text='Legacy ten-point question',
            description='Each survived round is still worth one point.',
            points=10,
            created_by=self.user,
        )
        self.items = [
            SortingItem.objects.create(
                topic=self.question,
                text=f'Item {index}',
                correct_rank=index,
            )
            for index in range(1, 5)
        ]

    def _set_survived_rounds(self, count):
        RoundSubmission.objects.filter(participant=self.participant).delete()
        ordered_ids = [item.id for item in self.items]
        for round_number in range(1, count + 1):
            RoundSubmission.objects.create(
                quiz=self.quiz,
                participant=self.participant,
                question=self.question,
                round_number=round_number,
                all_elements=ordered_ids[:round_number + 1],
            )
        self.participant.rounds_survived = count
        self.participant.save(update_fields=['rounds_survived'])
        return self.participant.calculate_total_score()

    def test_final_score_uses_one_point_per_survived_round(self):
        for survived_rounds in (0, 1, 3):
            with self.subTest(survived_rounds=survived_rounds):
                self.assertEqual(self._set_survived_rounds(survived_rounds), survived_rounds)

                results = get_post_game_results(
                    self.hub_session,
                    self.step,
                    participant_name=self.participant.name,
                )
                self.assertTrue(results['available'])
                self.assertEqual(results['rows'][0]['game_points'], survived_rounds)

                leaderboard = get_leaderboard_data(self.hub_session)
                player = next(
                    row for row in leaderboard['participants']
                    if row['name'] == self.participant.name
                )
                self.assertEqual(
                    player['game_scores'][f'step:{self.step.id}'],
                    survived_rounds,
                )
                self.assertEqual(player['total_score'], survived_rounds)

                consumer = SortingLadderGameConsumer()
                consumer.room_code = self.quiz.room_code
                final_scores = async_to_sync(consumer.get_final_scores)()
                self.assertEqual(
                    final_scores,
                    [{
                        'name': self.participant.name,
                        'rounds_survived': survived_rounds,
                        'is_eliminated': False,
                    }],
                )

    def test_ranking_uses_one_point_round_totals_for_each_participant(self):
        self._set_survived_rounds(3)
        HubParticipant.objects.create(session=self.hub_session, nickname='Bob')
        bob = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code=self.hub_session.code,
            rounds_survived=1,
        )
        ordered_ids = [item.id for item in self.items]
        RoundSubmission.objects.create(
            quiz=self.quiz,
            participant=bob,
            question=self.question,
            round_number=1,
            all_elements=ordered_ids[:2],
        )
        self.assertEqual(bob.calculate_total_score(), 1)

        results = get_post_game_results(self.hub_session, self.step)
        by_name = {row['participant']: row for row in results['rows']}

        self.assertEqual(by_name['Alice']['game_points'], 3)
        self.assertEqual(by_name['Alice']['rank'], 1)
        self.assertEqual(by_name['Bob']['game_points'], 1)
        self.assertEqual(by_name['Bob']['rank'], 2)

    def test_new_game_participant_starts_without_previous_score(self):
        self._set_survived_rounds(3)
        next_quiz = SortingLadderGame.objects.create(
            title='Next Sorting game',
            creator=self.user,
            room_code='SLNEXT',
        )
        next_participant = SortingLadderParticipant.objects.create(
            quiz=next_quiz,
            name='Alice',
            hub_session_code=self.hub_session.code,
        )

        self.assertEqual(next_participant.rounds_survived, 0)
        self.assertEqual(next_participant.total_score, 0)


class SortingLadderQuestionPhaseFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sorting-phase-user', password='pass')
        self.hub = HubSession.objects.create(code='SLPHASE', name='Sorting phase')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Phase Quiz',
            creator=self.user,
            room_code='SLPH',
            status='active',
        )
        HubGameStep.objects.create(
            session=self.hub,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        self.question = SortingQuestion.objects.create(
            question_text='Sort the planets',
            description='Smallest to largest',
            upper_label='Largest',
            lower_label='Smallest',
            round_time_limit=30,
            created_by=self.user,
        )
        self.items = [
            SortingItem.objects.create(
                topic=self.question,
                text=f'Planet {index}',
                correct_rank=index,
            )
            for index in range(1, 5)
        ]
        self.question.starting_item = self.items[1]
        self.question.save(update_fields=['starting_item'])
        self.quiz.selected_questions.add(self.question)
        self.quiz.question_order = [self.question.id]
        self.quiz.save(update_fields=['question_order'])
        self.participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.hub.code,
        )
        SortingLadderSession.objects.create(quiz=self.quiz)
        self.consumer = SortingLadderGameConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'sortingladder_{self.quiz.room_code}'
        self.consumer.channel_layer = DummyChannelLayer()
        self.consumer.channel_name = 'sorting-phase-channel'
        reset_question_flow(
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            session_code=self.hub.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def _action(self, *, snapshot=None, round_number=1, action_id=None):
        snapshot = snapshot or current_snapshot(
            'sorting_ladder',
            self.quiz.room_code,
            self.hub.code,
        )
        return {
            'question_id': self.question.id,
            'game_id': snapshot['game_id'],
            'state_revision': snapshot['state_revision'],
            'client_action_id': str(action_id or uuid.uuid4()),
            'expected_round': round_number,
            'expected_set': 1,
        }

    def _present(self, at):
        return async_to_sync(self.consumer.begin_sorting_question_phase)(
            self.quiz.id,
            self.question.id,
            self.question.round_time_limit,
            self.hub.code,
            self._action(),
            at=at,
        )

    def _reveal(self, context, at):
        return async_to_sync(self.consumer.reveal_sorting_round)(
            context,
            self._action(round_number=context['round_number']),
            at=at,
        )

    def _open(self, context, at, action=None):
        return async_to_sync(self.consumer.open_sorting_round)(
            context,
            action or self._action(round_number=context['round_number']),
            at=at,
        )

    def _submit_correct_current_round(self):
        session = SortingLadderSession.objects.get(quiz=self.quiz)
        shuffled_ids = [int(value) for value in session.shuffled_item_ids.split(',') if value]
        previous = (
            RoundSubmission.objects.filter(
                quiz=self.quiz,
                participant=self.participant,
                question=self.question,
                is_correct=True,
            )
            .order_by('-round_number')
            .first()
        )
        locked_ids = list(previous.all_elements) if previous else [shuffled_ids[0]]
        next_id = next(value for value in shuffled_ids if value not in locked_ids)
        rank_map = dict(
            SortingItem.objects.filter(id__in=[*locked_ids, next_id]).values_list('id', 'correct_rank')
        )
        ordered_ids = sorted([*locked_ids, next_id], key=rank_map.get)
        return async_to_sync(self.consumer.save_round_full_order)(
            self.participant.name,
            self.hub.code,
            ordered_ids,
        )

    def test_prompt_reveal_and_open_create_only_the_authoritative_deadline(self):
        self.assertTrue(get_question_flow_capabilities('sorting_ladder').uses_content_phase)
        presented_at = timezone.now()
        payload, presented = self._present(presented_at)

        self.assertTrue(presented.accepted)
        self.assertEqual(presented.snapshot['question_phase'], 'prompt_visible')
        self.assertEqual(
            timezone.datetime.fromisoformat(presented.snapshot['question_visible_at']) - presented_at,
            timedelta(seconds=1),
        )
        self.assertIsNone(presented.snapshot['answering_deadline_at'])
        self.assertIsNone(payload['ends_at'])
        session = SortingLadderSession.objects.get(quiz=self.quiz)
        self.assertFalse(session.is_round_active)
        self.assertIsNone(session.round_end_time)

        context = async_to_sync(self.consumer.get_current_round_context)()
        visible_at = timezone.datetime.fromisoformat(presented.snapshot['question_visible_at'])
        early_reveal = self._reveal(context, visible_at - timedelta(milliseconds=1))
        self.assertFalse(early_reveal.accepted)
        self.assertEqual(early_reveal.code, 'question_not_visible')

        revealed = self._reveal(context, visible_at)
        self.assertTrue(revealed.accepted)
        self.assertEqual(revealed.snapshot['question_phase'], 'content_visible')
        self.assertIsNone(revealed.snapshot['answering_deadline_at'])
        ready_at = sorting_ladder_reveal_ready_at(
            content_revealed_at=timezone.datetime.fromisoformat(
                revealed.snapshot['content_revealed_at']
            ),
            item_count=len(self.items),
            round_number=1,
        )
        early_open = self._open(context, ready_at - timedelta(milliseconds=1))
        self.assertFalse(early_open.accepted)
        self.assertEqual(early_open.code, 'content_reveal_in_progress')

        action = self._action(round_number=1)
        opened = self._open(context, ready_at, action=action)
        self.assertTrue(opened.accepted)
        self.assertEqual(opened.snapshot['question_phase'], 'answering_open')
        started_at = timezone.datetime.fromisoformat(opened.snapshot['answering_started_at'])
        deadline = timezone.datetime.fromisoformat(opened.snapshot['answering_deadline_at'])
        self.assertEqual(deadline - started_at, timedelta(seconds=30))
        session.refresh_from_db()
        self.assertTrue(session.is_round_active)
        self.assertEqual(session.round_start_time, started_at)
        self.assertEqual(session.round_end_time, deadline)

        duplicate = self._open(context, ready_at + timedelta(seconds=5), action=action)
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        session.refresh_from_db()
        self.assertEqual(session.round_end_time, deadline)

    def test_participant_actions_are_blocked_until_answering_open(self):
        payload, presented = self._present(timezone.now() - timedelta(seconds=1))
        context = async_to_sync(self.consumer.get_current_round_context)()
        shuffled_ids = [item['id'] for item in payload['items']]

        self.assertFalse(async_to_sync(self.consumer.update_pending_round_selection_db)(
            self.participant.name,
            self.hub.code,
            shuffled_ids[:1],
        ))
        self.assertIsNone(async_to_sync(self.consumer.save_round_full_order)(
            self.participant.name,
            self.hub.code,
            shuffled_ids[:2],
        ))

        revealed = self._reveal(context, timezone.datetime.fromisoformat(presented.snapshot['question_visible_at']))
        ready_at = sorting_ladder_reveal_ready_at(
            content_revealed_at=timezone.datetime.fromisoformat(revealed.snapshot['content_revealed_at']),
            item_count=len(self.items),
            round_number=1,
        )
        self.assertTrue(self._open(context, ready_at).accepted)
        self.assertTrue(async_to_sync(self.consumer.update_pending_round_selection_db)(
            self.participant.name,
            self.hub.code,
            shuffled_ids[:1],
        ))
        self.assertIsNotNone(self._submit_correct_current_round())

    def test_second_and_third_round_preserve_history_and_restart_prompt(self):
        _payload, presented = self._present(timezone.now() - timedelta(seconds=1))
        context = async_to_sync(self.consumer.get_current_round_context)()
        revealed = self._reveal(context, timezone.datetime.fromisoformat(presented.snapshot['question_visible_at']))
        first_ready = sorting_ladder_reveal_ready_at(
            content_revealed_at=timezone.datetime.fromisoformat(revealed.snapshot['content_revealed_at']),
            item_count=len(self.items),
            round_number=1,
        )
        self.assertTrue(self._open(context, first_ready).accepted)

        for expected_round in (2, 3):
            self.assertIsNotNone(self._submit_correct_current_round())
            current = current_snapshot('sorting_ladder', self.quiz.room_code, self.hub.code)
            round_state, decision = async_to_sync(self.consumer.prepare_next_round_phase)(
                self.quiz.id,
                self._action(snapshot=current, round_number=expected_round - 1),
            )
            self.assertTrue(decision.accepted)
            self.assertEqual(round_state['round_number'], expected_round)
            self.assertEqual(decision.snapshot['question_phase'], 'prompt_visible')
            session = SortingLadderSession.objects.get(quiz=self.quiz)
            self.assertFalse(session.is_round_active)
            self.assertIsNone(session.round_end_time)

            context = async_to_sync(self.consumer.get_current_round_context)()
            visible_at = timezone.datetime.fromisoformat(decision.snapshot['question_visible_at'])
            revealed = self._reveal(context, visible_at)
            counts = sorting_ladder_reveal_counts(
                item_count=len(self.items),
                round_number=expected_round,
            )
            self.assertEqual(counts, {
                'label_count': 0,
                'element_count': 0,
                'fixed_count': 0,
                'marker_group_count': 1,
            })
            ready_at = sorting_ladder_reveal_ready_at(
                content_revealed_at=timezone.datetime.fromisoformat(
                    revealed.snapshot['content_revealed_at']
                ),
                item_count=len(self.items),
                round_number=expected_round,
            )
            self.assertTrue(self._open(context, ready_at).accepted)

        self.assertEqual(
            RoundSubmission.objects.filter(participant=self.participant).count(),
            2,
        )

    def test_rejoin_reconstructs_reveal_progress_and_open_deadline(self):
        _payload, presented = self._present(timezone.now() - timedelta(seconds=1))
        context = async_to_sync(self.consumer.get_current_round_context)()
        revealed = self._reveal(
            context,
            timezone.datetime.fromisoformat(presented.snapshot['question_visible_at']),
        )

        reveal_snapshot = async_to_sync(self.consumer.get_rejoin_snapshot)(
            self.participant.name,
            self.hub.code,
        )
        reveal_payload = reveal_snapshot['question_payload']
        self.assertEqual(reveal_payload['question_phase'], 'content_visible')
        self.assertEqual(
            reveal_payload['content_revealed_at'],
            revealed.snapshot['content_revealed_at'],
        )
        self.assertIsNone(reveal_payload['answering_deadline_at'])
        self.assertGreater(
            timezone.datetime.fromisoformat(
                reveal_payload['sorting_ladder_reveal_ready_at']
            ),
            timezone.datetime.fromisoformat(reveal_payload['content_revealed_at']),
        )

        ready_at = timezone.datetime.fromisoformat(
            reveal_payload['sorting_ladder_reveal_ready_at']
        )
        opened = self._open(context, ready_at)
        self.assertTrue(opened.accepted)
        answering_snapshot = async_to_sync(self.consumer.get_rejoin_snapshot)(
            self.participant.name,
            self.hub.code,
        )
        answering_payload = answering_snapshot['question_payload']
        self.assertEqual(answering_payload['question_phase'], 'answering_open')
        self.assertEqual(
            answering_payload['answering_deadline_at'],
            opened.snapshot['answering_deadline_at'],
        )
        self.assertEqual(
            answering_snapshot['answering_deadline_at'],
            opened.snapshot['answering_deadline_at'],
        )

    def test_reveal_sequence_timing_and_templates_are_phase_gated(self):
        self.assertEqual(SORTING_LADDER_REVEAL_STAGGER_MS, 120)
        self.assertEqual(SORTING_LADDER_REVEAL_ANIMATION_MS, 160)
        self.assertEqual(
            sorting_ladder_reveal_step_count(item_count=4, round_number=1),
            7,
        )
        participant_source = (
            Path(__file__).resolve().parent.parent / 'templates' / 'sorting_ladder' / 'play.html'
        ).read_text(encoding='utf-8')
        sequence = [
            "if (upper) steps.push([upper]);",
            "if (lower) steps.push([lower]);",
            "document.querySelectorAll('#itemPool .answer-card').forEach(card => steps.push([card]));",
            "if (fixed) steps.push([fixed]);",
            "if (markerGroup.length) steps.push(markerGroup);",
        ]
        self.assertEqual([participant_source.index(value) for value in sequence], sorted(
            participant_source.index(value) for value in sequence
        ))
        self.assertIn("if (this.currentRound > 1) {", participant_source)
        self.assertIn("this.questionPhase !== 'answering_open'", participant_source)

        monitor_source = (
            Path(__file__).resolve().parent.parent
            / 'templates' / 'admin_dashboard' / 'sorting_ladder_monitor.html'
        ).read_text(encoding='utf-8')
        self.assertIn('LADDER UND ELEMENTE ENTHÜLLEN', monitor_source)
        self.assertIn('RUNDE FREIGEBEN', monitor_source)
        self.assertIn("'admin_reveal_question_content'", monitor_source)
        self.assertIn("'admin_open_answering'", monitor_source)


class SortingLadderTutorialRuntimeTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sorting-tutorial-user', password='pass')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Tutorial Quiz',
            creator=self.user,
            room_code='SLTUT',
            tutorial_enabled=True,
            tutorial_title='How it works',
            tutorial_text='Place the items in order.',
        )
        self.consumer = SortingLadderGameConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'sortingladder_{self.quiz.room_code}'
        self.consumer.channel_layer = DummyChannelLayer()
        self.consumer.channel_name = 'sorting-tutorial-channel'
        self.direct_messages = []

        async def _capture_send(*args, **kwargs):
            text_data = kwargs.get('text_data')
            if text_data is None and args:
                text_data = args[0]
            if text_data:
                self.direct_messages.append(json.loads(text_data))

        self.consumer.send = _capture_send

    def _create_question(self):
        question = SortingQuestion.objects.create(
            question_text='Sort these',
            description='Order the items.',
            upper_label='High',
            lower_label='Low',
            points=10,
            round_time_limit=30,
            created_by=self.user,
        )
        SortingItem.objects.create(topic=question, text='A', correct_rank=1)
        SortingItem.objects.create(topic=question, text='B', correct_rank=2)
        return question

    def _create_named_question(self, name):
        question = SortingQuestion.objects.create(
            question_text=name,
            description=f'Order {name}.',
            upper_label='High',
            lower_label='Low',
            points=10,
            round_time_limit=30,
            created_by=self.user,
        )
        items = [
            SortingItem.objects.create(
                topic=question,
                text=f'{name} {index}',
                correct_rank=index,
            )
            for index in range(1, 4)
        ]
        question.starting_item = items[1]
        question.save(update_fields=['starting_item'])
        return question, items

    @patch('sorting_ladder.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('sorting_ladder.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_admin_start_quiz_with_tutorial_sets_runtime_state_and_broadcasts_tutorial(self, _ready_mock, _activation_mock):
        async_to_sync(self.consumer.handle_admin_start_quiz)({'show_tutorial': True})

        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.tutorial_active)
        message_types = [message['type'] for _, message in self.consumer.channel_layer.sent]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)

    @patch('sorting_ladder.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('sorting_ladder.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_normal_start_clears_stale_unit_tutorial_request(self, _ready_mock, _activation_mock):
        tutorial_question = self._create_question()
        self.quiz.tutorial_question = tutorial_question
        self.quiz.save(update_fields=['tutorial_question'])
        hub_session = HubSession.objects.create(
            code='SLNORM',
            name='Sorting normal start',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        prepare_unit_tutorial_runtime(
            'sorting_ladder',
            self.quiz.room_code,
            hub_session.code,
            True,
        )

        async_to_sync(self.consumer.handle_admin_start_quiz)({
            'hub_session': hub_session.code,
            'show_tutorial': False,
            'play_tutorial': False,
        })

        state = get_unit_tutorial_state('sorting_ladder', self.quiz.room_code, hub_session.code)
        message_types = [message['type'] for _, message in self.consumer.channel_layer.sent]
        self.assertFalse(state['requested'])
        self.assertFalse(state['current_unit_is_tutorial'])
        self.assertNotIn('tutorial_start', message_types)

    @patch('sorting_ladder.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('sorting_ladder.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_normal_start_resets_stale_tutorial_round_without_starting_a_question(
        self,
        _ready_mock,
        _activation_mock,
    ):
        tutorial, tutorial_items = self._create_named_question('Tutorial T')
        regular_questions = [
            self._create_named_question(f'Regular {name}')[0]
            for name in ('A', 'B', 'C')
        ]
        self.quiz.tutorial_question = tutorial
        self.quiz.current_question = tutorial
        self.quiz.tutorial_active = True
        self.quiz.selected_questions.add(tutorial, *regular_questions)
        self.quiz.save(update_fields=['tutorial_question', 'current_question', 'tutorial_active'])

        participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='SLTARGET',
        )
        session = SortingLadderSession.objects.create(
            quiz=self.quiz,
            current_round=2,
            is_round_active=True,
            reveal_state=SortingLadderSession.REVEAL_REVEALED,
            round_start_time=timezone.now(),
            round_end_time=timezone.now() + timedelta(seconds=30),
            active_element=tutorial_items[2],
            shuffled_item_ids=','.join(str(item.id) for item in tutorial_items),
        )
        session.placed_elements.add(tutorial_items[0], tutorial_items[1])
        SortingPendingRoundSelection.objects.create(
            quiz=self.quiz,
            participant=participant,
            question=tutorial,
            round_number=2,
            ordered_item_ids=[tutorial_items[0].id, tutorial_items[1].id],
        )

        target_hub = HubSession.objects.create(
            code='SLTARGET',
            name='Sorting target session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=target_hub,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        decoy_hub = HubSession.objects.create(
            code='SLDECOY',
            name='Sorting decoy session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=decoy_hub,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        prepare_unit_tutorial_runtime('sorting_ladder', self.quiz.room_code, target_hub.code, True)
        prepare_unit_tutorial_runtime('sorting_ladder', self.quiz.room_code, decoy_hub.code, True)

        async_to_sync(self.consumer.handle_admin_start_quiz)({
            'hub_session': target_hub.code,
            'show_tutorial': False,
            'play_tutorial': False,
        })

        self.quiz.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.assertIsNone(self.quiz.current_question_id)
        self.assertFalse(self.quiz.tutorial_active)
        self.assertEqual(session.current_round, 0)
        self.assertFalse(session.is_round_active)
        self.assertEqual(session.reveal_state, SortingLadderSession.REVEAL_ACTIVE)
        self.assertIsNone(session.round_start_time)
        self.assertIsNone(session.round_end_time)
        self.assertIsNone(session.active_element_id)
        self.assertEqual(session.shuffled_item_ids, '')
        self.assertFalse(session.placed_elements.exists())
        self.assertFalse(SortingPendingRoundSelection.objects.filter(quiz=self.quiz).exists())
        target_state = get_unit_tutorial_state(
            'sorting_ladder', self.quiz.room_code, target_hub.code
        )
        decoy_state = get_unit_tutorial_state(
            'sorting_ladder', self.quiz.room_code, decoy_hub.code
        )
        self.assertFalse(target_state['requested'])
        self.assertTrue(decoy_state['requested'])
        snapshot = current_snapshot('sorting_ladder', self.quiz.room_code, target_hub.code)
        self.assertFalse(snapshot['question_phase'])
        self.assertFalse(snapshot['current_question_id'])
        self.assertIsNone(snapshot['answering_deadline_at'])
        message_types = [message['type'] for _, message in self.consumer.channel_layer.sent]
        self.assertIn('quiz_started', message_types)
        self.assertNotIn('question_started', message_types)
        self.assertNotIn('round_started', message_types)

    @patch('sorting_ladder.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('sorting_ladder.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_explicit_tutorial_start_keeps_tutorial_request_for_first_host_selected_question(
        self,
        _ready_mock,
        _activation_mock,
    ):
        tutorial, _items = self._create_named_question('Tutorial T')
        regular, _regular_items = self._create_named_question('Regular A')
        self.quiz.tutorial_question = tutorial
        self.quiz.selected_questions.add(tutorial, regular)
        self.quiz.save(update_fields=['tutorial_question'])
        hub_session = HubSession.objects.create(
            code='SLPLAYT',
            name='Sorting tutorial start',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )

        async_to_sync(self.consumer.handle_admin_start_quiz)({
            'hub_session': hub_session.code,
            'show_tutorial': False,
            'play_tutorial': True,
        })

        self.quiz.refresh_from_db()
        self.assertIsNone(self.quiz.current_question_id)
        tutorial_state = get_unit_tutorial_state(
            'sorting_ladder', self.quiz.room_code, hub_session.code
        )
        self.assertTrue(tutorial_state['requested'])
        tutorial_unit = start_unit_tutorial_if_needed(
            'sorting_ladder', self.quiz.room_code, hub_session.code
        )
        self.assertTrue(tutorial_unit['is_tutorial_round'])
        self.assertEqual(tutorial_unit['tutorial_question_id'], tutorial.id)

    def test_waiting_monitor_discards_stale_tutorial_question_and_lists_regular_questions(self):
        tutorial, tutorial_items = self._create_named_question('Tutorial T')
        regular_questions = [
            self._create_named_question(f'Regular {name}')[0]
            for name in ('A', 'B', 'C')
        ]
        self.quiz.tutorial_question = tutorial
        self.quiz.current_question = tutorial
        self.quiz.tutorial_active = True
        self.quiz.selected_questions.add(tutorial, *regular_questions)
        self.quiz.save(update_fields=['tutorial_question', 'current_question', 'tutorial_active'])
        session = SortingLadderSession.objects.create(
            quiz=self.quiz,
            current_round=1,
            is_round_active=True,
            active_element=tutorial_items[0],
            round_end_time=timezone.now() + timedelta(seconds=30),
        )
        session.placed_elements.add(tutorial_items[1])
        hub_session = HubSession.objects.create(
            code='SLMONI',
            name='Sorting monitor session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('admin_dashboard:sorting_ladder_monitor', args=[self.quiz.room_code]),
            {'hub_session': hub_session.code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="questionSelection"')
        self.assertNotContains(response, 'id="activeQuestion"')
        for question in regular_questions:
            self.assertContains(response, question.question_text)
        self.quiz.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, tutorial.id)
        self.assertTrue(self.quiz.tutorial_active)
        self.assertEqual(session.current_round, 1)
        self.assertTrue(session.is_round_active)

    def test_monitor_waits_for_authoritative_quiz_started_before_reloading(self):
        source = (
            Path(__file__).resolve().parent.parent
            / 'templates' / 'admin_dashboard' / 'sorting_ladder_monitor.html'
        ).read_text(encoding='utf-8')
        start_handler = source[source.index('startQuiz() {'):source.index('endQuiz() {')]
        started_handler = source[
            source.index("case 'quiz_started':"):source.index("case 'tutorial_start':")
        ]

        self.assertIn('hub_session: hub', start_handler)
        self.assertNotIn('location.reload()', start_handler)
        self.assertIn('location.reload()', started_handler)

    def test_starting_first_question_clears_tutorial_active(self):
        question = self._create_question()
        self.quiz.status = 'active'
        self.quiz.tutorial_active = True
        self.quiz.save(update_fields=['status', 'tutorial_active'])

        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': question.id})

        self.quiz.refresh_from_db()
        self.assertFalse(self.quiz.tutorial_active)

    def test_failed_first_question_start_preserves_tutorial_active(self):
        question = SortingQuestion.objects.create(
            question_text='Too short',
            description='Only one item',
            upper_label='High',
            lower_label='Low',
            points=10,
            round_time_limit=30,
            created_by=self.user,
        )
        SortingItem.objects.create(topic=question, text='Only', correct_rank=1)
        self.quiz.status = 'active'
        self.quiz.tutorial_active = True
        self.quiz.save(update_fields=['status', 'tutorial_active'])

        async_to_sync(self.consumer.handle_admin_send_question)({'question_id': question.id})

        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.tutorial_active)

    def test_failed_first_round_start_preserves_tutorial_active(self):
        self.quiz.status = 'active'
        self.quiz.tutorial_active = True
        self.quiz.save(update_fields=['status', 'tutorial_active'])

        async_to_sync(self.consumer.handle_admin_start_round)({})

        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.tutorial_active)

    def test_participant_rejoin_during_active_tutorial_receives_tutorial_start(self):
        hub_session = HubSession.objects.create(
            code='SLHUB1',
            name='Sorting Tutorial Rejoin',
            is_active=True,
            started_at=timezone.now(),
        )
        HubParticipant.objects.create(session=hub_session, nickname='Alice')
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=hub_session.code,
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.save(update_fields=['status'])
        activate_game_tutorial_runtime('sorting_ladder', self.quiz.room_code, hub_session.code, self.quiz, True)

        async_to_sync(self.consumer.handle_participant_join)({
            'name': participant.name,
            'hub_session_code': participant.hub_session_code,
        })

        message_types = [message['type'] for message in self.direct_messages]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)
