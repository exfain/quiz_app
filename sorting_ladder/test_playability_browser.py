import os, json, time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from asgiref.sync import async_to_sync
from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse
from games_hub.playwright_e2e import start_chromium_browser, install_browser_test_stubs
from games_hub.models import HubParticipant
from sorting_ladder import models as m
from sorting_ladder import test_playability as fixtures

class SortingPlayabilityBrowserTests(ChannelsLiveServerTestCase):

    def operate(self, fn):

        def work():
            try:
                return fn()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(work).result()
    visible_order = fixtures.SortingPlayabilityTests.visible_order

    def start(self):
        self.operate(lambda: fixtures.SortingPlayabilityTests.start(self))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = patch.dict(os.environ, {'DJANGO_ALLOW_ASYNC_UNSAFE': '1'})
        cls.env.start()
        cls.pw, cls.browser = start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.env.stop()
        super().tearDownClass()

    def setUp(self):
        fixtures.SortingPlayabilityTests.setUp(self)
        self.contexts = []
        self.errors = []
        for p in (self.alice, self.bob):
            HubParticipant.objects.create(session=self.hub, nickname=p.name)

    def tearDown(self):
        for c in self.contexts:
            c.close()
        time.sleep(0.15)

    def page(self, theme='standard', host=False):
        c = self.browser.new_context()
        self.contexts.append(c)
        install_browser_test_stubs(c)
        c.add_init_script('localStorage.setItem("participant_interface_theme",' + json.dumps(theme) + ');')
        if host:
            self.client.force_login(self.user)
            c.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        p = c.new_page()
        p.on('pageerror', lambda error: self.errors.append(str(error)))

        def instrument(route):
            response = route.fetch()
            html = response.text().replace('const player = new SortingLadderPlayer();', 'const player = new SortingLadderPlayer();window.auditPlayer=player;')
            route.fulfill(response=response, body=html)
        p.route('**/sorting-ladder/play/**', instrument)
        return p

    def url(self, name='Alice', hub=True):
        return self.live_server_url + reverse('sorting_ladder:play', args=[self.quiz.room_code, name]) + ('?hub_session=' + self.hub.code if hub else '')

    def ready(self, p):
        p.wait_for_function('() => window.auditPlayer?.currentQuestion && !window.auditPlayer.isInputLocked')

    def drag(self, p):
        p.locator('.answer-card[data-item-id="' + str(int(self.session.shuffled_item_ids.split(',')[1])) + '"]').drag_to(p.locator('.triangle-container[data-position="1"]'))
        p.wait_for_timeout(250)

    def test_names_are_literal_and_do_not_create_phantom_participants(self):
        names = ['Team <West', 'Trailing ' + chr(92)]
        for name in names:
            m.SortingLadderParticipant.objects.create(quiz=self.quiz, name=name, hub_session_code=self.hub.code)
        self.start()
        before = self.quiz.participants.count()
        for name in names:
            p = self.page('vhs')
            p.goto(self.url(name))
            self.ready(p)
            self.assertEqual(p.evaluate('window.auditPlayer.participantName'), name)
            p.close()
        self.assertEqual(self.quiz.participants.count(), before)
        self.assertFalse(self.errors)

    def test_score_modal_lives_outside_transformed_content(self):
        self.start()
        p = self.page(host=True)
        p.goto(self.live_server_url + reverse('admin_dashboard:sorting_ladder_monitor', args=[self.quiz.room_code]) + '?hub_session=' + self.hub.code)
        self.assertTrue(p.evaluate('document.getElementById("editScoreModal").parentElement === document.body'))
        self.assertFalse(self.errors)

    def test_both_players_drag_and_log_without_foreign_solution(self):
        self.start()
        pages = []
        for name, theme in [('Alice', 'standard'), ('Bob', 'vhs')]:
            p = self.page(theme)
            p.goto(self.url(name))
            self.ready(p)
            pages.append(p)
        self.drag(pages[0])
        pages[0].click('#submitRoundBtn')
        pages[0].wait_for_timeout(250)
        self.assertEqual(m.RoundSubmission.objects.count(), 1)
        self.assertFalse(pages[1].evaluate('window.auditPlayer.hasSubmittedMove'))
        self.assertFalse(pages[1].evaluate('window.auditPlayer.isInputLocked'))
        self.drag(pages[1])
        pages[1].click('#submitRoundBtn')
        pages[1].wait_for_timeout(250)
        self.assertEqual(m.RoundSubmission.objects.count(), 2)
        self.assertFalse(self.errors)

    def test_pending_order_and_confirmed_lock_survive_reload(self):
        self.start()
        p = self.page('vhs')
        p.goto(self.url())
        self.ready(p)
        self.drag(p)
        before = p.evaluate('window.auditPlayer.ladderItems.map(i=>i.id)')
        p.reload()
        self.ready(p)
        self.assertEqual(before, p.evaluate('window.auditPlayer.ladderItems.map(i=>i.id)'))
        p.click('#submitRoundBtn')
        p.wait_for_timeout(250)
        p.reload()
        p.wait_for_function('window.auditPlayer?.hasSubmittedMove === true')
        self.assertTrue(p.evaluate('window.auditPlayer.isInputLocked'))
        self.assertFalse(self.errors)

    def test_rejected_submission_recovers_selection(self):
        self.start()
        p = self.page()
        self.contexts[-1].add_init_script("(()=>{const send=WebSocket.prototype.send;WebSocket.prototype.send=function(raw){try{const d=JSON.parse(raw);if(d.type==='participant_submit_round'){d.state_revision=-1;raw=JSON.stringify(d)}}catch(e){}return send.call(this,raw)}})();")
        p.goto(self.url())
        self.ready(p)
        self.drag(p)
        before = p.evaluate('window.auditPlayer.ladderItems.map(i=>i.id)')
        p.click('#submitRoundBtn')
        p.wait_for_function('!window.auditPlayer.hasSubmittedMove && !window.auditPlayer.isInputLocked')
        self.assertEqual(before, p.evaluate('window.auditPlayer.ladderItems.map(i=>i.id)'))
        self.assertEqual(m.RoundSubmission.objects.count(), 0)
        self.assertFalse(self.errors)

    def test_native_pause_resume_preserves_round(self):
        self.start()
        host = self.page(host=True)
        p = self.page()
        host.on('dialog', lambda dialog: dialog.accept())
        host.goto(self.live_server_url + reverse('admin_dashboard:sorting_ladder_monitor', args=[self.quiz.room_code]) + '?hub_session=' + self.hub.code)
        p.goto(self.url())
        self.ready(p)
        host.click('#setInactiveBtn')
        host.wait_for_selector('#startQuizBtn')
        p.wait_for_function('window.auditPlayer.isInputLocked === true')
        host.click('#startQuizBtn')
        p.wait_for_function('window.auditPlayer.paused === false && !window.auditPlayer.isInputLocked')
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.session.refresh_from_db()
        self.assertEqual(self.session.current_round, 1)
        self.assertFalse(self.errors)
