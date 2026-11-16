import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse

from .models import ClueAnswer, CluePendingInput, ClueRushGame
from . import test_repairs as repairs
from games_hub.models import HubParticipant
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser


class ClueRushBrowserRepairTests(ChannelsLiveServerTestCase):
    action = repairs.ClueRushRepairTests.action
    make_consumer = repairs.ClueRushRepairTests.make_consumer

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = patch.dict(os.environ, {'DJANGO_ALLOW_ASYNC_UNSAFE': '1'})
        cls.env.start()
        cls.playwright, cls.browser = start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.env.stop()
        super().tearDownClass()

    def setUp(self):
        repairs.ClueRushRepairTests.setUp(self)
        self.contexts = []
        self.errors = []
        for participant in [self.alice, self.bob]:
            HubParticipant.objects.create(session=self.hub, nickname=participant.name)

    def tearDown(self):
        for context in self.contexts:
            context.close()

    def operate(self, fn):
        def work():
            try:
                return fn()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(work).result()

    def start(self, **kwargs):
        self.operate(lambda: repairs.ClueRushRepairTests.start(self, **kwargs))

    def page(self, theme='standard', host=False):
        context = self.browser.new_context()
        self.contexts.append(context)
        install_browser_test_stubs(context)
        context.add_init_script('localStorage.setItem("participant_interface_theme",' + json.dumps(theme) + ');')
        context.add_init_script("""(() => {
          const Native = WebSocket;
          window.WebSocket = new Proxy(Native, {construct(target, args) {
            const socket = Reflect.construct(target, args);
            if (String(args[0]).includes('/ws/clue-rush/')) window.testClueSocket = socket;
            return socket;
          }});
        })();""")
        if host:
            self.client.force_login(self.user)
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        page = context.new_page()
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        return page

    def player_url(self, name):
        return self.live_server_url + reverse('clue_rush:play', args=[self.quiz.room_code, name]) + '?hub_session=' + self.hub.code

    def monitor_url(self):
        return self.live_server_url + reverse('admin_dashboard:clue_rush_monitor', args=[self.quiz.room_code]) + '?hub_session=' + self.hub.code

    def test_special_names_two_players_no_live_answer_leak_and_common_reveal(self):
        self.alice.name, self.bob.name = 'Team <West', 'Trailing \\'
        for participant in [self.alice, self.bob]:
            participant.save()
            HubParticipant.objects.create(session=self.hub, nickname=participant.name)
        self.question.clues.update(duration=5)
        host, alice, bob = self.page(host=True), self.page(), self.page('vhs')
        host.on('dialog', lambda dialog: dialog.accept())
        frames = []
        bob.on('websocket', lambda socket: socket.on('framereceived', lambda payload: frames.append(json.loads(payload))))
        host.goto(self.monitor_url())
        alice.goto(self.player_url(self.alice.name))
        bob.goto(self.player_url(self.bob.name))
        host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
        selector = f'.send-question-btn[data-question-id="{self.question.pk}"]'
        host.click(selector)
        host.click(selector)
        host.wait_for_selector('#startCluesBtn:not([disabled])', timeout=15000)
        host.click('#startCluesBtn')
        for page in [alice, bob]:
            page.wait_for_selector('#shortAnswerInput:not([disabled])')
        alice.fill('#shortAnswerInput', 'Wrong')
        alice.click('#submitAnswerBtn')
        alice.wait_for_selector('#clueRushSubmittedAnswer:not(.d-none)')
        alice.reload()
        alice.wait_for_selector('#clueRushSubmittedAnswer:not(.d-none)')
        self.assertNotIn('Ada Lovelace', alice.locator('#clueHistoryList').inner_text())
        bob.fill('#shortAnswerInput', 'Ada Lovelace')
        bob.click('#submitAnswerBtn')
        bob.wait_for_selector('#clueRushSubmittedAnswer:not(.d-none)')
        self.assertFalse(any(frame.get('type') == 'participant_answered' for frame in frames))
        for page in [alice, bob]:
            page.wait_for_selector('#correctAnswerState:not(.d-none)', timeout=20000)
            self.assertEqual(page.locator('#playerCluesList .clue-rush-clue').count(), 3)
        host.wait_for_selector('#questionSelection', timeout=15000)
        self.assertEqual(ClueAnswer.objects.filter(quiz=self.quiz).count(), 2)
        host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
        host.click(selector)
        host.click(selector)
        host.wait_for_selector('#startCluesBtn:not([disabled])', timeout=15000)
        host.click('#startCluesBtn')
        for page in [alice, bob]:
            page.wait_for_selector('#shortAnswerInput:not([disabled])')
            self.assertEqual(page.input_value('#shortAnswerInput'), '')
            self.assertEqual(page.locator('#currentQuestionNumber').inner_text(), '2')
            self.assertEqual(page.locator('#totalQuestionCount').inner_text(), '6')
        alice.fill('#shortAnswerInput', 'Ada Lovelace')
        alice.click('#submitAnswerBtn')
        alice.wait_for_selector('#clueRushSubmittedAnswer:not(.d-none)')
        host.wait_for_selector('#endQuestionBtn')
        host.click('#endQuestionBtn')
        alice.wait_for_selector('#correctAnswerState:not(.d-none)')
        self.assertEqual(ClueAnswer.objects.filter(quiz=self.quiz).count(), 4)
        self.assertEqual(self.errors, [])

    def test_session_similarity_grades_confirmed_and_pending_typos_in_both_themes(self):
        self.start()
        host, alice, bob = self.page(host=True), self.page(), self.page('vhs')
        host.goto(self.monitor_url())
        alice.goto(self.player_url(self.alice.name))
        bob.goto(self.player_url(self.bob.name))
        for page in [alice, bob]:
            page.wait_for_selector('#shortAnswerInput:not([disabled])')
            page.fill('#shortAnswerInput', 'Ada Lovlace')
        alice.click('#submitAnswerBtn')
        alice.wait_for_selector('#clueRushSubmittedAnswer:not(.d-none)')
        bob.wait_for_timeout(350)
        self.assertTrue(alice.locator('#correctAnswerState').is_hidden())
        host.wait_for_selector('#endQuestionBtn:not([disabled])')
        host.click('#endQuestionBtn')
        for page in [alice, bob]:
            page.wait_for_selector('#correctAnswerState:not(.d-none)')
        self.assertEqual(self.operate(lambda: ClueAnswer.objects.filter(quiz=self.quiz, is_correct=True).count()), 2)
        self.assertEqual(self.errors, [])

    def test_snapshot_keeps_unsaved_text_and_pause_resume_keeps_question(self):
        self.start()
        host, player = self.page(host=True), self.page('vhs')
        host.on('dialog', lambda dialog: dialog.accept())
        host.goto(self.monitor_url())
        player.goto(self.player_url('Alice'))
        player.wait_for_selector('#shortAnswerInput:not([disabled])')
        player.evaluate("""() => {
          const input = document.getElementById('shortAnswerInput');
          input.value = 'Unsaved draft'; input.dispatchEvent(new Event('input', {bubbles: true}));
          window.testClueSocket.send(JSON.stringify({type: 'snapshot_request'}));
        }""")
        player.wait_for_timeout(650)
        self.assertEqual(player.input_value('#shortAnswerInput'), 'Unsaved draft')
        host.click('#setInactiveBtn')
        host.wait_for_selector('#startQuizBtn')
        player.wait_for_selector('#shortAnswerInput[disabled]')
        host.click('#startQuizBtn')
        host.wait_for_selector('#endQuizBtn')
        player.wait_for_selector('#shortAnswerInput:not([disabled])')
        self.assertEqual(player.input_value('#shortAnswerInput'), 'Unsaved draft')
        self.assertTrue(CluePendingInput.objects.filter(participant=self.alice).exists())
        self.assertEqual(self.errors, [])

    def test_immediate_unconfirmed_drafts_from_both_themes_survive_first_hint(self):
        host, first, second = self.page(host=True), self.page(), self.page('vhs')
        host.on('dialog', lambda dialog: dialog.accept())
        host.goto(self.monitor_url())
        first.goto(self.player_url('Alice'))
        second.goto(self.player_url('Bob'))
        host.wait_for_function('window.adminGameMonitor?.websocket?.readyState===1')
        selector = f'.send-question-btn[data-question-id="{self.question.pk}"]'
        host.click(selector)
        host.click(selector)
        host.wait_for_selector('#startCluesBtn:not([disabled])')
        host.click('#startCluesBtn')
        for page in [first, second]:
            page.wait_for_selector('#shortAnswerInput:not([disabled])')
            page.fill('#shortAnswerInput', 'Ada Lovelace')
        for _ in range(50):
            if self.operate(lambda: CluePendingInput.objects.filter(quiz=self.quiz, answer_text='Ada Lovelace').count()) == 2:
                break
            second.wait_for_timeout(100)
        self.assertEqual(self.operate(lambda: CluePendingInput.objects.filter(quiz=self.quiz).count()), 2)
        host.wait_for_function('window.adminGameMonitor?.websocket?.readyState===1 && window.adminGameMonitor.stateRevision>=0')
        host.click('#endQuestionBtn')
        for page in [first, second]:
            page.wait_for_selector('#correctAnswerState:not(.d-none)')
        self.assertEqual(self.operate(lambda: ClueAnswer.objects.filter(quiz=self.quiz, is_correct=True).count()), 2)
        self.assertEqual(self.errors, [])

    def test_score_modal_is_clickable_and_saves(self):
        host = self.page(host=True)
        host.goto(self.monitor_url())
        host.locator('.edit-score-btn').first.click()
        host.fill('#editScoreInput', '7')
        host.click('#editScoreSave')
        host.wait_for_selector('#editScoreModal', state='hidden')
        self.assertTrue(self.quiz.participants.filter(total_score=7, score_adjustment=7).exists())
        self.assertEqual(self.errors, [])

    def test_without_host_waits_then_reconnected_host_resolves(self):
        self.start(age=28)
        player = self.page()
        player.goto(self.player_url('Alice'))
        player.wait_for_timeout(3200)
        self.assertEqual(player.locator('#correctAnswerState:not(.d-none)').count(), 0)
        self.assertTrue(ClueRushGame.objects.filter(pk=self.quiz.pk, current_question_id=self.question.pk).exists())
        host = self.page(host=True)
        host.goto(self.monitor_url())
        player.wait_for_selector('#correctAnswerState:not(.d-none)', timeout=15000)
        self.assertEqual(self.errors, [])

    def test_standalone_join_play_submit_and_reveal(self):
        self.hub.steps.all().delete()
        self.quiz.status = 'waiting'
        self.quiz.started_at = None
        self.quiz.save()
        host, player = self.page(host=True), self.page()
        player.goto(self.live_server_url + reverse('clue_rush:join'))
        player.fill('#participantName', 'Solo West')
        player.fill('#roomCode', self.quiz.room_code)
        player.locator('button[type=submit]').click()
        player.wait_for_url('**/play/**')
        host.goto(self.monitor_url().split('?')[0])
        host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
        host.click('#startQuizBtn')
        host.wait_for_selector('#endQuizBtn')
        host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
        selector = f'.send-question-btn[data-question-id="{self.question.pk}"]'
        host.click(selector)
        host.click(selector)
        host.wait_for_selector('#startCluesBtn:not([disabled])', timeout=15000)
        host.click('#startCluesBtn')
        player.wait_for_selector('#shortAnswerInput:not([disabled])')
        player.fill('#shortAnswerInput', 'Ada Lovelace')
        player.click('#submitAnswerBtn')
        player.wait_for_selector('#clueRushSubmittedAnswer:not(.d-none)')
        host.wait_for_selector('#endQuestionBtn')
        host.click('#endQuestionBtn')
        player.wait_for_selector('#correctAnswerState:not(.d-none)')
        self.assertTrue(ClueAnswer.objects.filter(participant__name='Solo West', is_correct=True).exists())
        self.assertEqual(self.errors, [])
