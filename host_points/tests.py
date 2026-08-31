import json
import os
from pathlib import Path

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant
from Assign.models import AssignQuiz
from games_hub.lobby_join import issue_rejoin_token
from games_hub.models import (
    HubGameParticipantSnapshot,
    HubGameStep,
    HubParticipant,
    HubSession,
    ProcessedClientAction,
)
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser
from games_hub.views import get_leaderboard_data

from .consumers import HostPointsConsumer
from .models import HostPointsAdjustment, HostPointsGame, HostPointsParticipant

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserLiveServerTestCase
except ImportError:
    from django.test import LiveServerTestCase as _BrowserLiveServerTestCase


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class HostPointsFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('host-points-host', password='pw', is_staff=True)
        self.game = HostPointsGame.objects.create(title='Manuelle Punkte', creator=self.user)
        self.session = HubSession.objects.create(
            code='HP001',
            name='Host Points Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(
                session=self.session,
                nickname=name,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='host_points',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)

    def make_consumer(self):
        consumer = HostPointsConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'host_points_{self.game.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_creation_manage_games_and_session_registration(self):
        client = Client()
        client.force_login(self.user)

        create_response = client.post(
            reverse('admin_dashboard:create_host_points_game'),
            data=json.dumps({'title': 'Externe Challenge'}),
            content_type='application/json',
        )
        manage_response = client.get(reverse('admin_dashboard:manage_games'))
        monitor_response = client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(create_response.status_code, 200)
        self.assertTrue(HostPointsGame.objects.filter(title='Externe Challenge').exists())
        self.assertContains(manage_response, 'Host-Punktevergabe')
        self.assertContains(monitor_response, 'host_points')

    def test_host_monitor_uses_common_start_guard_and_consistent_controls(self):
        client = Client()
        client.force_login(self.user)

        response = client.get(
            reverse('admin_dashboard:host_points_monitor', args=[self.game.room_code]),
            {'hub_session': self.session.code},
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertContains(response, 'id="lobbyReturnGuardModal"')
        self.assertContains(response, 'id="startQuizBtn"')
        self.assertNotContains(response, 'id="startGameBtn"')
        self.assertIn('id="nextRoundBtn"', content)
        monitor_content = content.split('id="activeGameConflictModal"', 1)[0]
        self.assertIn('Spiel starten', content)
        self.assertIn('Nächste Runde', content)
        self.assertIn('Spiel beenden', monitor_content)
        self.assertNotIn('End Quiz', monitor_content)
        self.assertNotIn(f'href="/hub/lobby/{self.session.code}/"', content)
        self.assertIn(f'href="/hub/monitor/{self.session.code}/"', content)
        self.assertIn("js/authoritative-game-state.js", content)
        self.assertIn("js/host-game-runtime.js", content)
        self.assertIn('window.HostGameRuntime.create({', content)
        self.assertIn('hostRuntime.sendAction(payload', content)
        self.assertIn("hostRuntime.acceptSnapshot(data, {source: 'websocket'})", content)
        self.assertIn('return hostRuntime.socket;', content)

    def test_start_adjust_round_and_rejoin_state(self):
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_game)({'hub_session': self.session.code})
        self.game.refresh_from_db()
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.current_round_number, 1)

        success, _ = self.game.adjust_score(alice.id, 5)
        self.assertTrue(success)
        success, _ = self.game.adjust_score(bob.id, -1)
        self.assertTrue(success)
        self.assertTrue(self.game.next_round())

        state = self.game.serialize_state(self.session.code, 'Alice')
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertEqual(state['round']['number'], 2)
        self.assertEqual(state['participant']['score'], 5)
        self.assertEqual(state['round_scores'], [
            {'number': 1, 'points': 5},
            {'number': 2, 'points': None},
        ])
        self.assertEqual(alice.total_score, 5)
        self.assertEqual(bob.total_score, -1)

        self.assertTrue(self.game.adjust_score(alice.id, 2)[0])
        self.assertTrue(self.game.adjust_score(alice.id, -2)[0])
        rejoin_state = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(rejoin_state['round_scores'], [
            {'number': 1, 'points': 5},
            {'number': 2, 'points': 0},
        ])

    def test_inactive_previous_run_starts_as_clean_new_run(self):
        self.assertTrue(self.game.start_quiz(self.session.code))
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertTrue(self.game.adjust_score(alice.id, 5)[0])
        self.assertTrue(self.game.next_round(expected_round=1))
        self.game.status = 'inactive'
        self.game.save(update_fields=['status', 'updated_at'])
        self.game.participants.update(is_active=False)
        previous_started_at = self.game.started_at

        state = self.game.serialize_state(self.session.code)
        consumer, sent_messages = self.make_consumer()
        async_to_sync(consumer.receive)(json.dumps({
            'type': 'admin_start_game',
            'hub_session': self.session.code,
            'game_id': state['game_id'],
            'question_id': state.get('current_question_id'),
            'round_id': state.get('current_round_id'),
            'set_id': state.get('current_set_id'),
            'state_revision': state['state_revision'],
            'client_action_id': '018f9f58-5222-7ad0-b090-cc5b1f83e100',
        }))

        self.game.refresh_from_db()
        alice.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.current_round_number, 1)
        self.assertGreater(self.game.started_at, previous_started_at)
        self.assertEqual(alice.total_score, 0)
        self.assertTrue(alice.is_active)
        self.assertFalse(self.game.adjustments.filter(hub_session_code=self.session.code).exists())
        state = self.game.serialize_state(self.session.code)
        self.assertIsNone(state['question_phase'])
        self.assertIsNone(state['answering_deadline_at'])
        self.assertEqual(sent_messages, [])
        room_events = [message for _, message in consumer.channel_layer.group_messages]
        self.assertTrue(any(
            message.get('type') == 'host_points_state'
            and message.get('event_type') == 'game_started'
            for message in room_events
        ))
        self.assertTrue(any(
            message.get('type') == 'hub_event'
            and message.get('event', {}).get('type') == 'quiz_started'
            for message in room_events
        ))

    def test_duplicate_start_keeps_active_round_and_scores_without_lobby_recall(self):
        self.assertTrue(self.game.start_quiz(self.session.code))
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertTrue(self.game.adjust_score(alice.id, 4)[0])
        self.assertTrue(self.game.next_round(expected_round=1))
        started_at = self.game.started_at

        consumer, sent_messages = self.make_consumer()
        async_to_sync(consumer.handle_admin_start_game)({'hub_session': self.session.code})

        self.game.refresh_from_db()
        alice.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.current_round_number, 2)
        self.assertEqual(self.game.started_at, started_at)
        self.assertEqual(alice.total_score, 4)
        self.assertEqual(self.game.adjustments.count(), 1)
        self.assertFalse(any(
            message.get('type') == 'participants_not_in_lobby'
            for message in sent_messages
        ))
        self.assertTrue(any(
            message.get('type') == 'host_points_state'
            for message in sent_messages
        ))

    def test_completed_run_from_previous_session_can_start_in_current_session(self):
        previous_started_at = timezone.now() - timezone.timedelta(hours=2)
        self.game.status = 'completed'
        self.game.started_at = previous_started_at
        self.game.ended_at = timezone.now() - timezone.timedelta(hours=1)
        self.game.active_hub_session_code = 'OLDHP1'
        self.game.current_round_number = 4
        self.game.save(update_fields=[
            'status',
            'started_at',
            'ended_at',
            'active_hub_session_code',
            'current_round_number',
            'updated_at',
        ])
        state = self.game.serialize_state(self.session.code)
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.receive)(json.dumps({
            'type': 'admin_start_game',
            'hub_session': self.session.code,
            'game_id': state['game_id'],
            'question_id': state.get('current_question_id'),
            'round_id': state.get('current_round_id'),
            'set_id': state.get('current_set_id'),
            'state_revision': state['state_revision'],
            'client_action_id': '018f9f58-5222-7ad0-b090-cc5b1f83e101',
        }))

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.active_hub_session_code, self.session.code)
        self.assertEqual(self.game.current_round_number, 1)
        self.assertIsNone(self.game.ended_at)
        self.assertGreater(self.game.started_at, previous_started_at)
        self.assertFalse(any(message.get('type') == 'error' for message in sent_messages))

    def test_completed_run_from_current_session_cannot_restart(self):
        self.assertTrue(self.game.start_quiz(self.session.code))
        self.assertTrue(self.game.end_quiz())
        state = self.game.serialize_state(self.session.code)
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.receive)(json.dumps({
            'type': 'admin_start_game',
            'hub_session': self.session.code,
            'game_id': state['game_id'],
            'question_id': state.get('current_question_id'),
            'round_id': state.get('current_round_id'),
            'set_id': state.get('current_set_id'),
            'state_revision': state['state_revision'],
            'client_action_id': '018f9f58-5222-7ad0-b090-cc5b1f83e102',
        }))

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'completed')
        self.assertTrue(any(
            message.get('type') == 'action_rejected'
            and message.get('code') == 'invalid_phase'
            for message in sent_messages
        ))

    def test_round_scores_exclude_adjustments_from_another_hub_session(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertTrue(self.game.adjust_score(alice.id, 5)[0])
        HostPointsAdjustment.objects.create(
            quiz=self.game,
            participant=alice,
            hub_session_code='OLD001',
            round_number=1,
            points_delta=99,
        )

        state = self.game.serialize_state(self.session.code, 'Alice')

        self.assertEqual(state['round_scores'], [{'number': 1, 'points': 5}])

    def test_late_pending_and_spectator_participants_are_not_score_targets(self):
        self.game.start_quiz(self.session.code)
        late = HubParticipant.objects.create(
            session=self.session,
            nickname='Charlie',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        spectator = HubParticipant.objects.create(
            session=self.session,
            nickname='Spec',
            scoring_eligible=False,
        )
        HubGameParticipantSnapshot.objects.create(
            session=self.session,
            game_step=self.step,
            participant=spectator,
            included_in_scoring=False,
            active_player=True,
            reason=HubGameParticipantSnapshot.REASON_EXCLUDED,
        )

        client = Client()
        client.get(
            reverse('host_points:play', args=[self.game.room_code, late.nickname]),
            {'hub_session': self.session.code},
        )
        client.get(
            reverse('host_points:play', args=[self.game.room_code, spectator.nickname]),
            {'hub_session': self.session.code},
        )

        state = self.game.serialize_state(self.session.code)
        names = [participant['name'] for participant in state['participants']]
        late_participant = HostPointsParticipant.objects.get(name='Charlie', hub_session_code=self.session.code)
        spectator_participant = HostPointsParticipant.objects.get(name='Spec', hub_session_code=self.session.code)

        self.assertEqual(names, ['Alice', 'Bob'])
        self.assertFalse(late_participant.is_active)
        self.assertFalse(spectator_participant.is_active)
        self.assertFalse(self.game.adjust_score(late_participant.id, 1)[0])
        self.assertFalse(self.game.adjust_score(spectator_participant.id, 1)[0])

    def test_player_view_hides_lobby_return_during_active_game_and_result_has_lobby_return(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        play_response = Client().get(
            reverse('host_points:play', args=[self.game.room_code, alice.name]),
            {'hub_session': self.session.code},
        )
        self.game.end_quiz()
        result_response = Client().get(
            reverse('host_points:result', args=[self.game.room_code, alice.name]),
            {'hub_session': self.session.code},
        )

        self.assertContains(play_response, 'Der Host vergibt die Punkte manuell.')
        play_content = play_response.content.decode('utf-8')
        play_main = play_content.split('</main>', 1)[0]
        self.assertNotIn('<input', play_main)
        self.assertIn('<title>Host-Punktevergabe: Manuelle Punkte - QuizMaster</title>', play_content)
        self.assertIn('data-participant-name="Alice"', play_content)
        self.assertIn('MANUELLE PUNKTE · SPIEL 1', play_content)
        self.assertIn('id="hostPointsScoreRows"', play_content)
        self.assertIn('score.points !== null && score.points !== undefined', play_content)
        self.assertIn('class="mt-3 d-none" id="returnToLobbyContainer"', play_content)
        self.assertIn("returnToLobbyContainer.classList.toggle('d-none', gameStarted)", play_content)
        self.assertContains(result_response, 'Zur Lobby zurückkehren')
        self.assertContains(result_response, 'participant-return-to-lobby')

    def test_vhs_host_points_overrides_are_scoped_to_the_participant_screen(self):
        css_path = finders.find('themes/vhs/vhs.css')
        self.assertIsNotNone(css_path)
        css = Path(css_path).read_text(encoding='utf-8')

        self.assertIn(
            'body.host-points-play-page .qa-score-widget .host-points-score-box',
            css,
        )
        self.assertIn(
            'body.host-points-play-page .host-points-vhs-game-meta',
            css,
        )
        self.assertIn(
            'body.host-points-play-page :is(\n'
            '  .host-points-summary,\n'
            '  #connectionStatus,\n'
            '  #statusText\n'
            ')',
            css,
        )

    def test_end_game_idempotent_and_actions_after_end_are_rejected(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertTrue(self.game.adjust_score(alice.id, 3)[0])
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})
        first_message_count = len(consumer.channel_layer.group_messages)
        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})
        adjust_success, adjust_message = self.game.adjust_score(alice.id, 2)
        next_round_success = self.game.next_round()

        self.game.refresh_from_db()
        alice.refresh_from_db()
        self.assertEqual(self.game.status, 'completed')
        self.assertEqual(first_message_count, 2)
        self.assertEqual(len(consumer.channel_layer.group_messages), first_message_count)
        self.assertEqual(sent_messages[-1]['type'], 'host_points_state')
        self.assertFalse(adjust_success)
        self.assertIn('nicht aktiv', adjust_message)
        self.assertFalse(next_round_success)
        self.assertEqual(alice.total_score, 3)

    def test_stale_round_and_duplicate_host_action_are_rejected(self):
        self.assertTrue(self.game.start_quiz(self.session.code))
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        state = self.game.serialize_state(self.session.code)
        consumer, sent_messages = self.make_consumer()
        action = {
            'type': 'admin_adjust_score',
            'hub_session': self.session.code,
            'participant_id': alice.id,
            'delta': 1,
            'game_id': state['game_id'],
            'question_id': state.get('current_question_id'),
            'round_id': state['current_round_id'],
            'set_id': state.get('current_set_id'),
            'state_revision': state['state_revision'],
            'client_action_id': '018f9f58-5222-7ad0-b090-cc5b1f83e001',
        }

        async_to_sync(consumer.receive)(json.dumps(action))
        async_to_sync(consumer.receive)(json.dumps(action))

        alice.refresh_from_db()
        self.assertEqual(alice.total_score, 1)
        self.assertEqual(
            ProcessedClientAction.objects.filter(
                action_type='admin_adjust_score',
                participant_key='__host__',
            ).count(),
            1,
        )
        self.assertTrue(any(
            message.get('type') == 'action_rejected'
            and message.get('code') == 'already_submitted'
            for message in sent_messages
        ))
        self.assertTrue(self.game.next_round(expected_round=1))
        self.assertFalse(self.game.next_round(expected_round=1))

    def test_lobby_return_keeps_participant_out_of_game_after_end_event(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        response = Client().post(
            reverse('games_hub:participant_return_to_lobby', args=[self.session.code]),
            data=json.dumps({
                'game_key': 'host_points',
                'room_code': self.game.room_code,
                'participant_name': alice.name,
            }),
            content_type='application/json',
        )
        consumer, _ = self.make_consumer()
        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})

        alice.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(alice.is_active)


