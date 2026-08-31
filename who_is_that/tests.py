import json
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.layers import InMemoryChannelLayer
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.authoritative_state import (
    current_snapshot,
    finish_question_flow,
    question_content_is_visible,
    reset_question_flow,
    reveal_question_content,
    validate_and_reserve_action,
)
from games_hub.models import GameRuntimeState, HubGameStep, HubSession
from games_hub.spectator import _serialize_who_that
from .models import (
    WhoThatAnswer,
    WhoThatParticipant,
    WhoThatQuestion,
    WhoThatQuiz,
    WhoThatSession,
)
from .consumers import WhoThatConsumer


REPO_ROOT = Path(__file__).resolve().parent.parent


class WhoThatAutomaticQuestionStartTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.host = User.objects.create_user(
            username='who-that-phase-host',
            password='pw123456',
            is_staff=True,
        )
        self.hub = HubSession.objects.create(
            code='WTPHASE',
            name='Who That phases',
            is_active=True,
        )
        self.question = WhoThatQuestion.objects.create(
            question_text='Who is this person?',
            image=SimpleUploadedFile(
                'phase-person.jpg',
                b'phase-image',
                content_type='image/jpeg',
            ),
            correct_answer='Ada Lovelace',
            category='Science',
            hint_text='First programmer',
            time_limit=30,
            created_by=self.host,
        )
        self.quiz = WhoThatQuiz.objects.create(
            creator=self.host,
            status='active',
            started_at=timezone.now(),
        )
        self.quiz.selected_questions.add(self.question)
        self.quiz_session = WhoThatSession.objects.create(quiz=self.quiz)
        HubGameStep.objects.create(
            session=self.hub,
            order=1,
            game_key='who_that',
            room_code=self.quiz.room_code,
        )
        self.participant = WhoThatParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.hub.code,
        )
        self.consumer = WhoThatConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.configured = reset_question_flow(
            game_key='who_that',
            room_code=self.quiz.room_code,
            session_code=self.hub.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def action(self, snapshot, action_type, *, action_id=None):
        return {
            'type': action_type,
            'question_id': self.question.id,
            'game_id': str(self.quiz.id),
            'state_revision': snapshot['state_revision'],
            'client_action_id': action_id or str(uuid.uuid4()),
        }

    def present(self, *, at=None, action_id=None):
        return async_to_sync(self.consumer.present_who_that_question)(
            self.quiz.id,
            self.question.id,
            self.hub.code,
            self.action(
                self.configured,
                'admin_send_question',
                action_id=action_id,
            ),
            self.question.time_limit,
            at,
        )

    def test_send_schedules_automatic_answering_after_presentation_delay(self):
        presented_at = timezone.now()
        decision = self.present(at=presented_at)
        starts_at = presented_at + timedelta(seconds=1)
        deadline = starts_at + timedelta(seconds=self.question.time_limit)

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.snapshot['question_phase'], 'answering_open')
        self.assertEqual(
            decision.snapshot['question_visible_at'],
            starts_at.isoformat(),
        )
        self.assertEqual(decision.snapshot['answering_started_at'], starts_at.isoformat())
        self.assertEqual(decision.snapshot['answering_deadline_at'], deadline.isoformat())
        self.assertFalse(decision.snapshot['answering_allowed'])
        self.quiz.refresh_from_db()
        self.quiz_session.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.question.id)
        self.assertEqual(self.quiz.question_start_time, starts_at)
        self.assertTrue(self.quiz_session.is_question_active)
        self.assertEqual(self.quiz_session.question_end_time, deadline)
        self.assertFalse(question_content_is_visible(
            decision.snapshot,
            at=presented_at + timedelta(milliseconds=999),
        ))
        self.assertTrue(question_content_is_visible(
            decision.snapshot,
            at=presented_at + timedelta(seconds=1),
        ))

        reveal = reveal_question_content(
            game_key='who_that',
            room_code=self.quiz.room_code,
            session_code=self.hub.code,
            action=self.action(decision.snapshot, 'reveal_question_content'),
            at=presented_at + timedelta(seconds=1),
        )
        self.assertFalse(reveal.accepted)
        self.assertEqual(reveal.code, 'invalid_phase')

    def test_duplicate_send_does_not_move_automatic_deadline(self):
        presented_at = timezone.now()
        action_id = str(uuid.uuid4())
        first = self.present(at=presented_at, action_id=action_id)
        duplicate = self.present(
            at=presented_at + timedelta(seconds=5),
            action_id=action_id,
        )

        self.assertTrue(first.accepted)
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(
            duplicate.snapshot['answering_deadline_at'],
            first.snapshot['answering_deadline_at'],
        )

    def test_pending_and_submit_are_rejected_until_automatic_start(self):
        presented_at = timezone.now()
        presented = self.present(at=presented_at)

        participant_action = {
            'question_id': self.question.id,
            'game_id': str(self.quiz.id),
            'state_revision': presented.snapshot['state_revision'],
            'client_action_id': str(uuid.uuid4()),
        }
        with patch(
            'games_hub.authoritative_state.timezone.now',
            return_value=presented_at + timedelta(milliseconds=999),
        ):
            guarded_before = validate_and_reserve_action(
                game_key='who_that',
                room_code=self.quiz.room_code,
                session_code=self.hub.code,
                participant_name=self.participant.name,
                action_type='participant_submit_answer',
                action=participant_action,
            )
        self.assertFalse(guarded_before.accepted)
        self.assertEqual(guarded_before.code, 'invalid_phase')

        pending_before = async_to_sync(self.consumer.save_pending_answer)(
            self.participant.name,
            self.hub.code,
            'Ada',
            self.question.id,
        )
        answer_before = async_to_sync(self.consumer.save_participant_answer)(
            self.participant.name,
            self.hub.code,
            'Ada Lovelace',
            0,
            self.question.id,
        )
        self.assertFalse(pending_before)
        self.assertIsNone(answer_before)
        self.assertFalse(WhoThatAnswer.objects.exists())

        with patch(
            'who_is_that.consumers.timezone.now',
            return_value=presented_at + timedelta(seconds=1),
        ):
            pending_after = async_to_sync(self.consumer.save_pending_answer)(
                self.participant.name,
                self.hub.code,
                'Ada',
                self.question.id,
            )
            answer_after = async_to_sync(self.consumer.save_participant_answer)(
                self.participant.name,
                self.hub.code,
                'Ada Lovelace',
                0,
                self.question.id,
            )
        with patch(
            'games_hub.authoritative_state.timezone.now',
            return_value=presented_at + timedelta(seconds=1),
        ):
            guarded_after = validate_and_reserve_action(
                game_key='who_that',
                room_code=self.quiz.room_code,
                session_code=self.hub.code,
                participant_name=self.participant.name,
                action_type='participant_update_pending_answer',
                action={
                    **participant_action,
                    'client_action_id': str(uuid.uuid4()),
                },
            )
        self.assertTrue(guarded_after.accepted)
        self.assertTrue(pending_after)
        self.assertIsNotNone(answer_after)
        self.assertEqual(WhoThatAnswer.objects.count(), 1)

    def test_phase_safe_snapshots_and_templates_do_not_expose_prepared_image(self):
        presented_at = timezone.now()
        presented = self.present(at=presented_at)
        state = async_to_sync(self.consumer.get_current_question_state)(self.hub.code)
        self.assertFalse(state['content_visible'])
        self.assertNotIn('image_url', state['question'])
        self.assertNotIn('category', state['question'])
        self.assertNotIn('hint_text', state['question'])

        client = Client()
        response = client.get(
            f"{reverse('who_is_that:play', args=[self.quiz.room_code, self.participant.name])}?hub_session={self.hub.code}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['question_content_visible'])
        self.assertFalse(response.context['question_answering_open'])

        host_client = Client()
        host_client.force_login(self.host)
        monitor = host_client.get(
            f"{reverse('admin_dashboard:who_that_monitor', args=[self.quiz.room_code])}?hub_session={self.hub.code}"
        )
        self.assertNotContains(monitor, 'FRAGE FREIGEBEN')
        self.assertNotContains(monitor, 'BILD ANZEIGEN')
        self.assertNotContains(monitor, 'id="openAnsweringBtn"', html=False)

        with patch(
            'games_hub.authoritative_state.timezone.now',
            return_value=presented_at + timedelta(seconds=1),
        ):
            response = client.get(
                f"{reverse('who_is_that:play', args=[self.quiz.room_code, self.participant.name])}?hub_session={self.hub.code}"
            )
        self.assertTrue(response.context['question_content_visible'])
        self.assertTrue(response.context['question_answering_open'])

    def test_host_status_marks_only_an_ended_question_as_ended(self):
        ended_badge = (
            '<span class="badge bg-secondary" id="questionEndedStatus">'
            'Question ended</span>'
        )
        presented_at = timezone.now()
        presented = self.present(at=presented_at)
        host_client = Client()
        host_client.force_login(self.host)
        monitor_url = (
            f"{reverse('admin_dashboard:who_that_monitor', args=[self.quiz.room_code])}"
            f"?hub_session={self.hub.code}"
        )

        during_delay = host_client.get(monitor_url)
        self.assertTrue(during_delay.context['is_display_question_active'])
        self.assertFalse(during_delay.context['question_answering_open'])
        self.assertNotContains(during_delay, ended_badge, html=False)

        with patch(
            'games_hub.authoritative_state.timezone.now',
            return_value=presented_at + timedelta(seconds=1),
        ):
            answering = host_client.get(monitor_url)
        self.assertEqual(answering.context['question_phase'], 'answering_open')
        self.assertTrue(answering.context['question_answering_open'])
        self.assertNotContains(answering, ended_badge, html=False)

        self.quiz_session.end_current_question()
        finished = finish_question_flow(
            game_key='who_that',
            room_code=self.quiz.room_code,
            session_code=self.hub.code,
            question_id=self.question.id,
        )
        review = host_client.get(
            f"{monitor_url}&review_question={self.question.id}"
        )
        self.assertFalse(review.context['is_display_question_active'])
        self.assertContains(review, ended_badge, html=False)

        second = WhoThatQuestion.objects.create(
            question_text='Who is the next person?',
            image=SimpleUploadedFile(
                'phase-person-two.jpg',
                b'phase-image-two',
                content_type='image/jpeg',
            ),
            correct_answer='Grace Hopper',
            time_limit=30,
            created_by=self.host,
        )
        next_question = async_to_sync(self.consumer.present_who_that_question)(
            self.quiz.id,
            second.id,
            self.hub.code,
            {
                'type': 'admin_send_question',
                'question_id': second.id,
                'game_id': str(self.quiz.id),
                'state_revision': finished['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            second.time_limit,
            timezone.now(),
        )
        self.assertTrue(next_question.accepted)
        next_monitor = host_client.get(monitor_url)
        self.assertTrue(next_monitor.context['is_display_question_active'])
        self.assertNotContains(next_monitor, ended_badge, html=False)

    def test_spectator_releases_content_and_timer_at_automatic_start(self):
        presented_at = timezone.now()
        presented = self.present(at=presented_at)
        self.quiz.refresh_from_db()
        self.quiz_session.refresh_from_db()

        prepared = _serialize_who_that(self.quiz, self.hub)
        self.assertIsNone(prepared['question'])
        self.assertFalse(prepared['timer']['active'])

        visible_at = presented_at + timedelta(seconds=1)
        with patch(
            'games_hub.authoritative_state.timezone.now',
            return_value=visible_at,
        ):
            answering = _serialize_who_that(self.quiz, self.hub)
        self.assertEqual(answering['question_phase'], 'answering_open')
        self.assertEqual(answering['question']['id'], self.question.id)
        self.assertTrue(answering['question']['image_url'])
        self.assertTrue(answering['timer']['active'])
        self.assertNotIn('correct_answer', answering['question'])
        self.assertNotIn('explanation', answering['question'])

    def test_second_question_resets_pending_answer_and_deadline(self):
        first_presented_at = timezone.now() - timedelta(seconds=2)
        first = self.present(at=first_presented_at)
        self.assertTrue(async_to_sync(self.consumer.save_pending_answer)(
            self.participant.name,
            self.hub.code,
            'First pending',
            self.question.id,
        ))

        self.quiz_session.end_current_question()
        finished = finish_question_flow(
            game_key='who_that',
            room_code=self.quiz.room_code,
            session_code=self.hub.code,
            question_id=self.question.id,
        )
        second = WhoThatQuestion.objects.create(
            question_text='Second person',
            image=SimpleUploadedFile('second.jpg', b'second', content_type='image/jpeg'),
            correct_answer='Grace Hopper',
            time_limit=20,
            created_by=self.host,
        )
        second_presented_at = timezone.now()
        decision = async_to_sync(self.consumer.present_who_that_question)(
            self.quiz.id,
            second.id,
            self.hub.code,
            {
                'question_id': second.id,
                'game_id': str(self.quiz.id),
                'state_revision': finished['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            second.time_limit,
            second_presented_at,
        )

        self.assertTrue(decision.accepted)
        self.quiz.refresh_from_db()
        self.quiz_session.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, second.id)
        self.assertEqual(self.quiz_session.pending_answers, {})
        self.assertTrue(self.quiz_session.is_question_active)
        self.assertEqual(
            self.quiz_session.question_end_time,
            second_presented_at + timedelta(seconds=21),
        )


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group_name, message):
        self.group_messages.append((group_name, message))


class WhoThatPointsConfigurationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="admin",
            password="pw123456",
            is_staff=True,
        )
        self.client.force_login(self.user)

    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_question_model_default_points_is_one(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file(),
            correct_answer="Ada Lovelace",
            created_by=self.user,
        )

        self.assertEqual(question.points, 1)

    def test_add_question_without_points_uses_default_one(self):
        response = self.client.post(
            reverse("admin_dashboard:add_who_that_question"),
            {
                "question_text": "Who is this person?",
                "correct_answer": "Grace Hopper",
                "points": 9,
                "time_limit": 30,
                "image": self._image_file("grace.jpg"),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])

        question = WhoThatQuestion.objects.get(id=payload["question_id"])
        self.assertEqual(question.points, 1)

    def test_host_views_do_not_render_legacy_question_point_controls(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("selection.jpg"),
            correct_answer="Alan Turing",
            created_by=self.user,
            points=9,
        )
        quiz = WhoThatQuiz.objects.create(creator=self.user, status="waiting")
        WhoThatSession.objects.create(quiz=quiz)

        management_response = self.client.get(
            reverse("admin_dashboard:who_that_management")
        )
        self.assertEqual(management_response.status_code, 200)
        self.assertNotContains(management_response, 'id="points"', html=False)
        self.assertNotContains(management_response, "<th>Points</th>", html=False)

        selection_response = self.client.get(
            reverse("admin_dashboard:who_that_monitor", args=[quiz.room_code])
        )
        self.assertNotContains(selection_response, 'class="form-control form-control-sm question-points-input"', html=False)
        self.assertNotContains(selection_response, 'id="currentQuestionPointsInput"', html=False)

        quiz.status = "active"
        quiz.current_question = question
        quiz.question_start_time = timezone.now()
        quiz.save(update_fields=["status", "current_question", "question_start_time"])

        active_response = self.client.get(
            reverse("admin_dashboard:who_that_monitor", args=[quiz.room_code])
        )
        self.assertContains(active_response, "currentQuestionScoringValue")
        self.assertContains(active_response, "1 point for a correct answer, 0 for an incorrect answer.")
        self.assertNotContains(active_response, 'id="currentQuestionPointsInput"', html=False)

    def test_legacy_points_endpoint_keeps_fixed_single_point_scoring(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("active.jpg"),
            correct_answer="Albert Einstein",
            created_by=self.user,
            points=7,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(quiz=quiz)
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="Alice",
            hub_session_code="hub1",
        )
        answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Albert Einstein",
            time_taken=2.5,
        )

        self.assertEqual(answer.points_earned, 1)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, 1)

        response = self.client.post(
            reverse("admin_dashboard:set_who_that_question_points", args=[quiz.room_code]),
            data=json.dumps({"question_id": question.id, "points": 7}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["points"], 1)

        question.refresh_from_db()
        answer.refresh_from_db()
        participant.refresh_from_db()

        self.assertEqual(question.points, 1)
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)

    def test_selected_question_points_cannot_be_changed_away_from_one(self):
        other_user = User.objects.create_user(
            username="other_admin",
            password="pw123456",
            is_staff=True,
        )
        foreign_question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("foreign.jpg"),
            correct_answer="Marie Curie",
            created_by=other_user,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(creator=self.user, status="waiting")
        quiz.selected_questions.add(foreign_question)
        WhoThatSession.objects.create(quiz=quiz)

        response = self.client.post(
            reverse("admin_dashboard:set_who_that_question_points", args=[quiz.room_code]),
            data=json.dumps({"question_id": foreign_question.id, "points": 5}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["points"], 1)
        foreign_question.refresh_from_db()
        self.assertEqual(foreign_question.points, 1)


class WhoThatPartialMatchThresholdTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="who_that_match_host",
            password="pw123456",
            is_staff=True,
        )

    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_single_character_substring_is_not_treated_as_partial_match(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("tesla.jpg"),
            correct_answer="Nikola Tesla",
            created_by=self.user,
            points=5,
        )

        self.assertFalse(question.check_answer("a"))
        self.assertEqual(question.calculate_score("a"), 0)

    def test_meaningful_last_name_partial_match_remains_valid(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("tesla-lastname.jpg"),
            correct_answer="Nikola Tesla",
            created_by=self.user,
            points=5,
        )

        self.assertTrue(question.check_answer("Tesla"))
        self.assertGreater(question.calculate_score("Tesla"), 0)


