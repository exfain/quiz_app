import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from games_hub.active_game_guard import resolve_session_game_activation
from games_hub.authoritative_state import (
    QUESTION_PRESENTATION_DELAY_MS,
    current_snapshot,
    finish_question_flow,
    get_question_flow_capabilities,
    present_question,
    reset_question_flow,
)
from games_hub.consumers import HubConsumer
from games_hub.lobby_return_flow import (
    get_session_lobby_presence,
    mark_single_participant_inactive_for_lobby_return,
)
from games_hub.models import GameRuntimeState, HubGameStep, HubParticipant, HubSession

from .consumers import WhoConsumer
from .models import WhoAnswer, WhoParticipant, WhoQuestion, WhoQuiz, WhoSession


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class WhoLyingScoringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-scoring')
        self.question = WhoQuestion.objects.create(
            statement='Ich spiele in der Nationalmannschaft.',
            points=10,
            time_limit=30,
            people=[
                {'name': 'Joshua Kimmich', 'is_lying': False},
                {'name': 'Kingsley Coman', 'is_lying': True},
                {'name': 'Oliver Baumann', 'is_lying': False},
            ],
            created_by=self.user,
        )

    def test_calculate_score_matches_plus_minus_zero_rules(self):
        self.assertEqual(self.question.calculate_score([1]), 1)
        self.assertEqual(self.question.calculate_score([0]), -1)
        self.assertEqual(self.question.calculate_score([]), 0)
        self.assertEqual(self.question.calculate_score([0, 1]), 0)

    def test_people_order_survives_a_process_hash_seed_change(self):
        with patch('builtins.hash', return_value=1):
            order_before_restart = self.question.get_randomized_people(room_code='7610')['people']
        with patch('builtins.hash', return_value=4):
            order_after_restart = self.question.get_randomized_people(room_code='7610')['people']

        self.assertEqual(order_after_restart, order_before_restart)

    def test_correct_identifications_count_includes_unselected_truth_tellers(self):
        quiz = WhoQuiz.objects.create(
            title='Who Accuracy',
            room_code='7611',
            creator=self.user,
            status='completed',
        )
        participant = WhoParticipant.objects.create(quiz=quiz, name='Ada')
        answer = WhoAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.question,
            selected_liars=[],
            time_taken=5.0,
        )

        self.assertEqual(answer.get_correct_identifications_count(), 2)
        self.assertEqual(answer.get_accuracy_percentage(), round((2 / 3) * 100, 1))

    def test_participant_total_score_can_be_negative_after_false_accusation(self):
        quiz = WhoQuiz.objects.create(
            title='Who Negative Score',
            room_code='7616',
            creator=self.user,
            status='completed',
        )
        participant = WhoParticipant.objects.create(quiz=quiz, name='Ada')
        answer = WhoAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.question,
            selected_liars=[0],
            time_taken=4.0,
        )

        participant.refresh_from_db()
        self.assertEqual(answer.points_earned, -1)
        self.assertEqual(participant.total_score, -1)

    def test_session_starts_people_timeline_only_when_answering_opens(self):
        quiz = WhoQuiz.objects.create(
            title='Who Session',
            room_code='7612',
            creator=self.user,
            status='active',
        )
        session = WhoSession.objects.create(quiz=quiz)

        session.send_question(self.question, time_per_person=12)
        session.refresh_from_db()

        self.assertFalse(session.is_question_active)
        self.assertIsNone(session.question_end_time)
        self.assertIsNone(session.quiz.question_start_time)
        self.assertEqual(session.current_question_number, 1)
        self.assertEqual(session.total_questions_sent, 1)

        started_at = timezone.now()
        session.open_answering(
            self.question,
            started_at=started_at,
            answer_duration_seconds=36,
        )
        session.refresh_from_db()
        session.quiz.refresh_from_db()
        duration = (session.question_end_time - started_at).total_seconds()

        self.assertTrue(session.is_question_active)
        self.assertEqual(session.quiz.question_start_time, started_at)
        self.assertAlmostEqual(duration, 36, delta=1)


class WhoLyingConsumerTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = WhoConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'who_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def reset_manual_flow(self, quiz, hub_session=None):
        return reset_question_flow(
            game_key='who',
            room_code=quiz.room_code,
            session_code=hub_session,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def phase_action(self, quiz, question, hub_session=None):
        snapshot = current_snapshot('who', quiz.room_code, hub_session)
        return {
            'question_id': question.id,
            'game_id': snapshot['game_id'],
            'state_revision': snapshot['state_revision'],
            'client_action_id': str(uuid.uuid4()),
        }

    def test_who_phase_path_skips_content_visible(self):
        capabilities = get_question_flow_capabilities('who')

        self.assertTrue(capabilities.uses_prompt_phase)
        self.assertFalse(capabilities.uses_content_phase)
        self.assertEqual(
            capabilities.initial_phase,
            GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
        )

    def test_admin_start_uses_requested_hub_session_for_activation_and_broadcast(self):
        user = User.objects.create_user(username='who-explicit-hub-start')
        quiz = WhoQuiz.objects.create(
            title='Who Explicit Hub',
            room_code='WHS1',
            creator=user,
            status='waiting',
        )
        requested_session = HubSession.objects.create(
            code='WHOHUB1',
            name='Requested Who session',
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        newer_session = HubSession.objects.create(
            code='WHOHUB2',
            name='Newer unrelated session',
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        for session, nickname in (
            (requested_session, 'Ada'),
            (newer_session, 'Bea'),
        ):
            HubParticipant.objects.create(
                session=session,
                nickname=nickname,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
            HubGameStep.objects.create(
                session=session,
                order=0,
                game_key='who',
                room_code=quiz.room_code,
                title=quiz.title,
            )
        HubParticipant.objects.create(
            session=requested_session,
            nickname='Cara',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )

        preflight = resolve_session_game_activation(
            requested_session.code,
            'who',
            quiz.room_code,
            check_only=True,
        )
        quiz.refresh_from_db()
        self.assertTrue(preflight['success'])
        self.assertEqual(quiz.status, 'waiting')

        consumer, sent_messages = self.make_consumer(quiz)
        async_to_sync(consumer.handle_admin_start_quiz)({
            'hub_session': requested_session.code,
        })

        quiz.refresh_from_db()
        self.assertEqual(quiz.status, 'active')
        hub_groups = [
            group
            for group, message in consumer.channel_layer.group_messages
            if message.get('type') == 'hub_event'
        ]
        self.assertEqual(hub_groups, [f'hub_{requested_session.code}'])
        self.assertNotIn(f'hub_{newer_session.code}', hub_groups)
        self.assertTrue(WhoParticipant.objects.filter(
            quiz=quiz,
            name='Ada',
            hub_session_code=requested_session.code,
        ).exists())
        self.assertTrue(WhoParticipant.objects.filter(
            quiz=quiz,
            name='Cara',
            hub_session_code=requested_session.code,
        ).exists())
        self.assertFalse(WhoParticipant.objects.filter(
            quiz=quiz,
            name='Bea',
            hub_session_code=newer_session.code,
        ).exists())

    def test_admin_start_restarts_waiting_game_with_started_at_from_previous_session(self):
        user = User.objects.create_user(username='who-reused-start')
        previous_started_at = timezone.now() - timezone.timedelta(hours=1)
        quiz = WhoQuiz.objects.create(
            title='Who Reused Start',
            room_code='WHS6',
            creator=user,
            status='waiting',
            started_at=previous_started_at,
        )
        hub_session = HubSession.objects.create(
            code='WHOHUB6',
            name='Who reused session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        participant = HubParticipant.objects.create(
            session=hub_session,
            nickname='Ada',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='who',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        consumer, _sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_start_quiz)({
            'hub_session': hub_session.code,
        })

        quiz.refresh_from_db()
        self.assertEqual(quiz.status, 'active')
        self.assertGreater(quiz.started_at, previous_started_at)
        self.assertGreaterEqual(quiz.started_at, hub_session.started_at)
        self.assertTrue(WhoParticipant.objects.filter(
            quiz=quiz,
            name=participant.nickname,
            hub_session_code=hub_session.code,
        ).exists())
        hub_started_events = [
            message
            for group, message in consumer.channel_layer.group_messages
            if group == f'hub_{hub_session.code}'
            and message.get('type') == 'hub_event'
            and message.get('event', {}).get('type') == 'quiz_started'
        ]
        self.assertEqual(len(hub_started_events), 1)

    def test_admin_start_is_idempotent(self):
        user = User.objects.create_user(username='who-idempotent-start')
        quiz = WhoQuiz.objects.create(
            title='Who Idempotent Start',
            room_code='WHS4',
            creator=user,
            status='waiting',
        )
        hub_session = HubSession.objects.create(
            code='WHOHUB4',
            name='Who idempotent session',
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        HubParticipant.objects.create(
            session=hub_session,
            nickname='Ada',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='who',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_start_quiz)({
            'hub_session': hub_session.code,
        })
        quiz.refresh_from_db()
        started_at = quiz.started_at
        participant = WhoParticipant.objects.get(
            quiz=quiz,
            name='Ada',
            hub_session_code=hub_session.code,
        )
        participant.is_active = True
        participant.save(update_fields=['is_active'])

        async_to_sync(consumer.handle_admin_start_quiz)({
            'hub_session': hub_session.code,
        })
        quiz.refresh_from_db()

        self.assertEqual(quiz.started_at, started_at)
        quiz_started_events = [
            message
            for _, message in consumer.channel_layer.group_messages
            if message.get('type') == 'quiz_started'
        ]
        hub_started_events = [
            message
            for _, message in consumer.channel_layer.group_messages
            if message.get('type') == 'hub_event'
            and message.get('event', {}).get('type') == 'quiz_started'
        ]
        self.assertEqual(len(quiz_started_events), 1)
        self.assertEqual(len(hub_started_events), 1)
        self.assertEqual(sent_messages[-1]['type'], 'quiz_started')
        self.assertIn('already started', sent_messages[-1]['message'])

    def test_admin_start_rejects_hub_session_not_linked_to_who_game(self):
        user = User.objects.create_user(username='who-invalid-hub-start')
        quiz = WhoQuiz.objects.create(
            title='Who Invalid Hub',
            room_code='WHS2',
            creator=user,
            status='waiting',
        )
        unrelated_session = HubSession.objects.create(
            code='WHOHUB3',
            name='Unrelated session',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_start_quiz)({
            'hub_session': unrelated_session.code,
        })

        quiz.refresh_from_db()
        self.assertEqual(quiz.status, 'waiting')
        self.assertEqual(sent_messages[-1]['type'], 'error')
        self.assertFalse(consumer.channel_layer.group_messages)

    def test_lobby_guard_failure_leaves_start_retryable(self):
        user = User.objects.create_user(username='who-start-retry')
        quiz = WhoQuiz.objects.create(
            title='Who Retry Start',
            room_code='WHS5',
            creator=user,
            status='waiting',
        )
        hub_session = HubSession.objects.create(
            code='WHOHUB5',
            name='Who retry session',
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        HubParticipant.objects.create(
            session=hub_session,
            nickname='Ada',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=hub_session,
            order=0,
            game_key='who',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        stale_participant = WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=hub_session.code,
            is_active=True,
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_start_quiz)({
            'hub_session': hub_session.code,
        })

        quiz.refresh_from_db()
        self.assertEqual(quiz.status, 'waiting')
        self.assertIsNone(quiz.started_at)
        self.assertEqual(sent_messages[-1]['type'], 'participants_not_in_lobby')

        stale_participant.is_active = False
        stale_participant.save(update_fields=['is_active'])
        async_to_sync(consumer.handle_admin_start_quiz)({
            'hub_session': hub_session.code,
        })

        quiz.refresh_from_db()
        self.assertEqual(quiz.status, 'active')
        self.assertIsNotNone(quiz.started_at)

    def test_admin_send_question_prepares_set_and_open_starts_total_duration(self):
        user = User.objects.create_user(username='who-consumer')
        question = WhoQuestion.objects.create(
            statement='Wer würde lügen?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Live',
            room_code='7613',
            creator=user,
            status='active',
        )
        WhoSession.objects.create(quiz=quiz)
        consumer, sent_messages = self.make_consumer(quiz)
        self.reset_manual_flow(quiz)

        send_action = {
            'question_id': question.id,
            'custom_time_limit': 15,
            **self.phase_action(quiz, question),
        }
        async_to_sync(consumer.handle_admin_send_question)(send_action)

        quiz.refresh_from_db()
        quiz.session.refresh_from_db()
        self.assertEqual(quiz.current_question_id, question.id)
        self.assertFalse(quiz.session.is_question_active)
        self.assertIsNone(quiz.question_start_time)
        self.assertIsNone(quiz.session.question_end_time)
        payload = consumer.channel_layer.group_messages[-1][1]['question']
        self.assertEqual(payload['time_limit'], 15)
        self.assertEqual(payload['time_per_person'], 15)
        self.assertEqual(payload['people'], [])
        self.assertEqual(payload['current_person_index'], 0)
        self.assertEqual(payload['current_person_time_left'], 15)
        self.assertIsNone(payload['question_started_at'])
        self.assertIsNone(payload['question_end_time'])
        self.assertIsNotNone(payload['server_now'])
        self.assertEqual(payload['question_phase'], 'prompt_visible')

        early_action = self.phase_action(quiz, question)
        async_to_sync(consumer.handle_admin_open_answering)(early_action)
        self.assertEqual(sent_messages[-1]['type'], 'action_rejected')
        self.assertEqual(sent_messages[-1]['code'], 'question_not_visible')

        prompt_snapshot = current_snapshot('who', quiz.room_code)
        presented_at = parse_datetime(prompt_snapshot['question_presented_at'])
        visible_at = parse_datetime(prompt_snapshot['question_visible_at'])
        self.assertEqual(
            visible_at,
            presented_at + timezone.timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS),
        )
        open_action = self.phase_action(quiz, question)
        decision = async_to_sync(consumer.open_who_answering)(
            quiz.id,
            question.id,
            None,
            open_action,
            at=visible_at,
        )
        self.assertTrue(decision.accepted)

        quiz.refresh_from_db()
        quiz.session.refresh_from_db()
        duration = (quiz.session.question_end_time - quiz.question_start_time).total_seconds()
        self.assertTrue(quiz.session.is_question_active)
        self.assertAlmostEqual(duration, 45, delta=0.01)
        open_payload = async_to_sync(consumer.get_current_question_data)()
        self.assertEqual(open_payload['question_phase'], 'answering_open')
        self.assertEqual(len(open_payload['people']), 3)
        self.assertEqual(open_payload['current_person_index'], 0)
        self.assertTrue(14 <= open_payload['current_person_time_left'] <= 15)
        self.assertIsNotNone(open_payload['question_started_at'])
        self.assertIsNotNone(open_payload['question_end_time'])

        original_deadline = quiz.session.question_end_time
        duplicate = async_to_sync(consumer.open_who_answering)(
            quiz.id,
            question.id,
            None,
            open_action,
            at=visible_at + timezone.timedelta(seconds=5),
        )
        quiz.session.refresh_from_db()
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(quiz.session.question_end_time, original_deadline)

    def test_participant_answer_is_blocked_before_set_start_and_allowed_afterward(self):
        user = User.objects.create_user(username='who-phase-answer-guard')
        question = WhoQuestion.objects.create(
            statement='Wer luegt in diesem Set?',
            points=10,
            time_limit=12,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who guarded set',
            room_code='WHG1',
            creator=user,
            status='active',
        )
        participant = WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='WHO_GUARD',
        )
        WhoSession.objects.create(quiz=quiz)
        consumer, _ = self.make_consumer(quiz)
        self.reset_manual_flow(quiz)

        presented_at = timezone.now() - timezone.timedelta(seconds=2)
        present_decision = async_to_sync(consumer.present_who_question)(
            quiz.id,
            question.id,
            None,
            self.phase_action(quiz, question),
            12,
            at=presented_at,
        )
        self.assertTrue(present_decision.accepted)
        randomized = question.get_randomized_people(room_code=quiz.room_code)
        liar_position = next(
            person['id']
            for person in randomized['people']
            if question.people[person['original_index']]['is_lying']
        )

        blocked = async_to_sync(consumer.save_participant_answer)(
            participant.name,
            participant.hub_session_code,
            [liar_position],
            0,
            question.id,
        )
        self.assertIsNone(blocked)
        self.assertFalse(WhoAnswer.objects.filter(participant=participant).exists())

        open_decision = async_to_sync(consumer.open_who_answering)(
            quiz.id,
            question.id,
            None,
            self.phase_action(quiz, question),
            at=presented_at + timezone.timedelta(
                milliseconds=QUESTION_PRESENTATION_DELAY_MS,
            ),
        )
        self.assertTrue(open_decision.accepted)
        accepted = async_to_sync(consumer.save_participant_answer)(
            participant.name,
            participant.hub_session_code,
            [liar_position],
            0,
            question.id,
        )
        self.assertIsNotNone(accepted)
        self.assertTrue(
            WhoAnswer.objects.filter(
                participant=participant,
                question=question,
            ).exists()
        )

    def test_rejoin_reconstructs_prompt_and_later_person_from_server_time(self):
        user = User.objects.create_user(username='who-phase-rejoin')
        question = WhoQuestion.objects.create(
            statement='Serverzeit bestimmt die Person.',
            points=10,
            time_limit=15,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': False},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who reconnect set',
            room_code='WHR1',
            creator=user,
            status='active',
        )
        participant = WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='WHO_REJOIN',
        )
        WhoSession.objects.create(quiz=quiz)
        consumer, sent_messages = self.make_consumer(quiz)
        self.reset_manual_flow(quiz)

        presented_at = timezone.now() - timezone.timedelta(seconds=17)
        present_decision = async_to_sync(consumer.present_who_question)(
            quiz.id,
            question.id,
            participant.hub_session_code,
            self.phase_action(quiz, question, participant.hub_session_code),
            15,
            at=presented_at,
        )
        self.assertTrue(present_decision.accepted)
        async_to_sync(consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })
        prompt_payload = next(
            message for message in sent_messages
            if message['type'] == 'question_started'
        )
        self.assertEqual(prompt_payload['question_phase'], 'prompt_visible')
        self.assertEqual(prompt_payload['question']['people'], [])
        self.assertIsNone(prompt_payload['question']['question_started_at'])
        self.assertIsNone(prompt_payload['question']['question_end_time'])

        sent_messages.clear()
        open_decision = async_to_sync(consumer.open_who_answering)(
            quiz.id,
            question.id,
            participant.hub_session_code,
            self.phase_action(quiz, question, participant.hub_session_code),
            at=presented_at + timezone.timedelta(seconds=1),
        )
        self.assertTrue(open_decision.accepted)
        async_to_sync(consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })
        answering_payload = next(
            message for message in sent_messages
            if message['type'] == 'question_answering_opened'
        )
        self.assertEqual(answering_payload['question_phase'], 'answering_open')
        self.assertEqual(len(answering_payload['question']['people']), 3)
        self.assertEqual(answering_payload['question']['current_person_index'], 1)
        self.assertTrue(
            13 <= answering_payload['question']['current_person_time_left'] <= 14
        )

    def test_save_participant_answer_returns_person_results_for_reveal(self):
        user = User.objects.create_user(username='who-reveal-consumer')
        question = WhoQuestion.objects.create(
            statement='Wer lügt wirklich?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': False},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Reveal',
            room_code='7617',
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=60),
        )
        WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB1',
        )
        consumer, _ = self.make_consumer(quiz)

        randomized = question.get_randomized_people(room_code=quiz.room_code)
        liar_displayed = next(
            person for person in randomized['people']
            if question.people[person['original_index']]['is_lying']
        )

        result = async_to_sync(consumer.save_participant_answer)(
            'Ada', 'HUB1', [liar_displayed['id']], 2.5, question.id
        )

        self.assertIn('person_results', result)
        self.assertEqual(
            [entry['name'] for entry in result['person_results']],
            [person['name'] for person in randomized['people']],
        )
        liar_result = next(entry for entry in result['person_results'] if entry['name'] == liar_displayed['name'])
        self.assertTrue(liar_result['is_lying'])
        self.assertTrue(liar_result['was_selected'])
        self.assertTrue(liar_result['was_correct'])
        self.assertEqual(liar_result['points_effect'], 1)
        truth_result = next(entry for entry in result['person_results'] if entry['name'] != liar_displayed['name'])
        self.assertFalse(truth_result['is_lying'])
        self.assertFalse(truth_result['was_selected'])
        self.assertTrue(truth_result['was_correct'])
        self.assertEqual(truth_result['points_effect'], 0)

    def test_answer_submitted_payload_includes_full_person_reveal_results(self):
        user = User.objects.create_user(username='who-reveal-payload')
        question = WhoQuestion.objects.create(
            statement='Wer luegt wirklich?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': False},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Reveal Payload',
            room_code='7619',
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=60),
        )
        WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB3',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        randomized = question.get_randomized_people(room_code=quiz.room_code)
        liar_displayed = next(
            person for person in randomized['people']
            if question.people[person['original_index']]['is_lying']
        )

        async_to_sync(consumer.handle_participant_submit_answer)({
            'participant_name': 'Ada',
            'hub_session': 'HUB3',
            'selected_liars': [liar_displayed['id']],
            'time_taken': 1.8,
            'question_id': question.id,
        })

        payload = next(message for message in sent_messages if message['type'] == 'answer_submitted')
        self.assertEqual(len(payload['person_results']), len(question.people))
        self.assertEqual(payload['question_id'], question.id)
        self.assertEqual(payload['question_number'], 1)
        self.assertEqual(payload['max_points'], question.get_total_possible_points())
        self.assertEqual(
            payload['progress_history'],
            [{
                'question_id': question.id,
                'question_number': 1,
                'points': payload['points_earned'],
                'max_points': question.get_total_possible_points(),
            }],
        )
        liar_result = next(entry for entry in payload['person_results'] if entry['name'] == liar_displayed['name'])
        self.assertTrue(liar_result['is_lying'])
        self.assertTrue(liar_result['was_correct'])
        truth_result = next(entry for entry in payload['person_results'] if entry['name'] != liar_displayed['name'])
        self.assertFalse(truth_result['is_lying'])
        self.assertTrue(truth_result['was_correct'])

    def test_participant_join_receives_current_question_number(self):
        user = User.objects.create_user(username='who-current-question-number')
        first_question = WhoQuestion.objects.create(
            statement='Erste Runde',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        current_question = WhoQuestion.objects.create(
            statement='Zweite Runde',
            points=10,
            time_limit=25,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dave', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Current Question',
            room_code='7620',
            creator=user,
            status='active',
            current_question=current_question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=31),
            question_order=[first_question.id, current_question.id],
        )
        quiz.selected_questions.add(first_question, current_question)
        WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB4',
        )
        WhoSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=19),
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_participant_join)({
            'participant_name': 'Ada',
            'hub_session': 'HUB4',
        })

        payload = next(message for message in sent_messages if message['type'] == 'question_started')
        self.assertEqual(payload['question']['question_number'], 2)
        self.assertEqual(payload['question']['total_sets'], 2)
        self.assertEqual(payload['question']['time_per_person'], 25)
        self.assertEqual(payload['question']['current_person_index'], 1)
        self.assertTrue(18 <= payload['question']['current_person_time_left'] <= 19)
        self.assertIsNotNone(payload['question']['question_started_at'])
        self.assertIsNotNone(payload['question']['question_end_time'])
        self.assertIsNotNone(payload['question']['server_now'])

    def test_admin_send_question_uses_actual_send_order_for_question_number(self):
        user = User.objects.create_user(username='who-send-order')
        first_question = WhoQuestion.objects.create(
            statement='First round',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        second_question = WhoQuestion.objects.create(
            statement='Second round',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=user,
        )
        third_question = WhoQuestion.objects.create(
            statement='Third round',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Eve', 'is_lying': False},
                {'name': 'Finn', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Send Order',
            room_code='7622',
            creator=user,
            status='active',
            question_order=[first_question.id, second_question.id, third_question.id],
        )
        quiz.selected_questions.set([first_question, second_question, third_question])
        WhoSession.objects.create(quiz=quiz)
        consumer, _ = self.make_consumer(quiz)
        self.reset_manual_flow(quiz)

        async_to_sync(consumer.handle_admin_send_question)(
            self.phase_action(quiz, first_question)
        )
        first_snapshot = current_snapshot('who', quiz.room_code)
        first_visible_at = parse_datetime(first_snapshot['question_visible_at'])
        async_to_sync(consumer.open_who_answering)(
            quiz.id,
            first_question.id,
            None,
            self.phase_action(quiz, first_question),
            at=first_visible_at,
        )
        finish_question_flow(
            game_key='who',
            room_code=quiz.room_code,
            session_code=None,
            question_id=first_question.id,
        )
        WhoSession.objects.get(quiz=quiz).end_current_question()

        async_to_sync(consumer.handle_admin_send_question)(
            self.phase_action(quiz, third_question)
        )

        question_payloads = [
            message
            for _group, message in consumer.channel_layer.group_messages
            if message.get('type') == 'question_started'
        ]
        self.assertEqual(len(question_payloads), 2)
        first_payload, second_payload = question_payloads
        self.assertEqual(first_payload['question']['question_number'], 1)
        self.assertEqual(second_payload['question']['question_number'], 2)
        self.assertEqual(second_payload['question']['people'], [])
        self.assertEqual(second_payload['question_phase'], 'prompt_visible')
        quiz.session.refresh_from_db()
        self.assertEqual(quiz.session.current_question_number, 2)
        self.assertFalse(quiz.session.is_question_active)
        self.assertIsNone(quiz.session.question_end_time)

        second_snapshot = current_snapshot('who', quiz.room_code)
        second_visible_at = parse_datetime(second_snapshot['question_visible_at'])
        second_open = async_to_sync(consumer.open_who_answering)(
            quiz.id,
            third_question.id,
            None,
            self.phase_action(quiz, third_question),
            at=second_visible_at,
        )
        self.assertTrue(second_open.accepted)
        quiz.refresh_from_db()
        quiz.session.refresh_from_db()
        self.assertTrue(quiz.session.is_question_active)
        self.assertEqual(
            (quiz.session.question_end_time - quiz.question_start_time).total_seconds(),
            40,
        )
        second_open_payload = async_to_sync(consumer.get_current_question_data)()
        self.assertEqual(second_open_payload['question_phase'], 'answering_open')
        self.assertEqual(second_open_payload['current_person_index'], 0)
        self.assertEqual(len(second_open_payload['people']), 2)

    def test_rejoin_after_set_end_receives_persisted_reveal(self):
        user = User.objects.create_user(username='who-rejoin-reveal')
        question = WhoQuestion.objects.create(
            statement='Wer luegt nach dem Rejoin?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Rejoin Reveal',
            room_code='7624',
            creator=user,
            status='active',
        )
        participant = WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB6',
        )
        WhoAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            selected_liars=[1],
            time_taken=2.0,
        )
        WhoSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            recently_ended_question=question,
            recently_ended_at=timezone.now() - timezone.timedelta(minutes=5),
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })

        reveal = next(message for message in sent_messages if message['type'] == 'set_revealed')
        self.assertEqual(reveal['phase'], 'revealed')
        self.assertEqual(reveal['question_id'], question.id)
        self.assertEqual(reveal['question_number'], 1)
        self.assertEqual(reveal['statement'], question.statement)
        self.assertTrue(reveal['answer_locked'])
        self.assertEqual(
            reveal['progress_history'],
            [{
                'question_id': question.id,
                'question_number': 1,
                'points': reveal['points_earned'],
                'max_points': question.get_total_possible_points(),
            }],
        )
        self.assertEqual(reveal['selected_liars_names'], ['Bob'])
        self.assertEqual(len(reveal['person_results']), 2)

    def test_answer_submitted_progress_history_preserves_zero_points(self):
        user = User.objects.create_user(username='who-zero-score-payload')
        question = WhoQuestion.objects.create(
            statement='Wer luegt ohne Auswahl?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Zero Score',
            room_code='7630',
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now(),
            question_order=[question.id],
        )
        quiz.selected_questions.add(question)
        WhoSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=60),
        )
        WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB_ZERO',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_participant_submit_answer)({
            'participant_name': 'Ada',
            'hub_session': 'HUB_ZERO',
            'selected_liars': [],
            'question_id': question.id,
        })

        payload = next(message for message in sent_messages if message['type'] == 'answer_submitted')
        self.assertEqual(payload['points_earned'], 0)
        self.assertEqual(payload['progress_history'][0]['points'], 0)

    def test_reveal_payload_contains_two_set_history_for_current_participant_only(self):
        user = User.objects.create_user(username='who-two-set-score-payload')
        first_question = WhoQuestion.objects.create(
            statement='Wer luegt im ersten Set?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        second_question = WhoQuestion.objects.create(
            statement='Wer luegt im zweiten Set?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Two Set Scores',
            room_code='7631',
            creator=user,
            status='active',
            question_order=[first_question.id, second_question.id],
        )
        quiz.selected_questions.add(first_question, second_question)
        ada = WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB_TWO',
        )
        bea = WhoParticipant.objects.create(
            quiz=quiz,
            name='Bea',
            hub_session_code='HUB_TWO',
        )
        WhoAnswer.objects.create(
            quiz=quiz,
            participant=ada,
            question=first_question,
            selected_liars=[1],
            time_taken=2.0,
        )
        WhoAnswer.objects.create(
            quiz=quiz,
            participant=ada,
            question=second_question,
            selected_liars=[0],
            time_taken=3.0,
        )
        WhoAnswer.objects.create(
            quiz=quiz,
            participant=bea,
            question=first_question,
            selected_liars=[],
            time_taken=2.5,
        )
        WhoAnswer.objects.create(
            quiz=quiz,
            participant=bea,
            question=second_question,
            selected_liars=[1],
            time_taken=3.5,
        )
        WhoSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            recently_ended_question=second_question,
            recently_ended_at=timezone.now(),
        )
        consumer, _ = self.make_consumer(quiz)

        ada_reveal = async_to_sync(consumer.get_recent_participant_reveal)(
            ada.name,
            ada.hub_session_code,
        )
        bea_reveal = async_to_sync(consumer.get_recent_participant_reveal)(
            bea.name,
            bea.hub_session_code,
        )

        self.assertEqual(
            [entry['points'] for entry in ada_reveal['progress_history']],
            [1, -1],
        )
        self.assertEqual(
            [entry['points'] for entry in bea_reveal['progress_history']],
            [0, 1],
        )
        ada.refresh_from_db()
        bea.refresh_from_db()
        self.assertEqual(ada.total_score, 0)
        self.assertEqual(bea.total_score, 1)

    def test_submit_after_host_end_is_rejected_without_hidden_grace_period(self):
        user = User.objects.create_user(username='who-ended-submit')
        question = WhoQuestion.objects.create(
            statement='Wer luegt nach dem Host-Ende?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Host End',
            room_code='7623',
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=5),
        )
        WhoSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=75),
        )
        participant = WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB6',
        )
        consumer, sent_messages = self.make_consumer(quiz)
        randomized = question.get_randomized_people(room_code=quiz.room_code)
        truth_displayed = next(
            person for person in randomized['people']
            if not question.people[person['original_index']]['is_lying']
        )

        async_to_sync(consumer.handle_admin_end_question)({})
        quiz.session.refresh_from_db()
        self.assertEqual(quiz.session.recently_ended_question_id, question.id)
        self.assertIsNotNone(quiz.session.recently_ended_at)
        async_to_sync(consumer.handle_participant_submit_answer)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
            'question_id': question.id,
            'selected_liars': [truth_displayed['id']],
            'time_taken': 3.6,
        })

        self.assertFalse(
            WhoAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).exists()
        )
        participant.refresh_from_db()
        quiz.refresh_from_db()

        self.assertIsNone(quiz.current_question_id)
        self.assertEqual(participant.total_score, 0)
        payload = next(message for message in sent_messages if message['type'] == 'answer_rejected')
        self.assertEqual(payload['question_id'], question.id)

    def test_admin_set_time_per_person_is_blocked_during_running_question(self):
        user = User.objects.create_user(username='who-time-locked')
        question = WhoQuestion.objects.create(
            statement='Zeit sperren',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Time Locked',
            room_code='7621',
            creator=user,
            status='active',
            current_question=question,
        )
        WhoSession.objects.create(quiz=quiz)
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_set_time_per_person)({
            'time_per_person': 25,
        })

        self.assertEqual(consumer.channel_layer.group_messages, [])
        self.assertEqual(sent_messages[-1]['type'], 'error')


class WhoLyingResultViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-result', password='secret')
        self.quiz = WhoQuiz.objects.create(
            title='Who Results',
            room_code='7614',
            creator=self.user,
            status='completed',
        )
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
        )
        self.question = WhoQuestion.objects.create(
            statement='Ich habe einen Oscar gewonnen.',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Robin', 'is_lying': False},
                {'name': 'Cristiano', 'is_lying': True},
            ],
            created_by=self.user,
        )

    def test_result_page_renders_negative_points_without_double_plus(self):
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            selected_liars=[0],
            time_taken=3.2,
        )

        response = self.client.get(
            reverse('who_is_lying:result', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '-1 pts')
        self.assertNotContains(response, '+-1 pts')


class WhoLyingMonitorViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='who-monitor',
            password='secret',
            is_staff=True,
        )
        self.quiz = WhoQuiz.objects.create(
            title='Who Monitor',
            room_code='7615',
            creator=self.user,
            status='active',
        )
        self.question = WhoQuestion.objects.create(
            statement='Wer lügt?',
            points=10,
            time_limit=18,
            people=[
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
                {'name': 'D', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.session = WhoSession.objects.create(quiz=self.quiz)
        self.session.send_question(self.question)
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            total_score=-2,
        )
        self.client.force_login(self.user)

    def test_monitor_uses_per_name_timer_and_marks_current_person(self):
        start_time = timezone.now() - timezone.timedelta(seconds=37)
        total_duration = self.question.get_total_duration_seconds()
        randomized_people = self.question.get_randomized_people(room_code=self.quiz.room_code)['people']
        expected_index = 2

        self.quiz.question_start_time = start_time
        self.quiz.save(update_fields=['question_start_time'])
        self.session.question_end_time = start_time + timezone.timedelta(seconds=total_duration)
        self.session.save(update_fields=['question_end_time'])

        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_time_per_person'], 18)
        self.assertEqual(response.context['current_person_index'], expected_index)
        self.assertEqual(response.context['current_person_name'], randomized_people[expected_index]['name'])
        self.assertAlmostEqual(response.context['current_question_time_left'], 17, delta=1)
        self.assertContains(response, 'id="questionTimeLeft"')
        self.assertContains(response, 'Current name')
        self.assertContains(response, 'id="currentPersonName"')
        self.assertContains(response, 'id="currentPersonProgress"')
        self.assertContains(response, f'data-current-person-index="{expected_index}"')
        self.assertContains(response, 'data-time-per-person="18"')
        self.assertContains(response, 'active-current')
        self.assertContains(response, randomized_people[expected_index]['name'])
        self.assertContains(response, 'updateCurrentPersonDisplay(index)')
        self.assertContains(response, 'data-question-started-at=')
        self.assertContains(response, 'data-server-now=')
        self.assertContains(response, 'getQuestionTimerState()')
        self.assertContains(response, "case 'time_per_person_updated':")
        self.assertEqual(response.context['total_sets'], 1)
        self.assertContains(response, 'Current Set 1/1')

    def test_monitor_start_waits_for_authoritative_who_broadcast(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code]),
            {'hub_session': 'WHOHUB1'},
        )
        markup = response.content.decode()
        start_handler = markup.index('startQuiz() {')
        end_handler = markup.index('endQuiz() {', start_handler)
        source = markup[start_handler:end_handler]

        self.assertIn('hub_session: hub', source)
        self.assertNotIn("type: 'navigate_direct'", source)
        self.assertNotIn('location.reload()', source)

        event_handler_start = markup.index("case 'quiz_started':")
        event_handler_end = markup.index("case 'tutorial_start':", event_handler_start)
        event_handler = markup[event_handler_start:event_handler_end]
        self.assertIn('location.reload()', event_handler)
        self.assertNotIn('updateQuizStatus', event_handler)

    def test_active_monitor_reload_renders_in_game_controls(self):
        self.session.end_current_question()
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="startQuizBtn"')
        self.assertContains(response, 'Send Set')
        self.assertContains(response, 'class="btn btn-secondary send-question-btn"', html=False)

    def test_prompt_phase_offers_only_set_start_without_person_timeline(self):
        reset_snapshot = reset_question_flow(
            game_key='who',
            room_code=self.quiz.room_code,
            session_code=None,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        decision = present_question(
            game_key='who',
            room_code=self.quiz.room_code,
            session_code=None,
            action={
                'question_id': self.question.id,
                'game_id': reset_snapshot['game_id'],
                'state_revision': reset_snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            answer_duration_seconds=self.question.get_total_duration_seconds(),
        )
        self.assertTrue(decision.accepted)

        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertFalse(response.context['question_answering_open'])
        self.assertIsNone(response.context['current_person_name'])
        self.assertContains(response, 'id="startSetBtn"')
        self.assertContains(response, 'SET STARTEN')
        self.assertNotContains(response, 'PERSONEN ANZEIGEN')
        self.assertContains(response, 'class="question-timer d-none" id="questionTimerWrapper"')

    def test_monitor_does_not_render_non_negative_score_clamp(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<input type="number" class="form-control" id="editScoreInput" step="1">', html=False)
        self.assertContains(response, 'Please enter a valid integer.')

    def test_monitor_does_not_render_points_per_id_badge(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'pts per ID')
        self.assertContains(response, 'people')

    def test_monitor_uses_actual_truth_status_for_current_question_people(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        current_people = response.context['current_question_people']
        self.assertTrue(any(person['is_lying'] for person in current_people))
        self.assertTrue(any(not person['is_lying'] for person in current_people))
        self.assertContains(response, '<span>Liar</span>', html=False)
        self.assertContains(response, '<span>Truth Teller</span>', html=False)

    def test_monitor_disables_live_time_controls_during_running_question(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="timePerPersonInput"')
        self.assertContains(response, 'id="updateTimePerPersonBtn"')
        self.assertContains(response, 'Während eines laufenden Sets gesperrt')
        self.assertContains(response, 'id="timePerPersonInput" value="18" style="max-width:120px;" disabled', html=False)
        self.assertContains(response, 'id="updateTimePerPersonBtn" disabled', html=False)

    def test_update_question_blocks_running_question_content_changes(self):
        original_statement = self.question.statement
        response = self.client.post(
            reverse('admin_dashboard:update_who_question'),
            data=json.dumps({
                'question_id': self.question.id,
                'statement': 'Geaendert',
                'time_limit': 99,
                'people': [
                    {'name': 'X', 'is_lying': False},
                    {'name': 'Y', 'is_lying': True},
                ],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.question.refresh_from_db()
        self.assertEqual(self.question.statement, original_statement)
        self.assertEqual(self.question.time_limit, 18)
        self.assertEqual(
            self.question.people,
            [
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
                {'name': 'D', 'is_lying': True},
            ],
        )

    def test_admin_score_endpoint_accepts_negative_scores(self):
        response = self.client.post(
            reverse('admin_dashboard:set_who_participant_score'),
            data=json.dumps({
                'participant_id': self.participant.id,
                'score': -3,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, -3)
        self.assertJSONEqual(response.content, {'success': True, 'new_score': -3})


class WhoLyingPlayViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-play', password='secret')
        self.quiz = WhoQuiz.objects.create(
            title='Who Play',
            room_code='7618',
            creator=self.user,
            status='active',
        )
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUB2',
        )

    def test_play_template_contains_set_reveal_state_markup(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="setRevealState"')
        self.assertContains(response, 'showRevealState()')
        self.assertContains(response, 'reveal-row reveal-row-results')
        self.assertContains(response, "typeof person.was_correct === 'boolean'")
        self.assertContains(response, 'Dein Urteil:')
        self.assertContains(response, 'richtig')
        self.assertContains(response, 'falsch')
        self.assertContains(response, '.selection-instructions {')
        self.assertContains(response, 'display: none;')
        self.assertContains(response, 'question_id: this.activeQuestionId')
        self.assertContains(response, 'acceptStateRevision(data)')
        self.assertContains(response, 'revision < this.stateRevision')
        self.assertContains(response, 'lügt nicht')
        self.assertContains(response, 'gedrückt')
        self.assertContains(response, 'Set-Auflösung')
        self.assertContains(response, 'class="who-reveal-vhs"')
        self.assertContains(response, 'DIE RICHTIGE ANTWORT IST')
        self.assertContains(response, 'GEGEBENE ANTWORT')
        self.assertContains(response, 'BEHAUPTUNG')
        self.assertContains(response, 'id="whoRevealCorrectValue"')
        self.assertContains(response, 'id="whoRevealGivenValue"')
        self.assertContains(response, 'id="whoRevealPromptValue"')
        self.assertContains(response, 'id="totalSetCount"')
        self.assertContains(response, "currentSetNumber.textContent = String(this.currentQuestionIndex)")
        self.assertContains(response, "totalSetCount.textContent = String(Number(question.total_sets))")
        self.assertContains(response, "if (!this.currentSetResult || !this.questionHasEnded) return;")
        self.assertContains(response, "return value ? 'Stimmt' : 'Stimmt nicht';")

        markup = response.content.decode('utf-8')
        self.assertEqual(markup.count('WhoPlayer.prototype.showRevealState = function()'), 1)
        self.assertEqual(markup.count('class="who-reveal-vhs"'), 1)
        self.assertNotIn('showLegacyRevealState', markup)
        reveal_handler_start = markup.index('onSetRevealed(data) {')
        reveal_handler_end = markup.index('onQuizEnded(data) {', reveal_handler_start)
        self.assertIn(
            'this.syncAuthoritativeScoreHistory(data);',
            markup[reveal_handler_start:reveal_handler_end],
        )
        submitted_handler_start = markup.index('onAnswerSubmitted(data) {')
        submitted_handler_end = markup.index('isVhsTheme() {', submitted_handler_start)
        self.assertIn(
            'this.syncAuthoritativeScoreHistory(data)',
            markup[submitted_handler_start:submitted_handler_end],
        )

        vhs_css = (Path(__file__).resolve().parent.parent / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        self.assertIn('#setRevealState .who-reveal-default', vhs_css)
        self.assertIn('#setRevealState .who-reveal-vhs__correct', vhs_css)
        self.assertIn('color: #e9dfca;', vhs_css)

    def test_play_template_contains_vhs_current_person_layout(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="selection-summary who-person-position"')
        self.assertContains(response, 'id="personSwitchTimer"')
        self.assertContains(response, 'class="who-person-switch-timer__fill"')
        self.assertContains(response, 'class="who-person-actions"')
        self.assertContains(response, 'vhs-who-liar-button')
        self.assertContains(response, 'id="personProgressDefault"')
        self.assertContains(response, 'id="personProgressVhs"')
        self.assertContains(response, 'progressVhsEl.textContent = `${this.currentPersonPosition + 1} von ${this.peopleSequence.length}`')
        self.assertContains(response, '1 - (remaining / duration)')
        self.assertContains(response, '--who-person-progress')
        self.assertContains(response, "accuseBtn.classList.toggle('is-selected', isSelected)")
        self.assertContains(response, "accuseBtn.setAttribute('aria-pressed', isSelected ? 'true' : 'false')")

        markup = response.content.decode('utf-8')
        card_start = markup.index('id="currentPersonCard"')
        name_panel_start = markup.index('class="who-person-name-panel"', card_start)
        actions_start = markup.index('class="who-person-actions"', name_panel_start)
        button_position = markup.index('id="accuseLiarBtn"')
        self.assertGreater(button_position, actions_start)
        self.assertNotIn('id="accuseLiarBtn"', markup[name_panel_start:actions_start])

        vhs_css = (Path(__file__).resolve().parent.parent / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        self.assertIn('body.who-play-page .vhs-theme-shell #questionState .statement-text', vhs_css)
        self.assertIn('background: transparent !important;', vhs_css)
        self.assertIn('transform: scaleX(var(--who-person-progress, 0));', vhs_css)
        self.assertIn('transform-origin: left center;', vhs_css)
        self.assertIn('.vhs-who-liar-button:not(:disabled):hover', vhs_css)
        self.assertIn('.vhs-who-liar-button.is-selected', vhs_css)

    def test_vhs_set_timer_and_set_end_summary_use_who_specific_sources(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="playerSetTimeLeft"')
        self.assertContains(response, 'data-vhs-timer-key=""')
        self.assertContains(response, 'data-vhs-timer-maximum=""')
        self.assertContains(response, 'const endsAtMs = this.parseServerTimestamp(question?.question_end_time);')
        self.assertContains(response, 'const totalTimerElement = document.getElementById(\'playerSetTimeLeft\');')
        self.assertContains(response, 'const initialSetDuration = Math.max(1, peopleCount * this.currentTimePerPerson);')
        self.assertContains(response, "const setTimerKey = `${question?.id || this.currentQuestionIndex || 'active'}:${question?.question_started_at || ''}`;")
        self.assertContains(response, 'totalTimerElement.dataset.vhsTimerKey = setTimerKey;')
        self.assertContains(response, 'totalTimerElement.dataset.vhsTimerMaximum = String(initialSetDuration);')
        self.assertContains(response, 'totalTimerElement.textContent = timerState.totalTimeLeft')
        self.assertContains(response, 'personSwitchTimer.style.setProperty(\'--who-person-progress\', progress)')
        self.assertContains(response, 'class="submitted-details who-set-summary"')
        self.assertContains(response, 'class="who-set-ended-title__vhs">SET BEENDET</span>')
        self.assertContains(response, 'class="who-set-ended-waiting__vhs">Warte auf das n&auml;chste Set...</span>')
        self.assertContains(response, 'NICHT ERKANNTE L&Uuml;GNER')
        self.assertContains(response, 'FALSCHE BESCHULDIGUNGEN')
        self.assertContains(response, 'id="whoSetEvaluatingState"')
        self.assertContains(response, 'SET WIRD AUSGEWERTET...')
        self.assertContains(response, 'data-who-set-result-ready="false"')
        self.assertContains(response, "this.showState('whoSetEvaluatingState')")
        self.assertContains(response, 'this.renderSetResult(data);')
        self.assertContains(response, 'this.setSetResultReady(true);')

        markup = response.content.decode('utf-8')
        self.assertEqual(markup.count('id="whoSetEvaluatingState"'), 1)
        self.assertEqual(markup.count('id="answerSubmittedState"'), 1)
        self.assertEqual(markup.count('this.questionTimer = setInterval(renderTimerState, 250);'), 1)
        submitted_handler = markup.index('onAnswerSubmitted(data) {')
        render_call = markup.index('this.renderSetResult(data);', submitted_handler)
        final_show = markup.index('this.showRevealState();', render_call)
        render_start = markup.index('renderSetResult(data) {')
        result_ready = markup.index('this.setSetResultReady(true);', render_start)
        state_switch = markup.index('showState(stateId) {')
        hide_all_states = markup.index("state.classList.add('d-none');", state_switch)
        show_target_state = markup.index("targetState.classList.remove('d-none');", hide_all_states)
        self.assertLess(render_call, final_show)
        self.assertLess(render_start, result_ready)
        self.assertLess(hide_all_states, show_target_state)

        project_root = Path(__file__).resolve().parent.parent
        vhs_css = (project_root / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        accessibility = (project_root / 'templates' / 'includes' / 'accessibility_widget.html').read_text(encoding='utf-8')
        self.assertIn('body.who-play-page .vhs-theme-shell #answerSubmittedState .who-set-summary', vhs_css)
        self.assertIn('.who-set-ended-waiting {', vhs_css)
        self.assertIn('color: #9ba19c !important;', vhs_css)
        self.assertIn('.who-set-summary__group-values:empty::before', vhs_css)
        self.assertIn('content: "Keine";', vhs_css)
        self.assertIn('#answerSubmittedState:not([data-who-set-result-ready="true"])', vhs_css)
        self.assertIn('.who-set-evaluating-panel', vhs_css)
        self.assertIn("if (document.body.classList.contains('who-play-page')) return;", accessibility)
        self.assertIn("? document.getElementById('playerSetTimeLeft')", accessibility)
        self.assertIn("timerSource.dataset.vhsTimerMaximum", accessibility)
        self.assertIn("timerSource.dataset.vhsTimerKey", accessibility)
        self.assertIn("timerValue / vhsTimerMaximum", accessibility)
        self.assertIn("timerFill.style.transition = 'none';", accessibility)
        self.assertIn("timerFill.style.removeProperty('transition');", accessibility)

    def test_vhs_set_end_shows_the_authoritative_reveal(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        markup = response.content.decode('utf-8')

        question_ended_start = markup.index('onQuestionEnded() {')
        question_ended_end = markup.index('onQuizEnded(data) {', question_ended_start)
        question_ended_handler = markup[question_ended_start:question_ended_end]
        self.assertIn("if (this.isVhsTheme()) {", question_ended_handler)
        self.assertIn('this.showVhsSetEndedState();', question_ended_handler)
        self.assertLess(
            question_ended_handler.index('this.showVhsSetEndedState();'),
            question_ended_handler.index('this.showRevealState();'),
        )
        self.assertIn('return;', question_ended_handler)

        answer_submitted_start = markup.index('onAnswerSubmitted(data) {')
        answer_submitted_end = markup.index('isVhsTheme() {', answer_submitted_start)
        answer_submitted_handler = markup[answer_submitted_start:answer_submitted_end]
        self.assertIn("if (this.isVhsTheme()) {", answer_submitted_handler)
        self.assertIn('this.showVhsSetEndedState();', answer_submitted_handler)
        self.assertIn('} else if (this.questionHasEnded) {', answer_submitted_handler)

        set_ended_start = markup.index('showVhsSetEndedState() {')
        set_ended_end = markup.index('renderSetResult(data) {', set_ended_start)
        set_ended_handler = markup[set_ended_start:set_ended_end]
        self.assertIn('if (!this.currentSetResult) return;', set_ended_handler)
        self.assertIn('this.showRevealState();', set_ended_handler)
        self.assertNotIn("this.showState('answerSubmittedState');", set_ended_handler)
        self.assertIn("case 'set_revealed':", markup)
        self.assertIn('this.onSetRevealed(data);', markup)

        self.assertIn("this.showState('questionState');", markup)
        self.assertIn("this.showState('quizEndedState');", markup)
        self.assertEqual(markup.count('id="answerSubmittedState"'), 1)
        self.assertEqual(markup.count('id="setRevealState"'), 1)

    def test_play_view_embeds_server_timer_sync_fields_for_active_question(self):
        question = WhoQuestion.objects.create(
            statement='Aktive Runde',
            points=10,
            time_limit=18,
            people=[
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
            ],
            created_by=self.user,
        )
        start_time = timezone.now() - timezone.timedelta(seconds=19)
        self.quiz.current_question = question
        self.quiz.question_start_time = start_time
        self.quiz.save(update_fields=['current_question', 'question_start_time'])
        session = WhoSession.objects.create(quiz=self.quiz)
        session.question_end_time = start_time + timezone.timedelta(seconds=54)
        session.save(update_fields=['question_end_time'])

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_timer_state']['time_per_person'], 18)
        self.assertEqual(response.context['current_question_timer_state']['current_person_index'], 1)
        self.assertTrue(16 <= response.context['current_question_timer_state']['current_person_time_left'] <= 17)
        self.assertContains(response, 'question_started_at')
        self.assertContains(response, 'current_person_time_left')
        self.assertContains(response, 'server_now')
        self.assertContains(response, 'getQuestionTimerState(question)')
        self.assertContains(response, 'this.applyQuestionState(activeQuestion);')
        self.assertContains(response, "if (this.questionPhase === 'answering_open')")
        self.assertContains(response, 'this.startQuestionTimer(question);')


class WhoLyingHubParticipantLifecycleTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(username='who-hub-lifecycle')
        self.session = HubSession.objects.create(
            code='GI4XUE',
            name='Who lifecycle',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        self.hub_participants = [
            HubParticipant.objects.create(
                session=self.session,
                nickname='jkh',
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            ),
            HubParticipant.objects.create(
                session=self.session,
                nickname='Ada',
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            ),
        ]
        self.games = []
        self.steps = []
        for order, room_code in enumerate(('W123', 'W124', 'W125')):
            game = WhoQuiz.objects.create(
                creator=self.user,
                title=f'Who Lock Smoke {order + 1}',
                room_code=room_code,
                status='waiting',
            )
            step = HubGameStep.objects.create(
                session=self.session,
                order=order,
                game_key='who',
                room_code=room_code,
                title=game.title,
            )
            self.games.append(game)
            self.steps.append(step)

    def activate(self, index, action=None):
        result = resolve_session_game_activation(
            self.session.code,
            'who',
            self.games[index].room_code,
            action=action,
        )
        self.assertTrue(result['success'], result)
        return result

    def play_params(self, index, **overrides):
        params = {
            'hub_session': self.session.code,
            'game_start_intro': '1',
            'game_start_key': 'who',
            'game_start_room': self.games[index].room_code,
            'game_start_nonce': str(1784646760729 + index),
            'game_start_order': str(self.steps[index].order + 1),
            'game_start_title': self.games[index].title,
        }
        params.update(overrides)
        return params

    def open_play(self, index, nickname='jkh', **overrides):
        return self.client.get(
            reverse('who_is_lying:play', args=[self.games[index].room_code, nickname]),
            self.play_params(index, **overrides),
        )

    def test_three_consecutive_who_games_provision_current_participants(self):
        for index in range(3):
            self.activate(index, action='end' if index else None)

            response = self.open_play(index)

            self.assertEqual(response.status_code, 200)
            participant = WhoParticipant.objects.get(
                quiz=self.games[index],
                name='jkh',
                hub_session_code=self.session.code,
            )
            self.assertEqual(participant.quiz_id, self.games[index].id)
            self.assertEqual(
                WhoParticipant.objects.filter(
                    quiz=self.games[index],
                    hub_session_code=self.session.code,
                ).count(),
                2,
            )
            self.session.refresh_from_db()
            self.assertEqual(self.session.current_step_index, index)

        self.assertEqual(
            WhoParticipant.objects.filter(hub_session_code=self.session.code).count(),
            6,
        )

    def test_activation_provisions_bindings_without_moving_players_out_of_lobby(self):
        self.activate(0)

        presence = get_session_lobby_presence(self.session.code)
        participants = WhoParticipant.objects.filter(
            quiz=self.games[0],
            hub_session_code=self.session.code,
        )

        self.assertEqual(participants.count(), 2)
        self.assertFalse(participants.filter(is_active=True).exists())
        self.assertTrue(presence['all_in_lobby'])
        self.assertEqual(presence['not_in_lobby_count'], 0)

        join_response = self.client.post(
            reverse('who_is_lying:join'),
            json.dumps({
                'room_code': self.games[0].room_code,
                'participant_name': 'jkh',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )

        self.assertEqual(join_response.status_code, 200)
        self.assertTrue(join_response.json()['success'])
        self.assertTrue(participants.get(name='jkh').is_active)
        self.assertFalse(participants.get(name='Ada').is_active)

        second_join_response = self.client.post(
            reverse('who_is_lying:join'),
            json.dumps({
                'room_code': self.games[0].room_code,
                'participant_name': 'Ada',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )

        self.assertEqual(second_join_response.status_code, 200)
        self.assertTrue(second_join_response.json()['success'])
        self.assertEqual(participants.filter(is_active=True).count(), 2)
        self.assertEqual(participants.count(), 2)

    def test_lobby_disconnect_and_game_connect_order_cannot_overwrite_presence(self):
        self.activate(0)
        participant = WhoParticipant.objects.get(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        )
        hub_consumer = HubConsumer()
        hub_consumer.session_code = self.session.code
        hub_consumer.group_name = f'hub_{self.session.code}'
        hub_consumer.channel_name = 'hub-old-channel'
        hub_consumer.hub_participant_id = self.hub_participants[0].id
        hub_consumer.channel_layer = SimpleNamespace(group_discard=AsyncMock())

        async_to_sync(hub_consumer.disconnect)(1000)
        participant.refresh_from_db()
        self.assertFalse(participant.is_active)

        game_consumer = WhoConsumer()
        game_consumer.room_code = self.games[0].room_code
        game_consumer.room_group_name = f'who_{self.games[0].room_code}'
        game_consumer.who_participant_id = None
        game_consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
        game_consumer.send = AsyncMock()
        async_to_sync(game_consumer.handle_participant_join)({
            'participant_name': 'jkh',
            'hub_session': self.session.code,
        })

        async_to_sync(hub_consumer.disconnect)(1000)
        participant.refresh_from_db()
        self.assertTrue(participant.is_active)
        self.assertEqual(game_consumer.who_participant_id, participant.id)

    def test_reload_and_rejoin_in_third_game_use_current_server_step(self):
        for index in range(3):
            self.activate(index, action='end' if index else None)

        initial_response = self.open_play(2)
        reload_response = self.client.get(
            reverse('who_is_lying:play', args=[self.games[2].room_code, 'jkh']),
            {'hub_session': self.session.code},
        )
        participant = WhoParticipant.objects.get(
            quiz=self.games[2],
            name='jkh',
            hub_session_code=self.session.code,
        )
        participant.is_active = False
        participant.save(update_fields=['is_active'])
        rejoin_response = self.open_play(
            2,
            game_start_nonce=self.play_params(0)['game_start_nonce'],
            game_start_order=self.play_params(0)['game_start_order'],
        )

        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(reload_response.status_code, 200)
        self.assertEqual(rejoin_response.status_code, 200)
        participant.refresh_from_db()
        self.assertTrue(participant.is_active)
        self.assertEqual(
            WhoParticipant.objects.filter(
                quiz=self.games[2],
                name='jkh',
                hub_session_code=self.session.code,
            ).count(),
            1,
        )

    def test_duplicate_activation_join_and_rejoin_are_idempotent(self):
        self.activate(0)
        self.activate(0)

        join_url = reverse('who_is_lying:join')
        payload = {
            'room_code': self.games[0].room_code,
            'participant_name': 'jkh',
            'hub_session': self.session.code,
        }
        first_join = self.client.post(join_url, json.dumps(payload), content_type='application/json')
        second_join = self.client.post(join_url, json.dumps(payload), content_type='application/json')
        first_play = self.open_play(0)
        second_play = self.open_play(0)

        self.assertEqual(first_join.status_code, 200)
        self.assertEqual(second_join.status_code, 200)
        self.assertEqual(first_play.status_code, 200)
        self.assertEqual(second_play.status_code, 200)
        self.assertEqual(
            WhoParticipant.objects.filter(
                quiz=self.games[0],
                name='jkh',
                hub_session_code=self.session.code,
            ).count(),
            1,
        )

    def test_play_recovers_missing_binding_only_for_current_authorized_hub_identity(self):
        self.activate(0)
        WhoParticipant.objects.filter(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        ).delete()

        response = self.open_play(0)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WhoParticipant.objects.filter(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        ).exists())

    def test_stale_or_manipulated_start_context_cannot_create_participant(self):
        self.activate(0)
        self.activate(1, action='end')
        current_count = WhoParticipant.objects.filter(quiz=self.games[1]).count()

        stale_response = self.open_play(0)
        stale_rejoin_response = self.client.get(
            reverse('who_is_lying:play', args=[self.games[0].room_code, 'jkh']),
            {'hub_session': self.session.code},
        )
        unknown_name_response = self.open_play(1, nickname='Mallory')
        invalid_session_response = self.client.get(
            reverse('who_is_lying:play', args=[self.games[1].room_code, 'jkh']),
            self.play_params(1, hub_session='INVALID'),
        )

        self.assertEqual(stale_response.status_code, 302)
        self.assertIn(reverse('games_hub:lobby', args=[self.session.code]), stale_response.url)
        self.assertIn('who_join_error=', stale_response.url)
        self.assertEqual(stale_rejoin_response.status_code, 302)
        self.assertEqual(unknown_name_response.status_code, 302)
        self.assertEqual(invalid_session_response.status_code, 302)
        self.assertFalse(WhoParticipant.objects.filter(quiz=self.games[1], name='Mallory').exists())
        self.assertFalse(WhoParticipant.objects.filter(
            quiz=self.games[1],
            name='jkh',
            hub_session_code='INVALID',
        ).exists())
        self.assertEqual(WhoParticipant.objects.filter(quiz=self.games[1]).count(), current_count)

    def test_join_rejects_untrusted_hub_session_and_accepts_alphanumeric_room(self):
        self.activate(2)
        WhoParticipant.objects.filter(
            quiz=self.games[2],
            name='jkh',
            hub_session_code=self.session.code,
        ).delete()
        join_url = reverse('who_is_lying:join')

        valid_response = self.client.post(
            join_url,
            json.dumps({
                'room_code': 'W125',
                'participant_name': 'jkh',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )
        invalid_response = self.client.post(
            join_url,
            json.dumps({
                'room_code': 'W125',
                'participant_name': 'Mallory',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )

        self.assertEqual(valid_response.status_code, 200)
        self.assertTrue(valid_response.json()['success'])
        self.assertEqual(invalid_response.status_code, 403)
        self.assertFalse(invalid_response.json()['success'])
        self.assertFalse(WhoParticipant.objects.filter(quiz=self.games[2], name='Mallory').exists())

    def test_participants_and_sessions_remain_isolated_and_cleanup_is_room_scoped(self):
        self.activate(0)
        self.activate(1, action='end')
        self.open_play(1, nickname='jkh')
        mark_single_participant_inactive_for_lobby_return(
            self.session.code,
            'who',
            self.games[0].room_code,
            'jkh',
        )

        previous = WhoParticipant.objects.get(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        )
        current = WhoParticipant.objects.get(
            quiz=self.games[1],
            name='jkh',
            hub_session_code=self.session.code,
        )
        ada = WhoParticipant.objects.get(
            quiz=self.games[1],
            name='Ada',
            hub_session_code=self.session.code,
        )
        self.assertFalse(previous.is_active)
        self.assertTrue(current.is_active)
        self.assertFalse(ada.is_active)

        other_session = HubSession.objects.create(
            code='OTHER1',
            name='Other session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        HubParticipant.objects.create(
            session=other_session,
            nickname='jkh',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        other_game = WhoQuiz.objects.create(
            creator=self.user,
            title='Other Who',
            room_code='W126',
            status='waiting',
        )
        HubGameStep.objects.create(
            session=other_session,
            order=0,
            game_key='who',
            room_code=other_game.room_code,
            title=other_game.title,
        )

        result = resolve_session_game_activation(other_session.code, 'who', other_game.room_code)

        self.assertTrue(result['success'])
        self.assertTrue(WhoParticipant.objects.filter(
            quiz=other_game,
            name='jkh',
            hub_session_code=other_session.code,
        ).exists())
        self.assertEqual(
            WhoParticipant.objects.filter(name='jkh', hub_session_code=self.session.code).count(),
            2,
        )

    def test_lobby_does_not_redirect_who_after_failed_join(self):
        response = self.client.get(
            reverse('games_hub:lobby', args=[self.session.code]),
            {'nickname': 'jkh'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "step.game_key === 'who'")
        self.assertContains(response, 'Who join failed; keeping participant in the hub lobby.')

class WhoLyingScoreBoxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-score-box', password='secret')
        self.quiz = WhoQuiz.objects.create(
            title='Who Score Box',
            room_code='7621',
            creator=self.user,
            status='active',
        )
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUB5',
        )

    def test_play_view_hydrates_score_box_with_negative_history(self):
        first_question = WhoQuestion.objects.create(
            statement='Runde 1',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': True},
            ],
            created_by=self.user,
        )
        current_question = WhoQuestion.objects.create(
            statement='Runde 2',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Dan', 'is_lying': False},
                {'name': 'Eve', 'is_lying': True},
            ],
            created_by=self.user,
        )
        upcoming_question = WhoQuestion.objects.create(
            statement='Runde 3',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Finn', 'is_lying': False},
                {'name': 'Gina', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.quiz.question_order = [first_question.id, current_question.id, upcoming_question.id]
        self.quiz.current_question = current_question
        self.quiz.save(update_fields=['question_order', 'current_question'])
        self.quiz.selected_questions.add(first_question, current_question, upcoming_question)
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=first_question,
            selected_liars=[0],
            time_taken=2.0,
        )

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertEqual(
            response.context['initial_progress_history'],
            [{
                'question_id': first_question.id,
                'question_number': 1,
                'points': -1,
                'max_points': 2,
            }],
        )
        self.assertEqual(
            response.context['question_scoreboard'][0],
            {
                'id': first_question.id,
                'number': 1,
                'earned_points': -1,
                'max_points': 2,
                'status': 'played',
            },
        )
        self.assertEqual(response.context['question_scoreboard'][1]['status'], 'current')
        self.assertIsNone(response.context['question_scoreboard'][1]['max_points'])
        self.assertEqual(response.context['question_scoreboard'][2]['status'], 'upcoming')
        self.assertIsNone(response.context['question_scoreboard'][2]['max_points'])
        self.assertContains(response, 'score-box__row')
        self.assertContains(response, 'scoreHistoryTotal')
        self.assertContains(response, 'whoQuestionScoreboardData')
        self.assertContains(response, 'whoInitialProgressData')
        self.assertContains(response, 'whoCurrentQuestionNumber')
        self.assertContains(response, 'syncAuthoritativeScoreHistory(data)')
        self.assertContains(response, 'Array.isArray(data?.progress_history)')
        self.assertContains(response, 'const hasMaxPoints = isPlayed && maxPoints !== undefined && maxPoints !== null;')

    def test_play_view_uses_actual_send_order_for_out_of_order_current_round(self):
        first_question = WhoQuestion.objects.create(
            statement='Runde 1',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=self.user,
        )
        second_question = WhoQuestion.objects.create(
            statement='Runde 2',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=self.user,
        )
        third_question = WhoQuestion.objects.create(
            statement='Runde 3',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Eve', 'is_lying': False},
                {'name': 'Finn', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.quiz.question_order = [first_question.id, second_question.id, third_question.id]
        self.quiz.current_question = third_question
        self.quiz.save(update_fields=['question_order', 'current_question'])
        self.quiz.selected_questions.add(first_question, second_question, third_question)
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=first_question,
            selected_liars=[0],
            time_taken=2.0,
        )

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry['id'] for entry in response.context['question_scoreboard']],
            [first_question.id, third_question.id, second_question.id],
        )
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertContains(response, 'moveQuestionToNextFreeScoreSlot(question?.id, question?.total_possible_points);')

    def test_play_view_rejoin_after_host_end_uses_saved_post_end_history(self):
        first_question = WhoQuestion.objects.create(
            statement='Runde 1',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=self.user,
        )
        next_question = WhoQuestion.objects.create(
            statement='Runde 2',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.quiz.question_order = [first_question.id, next_question.id]
        self.quiz.save(update_fields=['question_order'])
        self.quiz.selected_questions.add(first_question, next_question)
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=first_question,
            selected_liars=[0],
            time_taken=2.5,
        )

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertEqual(
            response.context['initial_progress_history'],
            [{
                'question_id': first_question.id,
                'question_number': 1,
                'points': -1,
                'max_points': 1,
            }],
        )
        self.assertEqual(response.context['question_scoreboard'][0]['status'], 'played')
        self.assertEqual(response.context['question_scoreboard'][0]['earned_points'], -1)
        self.assertEqual(response.context['question_scoreboard'][1]['status'], 'current')
