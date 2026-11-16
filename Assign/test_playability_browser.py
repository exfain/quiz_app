import os, json, time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse
from games_hub.playwright_e2e import start_chromium_browser, install_browser_test_stubs
from games_hub.models import HubParticipant
from Assign import models as m
from Assign import test_playability as fixtures

class AssignPlayabilityBrowserTests(ChannelsLiveServerTestCase):
    select = fixtures.AssignPlayabilityTests.select
    advance = fixtures.AssignPlayabilityTests.advance
    finish = fixtures.AssignPlayabilityTests.finish

    def operate(self, fn):

        def work():
            try:
                return fn()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(work).result()

    def start(self):
        self.operate(lambda: fixtures.AssignPlayabilityTests.start(self))

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
        fixtures.AssignPlayabilityTests.setUp(self)
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
            html = response.text().replace('const player = new AssignPlayer();', 'const player = new AssignPlayer();window.auditPlayer=player;')
            route.fulfill(response=response, body=html)
        p.route('**/assign/play/**', instrument)
        return p

    def url(self, name='Alice', hub=True):
        return self.live_server_url + reverse('assign:play', args=[self.quiz.room_code, name]) + ('?hub_session=' + self.hub.code if hub else '')

    def ready(self, p):
        p.wait_for_function('() => window.auditPlayer?.assignInteractionEnabled === true')

    def test_names_are_literal_and_do_not_create_phantom_participants(self):
        names = ['Team <West', 'Trailing ' + chr(92)]
        for name in names:
            m.AssignParticipant.objects.create(quiz=self.quiz, name=name, hub_session_code=self.hub.code)
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
        p.goto(self.live_server_url + reverse('admin_dashboard:assign_monitor', args=[self.quiz.room_code]) + '?hub_session=' + self.hub.code)
        self.assertTrue(p.evaluate('document.getElementById("editScoreModal").parentElement === document.body'))
        self.assertFalse(self.errors)

    def test_independent_drag_and_login_in_standard_and_vhs(self):
        self.start()
        for name, theme, left in [('Alice', 'standard', 0), ('Bob', 'vhs', 1)]:
            p = self.page(theme)
            p.goto(self.url(name))
            self.ready(p)
            right = next((k for k, v in self.question.get_randomized_items(self.quiz.room_code)['position_to_original'].items() if v == left))
            p.locator('.draggable-item[data-left-index="' + str(left) + '"]').drag_to(p.locator('.drop-zone[data-right-index="' + str(right) + '"]'))
            p.click('#logRoundBtn')
            p.wait_for_timeout(200)
        self.assertEqual(m.AssignRoundParticipantState.objects.filter(is_locked=True).count(), 2)
        self.operate(lambda: self.advance())
        for context in self.contexts:
            p = context.pages[0]
            p.reload()
            self.ready(p)
            self.assertEqual(p.locator('.draggable-item:not(.completed-round)').count(), 2)
            self.assertEqual(len(p.evaluate('window.auditPlayer.solvedPairs')), 1)
        self.assertFalse(self.errors)

    def test_same_snapshot_preserves_local_unsaved_drag(self):
        self.start()
        p = self.page()
        frames = []
        p.on('websocket', lambda ws: ws.on('framereceived', lambda raw: frames.append(json.loads(raw))))
        p.goto(self.url())
        self.ready(p)
        snapshot = next((f for f in frames if f.get('type') == 'assign_state'))
        result = p.evaluate("snapshot=>{const x=window.auditPlayer,s=x.websocket;x.websocket=null;const source=document.querySelector('.draggable-item'),zone=document.querySelector('.drop-zone:not(.completed-round)');x.handleRoundDrop(Number(source.dataset.leftIndex),Number(zone.dataset.rightIndex),zone,source.textContent);x.websocket=s;const before=JSON.stringify(x.userMatches);x.handleWebSocketMessage(snapshot);return {before,after:JSON.stringify(x.userMatches)}}", snapshot)
        self.assertNotEqual(result['before'], '{}')
        self.assertEqual(result['before'], result['after'])
        self.assertFalse(self.errors)

    def test_deadline_does_not_resubmit_in_loop(self):
        from django.utils import timezone
        self.start()
        self.runtime.round_ends_at = timezone.now() + timezone.timedelta(seconds=1)
        self.runtime.save()
        sent = []
        p = self.page()
        p.on('websocket', lambda ws: ws.on('framesent', lambda raw: sent.append(json.loads(raw))))
        p.goto(self.url())
        p.wait_for_timeout(2400)
        self.assertFalse(any((f.get('type') == 'participant_log_round' for f in sent)))
        self.assertLess(len(sent), 8)
        self.assertFalse(self.errors)

    def test_eliminated_player_sees_solution_after_reload(self):
        self.start()

        def complete():
            from Assign.runtime import persist_final_answers, mark_revealed
            for _ in range(3):
                self.advance()
            persist_final_answers(self.runtime)
            mark_revealed(self.quiz.room_code, self.hub.code)
        self.operate(complete)
        p = self.page('vhs')
        p.goto(self.url())
        p.wait_for_selector('#solutionState:not(.d-none)')
        self.assertFalse(self.errors)

    def test_saved_selection_and_lock_are_restored_on_reload(self):
        self.start()
        p = self.page('vhs')
        p.goto(self.url())
        self.ready(p)
        right = next(k for k, v in self.question.get_randomized_items(self.quiz.room_code)['position_to_original'].items() if v == 0)
        p.locator('.draggable-item[data-left-index="0"]').drag_to(p.locator('.drop-zone[data-right-index="' + str(right) + '"]'))
        p.wait_for_timeout(200)
        before = p.evaluate('window.auditPlayer.userMatches')
        p.reload()
        self.ready(p)
        self.assertEqual(before, p.evaluate('window.auditPlayer.userMatches'))
        p.click('#logRoundBtn')
        p.wait_for_timeout(200)
        p.reload()
        p.wait_for_function('window.auditPlayer?.roundSubmitted === true')
        self.assertEqual(before, p.evaluate('window.auditPlayer.userMatches'))
        self.assertFalse(self.errors)
