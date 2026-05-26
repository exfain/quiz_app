import json
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from Estimation.consumers import EstimationConsumer
from Estimation.models import EstimationQuiz
from black_jack_quiz.models import BlackJackQuiz
from QuizGame.consumers import QuizConsumer
from QuizGame.models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion, QuizSession
from games_hub.consumers import HubConsumer
from games_hub.models import HubGameStep, HubSession


class ActiveGameGuardTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='guard_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)
        self.session = HubSession.objects.create(code='ACTIVE1', name='Session Guard')

        self.active_quiz = Quiz.objects.create(
            creator=self.user,
            title='Aktives Quick Quiz',
            status='active',
            started_at=timezone.now(),
        )
        self.active_question = QuizQuestion.objects.create(
            question_text='Was ist 2 + 2?',
            question_type='multiple_choice',
            option_a='4',
            option_b='5',
            correct_answer='A',
            created_by=self.user,
        )
        self.active_quiz.current_question = self.active_question
        self.active_quiz.question_start_time = timezone.now()
        self.active_quiz.save(update_fields=['current_question', 'question_start_time'])
        self.active_session = QuizSession.objects.create(
            quiz=self.active_quiz,
            current_question_number=3,
            total_questions_sent=3,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=30),
        )

        self.target_estimation = EstimationQuiz.objects.create(
            creator=self.user,
            title='Neue Estimation',
            status='waiting',
        )

        self.active_step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=self.active_quiz.room_code,
            title=self.active_quiz.title,
        )
        self.target_step = HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='estimation',
            room_code=self.target_estimation.room_code,
            title=self.target_estimation.title,
        )

    def _activate_target(self, action=None):
        payload = {
            'game_key': 'estimation',
            'room_code': self.target_estimation.room_code,
        }
        if action:
            payload['action'] = action
        return self.client.post(
            reverse('games_hub:activate_session_game', args=[self.session.code]),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def _create_other_session(self):
        other_session = HubSession.objects.create(code='ACTIVE2', name='Another Session', is_active=False)
        HubGameStep.objects.create(
            session=other_session,
            order=0,
            game_key='estimation',
            room_code=self.target_estimation.room_code,
            title='Andere Estimation',
        )
        return other_session

    def _start_target_http(self, session_code=None):
        url = reverse('admin_dashboard:start_estimation_quiz', args=[self.target_estimation.room_code])
        if session_code:
            url = f'{url}?hub_session={session_code}'
        return self.client.post(
            url,
            data=json.dumps({}),
            content_type='application/json',
        )

    def test_activate_endpoint_returns_conflict_for_existing_active_game(self):
        response = self._activate_target()

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertTrue(payload['conflict'])
        self.assertEqual(payload['active_game']['display_name'], self.active_quiz.title)

        self.target_estimation.refresh_from_db()
        self.assertEqual(self.target_estimation.status, 'waiting')

    def test_activate_endpoint_can_set_active_game_inactive_without_losing_progress(self):
        response = self._activate_target(action='inactive')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        self.active_quiz.refresh_from_db()
        self.active_session.refresh_from_db()
        self.target_estimation.refresh_from_db()

        self.assertEqual(self.active_quiz.status, 'inactive')
        self.assertEqual(self.active_quiz.current_question_id, self.active_question.id)
        self.assertTrue(self.active_session.is_question_active)
        self.assertEqual(self.active_session.current_question_number, 3)
        self.assertEqual(self.target_estimation.status, 'active')

    def test_activate_endpoint_cleans_up_multiple_active_games_to_single_active_game(self):
        extra_quiz = Quiz.objects.create(
            creator=self.user,
            title='Zweites aktives Quiz',
            status='active',
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=self.session,
            order=2,
            game_key='quiz',
            room_code=extra_quiz.room_code,
            title=extra_quiz.title,
        )

        response = self._activate_target(action='inactive')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        self.active_quiz.refresh_from_db()
        extra_quiz.refresh_from_db()
        self.target_estimation.refresh_from_db()

        self.assertEqual(self.active_quiz.status, 'inactive')
        self.assertEqual(extra_quiz.status, 'inactive')
        self.assertEqual(self.target_estimation.status, 'active')
        self.assertEqual(
            [self.active_quiz.status, extra_quiz.status, self.target_estimation.status].count('active'),
            1,
        )

    def test_activate_endpoint_can_end_active_game_before_starting_next(self):
        response = self._activate_target(action='end')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        self.active_quiz.refresh_from_db()
        self.active_session.refresh_from_db()
        self.target_estimation.refresh_from_db()

        self.assertEqual(self.active_quiz.status, 'completed')
        self.assertIsNotNone(self.active_quiz.ended_at)
        self.assertIsNone(self.active_quiz.current_question_id)
        self.assertFalse(self.active_session.is_question_active)
        self.assertEqual(self.target_estimation.status, 'active')

    def test_activate_endpoint_reactivates_inactive_game_without_resetting_progress(self):
        self.active_quiz.status = 'inactive'
        self.active_quiz.save(update_fields=['status'])
        self.target_estimation.status = 'inactive'
        self.target_estimation.started_at = timezone.now() - timezone.timedelta(minutes=5)
        self.target_estimation.save(update_fields=['status', 'started_at'])

        response = self._activate_target()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        self.target_estimation.refresh_from_db()
        self.assertEqual(self.target_estimation.status, 'active')
        self.assertIsNotNone(self.target_estimation.started_at)

    def test_starting_another_session_sets_active_game_in_previous_session_inactive(self):
        other_session = self._create_other_session()

        HubSession.activate_exclusive(other_session.code)

        self.session.refresh_from_db()
        other_session.refresh_from_db()
        self.active_quiz.refresh_from_db()
        self.active_session.refresh_from_db()

        self.assertFalse(self.session.is_active)
        self.assertTrue(other_session.is_active)
        self.assertEqual(self.active_quiz.status, 'inactive')
        self.assertEqual(self.active_quiz.current_question_id, self.active_question.id)
        self.assertIsNotNone(self.active_quiz.question_start_time)
        self.assertTrue(self.active_session.is_question_active)
        self.assertEqual(self.active_session.current_question_number, 3)

    def test_reactivated_session_does_not_auto_reactivate_previous_game(self):
        other_session = self._create_other_session()

        HubSession.activate_exclusive(other_session.code)
        HubSession.activate_exclusive(self.session.code)

        self.session.refresh_from_db()
        other_session.refresh_from_db()
        self.active_quiz.refresh_from_db()

        self.assertTrue(self.session.is_active)
        self.assertFalse(other_session.is_active)
        self.assertEqual(self.active_quiz.status, 'inactive')

    def test_game_can_be_manually_reactivated_after_session_reactivation(self):
        other_session = self._create_other_session()

        HubSession.activate_exclusive(other_session.code)
        HubSession.activate_exclusive(self.session.code)

        response = self.client.post(
            reverse('games_hub:activate_session_game', args=[self.session.code]),
            data=json.dumps({
                'game_key': 'quiz',
                'room_code': self.active_quiz.room_code,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        self.active_quiz.refresh_from_db()
        self.assertEqual(self.active_quiz.status, 'active')
        self.assertEqual(self.active_quiz.current_question_id, self.active_question.id)

    def test_reorder_steps_persists_after_reload(self):
        response = self.client.post(
            reverse('games_hub:reorder_steps', args=[self.session.code]),
            data=json.dumps({'order': [self.target_step.id, self.active_step.id]}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        self.active_step.refresh_from_db()
        self.target_step.refresh_from_db()
        self.assertEqual(self.target_step.order, 0)
        self.assertEqual(self.active_step.order, 1)

        monitor_response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))
        self.assertEqual(monitor_response.status_code, 200)
        steps = list(monitor_response.context['steps'])
        self.assertEqual([step.id for step in steps], [self.target_step.id, self.active_step.id])

    def test_reordered_steps_are_not_reverted_by_existing_gameplay_step_sync(self):
        reorder_response = self.client.post(
            reverse('games_hub:reorder_steps', args=[self.session.code]),
            data=json.dumps({'order': [self.target_step.id, self.active_step.id]}),
            content_type='application/json',
        )
        self.assertEqual(reorder_response.status_code, 200)

        consumer = HubConsumer()
        consumer.session_code = self.session.code
        async_to_sync(consumer.ensure_step_for_room)('quiz', self.active_quiz.room_code, self.active_quiz.title)
        async_to_sync(consumer.ensure_step_for_room)('estimation', self.target_estimation.room_code, self.target_estimation.title)

        refreshed_steps = list(self.session.steps.order_by('order'))
        self.assertEqual([step.id for step in refreshed_steps], [self.target_step.id, self.active_step.id])
        self.assertEqual(self.session.steps.count(), 2)

    def test_hub_monitor_exposes_next_planned_step_id_after_completed_steps(self):
        self.active_quiz.status = 'completed'
        self.active_quiz.ended_at = timezone.now()
        self.active_quiz.save(update_fields=['status', 'ended_at'])

        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['next_planned_step_id'], self.target_step.id)

    def test_hub_monitor_renders_out_of_order_warning_dialog(self):
        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'outOfOrderStartModal')
        self.assertContains(response, 'Reihenfolge verlassen?')
        self.assertContains(response, 'Dieses Spiel ist nicht das nächste geplante Spiel.')
        self.assertContains(response, 'Trotzdem fortfahren')

    def test_game_monitor_renders_active_game_conflict_dialog(self):
        response = self.client.get(
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.active_quiz.room_code])}?hub_session={self.session.code}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'activeGameConflictModal')
        self.assertContains(response, 'Ein anderes Spiel ist noch aktiv')
        self.assertContains(response, 'Ein anderes Spiel ist noch aktiv: ${activeName}')
        self.assertContains(response, 'Aktives Spiel beenden')
        self.assertContains(response, 'Aktives Spiel auf inaktiv setzen')
        self.assertContains(response, 'Zurück')

    def test_inactive_game_monitor_renders_reactivate_button(self):
        self.active_quiz.status = 'inactive'
        self.active_quiz.save(update_fields=['status'])

        response = self.client.get(
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.active_quiz.room_code])}?hub_session={self.session.code}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Spiel reaktivieren')
        self.assertContains(response, 'startQuizBtn')

    def test_active_game_monitor_renders_set_inactive_button(self):
        response = self.client.get(
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.active_quiz.room_code])}?hub_session={self.session.code}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Set inactive')
        self.assertContains(response, 'setInactiveBtn')

    def test_active_game_monitor_renders_leave_active_game_dialog(self):
        response = self.client.get(
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.active_quiz.room_code])}?hub_session={self.session.code}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'activeGameLeaveModal')
        self.assertContains(response, 'Aktives Spiel verlassen')
        self.assertContains(response, 'Back to overview')
        self.assertContains(response, 'Not all sets/questions were played yet.')
        self.assertContains(response, 'Zur&uuml;ck')

    def test_completed_game_monitor_hides_set_inactive_button(self):
        self.active_quiz.status = 'completed'
        self.active_quiz.ended_at = timezone.now()
        self.active_quiz.save(update_fields=['status', 'ended_at'])

        response = self.client.get(
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.active_quiz.room_code])}?hub_session={self.session.code}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'setInactiveBtn')
        self.assertNotContains(response, 'Set inactive')

    def test_set_inactive_helper_preserves_current_question(self):
        consumer = QuizConsumer()
        consumer.room_code = self.active_quiz.room_code

        async_to_sync(consumer.set_quiz_inactive_db)(self.active_quiz.id)

        self.active_quiz.refresh_from_db()
        self.assertEqual(self.active_quiz.status, 'inactive')
        self.assertEqual(self.active_quiz.current_question_id, self.active_question.id)
        self.assertIsNotNone(self.active_quiz.question_start_time)

    def test_inactive_quiz_rejects_new_answers(self):
        QuizParticipant.objects.create(
            quiz=self.active_quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        self.active_quiz.status = 'inactive'
        self.active_quiz.save(update_fields=['status'])

        consumer = QuizConsumer()
        consumer.room_code = self.active_quiz.room_code

        result = async_to_sync(consumer.save_participant_answer)(
            'Alice',
            self.session.code,
            'A',
            1.5,
        )

        self.assertIsNone(result)
        self.assertFalse(QuizAnswer.objects.filter(quiz=self.active_quiz).exists())

    def test_direct_consumer_start_cannot_bypass_active_game_guard(self):
        consumer = EstimationConsumer()
        consumer.room_code = self.target_estimation.room_code
        consumer.room_group_name = f'estimation_{self.target_estimation.room_code}'
        consumer.send = AsyncMock()

        async_to_sync(consumer.handle_admin_start_quiz)({})

        self.target_estimation.refresh_from_db()
        self.assertEqual(self.target_estimation.status, 'waiting')
        consumer.send.assert_awaited_once()

        payload = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(payload['type'], 'active_game_conflict')
        self.assertEqual(payload['active_game']['display_name'], self.active_quiz.title)

    def test_http_start_endpoint_cannot_bypass_active_game_guard(self):
        response = self._start_target_http(self.session.code)

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertTrue(payload['conflict'])
        self.assertEqual(payload['active_game']['display_name'], self.active_quiz.title)

        self.target_estimation.refresh_from_db()
        self.assertEqual(self.target_estimation.status, 'waiting')

    def test_http_start_endpoint_can_reactivate_inactive_game_when_no_conflict(self):
        self.active_quiz.status = 'inactive'
        self.active_quiz.save(update_fields=['status'])
        self.target_estimation.status = 'inactive'
        self.target_estimation.save(update_fields=['status'])

        response = self._start_target_http(self.session.code)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

        self.target_estimation.refresh_from_db()
        self.assertEqual(self.target_estimation.status, 'active')
        self.assertIsNotNone(self.target_estimation.started_at)

    def test_room_based_http_start_prefers_active_session_over_newer_inactive_room_reuse(self):
        self._create_other_session()

        response = self._start_target_http()

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertTrue(payload['conflict'])
        self.assertEqual(payload['active_game']['display_name'], self.active_quiz.title)

        self.target_estimation.refresh_from_db()
        self.assertEqual(self.target_estimation.status, 'waiting')

    def test_inactive_session_cannot_activate_or_reactivate_game(self):
        self.session.started_at = timezone.now() - timezone.timedelta(minutes=10)
        self.session.is_active = False
        self.session.save(update_fields=['started_at', 'is_active'])
        self.target_estimation.status = 'inactive'
        self.target_estimation.save(update_fields=['status'])

        response = self._activate_target()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('inaktiv', payload['error'].lower())

        self.target_estimation.refresh_from_db()
        self.assertEqual(self.target_estimation.status, 'inactive')

    def test_hub_monitor_labels_inactive_games_as_inactive(self):
        self.active_quiz.status = 'inactive'
        self.active_quiz.save(update_fields=['status'])
        self.target_estimation.status = 'inactive'
        self.target_estimation.save(update_fields=['status'])
        self.session.started_at = timezone.now() - timezone.timedelta(minutes=5)
        self.session.save(update_fields=['started_at'])

        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'inactive')
        self.assertContains(response, 'Reaktivieren')
        self.assertNotContains(response, 'Not started yet')

    def test_hub_monitor_shows_static_session_ended_label_without_end_button(self):
        self.session.ended_at = timezone.now()
        self.session.is_active = False
        self.session.save(update_fields=['ended_at', 'is_active'])

        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Session beendet')
        self.assertContains(response, 'sessionEndedLabel')
        self.assertNotContains(response, 'id="endSessionBtn"', html=False)

    def test_session_leaderboard_hides_session_monitor_dashboard_button(self):
        self.session.ended_at = timezone.now()
        self.session.is_active = False
        self.session.save(update_fields=['ended_at', 'is_active'])

        response = self.client.get(reverse('games_hub:session_leaderboard', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Back to Session Monitor Dashboard')

    def test_session_leaderboard_shows_overview_button_for_host(self):
        self.session.ended_at = timezone.now()
        self.session.is_active = False
        self.session.save(update_fields=['ended_at', 'is_active'])

        response = self.client.get(reverse('games_hub:session_leaderboard', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Zur Übersicht')
        self.assertContains(response, reverse('admin_dashboard:sessions_overview'))

    def test_session_leaderboard_keeps_overview_button_hidden_for_participants(self):
        self.session.ended_at = timezone.now()
        self.session.is_active = False
        self.session.save(update_fields=['ended_at', 'is_active'])

        self.client.logout()
        response = self.client.get(reverse('games_hub:session_leaderboard', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Zur Übersicht')
        self.assertNotContains(response, reverse('admin_dashboard:sessions_overview'))

    def test_hub_monitor_renders_restored_session_monitor_handlers(self):
        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'legacyBrokenSessionMonitorScript')
        self.assertContains(response, 'new WebSocket(`${wsScheme}//${window.location.host}/ws/hub/${SESSION_CODE}/`)')
        self.assertContains(response, '/hub/api/session/${SESSION_CODE}/activate-game/')
        self.assertContains(response, '/hub/api/games/${gameKey}/instances/')
        self.assertContains(response, '/hub/api/session/${SESSION_CODE}/add-step/')
        self.assertContains(response, '/hub/api/session/${SESSION_CODE}/reorder-steps/')
        self.assertContains(response, '/hub/api/session/${SESSION_CODE}/delete-step/${button.dataset.stepId}/')
        self.assertContains(response, 'buildMonitorUrl')
        self.assertContains(response, "sendWhenSocketOpen({ type: 'start_session' })")

    def test_get_game_instances_endpoint_lists_existing_blackjack_games_for_session_modal(self):
        blackjack_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Session Modal Black Jack',
            status='waiting',
        )

        response = self.client.get(reverse('games_hub:get_game_instances', args=['blackjack']))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn('instances', payload)
        self.assertTrue(any(item['id'] == blackjack_quiz.id for item in payload['instances']))
