import json
import re
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
from black_jack_quiz.consumers import BlackJackConsumer
from black_jack_quiz.models import BlackJackQuiz
from buzzer.consumers import BuzzerConsumer
from buzzer.models import BuzzerGame
from games_hub.authoritative_state import disconnect_socket_connection, register_socket_connection
from games_hub.consumers import HubConsumer
from games_hub.models import HubGameStep, HubParticipant, HubSession
from host_points.consumers import HostPointsConsumer
from host_points.models import HostPointsGame
from wann_war_das.consumers import WannWarDasConsumer
from wann_war_das.models import WannWarDasGame
from who_is_that.models import WhoThatParticipant, WhoThatQuiz


class LobbyReturnFlowTests(TransactionTestCase):
    MONITOR_START_BUTTONS = {
        'quiz': ('templates/admin_dashboard/quiz_monitor.html', 'startQuizBtn'),
        'assign': ('templates/admin_dashboard/assign_monitor.html', 'startQuizBtn'),
        'estimation': ('templates/admin_dashboard/estimation_monitor.html', 'startQuizBtn'),
        'where': ('templates/admin_dashboard/where_monitor.html', 'startQuizBtn'),
        'who': ('templates/admin_dashboard/who_lying_monitor.html', 'startQuizBtn'),
        'who_that': ('templates/admin_dashboard/who_that_monitor.html', 'startQuizBtn'),
        'blackjack': ('templates/admin_dashboard/blackjack_monitor.html', 'startQuizBtn'),
        'sorting_ladder': ('templates/admin_dashboard/sorting_ladder_monitor.html', 'startQuizBtn'),
        'clue_rush': ('templates/admin_dashboard/clue_rush_monitor.html', 'startQuizBtn'),
        'buzzer': ('templates/admin_dashboard/buzzer_monitor.html', 'startGameBtn'),
        'host_points': ('templates/admin_dashboard/host_points_monitor.html', 'startQuizBtn'),
        'wann_war_das': ('templates/admin_dashboard/wann_war_das_monitor.html', 'startGameBtn'),
        'wer_weiss_mehr': ('templates/admin_dashboard/wer_weiss_mehr_monitor.html', 'startQuizBtn'),
    }

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

    def test_active_game_assignment_blocks_start_independent_of_socket_presence(self):
        QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        register_socket_connection(
            channel_name='alice.reconnecting',
            session_code=self.session.code,
            participant_name='Alice',
            scope_kind='game',
            game_key='quiz',
            room_code=self.quiz.room_code,
        )

        reconnecting = self.client.get(
            reverse('games_hub:session_lobby_presence_api', args=[self.session.code])
        ).json()
        self.assertFalse(reconnecting['all_in_lobby'])
        self.assertEqual(reconnecting['participants_not_in_lobby'][0]['name'], 'Alice')

        self.assertTrue(disconnect_socket_connection('alice.reconnecting'))
        disconnected = self.client.get(
            reverse('games_hub:session_lobby_presence_api', args=[self.session.code])
        ).json()
        self.assertFalse(disconnected['all_in_lobby'])
        self.assertEqual(disconnected['participants_not_in_lobby'][0]['name'], 'Alice')

    def test_who_that_waiting_player_blocks_blackjack_start(self):
        who_that = WhoThatQuiz.objects.create(
            creator=self.user,
            title='Who Is That Waiting',
            status='waiting',
        )
        blackjack = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Blocked Black Jack',
            status='waiting',
        )
        HubGameStep.objects.create(
            session=self.session,
            order=2,
            game_key='who_that',
            room_code=who_that.room_code,
            title=who_that.title,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=3,
            game_key='blackjack',
            room_code=blackjack.room_code,
            title=blackjack.title,
        )
        WhoThatParticipant.objects.create(
            quiz=who_that,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )
        register_socket_connection(
            channel_name='alice.lobby-only-socket',
            session_code=self.session.code,
            participant_name='Alice',
            scope_kind='lobby',
        )

        presence = self.client.get(
            reverse('games_hub:session_lobby_presence_api', args=[self.session.code])
        ).json()

        self.assertFalse(presence['all_in_lobby'])
        self.assertEqual(presence['participants_not_in_lobby'], [{
            'name': 'Bob',
            'games': [{
                'game_key': 'who_that',
                'room_code': who_that.room_code,
                'title': who_that.title,
            }],
        }])

        consumer = BlackJackConsumer()
        consumer.room_code = blackjack.room_code
        consumer.room_group_name = f'blackjack_{blackjack.room_code}'
        consumer.send = AsyncMock()
        async_to_sync(consumer.handle_admin_start_quiz)({})

        blackjack.refresh_from_db()
        self.assertEqual(blackjack.status, 'waiting')
        self.assertIsNone(blackjack.started_at)
        payload = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(payload['type'], 'participants_not_in_lobby')
        self.assertEqual(payload['participants_not_in_lobby'][0]['name'], 'Bob')

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

        repeated_response = self.client.post(
            reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
        )
        self.assertEqual(repeated_response.status_code, 200)
        self.assertTrue(repeated_response.json()['all_in_lobby'])

    def test_recall_keeps_existing_lobby_players_and_stale_socket_in_lobby(self):
        alice = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        register_socket_connection(
            channel_name='alice.stale-game-socket',
            session_code=self.session.code,
            participant_name='Alice',
            scope_kind='game',
            game_key='quiz',
            room_code=self.quiz.room_code,
        )

        response = self.client.post(
            reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
        )

        alice.refresh_from_db()
        payload = response.json()
        self.assertFalse(alice.is_active)
        self.assertTrue(payload['all_in_lobby'])
        self.assertEqual(payload['participants_in_lobby'], [
            {'name': 'Alice'},
            {'name': 'Bob'},
        ])

        reloaded_presence = self.client.get(
            reverse('games_hub:session_lobby_presence_api', args=[self.session.code])
        ).json()
        self.assertTrue(reloaded_presence['all_in_lobby'])

    def test_new_game_can_start_after_recall(self):
        old_participant = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        response = self.client.post(
            reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
        )
        self.assertTrue(response.json()['all_in_lobby'])
        self.quiz.status = 'inactive'
        self.quiz.save(update_fields=['status'])

        consumer = EstimationConsumer()
        consumer.room_code = self.estimation.room_code
        consumer.room_group_name = f'estimation_{self.estimation.room_code}'
        consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
        consumer.send = AsyncMock()
        async_to_sync(consumer.handle_admin_start_quiz)({})

        old_participant.refresh_from_db()
        self.estimation.refresh_from_db()
        self.assertFalse(old_participant.is_active)
        self.assertEqual(self.estimation.status, 'active')
        self.assertFalse(any(
            json.loads(call.kwargs['text_data']).get('type') == 'participants_not_in_lobby'
            for call in consumer.send.await_args_list
            if call.kwargs.get('text_data')
        ))

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

    def test_all_registered_host_start_buttons_use_the_semantic_guard(self):
        registered_game_keys = {key for key, _label in HubGameStep.GAME_CHOICES}
        self.assertEqual(set(self.MONITOR_START_BUTTONS), registered_game_keys)

        for game_key, (relative_path, button_id) in self.MONITOR_START_BUTTONS.items():
            with self.subTest(game_key=game_key, button_id=button_id):
                source = Path(settings.BASE_DIR / relative_path).read_text(encoding='utf-8')
                button = re.search(
                    rf'<button\b(?=[^>]*\bid="{re.escape(button_id)}")[^>]*>',
                    source,
                    re.DOTALL,
                )
                self.assertIsNotNone(button)
                self.assertIn('data-host-game-start', button.group(0))
                self.assertEqual(source.count('data-host-game-start'), 1)

    def test_shared_start_and_leave_guards_are_not_bound_to_legacy_ids(self):
        source = Path(settings.BASE_DIR / 'templates/admin_dashboard/base.html').read_text(encoding='utf-8')

        self.assertIn("const startButtonSelector = '[data-host-game-start]';", source)
        self.assertIn("const leaveButtonSelector = '[data-host-game-leave]';", source)
        self.assertNotIn("const startButtonSelector = '#startQuizBtn';", source)
        self.assertNotIn("event.target.closest('#backToHubBtn')", source)
        self.assertIn('window.hostGameStartGuard = {', source)
        self.assertIn('showLobbyReturn: showLobbyReturnGuard', source)
        self.assertIn('showActiveGameConflict', source)
        self.assertIn('installBrowserLeaveGuard();', source)

    def test_start_guard_preflights_without_activating_target_game(self):
        source = Path(settings.BASE_DIR / 'templates/admin_dashboard/base.html').read_text(encoding='utf-8')

        self.assertIn('const previewResult = await requestActivation(context, null, true);', source)
        self.assertIn('requestActivation(guardState.pendingContext, action, true)', source)
        self.assertNotIn('const result = await requestActivation(context);', source)

    def test_every_registered_monitor_uses_the_shared_leave_hook(self):
        for game_key, (relative_path, _button_id) in self.MONITOR_START_BUTTONS.items():
            with self.subTest(game_key=game_key):
                source = Path(settings.BASE_DIR / relative_path).read_text(encoding='utf-8')
                self.assertIn('data-host-game-leave', source)

    def test_new_monitors_route_start_rejections_and_leave_through_shared_guards(self):
        for relative_path in (
            'templates/admin_dashboard/buzzer_monitor.html',
            'templates/admin_dashboard/host_points_monitor.html',
            'templates/admin_dashboard/wann_war_das_monitor.html',
            'templates/admin_dashboard/wer_weiss_mehr_monitor.html',
        ):
            with self.subTest(template=relative_path):
                source = Path(settings.BASE_DIR / relative_path).read_text(encoding='utf-8')
                self.assertIn('window.hostGameStartGuard?.showLobbyReturn', source)
                self.assertIn('window.hostGameStartGuard?.showActiveGameConflict', source)
                self.assertIn('window.adminGameMonitor = {', source)
                self.assertIn('get isUnfinished()', source)
                self.assertIn('data-host-game-leave', source)

    def test_new_monitor_inactive_actions_preserve_started_game_context(self):
        started_at = timezone.now()
        cases = (
            (BuzzerGame, BuzzerConsumer, 'Buzzer Leave Guard'),
            (HostPointsGame, HostPointsConsumer, 'Host Points Leave Guard'),
            (WannWarDasGame, WannWarDasConsumer, 'Wann War Das Leave Guard'),
        )

        for model, consumer_class, title in cases:
            with self.subTest(model=model.__name__):
                game = model.objects.create(
                    creator=self.user,
                    title=title,
                    status='active',
                    started_at=started_at,
                )
                consumer = consumer_class()
                consumer.room_code = game.room_code

                self.assertTrue(async_to_sync(consumer.set_game_inactive)())
                self.assertFalse(async_to_sync(consumer.set_game_inactive)())
                game.refresh_from_db()
                self.assertEqual(game.status, 'inactive')
                self.assertEqual(game.started_at, started_at)
                self.assertIsNone(game.ended_at)

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
            'templates/wann_war_das/play.html',
            'templates/buzzer/play.html',
            'templates/host_points/play.html',
        ]

        for relative_path in template_paths:
            with self.subTest(template=relative_path):
                content = Path(settings.BASE_DIR / relative_path).read_text(encoding='utf-8')
                self.assertIn("_hub_return_to_lobby_player.html", content)
                self.assertIn('createHubLobbyReturnController', content)
                has_inline_hub_recall = all((
                    '/ws/hub/' in content,
                    'lobby_return_countdown_started' in content,
                    'startRecallCountdown(data);' in content,
                    'players_recalled_to_lobby' in content,
                    'returnToLobby({ markInactive: false });' in content,
                ))
                uses_shared_hub_recall = 'connectHubRecallSocket();' in content
                self.assertTrue(
                    has_inline_hub_recall or uses_shared_hub_recall,
                    f'{relative_path} does not subscribe to the shared lobby recall events',
                )
                self.assertNotIn("After 2 seconds, return to lobby with nickname preserved", content)

    def test_shared_player_lobby_return_include_contains_countdown_banner(self):
        content = Path(settings.BASE_DIR / 'templates/includes/_hub_return_to_lobby_player.html').read_text(encoding='utf-8')
        self.assertIn('Automatisches Zurückkehren in die Lobby in', content)
        self.assertIn('syncRecallCountdownState', content)
        self.assertIn('startRecallCountdown', content)
        self.assertIn('connectHubRecallSocket', content)
        self.assertIn("payload.type === 'players_recalled_to_lobby'", content)
        self.assertIn('returnToLobby({ markInactive: false });', content)

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
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.lobby_return_countdown_ends_at)
        self.assertEqual(self.session.lobby_return_countdown_duration_seconds, 10)

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
