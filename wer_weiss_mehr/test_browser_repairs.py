import os, json, time
from concurrent.futures import ThreadPoolExecutor
from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse
from django.utils import timezone
from games_hub.models import HubParticipant
from games_hub.playwright_e2e import start_chromium_browser, install_browser_test_stubs
from games_hub.authoritative_state import reset_question_flow, current_snapshot
from wer_weiss_mehr import services as s, models as m
from wer_weiss_mehr import test_repairs as fixtures

class WwmBrowserRepairs(ChannelsLiveServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = '1'
        cls.pw, cls.browser = start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        super().tearDownClass()

    def setUp(self):
        fixtures.WwmPlayabilityTests.setUp(self)
        self.contexts = []
        self.errors = []
        for p in (self.p1, self.p2):
            HubParticipant.objects.create(session=self.hub, nickname=p.name)

    def tearDown(self):
        for c in self.contexts:
            for p in c.pages:
                try:
                    p.evaluate('()=>{clearInterval(pollTimer);if(typeof liveResponsePollTimer!=="undefined")clearInterval(liveResponsePollTimer);clearTimeout(reconnectTimer);clearTimeout(pendingInputTimer);if(ws){ws.onclose=null;ws.close()}if(hubWs){hubWs.onclose=null;hubWs.close()}}')
                except Exception:
                    pass
            c.close()
        time.sleep(0.4)

    def operate(self, fn):

        def work():
            try:
                return fn()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(work).result()

    def start(self, limit=90):

        def work():
            reset_question_flow(game_key='wer_weiss_mehr', room_code=self.game.room_code, session_code=self.hub.code, mode='manual_three_phase')
            snap = current_snapshot('wer_weiss_mehr', self.game.room_code, self.hub.code)
            import uuid
            d = s.present_set_round(self.game, self.question.pk, hub_session_code=self.hub.code, time_limit_seconds=limit, action={'client_action_id': str(uuid.uuid4()), 'state_revision': snap['state_revision'], 'game_id': snap['game_id'], 'question_id': self.question.pk}, at=timezone.now() - timezone.timedelta(seconds=5))
            self.assertTrue(d.accepted)
            self.game.refresh_from_db()
            state = s.build_game_state(self.game, self.hub.code)
            d = s.open_prepared_round(self.game, hub_session_code=self.hub.code, action={'client_action_id': str(uuid.uuid4()), 'state_revision': state['state_revision'], 'game_id': state['game_id'], 'question_id': self.question.pk, 'round_id': 1, 'set_id': state['current_set_id']}, at=timezone.now())
            self.assertTrue(d.accepted)
        self.operate(work)

    def page(self, name='Lisa', theme='standard', host=False):
        c = self.browser.new_context()
        self.contexts.append(c)
        install_browser_test_stubs(c)
        c.add_init_script('localStorage.setItem("participant_interface_theme",' + json.dumps(theme) + ');')
        if host:
            self.client.force_login(self.user)
            c.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        p = c.new_page()
        p.on('pageerror', lambda e: self.errors.append(str(e)))
        p.on('dialog', lambda d: d.accept())
        path = reverse('admin_dashboard:wer_weiss_mehr_monitor', args=[self.game.room_code]) if host else reverse('wer_weiss_mehr:play', args=[self.game.room_code, name])
        p.goto(self.live_server_url + path + '?hub_session=' + self.hub.code)
        p.wait_for_function('state !== null' if host else 'latestState !== null')
        return p

    def test_two_players_two_rounds_and_repeat_keeps_all_points(self):
        self.start()
        host = self.page(host=True)
        players = [self.page('Lisa', 'standard'), self.page('Max', 'vhs')]
        for word in ('Bayern', 'Saarland'):
            for player in players:
                player.wait_for_selector('#answerInput:not([disabled])')
                player.fill('#answerInput', word)
                player.click('#submitBtn')
                player.wait_for_function('latestState.participant_state.has_submitted')
            with host.expect_response(lambda response: '/end-round/' in response.url and response.request.method == 'POST') as ended:
                host.click('#endRoundBtn')
            self.assertEqual(ended.value.status, 200, ended.value.text())
            host.wait_for_function('state.phase === "review" && !hostRuntime.hasPendingAction')
            if word == 'Bayern':
                with host.expect_response(lambda response: '/next-round/' in response.url and response.request.method == 'POST') as advanced:
                    host.click('#nextRoundBtn')
                self.assertEqual(advanced.value.status, 200, advanced.value.text())
                host.wait_for_function('state.current_round === 2 && state.phase === "round_active"')
        host.click('#finishSetBtn')
        host.wait_for_function('state.phase === "set_completed"')
        host.click('#clearSetBtn')
        host.wait_for_function('!state.question')
        host.locator(f'.start-set-btn[data-question-id="{self.question.pk}"]').click()
        if host.evaluate('!state.question'):
            host.locator(f'.start-set-btn[data-question-id="{self.question.pk}"]').click()
        host.wait_for_function('state.question_phase === "prompt_visible"')
        host.wait_for_selector('#openRoundBtn:not([disabled])')
        host.click('#openRoundBtn')
        host.wait_for_function('state.phase === "round_active"')
        for player in players:
            player.wait_for_selector('#answerInput:not([disabled])')
            self.assertEqual(player.input_value('#answerInput'), '')
            player.fill('#answerInput', 'Bayern')
            player.click('#submitBtn')
            player.wait_for_function('latestState.participant_state.has_submitted')
        host.click('#endRoundBtn')
        host.wait_for_function('state.phase === "review"')
        self.assertEqual(self.operate(lambda: list(self.game.participants.order_by('name').values_list('total_score', flat=True))), [3, 3])
        self.assertEqual(host.evaluate('state.scorebox.length'), 3)
        self.assertEqual(host.evaluate('state.current_set_number'), 2)
        self.assertEqual(self.errors, [])

    def test_deadline_has_bounded_polling_and_stays_locked_without_host(self):
        self.start(limit=2)
        player = self.page()
        calls = []
        player.on('request', lambda req: calls.append(req.url) if '/state/' in req.url else None)
        player.wait_for_timeout(3200)
        self.assertLessEqual(len(calls), 3)
        self.assertFalse(player.locator('#submitBtn').is_enabled())
        self.assertFalse(player.locator('#answerInput').is_enabled())
        self.assertEqual(self.operate(lambda: m.WerWeissMehrSession.objects.get(quiz=self.game).phase), 'round_active')
        self.assertEqual(self.errors, [])

    def test_special_names_draft_reload_and_private_solutions(self):
        self.p1.name = 'Team <West'
        self.p1.save()
        self.p2.name = 'Trailing ' + chr(92)
        self.p2.save()
        self.start()
        for name, theme in ((self.p1.name, 'standard'), (self.p2.name, 'vhs')):
            player = self.page(name, theme)
            player.fill('#answerInput', 'Bayern')
            player.wait_for_timeout(450)
            player.reload()
            player.wait_for_function('latestState !== null')
            self.assertEqual(player.input_value('#answerInput'), 'Bayern')
            self.assertIsNone(player.evaluate('latestState._revision_state || null'))
            self.assertEqual(player.locator('.tile').all_text_contents(), ['?', '?', '?'])
            player.click('#submitBtn')
            player.wait_for_function('latestState.participant_state.has_submitted')
            player.reload()
            player.wait_for_function('latestState !== null')
            self.assertFalse(player.locator('#submitBtn').is_enabled())
        self.assertEqual(self.errors, [])

    def test_pause_resume_keeps_set_and_draft_and_does_not_require_lobby(self):
        self.start()
        host = self.page(host=True)
        player = self.page('Lisa', 'vhs')
        player.fill('#answerInput', 'Bayern')
        player.wait_for_timeout(450)
        old_set = host.evaluate('state.current_set_id')
        host.click('#setInactiveBtn')
        host.wait_for_function('state.game_status === "inactive"')
        before = host.evaluate('state.timer.remaining_seconds')
        host.wait_for_timeout(1700)
        host.click('#startQuizBtn')
        host.wait_for_function('state.game_status === "active"')
        player.wait_for_selector('#answerInput:not([disabled])')
        self.assertEqual(player.input_value('#answerInput'), 'Bayern')
        self.assertEqual(host.evaluate('state.current_set_id'), old_set)
        self.assertLessEqual(abs(host.evaluate('state.timer.remaining_seconds') - before), 1)
        self.assertEqual(host.locator('#lobbyReturnGuardModal.show').count(), 0)
        self.assertEqual(self.errors, [])