class WhoThatPlayTextCleanupTests(TestCase):
    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_play_view_removes_default_question_copy_and_renames_submit_button(self):
        host = User.objects.create_user(
            username="host_play_texts",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file(),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(quiz=quiz)
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="PlayerOne",
            hub_session_code=None,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "question.question_text === 'Who is this person?' ? '' : question.question_text;",
        )
        self.assertNotContains(response, "Alternative spellings and nicknames are often accepted")
        self.assertContains(response, "Einloggen")

    def test_waiting_screen_is_minimal_without_participant_count(self):
        host = User.objects.create_user(
            username="host_waiting_texts",
            password="pw123456",
            is_staff=True,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="waiting",
        )
        WhoThatSession.objects.create(quiz=quiz)
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="PlayerWaiting",
            hub_session_code=None,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="waitingQuizState"', html=False)
        self.assertNotContains(response, "Waiting for Photo Quiz to Start")
        self.assertNotContains(
            response,
            "Get ready to identify famous people! The quiz host will start the session shortly.",
        )
        self.assertNotContains(response, 'class="participants-count"', html=False)
        self.assertNotContains(response, '<span id="waitingParticipantCount"', html=False)
        self.assertNotContains(response, "participants joined")


class WhoThatRevealScreenTests(TestCase):
    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_play_view_uses_reveal_screen_instead_of_waiting_for_next_question_screen(self):
        host = User.objects.create_user(
            username="host_reveal",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Identify this person",
            image=self._image_file("reveal.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(quiz=quiz)
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="PlayerReveal",
            hub_session_code=None,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Waiting for Next Question")
        self.assertNotContains(response, "showWaitingForNextQuestion")
        self.assertContains(response, 'id="correctAnswerPhotoDisplay"', html=False)
        self.assertContains(response, 'id="answerResultIcon"', html=False)
        self.assertContains(response, "Your Answer")
        self.assertContains(response, 'class="answer-display who-that-reveal-answer"', html=False)
        self.assertContains(response, 'class="comparison-display who-that-reveal-comparison"', html=False)


class WhoThatVhsLayoutTests(SimpleTestCase):
    def test_vhs_states_use_scoped_layout_and_single_reveal_hierarchy(self):
        template = (REPO_ROOT / "templates/who_is_that/play.html").read_text(encoding="utf-8")
        vhs_css = (REPO_ROOT / "static/themes/vhs/vhs.css").read_text(encoding="utf-8")

        self.assertEqual(template.count('class="who-that-interaction-stack"'), 1)
        self.assertIn("vhs-who-that-submit", template)
        self.assertIn("who-that-submitted-answer-label", template)
        self.assertIn("who-that-submitted-answer-value", template)
        self.assertIn("who-that-reveal-comparison", template)
        self.assertEqual(template.count('id="correctAnswerDisplay"'), 1)

        scope = 'html[data-participant-theme="vhs"] body.who-that-play-page'
        self.assertIn(f"{scope} .vhs-theme-shell", vhs_css)
        self.assertIn("#questionState .input-icon", vhs_css)
        self.assertIn("#questionState .who-that-interaction-stack", vhs_css)
        self.assertIn("#answerSubmittedState .who-that-submitted-waiting", vhs_css)
        self.assertIn("#answerSubmittedState .who-that-submitted-answer-value", vhs_css)
        self.assertIn("#correctAnswerState .vhs-reveal-correct-value", vhs_css)
        self.assertIn(
            ".qa-score-widget .who-that-status-answer-text",
            vhs_css,
        )
        self.assertIn("color: var(--vhs-answer-text) !important;", vhs_css)
        self.assertIn("who-that-status-result score-box__value", template)
        self.assertIn("row.dataset.pointsEarned = hasResult ? String(earnedPoints) : '';", template)
        self.assertIn(
            "#correctAnswerState :is(.vhs-reveal-vs, .vhs-reveal-duplicate, #performanceBadge)",
            vhs_css,
        )
        self.assertIn("@media (max-width: 1000px)", vhs_css)

    def test_shared_vhs_state_sync_localizes_labels_without_changing_other_themes(self):
        widget = (REPO_ROOT / "templates/includes/accessibility_widget.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("setReversibleVhsText(heading, 'DIE RICHTIGE ANTWORT IST')", widget)
        self.assertIn("setReversibleVhsText(answerLabel, 'GEGEBENE ANTWORT')", widget)
        self.assertIn("setReversibleVhsText(waiting, 'Warte auf die nächste Runde...')", widget)


class WhoThatSubmitStateTests(TestCase):
    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_play_view_keeps_current_submitted_question_neutral_until_question_end(self):
        host = User.objects.create_user(
            username="host_submit_state",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Identify this person",
            image=self._image_file("submit-state.jpg"),
            correct_answer="Nikola Tesla",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
            question_order=[question.id],
        )
        quiz.selected_questions.set([question])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="SubmittedPlayer",
            hub_session_code=None,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Nikola Tesla",
            time_taken=1.2,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(len(board), 1)
        self.assertIsNone(board[0]["result"])
        self.assertIsNone(board[0]["earned_points"])
        self.assertContains(response, 'id="submittedPhotoDisplay"', html=False)
        self.assertContains(response, "Waiting for the other participants before the answer is revealed.")
        self.assertContains(response, "Nikola Tesla")
        self.assertContains(response, "this.initialSubmittedState = {")
        self.assertNotContains(response, 'id="answerResult"', html=False)
        self.assertNotContains(response, 'id="pointsEarned"', html=False)
        self.assertNotContains(response, 'id="matchQuality"', html=False)

    def test_play_view_submit_state_defers_result_updates_until_question_end(self):
        host = User.objects.create_user(
            username="host_submit_hooks",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Identify this person",
            image=self._image_file("submit-hooks.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="HookPlayer",
            hub_session_code=None,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this.showState('answerSubmittedState');")
        self.assertContains(response, "this.updateSubmittedAnswerState();")
        self.assertContains(response, "this.showState(submittedState ? 'answerSubmittedState' : 'questionState');")
        self.assertNotContains(response, "Result:")
        self.assertNotContains(response, "Points Earned:")
        self.assertNotContains(response, "Match Quality:")


class WhoThatQuestionStatusBoxTests(TestCase):
    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_play_view_renders_question_status_box_with_selected_questions(self):
        host = User.objects.create_user(
            username="host_status_box",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Identify person 1",
            image=self._image_file("one.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Identify person 2",
            image=self._image_file("two.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        question_three = WhoThatQuestion.objects.create(
            question_text="Identify person 3",
            image=self._image_file("three.jpg"),
            correct_answer="Alan Turing",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question_two,
            question_start_time=timezone.now(),
            question_order=[question_one.id, question_two.id, question_three.id],
        )
        quiz.selected_questions.set([question_one, question_two, question_three])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="StatusPlayer",
            hub_session_code=None,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_one,
            user_answer="Ada Lovelace",
            time_taken=1.2,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="whoThatQuestionStatusBox"', html=False)
        self.assertContains(response, 'data-question-number="1"', html=False)
        self.assertContains(response, 'data-question-number="2"', html=False)
        self.assertContains(response, 'data-question-number="3"', html=False)

        board = response.context["question_status_board"]
        self.assertEqual(len(board), 3)
        self.assertEqual(board[0]["result"], "correct")
        self.assertTrue(board[1]["is_current"])
        self.assertIsNone(board[1]["result"])
        self.assertIsNone(board[2]["result"])

    def test_play_view_keeps_active_current_question_neutral_before_evaluation(self):
        host = User.objects.create_user(
            username="host_status_missed",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Identify person 1",
            image=self._image_file("missed-one.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Identify person 2",
            image=self._image_file("missed-two.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question_one,
            question_start_time=timezone.now(),
            question_order=[question_one.id, question_two.id],
        )
        quiz.selected_questions.set([question_one, question_two])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="LatePlayer",
            hub_session_code=None,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        current_entry = next(entry for entry in board if entry["id"] == question_one.id)
        self.assertTrue(current_entry["is_current"])
        self.assertIsNone(current_entry["result"])
        self.assertEqual(current_entry["solution_text"], "")
        self.assertIsNone(current_entry["earned_points"])

    def test_play_view_renders_solution_text_and_score_totals_for_played_questions(self):
        host = User.objects.create_user(
            username="host_status_solution",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Identify person 1",
            image=self._image_file("solution-one.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Identify person 2",
            image=self._image_file("solution-two.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        question_three = WhoThatQuestion.objects.create(
            question_text="Identify person 3",
            image=self._image_file("solution-three.jpg"),
            correct_answer="Alan Turing",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question_three,
            question_start_time=timezone.now(),
            question_order=[question_one.id, question_two.id, question_three.id],
        )
        quiz.selected_questions.set([question_one, question_two, question_three])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=3,
            total_questions_sent=3,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="SolutionPlayer",
            hub_session_code=None,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_one,
            user_answer="Ada Lovelace",
            time_taken=1.0,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_two,
            user_answer="Wrong Answer",
            time_taken=1.5,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(board[0]["solution_text"], "Ada Lovelace")
        self.assertEqual(board[0]["earned_points"], 1)
        self.assertEqual(board[0]["max_points"], 1)
        self.assertEqual(board[1]["solution_text"], "Grace Hopper")
        self.assertEqual(board[1]["result"], "incorrect")
        self.assertEqual(board[1]["earned_points"], 0)
        self.assertContains(response, 'id="whoThatQuestionStatusTotal"', html=False)
        self.assertContains(response, 'who-that-status-answer-text')
        self.assertContains(response, 'score-box__row')
        self.assertContains(response, 'data-max-points="1"', count=3, html=False)
        self.assertContains(response, 'data-points-earned="1"', count=1, html=False)
        self.assertContains(response, 'data-points-earned="0"', count=1, html=False)
        self.assertContains(response, 'data-points-earned=""', count=1, html=False)
        self.assertContains(response, 'who-that-status-result score-box__value', html=False)

    def test_play_view_keeps_answer_fallback_for_legacy_quiz_without_selected_questions(self):
        host = User.objects.create_user(
            username="host_status_legacy",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Legacy person 1",
            image=self._image_file("legacy-one.jpg"),
            correct_answer="Frida Kahlo",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Legacy person 2",
            image=self._image_file("legacy-two.jpg"),
            correct_answer="Hedy Lamarr",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question_two,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="LegacyPlayer",
            hub_session_code=None,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_one,
            user_answer="Wrong",
            time_taken=1.0,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(len(board), 2)
        self.assertEqual(board[0]["solution_text"], "Frida Kahlo")
        self.assertEqual(board[1]["id"], question_two.id)
        self.assertTrue(board[1]["is_current"])

    def test_play_view_ignores_stale_waiting_session_progress_for_new_session(self):
        host = User.objects.create_user(
            username="host_status_waiting_reset",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Waiting person 1",
            image=self._image_file("waiting-one.jpg"),
            correct_answer="Frida Kahlo",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Waiting person 2",
            image=self._image_file("waiting-two.jpg"),
            correct_answer="Hedy Lamarr",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="waiting",
            question_order=[question_one.id, question_two.id],
        )
        quiz.selected_questions.set([question_one, question_two])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=False,
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="FreshSessionPlayer",
            hub_session_code="NEW123",
        )

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=NEW123"
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(response.context["current_question_number"], 0)
        self.assertEqual(len(board), 2)
        self.assertIsNone(board[0]["result"])
        self.assertIsNone(board[0]["earned_points"])
        self.assertIsNone(board[1]["result"])
        self.assertIsNone(board[1]["earned_points"])

    def test_waiting_start_resets_stale_session_progress_before_new_run(self):
        host = User.objects.create_user(
            username="host_status_start_reset",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Reset person",
            image=self._image_file("reset-person.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="waiting",
            current_question=question,
            question_start_time=timezone.now(),
            tutorial_active=True,
            ended_at=timezone.now(),
        )
        session = WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=15),
            pending_answers={"1": {"question_id": question.id, "user_answer": "Old"}},
            total_responses_current_question=3,
            correct_responses_current_question=1,
            average_response_time_current_question=5.5,
        )

        quiz.start_quiz()
        quiz.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(quiz.status, "active")
        self.assertFalse(quiz.tutorial_active)
        self.assertIsNone(quiz.current_question)
        self.assertIsNone(quiz.question_start_time)
        self.assertIsNone(quiz.ended_at)
        self.assertEqual(session.current_question_number, 0)
        self.assertEqual(session.total_questions_sent, 0)
        self.assertFalse(session.is_question_active)
        self.assertIsNone(session.question_end_time)
        self.assertEqual(session.pending_answers, {})
        self.assertEqual(session.total_responses_current_question, 0)
        self.assertEqual(session.correct_responses_current_question, 0)
        self.assertEqual(session.average_response_time_current_question, 0)

    def test_play_view_hides_previous_run_results_before_first_question(self):
        host = User.objects.create_user(
            username="host_status_fresh_start",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Fresh start person 1",
            image=self._image_file("fresh-one.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Fresh start person 2",
            image=self._image_file("fresh-two.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="waiting",
            question_order=[question_one.id, question_two.id],
        )
        quiz.selected_questions.set([question_one, question_two])
        WhoThatSession.objects.create(quiz=quiz)
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="FreshStartPlayer",
            hub_session_code="RUN1",
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_one,
            user_answer="Old wrong answer",
            time_taken=1.0,
        )

        quiz.start_quiz()

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=RUN1"
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(response.context["current_question_number"], 0)
        self.assertEqual(len(board), 2)
        self.assertIsNone(board[0]["result"])
        self.assertEqual(board[0]["solution_text"], "")
        self.assertIsNone(board[1]["result"])
        self.assertEqual(board[1]["solution_text"], "")

    def test_play_view_ignores_stale_active_session_progress_before_first_question(self):
        host = User.objects.create_user(
            username="host_status_active_stale_start",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Active stale person 1",
            image=self._image_file("active-stale-one.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Active stale person 2",
            image=self._image_file("active-stale-two.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            started_at=timezone.now(),
            question_order=[question_one.id, question_two.id],
        )
        quiz.selected_questions.set([question_one, question_two])
        session = WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=False,
        )
        WhoThatSession.objects.filter(id=session.id).update(
            updated_at=quiz.started_at - timezone.timedelta(seconds=5)
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="ActiveStalePlayer",
            hub_session_code="ACTIVE-ST",
        )

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=ACTIVE-ST"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_question_number"], 0)
        board = response.context["question_status_board"]
        self.assertEqual(len(board), 2)
        self.assertIsNone(board[0]["result"])
        self.assertEqual(board[0]["solution_text"], "")
        self.assertIsNone(board[1]["result"])
        self.assertEqual(board[1]["solution_text"], "")

    def test_play_view_ignores_previous_run_answer_for_active_question(self):
        host = User.objects.create_user(
            username="host_status_previous_run_current",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Current person",
            image=self._image_file("current-person.jpg"),
            correct_answer="Alan Turing",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="waiting",
            question_order=[question.id],
        )
        quiz.selected_questions.set([question])
        WhoThatSession.objects.create(quiz=quiz)
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="CurrentRunPlayer",
            hub_session_code="RUN2",
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Old answer",
            time_taken=1.0,
        )

        quiz.start_quiz()
        quiz.current_question = question
        quiz.question_start_time = timezone.now()
        quiz.save(update_fields=["current_question", "question_start_time"])
        session = quiz.session
        session.current_question_number = 1
        session.total_questions_sent = 1
        session.is_question_active = True
        session.question_end_time = timezone.now() + timezone.timedelta(seconds=20)
        session.save(update_fields=[
            "current_question_number",
            "total_questions_sent",
            "is_question_active",
            "question_end_time",
            "updated_at",
        ])

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=RUN2"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["current_participant_answer"])
        board = response.context["question_status_board"]
        self.assertEqual(len(board), 1)
        self.assertTrue(board[0]["is_current"])
        self.assertIsNone(board[0]["result"])
        self.assertEqual(board[0]["solution_text"], "")
        self.assertIsNone(board[0]["earned_points"])

    def test_play_view_rejoin_only_uses_current_run_history(self):
        host = User.objects.create_user(
            username="host_status_rejoin_current_run_only",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Rejoin person 1",
            image=self._image_file("rejoin-one.jpg"),
            correct_answer="Frida Kahlo",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Rejoin person 2",
            image=self._image_file("rejoin-two.jpg"),
            correct_answer="Hedy Lamarr",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="waiting",
            question_order=[question_one.id, question_two.id],
        )
        quiz.selected_questions.set([question_one, question_two])
        WhoThatSession.objects.create(quiz=quiz)
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="RejoinCurrentRunPlayer",
            hub_session_code="RUN3",
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_one,
            user_answer="Old answer",
            time_taken=1.1,
        )

        quiz.start_quiz()
        quiz.current_question = question_two
        quiz.question_start_time = timezone.now()
        quiz.save(update_fields=["current_question", "question_start_time"])
        session = quiz.session
        session.current_question_number = 1
        session.total_questions_sent = 1
        session.is_question_active = True
        session.question_end_time = timezone.now() + timezone.timedelta(seconds=20)
        session.save(update_fields=[
            "current_question_number",
            "total_questions_sent",
            "is_question_active",
            "question_end_time",
            "updated_at",
        ])

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=RUN3"
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual([entry["id"] for entry in board], [question_two.id, question_one.id])
        self.assertTrue(board[0]["is_current"])
        self.assertIsNone(board[0]["result"])
        self.assertIsNone(board[1]["result"])
        self.assertEqual(board[1]["solution_text"], "")

    def test_play_view_keeps_existing_history_for_active_session_rejoin(self):
        host = User.objects.create_user(
            username="host_status_active_rejoin",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Active person 1",
            image=self._image_file("active-one.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Active person 2",
            image=self._image_file("active-two.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question_two,
            question_start_time=timezone.now(),
            question_order=[question_one.id, question_two.id],
        )
        quiz.selected_questions.set([question_one, question_two])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="RejoinPlayer",
            hub_session_code="ACTIVE1",
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_one,
            user_answer="Ada Lovelace",
            time_taken=1.1,
        )

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=ACTIVE1"
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(board[0]["result"], "correct")
        self.assertEqual(board[0]["earned_points"], 1)
        self.assertTrue(board[1]["is_current"])
        self.assertIsNone(board[1]["result"])
        self.assertEqual(board[1]["solution_text"], "")
        self.assertIsNone(board[1]["earned_points"])
        self.assertContains(response, "entry.result = null;")
        self.assertContains(response, "entry.solution_text = '';")
        self.assertContains(response, "entry.earned_points = null;")

    def test_play_view_shows_incorrect_solution_only_after_question_end(self):
        host = User.objects.create_user(
            username="host_status_incorrect_end",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Reveal wrong answer",
            image=self._image_file("incorrect-end.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=None,
            question_order=[question.id],
        )
        quiz.selected_questions.set([question])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="IncorrectAfterEnd",
            hub_session_code=None,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Wrong Answer",
            time_taken=1.0,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(board[0]["result"], "incorrect")
        self.assertEqual(board[0]["solution_text"], "Ada Lovelace")
        self.assertEqual(board[0]["earned_points"], 0)

    def test_play_view_shows_correct_solution_only_after_question_end(self):
        host = User.objects.create_user(
            username="host_status_correct_end",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Reveal correct answer",
            image=self._image_file("correct-end.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=None,
            question_order=[question.id],
        )
        quiz.selected_questions.set([question])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="CorrectAfterEnd",
            hub_session_code=None,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Grace Hopper",
            time_taken=1.0,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual(board[0]["result"], "correct")
        self.assertEqual(board[0]["solution_text"], "Grace Hopper")
        self.assertEqual(board[0]["earned_points"], 1)

    def test_play_view_marks_unanswered_revealed_question_incorrect_only_after_real_current_run_progress(self):
        host = User.objects.create_user(
            username="host_status_unanswered_after_reveal",
            password="pw123456",
            is_staff=True,
        )
        question = WhoThatQuestion.objects.create(
            question_text="Reveal unanswered question",
            image=self._image_file("reveal-unanswered.jpg"),
            correct_answer="Katherine Johnson",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            started_at=timezone.now() - timezone.timedelta(minutes=1),
            current_question=None,
            question_order=[question.id],
        )
        quiz.selected_questions.set([question])
        session = WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        WhoThatSession.objects.filter(id=session.id).update(
            updated_at=timezone.now() - timezone.timedelta(seconds=5)
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="RevealNoAnswerPlayer",
            hub_session_code=None,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_question_number"], 1)
        board = response.context["question_status_board"]
        self.assertEqual(board[0]["result"], "incorrect")
        self.assertEqual(board[0]["solution_text"], "Katherine Johnson")
        self.assertEqual(board[0]["earned_points"], 0)

    def test_play_view_uses_actual_send_order_for_out_of_order_current_question(self):
        host = User.objects.create_user(
            username="host_status_out_of_order",
            password="pw123456",
            is_staff=True,
        )
        question_one = WhoThatQuestion.objects.create(
            question_text="Active person 1",
            image=self._image_file("out-one.jpg"),
            correct_answer="Ada Lovelace",
            created_by=host,
            points=1,
        )
        question_two = WhoThatQuestion.objects.create(
            question_text="Active person 2",
            image=self._image_file("out-two.jpg"),
            correct_answer="Grace Hopper",
            created_by=host,
            points=1,
        )
        question_three = WhoThatQuestion.objects.create(
            question_text="Active person 3",
            image=self._image_file("out-three.jpg"),
            correct_answer="Alan Turing",
            created_by=host,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=host,
            status="active",
            current_question=question_three,
            question_start_time=timezone.now(),
            question_order=[question_one.id, question_two.id, question_three.id],
        )
        quiz.selected_questions.set([question_one, question_two, question_three])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="OutOfOrderPlayer",
            hub_session_code="ACTIVE2",
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question_one,
            user_answer="Ada Lovelace",
            time_taken=1.1,
        )

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=ACTIVE2"
        )

        self.assertEqual(response.status_code, 200)
        board = response.context["question_status_board"]
        self.assertEqual([entry["id"] for entry in board], [question_one.id, question_three.id, question_two.id])
        self.assertEqual(board[0]["result"], "correct")
        self.assertTrue(board[1]["is_current"])
        self.assertIsNone(board[2]["result"])
        self.assertContains(response, 'moveQuestionToNextFreeStatusSlot(questionId)')


class WhoThatTimerAndPendingAnswerTests(TransactionTestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="timer_host",
            password="pw123456",
            is_staff=True,
        )
        self.client.force_login(self.user)

    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_monitor_and_player_share_same_current_question_time_left(self):
        question = WhoThatQuestion.objects.create(
            question_text="Identify this person",
            image=self._image_file(),
            correct_answer="Rosalind Franklin",
            created_by=self.user,
            points=1,
            time_limit=30,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        session = WhoThatSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=17),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="PlayerOne",
            hub_session_code=None,
        )

        monitor_response = self.client.get(
            reverse("admin_dashboard:who_that_monitor", args=[quiz.room_code])
        )
        play_response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(
            monitor_response.context["current_question_time_left"],
            play_response.context["current_question_time_left"],
        )
        self.assertTrue(16 <= monitor_response.context["current_question_time_left"] <= 17)
        self.assertEqual(
            play_response.context["current_question_end_time"].replace(microsecond=0),
            session.question_end_time.replace(microsecond=0),
        )

    def test_consumer_current_question_data_uses_session_end_time(self):
        question = WhoThatQuestion.objects.create(
            question_text="Identify this person",
            image=self._image_file("consumer.jpg"),
            correct_answer="Katherine Johnson",
            created_by=self.user,
            points=1,
            time_limit=30,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=22),
        )

        consumer = WhoThatConsumer()
        consumer.room_code = quiz.room_code

        question_data = async_to_sync(consumer.get_current_question_data)()

        self.assertEqual(question_data["id"], question.id)
        self.assertIn("question_end_time", question_data)
        self.assertTrue(21 <= question_data["time_left"] <= 22)

    def test_handle_admin_end_question_finalizes_pending_answer_before_clearing_question(self):
        question = WhoThatQuestion.objects.create(
            question_text="Identify this person",
            image=self._image_file("pending.jpg"),
            correct_answer="Albert Einstein",
            created_by=self.user,
            points=3,
            time_limit=30,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=5),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="Alice",
            hub_session_code="hub1",
        )
        session = WhoThatSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=25),
            pending_answers={
                str(participant.id): {
                    "question_id": question.id,
                    "user_answer": "Albert Einstein",
                    "updated_at": timezone.now().isoformat(),
                    "participant_name": participant.name,
                    "hub_session_code": participant.hub_session_code,
                }
            },
        )

        consumer = WhoThatConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f"who_that_{quiz.room_code}"
        consumer.channel_layer = InMemoryChannelLayer()
        consumer.channel_name = "test-channel"

        async_to_sync(consumer.handle_admin_end_question)({})

        answer = WhoThatAnswer.objects.get(quiz=quiz, participant=participant, question=question)
        participant.refresh_from_db()
        quiz.refresh_from_db()
        session.refresh_from_db()

        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)
        self.assertIsNone(quiz.current_question)
        self.assertEqual(session.pending_answers, {})


class WhoThatHostManualCorrectTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="who_that_manual_host",
            password="pw123456",
            is_staff=True,
        )
        self.client.force_login(self.user)

    def _image_file(self, name="person.jpg"):
        return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")

    def test_live_responses_expose_free_text_answer_and_manual_action(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("manual-visible.jpg"),
            correct_answer="Ada Lovelace",
            created_by=self.user,
            points=5,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="Alice",
            hub_session_code="hub1",
        )
        answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Grace Hopper",
            time_taken=1.5,
        )

        response = self.client.get(
            reverse("admin_dashboard:api_who_that_live_responses", args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["responses"]), 1)
        live_response = payload["responses"][0]
        self.assertEqual(live_response["answer_id"], answer.id)
        self.assertEqual(live_response["user_answer"], "Grace Hopper")
        self.assertFalse(live_response["is_correct"])
        self.assertTrue(live_response["can_mark_correct"])

    def test_live_responses_only_include_current_session_current_question_and_current_run(self):
        current_question = WhoThatQuestion.objects.create(
            question_text="Current live question",
            image=self._image_file("live-current.jpg"),
            correct_answer="Ada Lovelace",
            created_by=self.user,
            points=1,
        )
        previous_question = WhoThatQuestion.objects.create(
            question_text="Previous live question",
            image=self._image_file("live-previous.jpg"),
            correct_answer="Grace Hopper",
            created_by=self.user,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            started_at=timezone.now() - timezone.timedelta(minutes=1),
            current_question=current_question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )

        session_a = HubSession.objects.create(
            code="WTAA",
            started_at=timezone.now() - timezone.timedelta(hours=1),
            ended_at=timezone.now() - timezone.timedelta(minutes=30),
            is_active=False,
        )
        session_b = HubSession.objects.create(
            code="WTAB",
            started_at=timezone.now() - timezone.timedelta(minutes=5),
            is_active=True,
        )
        HubGameStep.objects.create(session=session_a, order=1, game_key="who_that", room_code=quiz.room_code)
        HubGameStep.objects.create(session=session_b, order=1, game_key="who_that", room_code=quiz.room_code)

        old_session_participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="OldSession",
            hub_session_code=session_a.code,
        )
        current_participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="CurrentSession",
            hub_session_code=session_b.code,
        )
        old_run_participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="OldRunCurrentSession",
            hub_session_code=session_b.code,
        )

        valid_answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=current_participant,
            question=current_question,
            user_answer="Ada Lovelace",
            time_taken=1.2,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=old_session_participant,
            question=current_question,
            user_answer="Old Session Answer",
            time_taken=1.5,
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=current_participant,
            question=previous_question,
            user_answer="Previous Question Answer",
            time_taken=1.1,
        )
        old_run_answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=old_run_participant,
            question=current_question,
            user_answer="Old Run Answer",
            time_taken=1.8,
        )
        old_run_answer.submitted_at = quiz.started_at - timezone.timedelta(seconds=5)
        old_run_answer.save(update_fields=["submitted_at"])

        response = self.client.get(
            reverse("admin_dashboard:api_who_that_live_responses", args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["responses"]), 1)
        live_response = payload["responses"][0]
        self.assertEqual(live_response["answer_id"], valid_answer.id)
        self.assertEqual(live_response["participant_name"], "CurrentSession")
        self.assertEqual(live_response["hub_session_code"], session_b.code)
        self.assertEqual(live_response["question_id"], current_question.id)
        self.assertEqual(live_response["game_id"], quiz.id)

    def test_live_responses_are_empty_without_active_current_question(self):
        question = WhoThatQuestion.objects.create(
            question_text="Ended live question",
            image=self._image_file("live-ended.jpg"),
            correct_answer="Alan Turing",
            created_by=self.user,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            started_at=timezone.now() - timezone.timedelta(minutes=1),
            current_question=None,
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            is_question_active=False,
            question_end_time=None,
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="EndedPlayer",
            hub_session_code="LIVE0",
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Alan Turing",
            time_taken=1.0,
        )

        response = self.client.get(
            reverse("admin_dashboard:api_who_that_live_responses", args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["responses"], [])

    def test_live_responses_can_reload_last_ended_question_in_review_mode(self):
        question = WhoThatQuestion.objects.create(
            question_text="Ended review question",
            image=self._image_file("live-review.jpg"),
            correct_answer="Katherine Johnson",
            created_by=self.user,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            started_at=timezone.now() - timezone.timedelta(minutes=1),
            current_question=None,
        )
        quiz.selected_questions.set([question])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
            question_end_time=None,
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="EndedPlayer",
            hub_session_code="LIVE1",
        )
        answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Wrong Name",
            time_taken=1.0,
        )

        response = self.client.get(
            f"{reverse('admin_dashboard:api_who_that_live_responses', args=[quiz.room_code])}?review_question={question.id}"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["responses"]), 1)
        live_response = payload["responses"][0]
        self.assertEqual(live_response["answer_id"], answer.id)
        self.assertEqual(live_response["question_id"], question.id)
        self.assertEqual(live_response["participant_name"], participant.name)
        self.assertTrue(live_response["can_mark_correct"])

    def test_monitor_contains_live_response_scope_guards(self):
        question = WhoThatQuestion.objects.create(
            question_text="Guarded live question",
            image=self._image_file("live-guard.jpg"),
            correct_answer="Hedy Lamarr",
            created_by=self.user,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=15),
        )

        response = self.client.get(
            f"{reverse('admin_dashboard:who_that_monitor', args=[quiz.room_code])}?hub_session=LIVEB"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this.activeHubSessionCode = 'LIVEB';")
        self.assertContains(response, "responsesUrl.searchParams.set('hub_session', this.activeHubSessionCode);")
        self.assertContains(response, "isLiveResponseRelevant(response)")
        self.assertContains(response, "response?.question_id")
        self.assertContains(response, "response?.hub_session_code")
        self.assertContains(response, "if (!this.isLiveResponseRelevant(response)) return;")

    def test_monitor_renders_review_mode_and_back_to_overview_after_question_end(self):
        question = WhoThatQuestion.objects.create(
            question_text="Reviewable question",
            image=self._image_file("review-mode.jpg"),
            correct_answer="Rosalind Franklin",
            created_by=self.user,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            started_at=timezone.now() - timezone.timedelta(minutes=1),
            current_question=None,
        )
        quiz.selected_questions.set([question])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
            question_end_time=None,
        )

        response = self.client.get(
            f"{reverse('admin_dashboard:who_that_monitor', args=[quiz.room_code])}?hub_session=LIVEB&review_question={question.id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Question Review")
        self.assertContains(response, "The question has ended. Live responses remain visible")
        self.assertContains(response, 'id="backToQuestionOverviewBtn"', html=False)
        self.assertContains(response, f"this.reviewQuestionId = Number('{question.id}') || null;")
        self.assertContains(response, "responsesUrl.searchParams.set('review_question', String(this.reviewQuestionId));")
        self.assertContains(response, "this.enterQuestionReviewMode(this.currentQuestionId || this.reviewQuestionId);")
        self.assertContains(response, "this.backToQuestionOverview();")

    def test_host_can_promote_answer_to_correct_without_double_scoring(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("manual-promote.jpg"),
            correct_answer="Ada Lovelace",
            created_by=self.user,
            points=7,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="Bob",
            hub_session_code="hub1",
        )
        answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Grace Hopper",
            time_taken=2.0,
        )

        self.assertFalse(answer.is_correct)
        self.assertEqual(participant.total_score, 0)

        response = self.client.post(
            reverse("admin_dashboard:promote_who_that_answer_correct", args=[quiz.room_code]),
            data=json.dumps({"answer_id": answer.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        answer.refresh_from_db()
        participant.refresh_from_db()
        payload = response.json()

        self.assertTrue(payload["success"])
        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)
        self.assertEqual(payload["question_id"], question.id)
        self.assertTrue(payload["is_manual_override"])

        second_response = self.client.post(
            reverse("admin_dashboard:promote_who_that_answer_correct", args=[quiz.room_code]),
            data=json.dumps({"answer_id": answer.id}),
            content_type="application/json",
        )

        self.assertEqual(second_response.status_code, 400)
        answer.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual(participant.total_score, 1)

    @patch("admin_dashboard.views.get_channel_layer")
    def test_host_promote_broadcasts_participant_sync_update(self, channel_layer_mock):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("manual-broadcast.jpg"),
            correct_answer="Ada Lovelace",
            created_by=self.user,
            points=4,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            started_at=timezone.now(),
            current_question=question,
            question_start_time=timezone.now(),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="Carol",
            hub_session_code="hub1",
        )
        WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer="Grace Hopper",
            time_taken=1.7,
        )
        fake_channel_layer = FakeChannelLayer()
        channel_layer_mock.return_value = fake_channel_layer

        response = self.client.post(
            reverse("admin_dashboard:promote_who_that_answer_correct", args=[quiz.room_code]),
            data=json.dumps({"answer_id": WhoThatAnswer.objects.get(quiz=quiz, participant=participant, question=question).id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(fake_channel_layer.group_messages), 1)
        group_name, message = fake_channel_layer.group_messages[0]
        self.assertEqual(group_name, f"who_that_{quiz.room_code}")
        self.assertEqual(message["type"], "answer_corrected")
        self.assertEqual(message["participant_id"], participant.id)
        self.assertEqual(message["participant_name"], participant.name)
        self.assertEqual(message["question_id"], question.id)
        self.assertTrue(message["is_correct"])
        self.assertEqual(message["points_earned"], 1)
        self.assertEqual(message["total_score"], 1)

    def test_monitor_contains_manual_correct_hook(self):
        quiz = WhoThatQuiz.objects.create(creator=self.user, status="active")

        response = self.client.get(
            reverse("admin_dashboard:who_that_monitor", args=[quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "promote-correct-btn")
        self.assertContains(
            response,
            reverse("admin_dashboard:promote_who_that_answer_correct", args=[quiz.room_code]),
        )

    def test_play_view_contains_manual_correction_sync_hooks(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("manual-sync.jpg"),
            correct_answer="Ada Lovelace",
            created_by=self.user,
            points=3,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=15),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="Dana",
            hub_session_code=None,
        )

        response = self.client.get(
            reverse("who_is_that:play", args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "case 'answer_corrected':")
        self.assertContains(response, "this.onAnswerCorrected(data);")
        self.assertContains(response, "if (Number(data.participant_id) !== Number(this.participantId))")
        self.assertContains(response, "this.updateSubmittedAnswerState();")
        self.assertContains(response, "this.showCorrectAnswer(this.lastCorrectAnswerData);")

    def test_play_view_hub_lobby_return_listener_uses_rendered_session_fallback(self):
        question = WhoThatQuestion.objects.create(
            question_text="Who is this person?",
            image=self._image_file("lobby-return.jpg"),
            correct_answer="Ada Lovelace",
            created_by=self.user,
            points=1,
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            status="active",
            current_question=question,
            question_start_time=timezone.now(),
        )
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=15),
        )
        participant = WhoThatParticipant.objects.create(
            quiz=quiz,
            name="Eve",
            hub_session_code="H123",
        )

        response = self.client.get(
            f"{reverse('who_is_that:play', args=[quiz.room_code, participant.name])}?hub_session=H123"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'const configuredHubSessionCode = \'H123\';')
        self.assertContains(
            response,
            "hubCode = configuredHubSessionCode || new URLSearchParams(window.location.search).get('hub_session') || '';",
        )
        self.assertContains(response, "localStorage.setItem('hub_session_code', hubCode);")
        self.assertContains(response, "if (data.type === 'players_recalled_to_lobby') {")
        self.assertContains(response, "lobbyReturnController.returnToLobby({ markInactive: false });")
