import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse

from . import test_manual_repairs as fixtures
from .lobby_join import issue_rejoin_token
from .models import HubGameTutorialAcknowledgement
from .playwright_e2e import install_browser_test_stubs, start_chromium_browser


class ManualGameBrowserRepairs(ChannelsLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.old_async = os.environ.get('DJANGO_ALLOW_ASYNC_UNSAFE')
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = '1'
        cls.pw, cls.browser = start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        if cls.old_async is None:
            os.environ.pop('DJANGO_ALLOW_ASYNC_UNSAFE', None)
        else:
            os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = cls.old_async
        super().tearDownClass()

    def setUp(self):
        fixtures.ManualGameRepairs.setUp(self)
        self.contexts, self.errors, self.dialogs = [], [], []
        self.frames = []

    def tearDown(self):
        for context in self.contexts:
            context.close()
        time.sleep(0.5)
        super().tearDown()

    def operate(self, fn):
        def work():
            try:
                return fn()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(work).result()

    def page(self, key='buzzer', name='Alice', *, host=False, theme='standard'):
        game = self.buzzer if key == 'buzzer' else self.points
        context = self.browser.new_context()
        self.contexts.append(context)
        install_browser_test_stubs(context)
        context.add_init_script('localStorage.setItem("participant_interface_theme",' + json.dumps(theme) + ');')
        if host:
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        else:
            participant = next(p for p in self.hub_players if p.nickname == name)
            credential = json.dumps({'nickname': name, 'token': issue_rejoin_token(participant)})
            context.add_init_script('localStorage.setItem(' + json.dumps('hub_lobby_rejoin:' + self.hub.code)
                + ',' + json.dumps(credential) + ');')
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        def capture_frame(raw):
            data = json.loads(raw)
            if data.get('type') in {'action_rejected', 'error', 'tutorial_completed', 'tutorial_progress'}:
                self.frames.append({key: data.get(key) for key in ['type', 'code', 'message', 'state_revision', 'round_id', 'completed', 'total']})
        def capture_socket(socket):
            socket.on('framereceived', capture_frame)
            socket.on('framesent', capture_frame)
        page.on('websocket', capture_socket)
        def dialog_handler(dialog):
            self.dialogs.append(dialog.message)
            dialog.accept()
        page.on('dialog', dialog_handler)
        path = reverse('admin_dashboard:' + key + '_monitor', args=[game.room_code]) if host else reverse(
            key + ':play', args=[game.room_code, name])
        page.goto(self.live_server_url + path + '?hub_session=' + self.hub.code, wait_until='domcontentloaded')
        if host:
            page.wait_for_function('typeof hostRuntime !== "undefined" && hostRuntime.snapshot && hostRuntime.socket?.readyState === 1')
        else:
            page.wait_for_function('document.getElementById("connectionStatus").textContent === "Verbunden"')
            page.wait_for_timeout(150)
        return page

    def test_buzzer_two_players_scoring_and_next_round(self):
        host = self.page(host=True)
        alice, bob = self.page(), self.page(name='Bob', theme='vhs')
        host.click('#startGameBtn')
        host.wait_for_selector('#startRoundBtn:not(.d-none)')
        host.click('#startRoundBtn')
        host.wait_for_selector('#openBuzzerBtn:not(.d-none)')
        host.click('#openBuzzerBtn')
        alice.wait_for_selector('#buzzButton:not([disabled])')
        alice.click('#buzzButton')
        host.wait_for_function('document.getElementById("currentBuzzName").textContent === "Alice"')
        host.click('#markWrongBtn')
        bob.wait_for_selector('#buzzButton:not([disabled])')
        bob.click('#buzzButton')
        host.wait_for_function('document.getElementById("currentBuzzName").textContent === "Bob"')
        host.click('#markCorrectBtn')
        bob.wait_for_function('document.getElementById("ownScore").textContent === "3"')
        self.assertIn('3/3', bob.inner_text('#buzzerScoreRows'))
        host.wait_for_selector('#startRoundBtn:not(.d-none)')
        host.click('#startRoundBtn')
        host.wait_for_selector('#openBuzzerBtn:not(.d-none)')
        host.click('#openBuzzerBtn')
        alice.wait_for_selector('#buzzButton:not([disabled])')
        self.assertEqual(alice.locator('#buzzButton').get_attribute('aria-pressed'), 'false')
        host.click('#endRoundBtn')
        host.click('#endGameBtn')
        for page in [alice, bob]:
            page.wait_for_url('**/buzzer/result/**')
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dialogs, ['Buzzer-Spiel beenden?'])

    def test_buzzer_pause_reload_resume_keeps_answer_right_and_points(self):
        self.operate(lambda: self.buzzer.start_quiz(self.hub.code))
        self.operate(lambda: self.buzzer.start_round(self.hub.code))
        self.operate(lambda: self.buzzer.open_buzzer())
        host, player = self.page(host=True), self.page(theme='vhs')
        player.wait_for_selector('#buzzButton:not([disabled])')
        player.click('#buzzButton')
        host.wait_for_function('document.getElementById("currentBuzzName").textContent === "Alice"')
        host.evaluate('window.adminGameMonitor.setInactive()')
        host.wait_for_selector('#startGameBtn:not(.d-none)')
        player.reload()
        host.click('#startGameBtn')
        player.wait_for_function('document.getElementById("buzzButton").getAttribute("aria-pressed") === "true"')
        self.assertEqual(host.inner_text('#hostRoundNumber'), '1')
        host.click('#markCorrectBtn')
        player.wait_for_function('document.getElementById("ownScore").textContent === "3"')
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dialogs, [])

    def test_points_resume_and_http_update_reach_players(self):
        host = self.page('host_points', host=True)
        alice, bob = self.page('host_points'), self.page('host_points', name='Bob', theme='vhs')
        host.click('#startQuizBtn')
        host.wait_for_selector('#nextRoundBtn:not(.d-none)')
        host.locator('.score-btn[data-delta="5"]').first.click()
        alice.wait_for_function('document.getElementById("ownScore").textContent === "5"')
        host.click('#nextRoundBtn')
        host.wait_for_function('document.getElementById("hostRoundNumber").textContent === "2"')
        host.evaluate('window.adminGameMonitor.setInactive()')
        host.wait_for_selector('#startQuizBtn:not(.d-none)')
        host.click('#startQuizBtn')
        host.wait_for_selector('#nextRoundBtn:not(.d-none)')
        alice.wait_for_function('document.getElementById("roundNumber").textContent === "2"')
        self.assertEqual(alice.inner_text('#ownScore'), '5')
        def adjust():
            player = self.points.participants.get(name='Alice')
            action = fixtures.ManualGameRepairs.action(self, self.points, 'admin_adjust_score')
            return {**action, 'participant_id': player.pk, 'delta': 1}
        body = self.operate(adjust)
        csrf = next(cookie['value'] for cookie in host.context.cookies() if cookie['name'] == 'csrftoken')
        response = host.request.post(self.live_server_url + reverse('host_points:adjust_score', args=[self.points.room_code]),
            data=json.dumps(body), headers={'Content-Type': 'application/json', 'X-CSRFToken': csrf})
        self.assertEqual(response.status, 200, response.text())
        alice.wait_for_function('document.getElementById("ownScore").textContent === "6"')
        bob.reload()
        bob.wait_for_selector('#hostPointsScoreRows .score-box__row', state='attached')
        self.assertIn('0', bob.inner_text('#hostPointsScoreRows'))
        host.click('#endGameBtn')
        alice.wait_for_url('**/host-points/result/**')
        bob.wait_for_url('**/host-points/result/**')
        self.assertEqual(self.errors, [])

    def test_host_points_tutorial_ok_is_persisted(self):
        self.points.tutorial_enabled, self.points.tutorial_text = True, 'Spielerklaerung'
        self.points.save()
        host, player = self.page('host_points', host=True), self.page('host_points')
        host.check('#showTutorialToggle')
        host.click('#startQuizBtn')
        player.wait_for_selector('#gameTutorialOverlay.is-visible')
        player.click('#gameTutorialCloseBtn')
        host.wait_for_function('document.getElementById("tutorialProgressText").textContent.startsWith("1/2")')
        self.assertEqual(self.operate(lambda: HubGameTutorialAcknowledgement.objects.count()), 1)
        player.reload()
        player.wait_for_timeout(500)
        self.assertEqual(player.locator('#gameTutorialOverlay').get_attribute('aria-hidden'), 'true')
        self.assertEqual(self.errors, [])

    def test_forced_buzzer_start_closes_overlay_and_disconnect_disables_button(self):
        self.buzzer.tutorial_enabled, self.buzzer.tutorial_text = True, 'Spielerklaerung'
        self.buzzer.save()
        host, player = self.page(host=True), self.page()
        host.check('#showTutorialToggle')
        host.click('#startGameBtn')
        player.wait_for_selector('#gameTutorialOverlay.is-visible')
        host.click('#startRoundBtn')
        host.wait_for_selector('#tutorialAckWarningModal.show')
        host.click('#tutorialAckWarningContinueBtn')
        host.wait_for_selector('#openBuzzerBtn:not(.d-none)')
        host.click('#openBuzzerBtn')
        player.wait_for_selector('#gameTutorialOverlay.is-visible', state='hidden')
        player.wait_for_selector('#buzzButton:not([disabled])')
        player.evaluate('socket.close()')
        player.wait_for_function('document.getElementById("connectionStatus").textContent.includes("getrennt")')
        self.assertTrue(player.locator('#buzzButton').is_disabled())
        self.assertEqual(player.locator('#buzzButton').get_attribute('aria-pressed'), 'false')
        self.assertEqual(self.errors, [])

    def test_spectator_distinguishes_finished_round_and_extends_planned_counter(self):
        self.operate(lambda: self.buzzer.start_quiz(self.hub.code))
        self.operate(lambda: self.buzzer.start_round(self.hub.code))
        self.operate(lambda: self.buzzer.open_buzzer())
        host = self.page(host=True)
        context = self.browser.new_context()
        self.contexts.append(context)
        install_browser_test_stubs(context)
        spectator = context.new_page()
        spectator.goto(self.live_server_url + reverse('games_hub:spectate_session', args=[self.hub.code]))
        spectator.wait_for_function('document.body.innerText.includes("BUZZER OFFEN")')
        host.click('#endRoundBtn')
        spectator.wait_for_function('document.body.innerText.includes("RUNDE BEENDET")')
        host.click('#startRoundBtn')
        spectator.wait_for_function('document.getElementById("progressText").textContent.includes("RUNDE 2 / 2")')
        self.assertEqual(self.errors, [])

    def test_special_names_are_preserved_by_both_native_transports(self):
        names = ['Team <West', 'Team ' + chr(92)]
        for participant, name in zip(self.hub_players, names):
            participant.nickname = name
            participant.save(update_fields=['nickname'])
        host = self.page(host=True)
        first, second = self.page(name=names[0], theme='vhs'), self.page(name=names[1])
        host.click('#startGameBtn')
        host.wait_for_selector('#startRoundBtn:not(.d-none)')
        host.click('#startRoundBtn')
        host.wait_for_selector('#openBuzzerBtn:not(.d-none)')
        host.click('#openBuzzerBtn')
        first.wait_for_selector('#buzzButton:not([disabled])')
        first.click('#buzzButton')
        host.wait_for_function('name => document.getElementById("currentBuzzName").textContent === name', arg=names[0])
        host.click('#markWrongBtn')
        second.wait_for_selector('#buzzButton:not([disabled])')
        second.click('#buzzButton')
        host.wait_for_function('name => document.getElementById("currentBuzzName").textContent === name', arg=names[1])
        host.click('#markCorrectBtn')
        second.wait_for_function('document.getElementById("ownScore").textContent === "3"')
        self.assertEqual(self.errors, [])

    def test_standalone_waiting_player_can_buzz_after_host_start_without_reload(self):
        from buzzer.models import BuzzerGame
        game = BuzzerGame.objects.create(creator=self.user)
        player_context = self.browser.new_context()
        self.contexts.append(player_context)
        install_browser_test_stubs(player_context)
        player = player_context.new_page()
        player.goto(self.live_server_url + reverse('buzzer:join'))
        player.fill('#room_code', game.room_code)
        player.fill('#participant_name', 'Solo')
        player.click('button[type="submit"]')
        player.wait_for_url('**/buzzer/play/**')
        host_context = self.browser.new_context()
        self.contexts.append(host_context)
        install_browser_test_stubs(host_context)
        host_context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
            'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        host = host_context.new_page()
        host.goto(self.live_server_url + reverse('admin_dashboard:buzzer_monitor', args=[game.room_code]))
        host.wait_for_function('typeof hostRuntime !== "undefined" && hostRuntime.snapshot')
        host.click('#startGameBtn')
        host.wait_for_selector('#startRoundBtn:not(.d-none)')
        host.click('#startRoundBtn')
        host.wait_for_selector('#openBuzzerBtn:not(.d-none)')
        host.click('#openBuzzerBtn')
        player.wait_for_selector('#buzzButton:not([disabled])')
        player.click('#buzzButton')
        host.wait_for_function('document.getElementById("currentBuzzName").textContent === "Solo"')
        self.assertEqual(self.errors, [])
