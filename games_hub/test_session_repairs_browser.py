import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse

from .lobby_join import issue_rejoin_token
from .playwright_e2e import install_browser_test_stubs, start_chromium_browser
from .readiness import get_readiness_state, start_readiness_check
from . import test_manual_repairs as manual


class SessionBrowserBase(ChannelsLiveServerTestCase):
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
        self.contexts = []
        self.errors = []

    def tearDown(self):
        for context in self.contexts:
            context.close()
        time.sleep(.2)
        super().tearDown()

    def db(self, operation):
        def run():
            try:
                return operation()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    def context(self, *, player=None, host=False, theme='standard', viewport=None):
        context = self.browser.new_context(**({'viewport': viewport} if viewport else {}))
        self.contexts.append(context)
        install_browser_test_stubs(context)
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url)
                      else route.fulfill(status=200, content_type='text/javascript', body=''))
        context.add_init_script('localStorage.setItem("participant_interface_theme",' + json.dumps(theme) + ');')
        if player:
            credential = json.dumps({'nickname': player.nickname, 'token': issue_rejoin_token(player)})
            context.add_init_script('localStorage.setItem(' + json.dumps('hub_lobby_rejoin:' + self.hub.code) + ',' + json.dumps(credential) + ');')
        if host:
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        return context

    def page(self, path, **options):
        context = self.context(**options)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        page.goto(self.live_server_url + path, wait_until='domcontentloaded')
        return page