class HostPointsBrowserStartFlowTests(_BrowserLiveServerTestCase):
    TIMEOUT = 15_000

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
            username='host-points-browser-host',
            password=self.password,
            email='',
        )
        self.game = HostPointsGame.objects.create(
            title='Host Points Browser',
            creator=self.user,
        )
        self.session = HubSession.objects.create(
            code='HPB001',
            name='Host Points Browser Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        HubParticipant.objects.create(
            session=self.session,
            nickname='Alice',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='host_points',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)

        self.context = self._browser.new_context()
        install_browser_test_stubs(self.context)
        self.page = self.context.new_page()
        self.browser_errors = []
        self.page.on('pageerror', lambda exc: self.browser_errors.append(str(exc)))
        self.page.goto(f'{self.live_server_url}{reverse("admin_dashboard:login")}')
        self.page.fill("input[name='username']", self.user.username)
        self.page.fill("input[name='password']", self.password)
        self.page.click("button[type='submit']")
        self.page.wait_for_url(
            f'**{reverse("admin_dashboard:home")}**',
            timeout=self.TIMEOUT,
        )

    def tearDown(self):
        if getattr(self, 'page', None):
            self.page.close()
        if getattr(self, 'context', None):
            self.context.close()

    def test_start_score_reload_and_end_in_browser(self):
        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:host_points_monitor", args=[self.game.room_code])}'
            f'?hub_session={self.session.code}'
        )
        self.page.goto(monitor_url)
        self.page.wait_for_selector('#startQuizBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.page.click('#startQuizBtn')
        self.page.wait_for_selector('#nextRoundBtn:not(.d-none)', timeout=self.TIMEOUT)

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.current_round_number, 1)

        self.page.wait_for_selector('.score-btn[data-delta="1"]:not([disabled])', timeout=self.TIMEOUT)
        self.page.locator('.score-btn[data-delta="1"]').first.click()
        self.page.wait_for_function(
            "() => document.querySelector('#participantRows strong')?.textContent === '1'",
            timeout=self.TIMEOUT,
        )

        self.page.reload()
        self.page.wait_for_selector('#nextRoundBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.assertEqual(self.page.locator('#hostRoundNumber').inner_text(), '1')
        self.assertEqual(self.page.locator('#participantRows strong').inner_text(), '1')

        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.click('#endGameBtn')
        self.page.wait_for_selector('#completedBanner:not(.d-none)', timeout=self.TIMEOUT)

        participant = self.game.participants.get(
            name='Alice',
            hub_session_code=self.session.code,
        )
        self.game.refresh_from_db()
        self.assertEqual(participant.total_score, 1)
        self.assertEqual(self.game.status, 'completed')
        self.page.click('#backToHubBtn')
        self.page.wait_for_url(f'**/hub/monitor/{self.session.code}/', timeout=self.TIMEOUT)
        self.assertEqual(self.browser_errors, [])

    def test_cross_session_active_run_starts_clean_two_participant_flow(self):
        bob_hub = HubParticipant.objects.create(
            session=self.session,
            nickname='Bob',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        HubGameParticipantSnapshot.objects.create(
            session=self.session,
            game_step=self.step,
            participant=bob_hub,
            included_in_scoring=True,
            active_player=True,
        )
        previous_started_at = timezone.now() - timezone.timedelta(hours=2)
        self.game.status = 'active'
        self.game.started_at = previous_started_at
        self.game.ended_at = timezone.now() - timezone.timedelta(hours=1)
        self.game.active_hub_session_code = 'OLDHP2'
        self.game.current_round_number = 6
        self.game.save(update_fields=[
            'status',
            'started_at',
            'ended_at',
            'active_hub_session_code',
            'current_round_number',
            'updated_at',
        ])
        old_participant = HostPointsParticipant.objects.create(
            quiz=self.game,
            name='Old player',
            hub_session_code='OLDHP2',
            is_active=True,
            total_score=9,
        )
        HostPointsAdjustment.objects.create(
            quiz=self.game,
            participant=old_participant,
            hub_session_code='OLDHP2',
            round_number=6,
            points_delta=9,
        )

        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:host_points_monitor", args=[self.game.room_code])}'
            f'?hub_session={self.session.code}'
        )
        self.page.goto(monitor_url)
        self.page.wait_for_selector('#startQuizBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.page.click('#startQuizBtn')
        self.page.wait_for_selector('#nextRoundBtn:not(.d-none)', timeout=self.TIMEOUT)

        self.game.refresh_from_db()
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)
        old_participant.refresh_from_db()
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.active_hub_session_code, self.session.code)
        self.assertEqual(self.game.current_round_number, 1)
        self.assertIsNone(self.game.ended_at)
        self.assertGreater(self.game.started_at, previous_started_at)
        self.assertEqual((alice.total_score, bob.total_score), (0, 0))
        self.assertFalse(old_participant.is_active)
        self.assertEqual(self.page.locator('#participantRows tr').count(), 2)

        alice_row = self.page.locator('#participantRows tr').filter(has_text='Alice')
        bob_row = self.page.locator('#participantRows tr').filter(has_text='Bob')
        alice_row.locator('.score-btn[data-delta="1"]').click()
        self.page.wait_for_function(
            """() => Array.from(document.querySelectorAll('#participantRows tr')).some(row =>
                row.querySelector('td')?.textContent.trim() === 'Alice'
                && row.querySelector('strong')?.textContent.trim() === '1'
            )""",
            timeout=self.TIMEOUT,
        )
        bob_row = self.page.locator('#participantRows tr').filter(has_text='Bob')
        bob_row.locator('.score-btn[data-delta="2"]').click()
        self.page.wait_for_function(
            """() => Array.from(document.querySelectorAll('#participantRows tr')).every(row => {
                const name = row.querySelector('td')?.textContent.trim();
                const score = row.querySelector('strong')?.textContent.trim();
                return name === 'Alice' ? score === '1' : name === 'Bob' ? score === '2' : true;
            })""",
            timeout=self.TIMEOUT,
        )
        self.page.click('#nextRoundBtn')
        self.page.wait_for_function(
            "document.getElementById('hostRoundNumber').textContent === '2'",
            timeout=self.TIMEOUT,
        )
        alice_row = self.page.locator('#participantRows tr').filter(has_text='Alice')
        alice_row.locator('.score-btn[data-delta="1"]').click()
        self.page.wait_for_function(
            "document.querySelector('#participantRows tr td')?.textContent.trim() === 'Alice' && "
            "document.querySelector('#participantRows tr strong')?.textContent.trim() === '2'",
            timeout=self.TIMEOUT,
        )

        self.page.reload()
        self.page.wait_for_selector('#nextRoundBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.assertEqual(self.page.locator('#hostRoundNumber').inner_text(), '2')
        self.assertTrue(self.page.locator('#startQuizBtn').evaluate(
            "button => button.classList.contains('d-none')"
        ))

        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.click('#endGameBtn')
        self.page.wait_for_selector('#completedBanner:not(.d-none)', timeout=self.TIMEOUT)
        self.game.refresh_from_db()
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertEqual(self.game.status, 'completed')
        self.assertEqual((alice.total_score, bob.total_score), (2, 2))
        self.assertEqual(self.browser_errors, [])

    def test_browser_back_uses_leave_guard_and_preserves_inactive_game(self):
        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:host_points_monitor", args=[self.game.room_code])}'
            f'?hub_session={self.session.code}'
        )
        self.page.goto(monitor_url)
        self.page.wait_for_selector('#startQuizBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.page.click('#startQuizBtn')
        self.page.wait_for_selector('#nextRoundBtn:not(.d-none)', timeout=self.TIMEOUT)

        self.page.evaluate('history.back()')
        self.page.wait_for_function(
            "document.getElementById('activeGameLeaveMessage').textContent.includes('noch aktiv')",
            timeout=self.TIMEOUT,
        )
        self.assertIn('/host-points/', self.page.url)
        self.assertFalse(self.page.locator('#activeGameLeaveInactiveBtn').evaluate(
            "button => button.classList.contains('d-none')"
        ))

        self.page.locator('#activeGameLeaveInactiveBtn').evaluate('button => button.click()')
        self.page.wait_for_url(f'**/hub/monitor/{self.session.code}/', timeout=self.TIMEOUT)

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'inactive')
        self.assertIsNotNone(self.game.started_at)
        self.assertIsNone(self.game.ended_at)
        self.assertEqual(self.browser_errors, [])

    def test_lobby_guard_cancel_recall_and_start_flow(self):
        previous_game = Quiz.objects.create(
            creator=self.user,
            title='Previous Game',
            status='completed',
            ended_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='quiz',
            room_code=previous_game.room_code,
            title=previous_game.title,
        )
        previous_participant = QuizParticipant.objects.create(
            quiz=previous_game,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        monitor_url = (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:host_points_monitor", args=[self.game.room_code])}'
            f'?hub_session={self.session.code}'
        )
        self.page.goto(monitor_url)
        self.page.wait_for_selector('#startQuizBtn:not(.d-none)', timeout=self.TIMEOUT)

        self.page.click('#startQuizBtn')
        self.page.wait_for_function(
            "document.getElementById('lobbyReturnGuardNames').textContent.includes('Alice')",
            timeout=self.TIMEOUT,
        )
        self.assertIn('Alice', self.page.locator('#lobbyReturnGuardNames').inner_text())
        self.assertTrue(self.page.locator('#startQuizBtn').is_disabled())
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'waiting')

        self.page.locator('#lobbyReturnGuardCancelBtn').evaluate('button => button.click()')
        self.page.locator('#lobbyReturnGuardModal').evaluate(
            "modal => modal.dispatchEvent(new Event('hidden.bs.modal'))"
        )
        self.page.locator('#lobbyReturnGuardNames').evaluate("element => { element.textContent = ''; }")
        self.assertFalse(self.page.locator('#startQuizBtn').is_disabled())
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'waiting')

        self.page.click('#startQuizBtn')
        self.page.wait_for_function(
            "document.getElementById('lobbyReturnGuardNames').textContent.includes('Alice')",
            timeout=self.TIMEOUT,
        )
        self.page.locator('#lobbyReturnGuardPrimaryBtn').evaluate('button => button.click()')
        self.page.wait_for_timeout(11000)
        previous_participant.refresh_from_db()
        self.assertFalse(previous_participant.is_active)
        self.page.locator('#lobbyReturnGuardModal').evaluate(
            "modal => modal.dispatchEvent(new Event('hidden.bs.modal'))"
        )
        self.page.click('#startQuizBtn')
        self.page.wait_for_selector('#nextRoundBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.browser_errors, [])

    def test_host_points_players_are_recalled_in_browser_before_assign_start(self):
        bob_hub = HubParticipant.objects.create(
            session=self.session,
            nickname='Bob',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        HubGameParticipantSnapshot.objects.create(
            session=self.session,
            game_step=self.step,
            participant=bob_hub,
            included_in_scoring=True,
            active_player=True,
        )
        self.assertTrue(self.game.start_quiz(self.session.code))
        alice_hub = self.session.participants.get(nickname='Alice')

        player_contexts = []
        player_pages = []
        hub_socket_urls = {'Alice': [], 'Bob': []}
        try:
            for hub_participant in (alice_hub, bob_hub):
                context = self._browser.new_context()
                install_browser_test_stubs(context)
                credential = json.dumps({
                    'nickname': hub_participant.nickname,
                    'token': issue_rejoin_token(hub_participant),
                })
                context.add_init_script(
                    f"localStorage.setItem('hub_session_code', {json.dumps(self.session.code)});"
                    f"localStorage.setItem('hub_lobby_rejoin:{self.session.code}', "
                    f"{json.dumps(credential)});"
                )
                page = context.new_page()
                page.on(
                    'websocket',
                    lambda websocket, name=hub_participant.nickname: hub_socket_urls[name].append(websocket.url),
                )
                page.goto(
                    f'{self.live_server_url}'
                    f'{reverse("host_points:play", args=[self.game.room_code, hub_participant.nickname])}'
                    f'?hub_session={self.session.code}'
                )
                page.wait_for_function(
                    "document.getElementById('connectionStatus').textContent === 'Verbunden'",
                    timeout=self.TIMEOUT,
                )
                page.wait_for_timeout(250)
                player_contexts.append(context)
                player_pages.append(page)

            for name, page in zip(('Alice', 'Bob'), player_pages):
                page.wait_for_function(
                    "expected => window.location.pathname.includes('/host-points/play/')",
                    arg=name,
                    timeout=self.TIMEOUT,
                )
                deadline = timezone.now() + timezone.timedelta(seconds=10)
                while not any(f'/ws/hub/{self.session.code}/' in url for url in hub_socket_urls[name]):
                    if timezone.now() >= deadline:
                        self.fail(f'{name} did not open the shared hub recall socket: {hub_socket_urls[name]}')
                    page.wait_for_timeout(100)

            self.game.status = 'completed'
            self.game.ended_at = timezone.now()
            self.game.save(update_fields=['status', 'ended_at', 'updated_at'])

            self.page.goto(
                f'{self.live_server_url}{reverse("games_hub:monitor", args=[self.session.code])}'
            )
            self.page.click('[data-session-panel-target="checkInPanel"]')
            self.page.click('#recallLobbyBtn')
            self.page.wait_for_function(
                "document.getElementById('lobbyReturnGuardNames').textContent.includes('Alice') "
                "&& document.getElementById('lobbyReturnGuardNames').textContent.includes('Bob')",
                timeout=self.TIMEOUT,
            )
            self.page.locator('#lobbyReturnGuardPrimaryBtn').evaluate('button => button.click()')

            for page in player_pages:
                page.wait_for_url(
                    f'**/hub/lobby/{self.session.code}/**',
                    timeout=self.TIMEOUT,
                )
                page.wait_for_function(
                    "document.getElementById('joinCard').style.display === 'none'",
                    timeout=self.TIMEOUT,
                )

            self.assertFalse(self.game.participants.get(name='Alice').is_active)
            self.assertFalse(self.game.participants.get(name='Bob').is_active)

            assign_game = AssignQuiz.objects.create(
                creator=self.user,
                title='Assign after recall',
                status='waiting',
            )
            assign_step = HubGameStep.objects.create(
                session=self.session,
                order=1,
                game_key='assign',
                room_code=assign_game.room_code,
                title=assign_game.title,
            )
            HubGameParticipantSnapshot.create_for_step(assign_step)
            self.page.goto(
                f'{self.live_server_url}'
                f'{reverse("admin_dashboard:assign_monitor", args=[assign_game.room_code])}'
                f'?hub_session={self.session.code}'
            )
            self.page.click('#startQuizBtn')
            for page in player_pages:
                page.wait_for_url(
                    f'**/assign/play/{assign_game.room_code}/**',
                    timeout=self.TIMEOUT,
                )
        finally:
            for page in player_pages:
                page.close()
            for context in player_contexts:
                context.close()


class HostPointsLeaderboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('host-points-score-host', password='pw', is_staff=True)
        self.game = HostPointsGame.objects.create(title='Score only', creator=self.user)
        self.session = HubSession.objects.create(
            code='HP002',
            name='Leaderboard Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(
                session=self.session,
                nickname=name,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='host_points',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)
        self.game.start_quiz(self.session.code)
        self.alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)

    def test_simple_and_ranking_leaderboards_use_host_scores(self):
        self.alice.total_score = 7
        self.alice.save(update_fields=['total_score'])
        self.bob.total_score = 0
        self.bob.save(update_fields=['total_score'])
        self.game.end_quiz()

        simple = get_leaderboard_data(self.session)
        alice = next(row for row in simple['participants'] if row['name'] == 'Alice')
        bob = next(row for row in simple['participants'] if row['name'] == 'Bob')
        self.assertEqual(alice['game_scores'][f'step:{self.step.id}'], 7)
        self.assertEqual(bob['game_scores'][f'step:{self.step.id}'], 0)

        self.session.overall_scoring_mode = HubSession.OVERALL_SCORING_RANKING
        self.session.save(update_fields=['overall_scoring_mode'])
        ranking = get_leaderboard_data(self.session)
        alice_ranked = next(row for row in ranking['participants'] if row['name'] == 'Alice')
        bob_ranked = next(row for row in ranking['participants'] if row['name'] == 'Bob')
        self.assertEqual(alice_ranked['game_base_scores'][f'step:{self.step.id}'], 2)
        self.assertEqual(bob_ranked['game_base_scores'][f'step:{self.step.id}'], 1)
