import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from Estimation.consumers import EstimationConsumer
from Estimation.models import EstimationParticipant, EstimationQuiz
from QuizGame.models import Quiz, QuizParticipant, QuizQuestion
from black_jack_quiz.models import BlackJackQuiz
from games_hub.consumers import HubConsumer
from games_hub.models import HubGameStep, HubParticipant, HubSession


class LobbyReturnFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='lobby_guard_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)

        self.session = HubSession.objects.create(code='LOBBY1', name='Lobby Return Session')
        HubParticipant.objects.create(session=self.session, nickname='Alice')
        HubParticipant.objects.create(session=self.session, nickname='Bob')

        self.quiz = Quiz.objects.create(
            creator=self.user,
            title='Quick Quiz',
            status='active',
        )
        self.question = QuizQuestion.objects.create(
            question_text='Was ist 2 + 2?',
            question_type='multiple_choice',
            option_a='4',
            option_b='5',
            correct_answer='A',
            created_by=self.user,
        )
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])

        self.estimation = EstimationQuiz.objects.create(
            creator=self.user,
            title='Estimation',
            status='waiting',
        )

        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='estimation',
            room_code=self.estimation.room_code,
            title=self.estimation.title,
        )

    def test_session_lobby_presence_endpoint_reports_players_still_in_game(self):
        QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )

        response = self.client.get(
            reverse('games_hub:session_lobby_presence_api', args=[self.session.code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertFalse(payload['allowed'])
        self.assertFalse(payload['all_in_lobby'])
        self.assertEqual(payload['not_in_lobby_count'], 1)
        self.assertEqual(payload['in_lobby_count'], 1)
        self.assertEqual(payload['participants_not_in_lobby'][0]['name'], 'Alice')
        self.assertEqual(payload['participants_not_in_lobby'][0]['games'][0]['game_key'], 'quiz')

    def test_participant_return_to_lobby_marks_only_current_player_inactive(self):
        alice = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        bob = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )

        response = self.client.post(
            reverse('games_hub:participant_return_to_lobby', args=[self.session.code]),
            data=json.dumps({
                'game_key': 'quiz',
                'room_code': self.quiz.room_code,
                'participant_name': 'Alice',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertFalse(alice.is_active)
        self.assertTrue(bob.is_active)

    def test_recall_to_lobby_marks_all_active_session_participants_inactive(self):
        alice = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        bob = EstimationParticipant.objects.create(
            quiz=self.estimation,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )

        response = self.client.post(
            reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['all_in_lobby'])

        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertFalse(alice.is_active)
        self.assertFalse(bob.is_active)

    def test_consumer_start_is_blocked_when_players_are_not_in_lobby(self):
        QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )

        consumer = EstimationConsumer()
        consumer.room_code = self.estimation.room_code
        consumer.room_group_name = f'estimation_{self.estimation.room_code}'
        consumer.send = AsyncMock()

        async_to_sync(consumer.handle_admin_start_quiz)({})

        self.estimation.refresh_from_db()
        self.assertEqual(self.estimation.status, 'waiting')
        consumer.send.assert_awaited_once()

        payload = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(payload['type'], 'participants_not_in_lobby')
        self.assertEqual(payload['not_in_lobby_count'], 1)
        self.assertEqual(payload['participants_not_in_lobby'][0]['name'], 'Alice')

    def test_game_monitor_renders_lobby_return_guard_modal(self):
        response = self.client.get(
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.quiz.room_code])}?hub_session={self.session.code}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'lobbyReturnGuardModal')
        self.assertContains(response, 'Teilnehmer sind noch im Spiel')
        self.assertContains(response, 'Teilnehmer in die Lobby zurückschicken (10)')
        self.assertContains(response, '/lobby-presence/')
        self.assertContains(response, '/start-recall-countdown/')
        self.assertContains(response, '/recall-to-lobby/')

    def test_session_monitor_uses_same_recall_button_flow(self):
        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'recallLobbyBtn')
        self.assertContains(response, 'Teilnehmer in die Lobby zur')
        self.assertContains(response, '/lobby-presence/')
        self.assertNotContains(response, 'Debug-only')
        self.assertNotContains(response, "type: 'recall_to_lobby'")

    def test_all_player_templates_use_manual_lobby_return_flow(self):
        template_paths = [
            'templates/quiz/play.html',
            'templates/estimation/play.html',
            'templates/assign/play.html',
            'templates/where_is_this/play.html',
            'templates/who_is_lying/play.html',
            'templates/who_is_that/play.html',
            'templates/black_jack_quiz/play.html',
            'templates/clue_rush/play.html',
            'templates/sorting_ladder/play.html',
        ]

        for relative_path in template_paths:
            with self.subTest(template=relative_path):
                content = Path(settings.BASE_DIR / relative_path).read_text(encoding='utf-8')
                self.assertIn("_hub_return_to_lobby_player.html", content)
                self.assertIn('createHubLobbyReturnController', content)
                self.assertIn('lobby_return_countdown_started', content)
                self.assertIn('startRecallCountdown(data);', content)
                self.assertIn("players_recalled_to_lobby", content)
                self.assertIn("returnToLobby({ markInactive: false });", content)
                self.assertNotIn("After 2 seconds, return to lobby with nickname preserved", content)

    def test_shared_player_lobby_return_include_contains_countdown_banner(self):
        content = Path(settings.BASE_DIR / 'templates/includes/_hub_return_to_lobby_player.html').read_text(encoding='utf-8')
        self.assertIn('Automatisches Zurückkehren in die Lobby in', content)
        self.assertIn('syncRecallCountdownState', content)
        self.assertIn('startRecallCountdown', content)

    def test_recall_countdown_state_is_inactive_by_default(self):
        response = self.client.get(
            reverse('games_hub:session_recall_countdown_state_api', args=[self.session.code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertFalse(payload['active'])
        self.assertEqual(payload['remaining_seconds'], 0)

    def test_start_recall_countdown_endpoint_exposes_active_timer_state(self):
        response = self.client.post(
            reverse('games_hub:start_recall_countdown', args=[self.session.code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['active'])
        self.assertEqual(payload['duration_seconds'], 10)
        self.assertGreaterEqual(payload['remaining_seconds'], 9)
        self.assertIsNotNone(payload['ends_at'])

        state_response = self.client.get(
            reverse('games_hub:session_recall_countdown_state_api', args=[self.session.code])
        )
        state_payload = state_response.json()
        self.assertTrue(state_payload['active'])
        self.assertGreaterEqual(state_payload['remaining_seconds'], 9)

    def test_hub_join_does_not_redirect_to_active_blackjack_without_started_at(self):
        session = HubSession.objects.create(code='BJROUTE1', name='Blackjack Routing')
        blackjack_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Prepared Black Jack',
            status='active',
            started_at=None,
        )
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='blackjack',
            room_code=blackjack_quiz.room_code,
            title=blackjack_quiz.title,
        )
        consumer = HubConsumer()
        consumer.session_code = session.code

        game_key, room_code = async_to_sync(consumer.get_active_game_for_session)()

        self.assertIsNone(game_key)
        self.assertIsNone(room_code)

    def test_hub_join_redirects_to_blackjack_after_real_start(self):
        session = HubSession.objects.create(code='BJROUTE2', name='Blackjack Routing')
        blackjack_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Started Black Jack',
            status='active',
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='blackjack',
            room_code=blackjack_quiz.room_code,
            title=blackjack_quiz.title,
        )
        consumer = HubConsumer()
        consumer.session_code = session.code

        game_key, room_code = async_to_sync(consumer.get_active_game_for_session)()

        self.assertEqual(game_key, 'blackjack')
        self.assertEqual(room_code, blackjack_quiz.room_code)

    def test_navigate_direct_does_not_route_waiting_blackjack_before_start(self):
        session = HubSession.objects.create(code='BJROUTE3', name='Blackjack Routing')
        blackjack_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Waiting Black Jack',
            status='waiting',
        )
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='blackjack',
            room_code=blackjack_quiz.room_code,
            title=blackjack_quiz.title,
        )
        consumer = HubConsumer()
        consumer.session_code = session.code
        consumer.group_name = f'hub_{session.code}'
        consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())

        async_to_sync(consumer.handle_navigate_direct)({
            'game_key': 'blackjack',
            'room_code': blackjack_quiz.room_code,
        })

        consumer.channel_layer.group_send.assert_not_awaited()
        blackjack_quiz.refresh_from_db()
        self.assertEqual(blackjack_quiz.status, 'waiting')
        self.assertIsNone(blackjack_quiz.started_at)

    def test_navigate_direct_routes_blackjack_after_real_start(self):
        session = HubSession.objects.create(code='BJROUTE4', name='Blackjack Routing')
        blackjack_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Started Black Jack',
            status='active',
            started_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='blackjack',
            room_code=blackjack_quiz.room_code,
            title=blackjack_quiz.title,
        )
        consumer = HubConsumer()
        consumer.session_code = session.code
        consumer.group_name = f'hub_{session.code}'
        consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())

        async_to_sync(consumer.handle_navigate_direct)({
            'game_key': 'blackjack',
            'room_code': blackjack_quiz.room_code,
        })

        consumer.channel_layer.group_send.assert_awaited_once()
        group_name, event = consumer.channel_layer.group_send.await_args.args
        self.assertEqual(group_name, f'hub_{session.code}')
        self.assertEqual(event['type'], 'navigate')
        self.assertEqual(event['step']['game_key'], 'blackjack')
        self.assertEqual(event['step']['room_code'], blackjack_quiz.room_code)