class LobbyAndReadinessBrowserRepairs(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        manual.ManualGameRepairs.setUp(self)

    def test_typing_before_connection_is_rechecked_and_invalid_character_hint_is_immediate(self):
        context = self.context()
        context.add_init_script("""(() => {
          const Native = window.WebSocket;
          class Delayed extends EventTarget {
            static CONNECTING=0; static OPEN=1; static CLOSING=2; static CLOSED=3;
            constructor(url) { super(); this.url=url; this.readyState=0;
              setTimeout(() => {
                this.inner=new Native(url);
                for (const type of ['open','message','close','error']) this.inner.addEventListener(type,e=>{
                  this.readyState=this.inner.readyState;
                  const next=type==='message'?new MessageEvent(type,{data:e.data}):new Event(type);
                  this.dispatchEvent(next); this['on'+type]?.(next);
                });
              },1500);
            }
            send(data) { this.inner.send(data); }
            close() { this.inner?.close(); }
          }
          window.WebSocket=Delayed;
        })();""")
        page = context.new_page()
        page.goto(self.live_server_url + reverse('games_hub:lobby', args=[self.hub.code]), wait_until='domcontentloaded')
        self.assertFalse(page.evaluate("window.TeamNames.validCharacters('Team\\n')"))
        self.assertFalse(page.evaluate("window.TeamNames.validCharacters('Team\\t')"))
        page.fill('#nickname', 'New Team')
        self.assertTrue(page.locator('#joinBtn').is_disabled())
        page.wait_for_selector('#joinBtn:not([disabled])')
        page.fill('#nickname', 'New <Team')
        self.assertIn('ÄÖÜäöüß', page.inner_text('#nicknameStatus'))
        self.assertIn('Unterstrich', page.inner_text('#nicknameStatus'))
        self.assertTrue(page.locator('#joinBtn').is_disabled())
        page.fill('#nickname', 'New Ä-Team_25')
        page.wait_for_selector('#joinBtn:not([disabled])')
        page.click('#joinBtn')
        page.wait_for_function("document.getElementById('joinCard').style.display==='none'")

    def test_navigation_before_identity_waits_and_never_builds_empty_name_url(self):
        page = self.page(reverse('games_hub:lobby', args=[self.hub.code]))
        requests = []
        page.on('request', lambda request: requests.append(request.url))
        page.evaluate("step=>window.hubLobbySocket.onmessage({data:JSON.stringify({type:'navigate',step})})",
                      {'game_key': 'host_points', 'room_code': self.points.room_code})
        self.assertIn('/hub/lobby/', page.url)
        page.fill('#nickname', 'New Player')
        page.wait_for_selector('#joinBtn:not([disabled])')
        page.click('#joinBtn')
        page.wait_for_url('**/host-points/play/**')
        self.assertIn('New%20Player', page.url)
        self.assertFalse(any('/' + self.points.room_code + '//' in url for url in requests))
        self.assertFalse(self.errors)

    def test_readiness_in_lobby_accepts_nested_button_click_in_standard_and_vhs(self):
        pages = [self.page(reverse('games_hub:lobby', args=[self.hub.code]),
            player=p, theme='vhs' if i else 'standard') for i, p in enumerate(self.hub_players)]
        for page in pages:
            page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host = self.page(reverse('games_hub:monitor', args=[self.hub.code]), host=True)
        host.wait_for_function("document.querySelector('#wsStatus .status-dot')?.classList.contains('connected')")
        host.locator('[data-session-panel-target="readinessPanel"]').evaluate('(e)=>e.click()')
        host.locator('#startReadinessBtn').click()
        for page in pages:
            page.wait_for_selector('#readinessOverlay.is-visible')
            page.locator('#readinessReadyBtn').evaluate("b=>b.innerHTML='<span>Bereit</span>'")
            page.locator('#readinessReadyBtn span').first.click()
            page.wait_for_selector('#readinessOverlay:not(.is-visible)', state='attached')
        self.assertEqual(self.db(lambda: get_readiness_state(self.hub))['readiness_counts']['ready'], 2)
        self.assertFalse(self.errors)

    def test_readiness_during_running_game_is_rejected_without_player_overlay(self):
        self.db(lambda: self.points.start_quiz(self.hub.code))
        player = self.page(reverse('host_points:play', args=[self.points.room_code, self.hub_players[0].nickname]) + '?hub_session=' + self.hub.code,
            player=self.hub_players[0], theme='vhs')
        host = self.page(reverse('games_hub:monitor', args=[self.hub.code]), host=True)
        host.wait_for_function("document.querySelector('#wsStatus .status-dot')?.classList.contains('connected')")
        host.locator('[data-session-panel-target="readinessPanel"]').evaluate('(e)=>e.click()')
        host.click('#startReadinessBtn')
        host.wait_for_selector('#readinessError:not(.d-none)')
        self.assertIn('zwischen Spielen', host.inner_text('#readinessError'))
        self.assertFalse(self.db(lambda: get_readiness_state(self.hub))['readiness_check']['active'])
        self.assertFalse(player.locator('#sessionReadinessOverlay').is_visible())
        self.assertFalse(self.errors)

    def test_returning_from_old_game_does_not_miss_new_game_started_before_lobby_connected(self):
        self.db(lambda: self.points.start_quiz(self.hub.code))
        player = self.hub_players[0]
        page = self.page(reverse('games_hub:lobby', args=[self.hub.code]) + '?return=1&from_game=buzzer&from_room=' + self.buzzer.room_code,
                         player=player)
        page.wait_for_url('**/host-points/play/**')
        self.assertIn(player.nickname, page.url)
        self.assertFalse(self.errors)

    def test_host_score_buttons_show_pending_lock_and_both_teams_can_be_awarded(self):
        self.db(lambda: self.points.start_quiz(self.hub.code))
        page = self.page(reverse('admin_dashboard:host_points_monitor', args=[self.points.room_code]) + '?hub_session=' + self.hub.code, host=True)
        page.wait_for_function("typeof hostRuntime!=='undefined' && hostRuntime.snapshot?.participants.length===2")
        locked = page.locator('.score-btn[data-delta="1"]').evaluate_all("bs=>{bs[0].click();return bs[1].disabled}")
        self.assertTrue(locked)
        page.wait_for_function('currentState.participants[0].score===1')
        page.locator('.score-btn[data-delta="1"]').nth(1).click()
        page.wait_for_function('currentState.participants.every(p=>p.score===1)')
        self.assertFalse(self.errors)


class WannWarDasRevealBrowserRepairs(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        from wann_war_das.test_repairs import WannWarDasSessionRepairs
        WannWarDasSessionRepairs.setUp(self)
        self.client.force_login(self.user)

    def test_repeated_question_displays_dynamic_total_for_host_and_both_player_themes(self):
        def repeat():
            self.game.reveal_current_question(self.hub.code)
            self.game.start_question(self.q, self.hub.code)
        self.db(repeat)
        host = self.page(reverse('admin_dashboard:wann_war_das_monitor', args=[self.game.room_code]) + '?hub_session=' + self.hub.code, host=True)
        pages = [host]
        for i, player in enumerate(self.players[:2]):
            pages.append(self.page(reverse('wann_war_das:play', args=[self.game.room_code, player.name]) + '?hub_session=' + self.hub.code,
                player=self.hub.participants.get(nickname=player.name), theme='vhs' if i else 'standard'))
        for page in pages:
            page.wait_for_function("document.getElementById('questionNumber').textContent==='2' && document.getElementById('totalQuestionCount').textContent==='3'")
        self.assertEqual(pages[-1].locator('body').get_attribute('data-vhs-progress-source'), '#questionProgress')
        self.assertFalse(self.errors)

    def test_both_players_see_last_reveal_before_result_and_draft_survives(self):
        host = self.page(reverse('admin_dashboard:wann_war_das_monitor', args=[self.game.room_code]) + '?hub_session=' + self.hub.code, host=True)
        host.wait_for_function("typeof hostRuntime!=='undefined' && hostRuntime.snapshot?.question?.correct_answer===2000")
        from .authoritative_state import current_snapshot
        public = self.db(lambda: current_snapshot('wann_war_das', self.game.room_code, self.hub.code))
        self.assertNotIn('correct_answer', public.get('question') or {})
        pages = []
        for i, player in enumerate(self.players[:2]):
            hub_player = self.hub.participants.get(nickname=player.name)
            page = self.page(reverse('wann_war_das:play', args=[self.game.room_code, player.name]) + '?hub_session=' + self.hub.code,
                player=hub_player, theme='vhs' if i else 'standard')
            page.wait_for_selector('#answerInput:not([disabled])')
            page.fill('#answerInput', '2000')
            pages.append(page)
        pages[0].click('#submitBtn')
        pages[0].wait_for_selector('#submittedAlert:not(.d-none)')
        pages[1].wait_for_timeout(400)
        self.assertEqual(self.db(lambda: self.game.pending_inputs.filter(participant=self.players[1]).count()), 1)
        self.db(lambda: self.game.end_quiz())
        for page in pages:
            page.wait_for_selector('#revealArea:not(.d-none)')
            self.assertIn('/wann-war-das/play/', page.url)
            self.assertEqual(page.inner_text('#correctAnswerText'), '2000')
            page.click('#continueRevealBtn')
            page.wait_for_url('**/wann-war-das/result/**')
        self.assertFalse(self.errors)
