import json
import re
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubSession

from .consumers import SortingLadderGameConsumer
from .models import (
    RoundSubmission,
    SortingItem,
    SortingLadderGame,
    SortingLadderParticipant,
    SortingLadderSession,
    SortingQuestion,
)


class DummyChannelLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group_name, message):
        self.sent.append((group_name, message))


class SortingLadderPendingSelectionTest(TransactionTestCase):
    def setUp(self):
        SortingLadderGameConsumer._pending_round_orders = {}

        self.user = User.objects.create_user(username='sorting-user', password='pass')
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Test',
            creator=self.user,
            room_code='6789',
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
        self.assertEqual(round_result['round_number'], 1)
        self.assertTrue(round_result['is_correct'])
        self.assertFalse(round_result['is_eliminated'])
        self.assertTrue(round_result['has_more_rounds'])

        self.assertIsNotNone(round_started)
        self.assertEqual(round_started['round']['round_number'], 2)

        submission = RoundSubmission.objects.get(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
        )
        self.assertEqual(submission.all_elements, [small.id, medium.id])
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.rounds_survived, 1)
        self.assertFalse(self.participant.is_eliminated)

        session = self.quiz.session
        session.refresh_from_db()
        self.assertEqual(session.current_round, 2)
        self.assertTrue(session.is_round_active)
        self.assertEqual(SortingLadderGameConsumer._pending_round_orders, {})

    def test_host_early_end_question_evaluates_pending_final_round_selection(self):
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
        question_ended = next(
            (message for message in new_messages if message.get('type') == 'question_ended'),
            None,
        )

        self.assertIsNotNone(round_result)
        self.assertEqual(round_result['participant_name'], self.participant.name)
        self.assertEqual(round_result['round_number'], 1)
        self.assertTrue(round_result['is_correct'])
        self.assertFalse(round_result['is_eliminated'])
        self.assertFalse(round_result['has_more_rounds'])

        self.assertIsNotNone(question_ended)
        self.assertEqual(question_ended['correct_order_ids'], [earlier.id, later.id])

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
        self.assertIsNone(self.quiz.current_question_id)
        self.quiz.session.refresh_from_db()
        self.assertFalse(self.quiz.session.is_round_active)

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
        self.assertTrue(SortingLadderGameConsumer._pending_round_orders)

        payload = async_to_sync(self.consumer.initialize_question_for_quiz)(
            self.quiz.id,
            question_two.id,
            None,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload['question']['id'], question_two.id)
        self.assertEqual(SortingLadderGameConsumer._pending_round_orders, {})
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, question_two.id)

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
        self.assertEqual(result['correct_order_ids'], [small.id, medium.id, items[2].id])

        submission = RoundSubmission.objects.get(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
        )
        self.assertEqual(submission.all_elements, [medium.id, small.id])
        self.assertFalse(submission.is_correct)
        self.participant.refresh_from_db()
        self.assertTrue(self.participant.is_eliminated)

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


class SortingLadderRoundAnswerStatusScopeTest(TestCase):
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
        }])


class SortingLadderScoreBoxTests(TestCase):
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
            all_elements=ordered_ids[:2],
        )
        RoundSubmission.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
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


class SortingLadderTutorialRuntimeTests(TestCase):
    def setUp(self):
        SortingLadderGameConsumer._pending_round_orders = {}
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

    @patch('sorting_ladder.consumers.resolve_session_game_activation_for_room', return_value={'success': True})
    @patch('sorting_ladder.consumers.ensure_session_players_ready_for_game_start_for_room', return_value={'allowed': True})
    def test_admin_start_quiz_with_tutorial_sets_runtime_state_and_broadcasts_tutorial(self, _ready_mock, _activation_mock):
        async_to_sync(self.consumer.handle_admin_start_quiz)({'show_tutorial': True})

        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.tutorial_active)
        message_types = [message['type'] for _, message in self.consumer.channel_layer.sent]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)

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
        participant = SortingLadderParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='HUB1',
            is_active=True,
        )
        self.quiz.status = 'active'
        self.quiz.tutorial_active = True
        self.quiz.save(update_fields=['status', 'tutorial_active'])

        async_to_sync(self.consumer.handle_participant_join)({
            'name': participant.name,
            'hub_session_code': participant.hub_session_code,
        })

        message_types = [message['type'] for message in self.direct_messages]
        self.assertIn('quiz_started', message_types)
        self.assertIn('tutorial_start', message_types)
