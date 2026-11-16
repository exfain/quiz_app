"""Exercise native host, Standard/VHS player and spectator paths in Chromium."""
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse
from django.utils import timezone

from games_hub.authoritative_state import current_snapshot, reset_question_flow
from games_hub.playwright_e2e import start_chromium_browser, install_browser_test_stubs
from . import runtime as r, models as m, test_repairs as fixtures


class BlackJackBrowserRepairs(ChannelsLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.old_async_unsafe = os.environ.get('DJANGO_ALLOW_ASYNC_UNSAFE')
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = '1'
        cls.pw, cls.browser = start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        if cls.old_async_unsafe is None:
            os.environ.pop('DJANGO_ALLOW_ASYNC_UNSAFE', None)
        else:
            os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = cls.old_async_unsafe
        super().tearDownClass()

    def setUp(self):
        fixtures.BlackJackRepairs.setUp(self)
        self.contexts, self.errors, self.dialogs = [], [], []

    def tearDown(self):
        for context in self.contexts:
            context.close()
        # Let Daphne finish socket disconnects before TransactionTestCase flushes
        # the database shared with its separate server process.
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

    def start(self, index=0, limit=60):
        def work():
            reset_question_flow(game_key='blackjack', room_code=self.quiz.room_code,
                session_code=self.hub.code, mode='manual_three_phase')
            state = current_snapshot('blackjack', self.quiz.room_code, self.hub.code)
            action = {'game_id': state['game_id'], 'state_revision': state['state_revision'],
                'question_id': self.questions[index].pk, 'client_action_id': str(uuid.uuid4())}
            decision = r.present(self.quiz, self.questions[index].pk, self.hub.code, action, limit,
                at=timezone.now() - timedelta(seconds=10))
            self.assertTrue(decision.accepted, decision)
            state = decision.snapshot
            action.update(state_revision=state['state_revision'], client_action_id=str(uuid.uuid4()),
                round_id=state['current_round_id'], set_id=state['current_set_id'])
            self.assertTrue(r.open_question(self.quiz, self.hub.code, action, at=timezone.now()).accepted)
        self.operate(work)

    def page(self, name='Lisa', theme='standard', host=False, clock_offset=0):
        context = self.browser.new_context()
        self.contexts.append(context)
        install_browser_test_stubs(context)
        context.add_init_script('localStorage.setItem("participant_interface_theme",' + json.dumps(theme) + ');')
        context.add_init_script("""(() => {
            const Native = window.WebSocket;
            window.WebSocket = new Proxy(Native, {construct(target, args) {
                const socket = Reflect.construct(target, args);
                if (String(args[0]).includes('/ws/blackjack/')) window.testBlackjackSocket = socket;
                return socket;
            }});
        })();""")
        if clock_offset:
            context.add_init_script('Date.now=(()=>{const original=Date.now;return ()=>original()+' + str(clock_offset) + '})()')
        if host:
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value, 'url': self.live_server_url}])
        page = context.new_page()
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        def dialog_handler(dialog):
            self.dialogs.append(dialog.message)
            dialog.accept()
        page.on('dialog', dialog_handler)
        path = reverse('admin_dashboard:blackjack_monitor', args=[self.quiz.room_code]) if host else reverse(
            'black_jack_quiz:play', args=[self.quiz.room_code, name])
        page.goto(self.live_server_url + path + '?hub_session=' + self.hub.code, wait_until='domcontentloaded')
        if host:
            page.wait_for_timeout(400)
        else:
            try:
                page.wait_for_selector('#answerInput:not([disabled])', timeout=8000)
            except Exception:
                print('BLACKJACK BROWSER ERRORS', self.errors, self.dialogs, page.locator('body').inner_text()[-1200:])
                raise
        return page

    def answer(self, page, value):
        page.wait_for_selector('#answerInput:not([disabled])')
        page.fill('#answerInput', str(value))
        page.click('#submitAnswerBtn', force=True)
        page.wait_for_selector('#questionSubmitFeedback:not(.d-none)')
        page.wait_for_selector('#answerInput[disabled]')

    def next_question(self, host, index):
        selector = f'.send-question-btn[data-question-id="{self.questions[index].pk}"]'
        host.wait_for_selector(selector)
        host.locator(selector).click()
        host.locator(selector).click()
        host.wait_for_selector('#openAnsweringBtn:not([disabled])')
        host.click('#openAnsweringBtn')

    def test_two_players_and_last_question_reveal_before_set_and_final_results(self):
        self.start()
        host = self.page(host=True)
        players = [self.page('Lisa'), self.page('Max', 'vhs')]
        for index in range(4):
            for page in players:
                self.answer(page, self.questions[index].correct_answer - 1)
            host.click('#endQuestionBtn')
            for page in players:
                page.wait_for_selector('#correctAnswerState:not(.d-none)')
                self.assertEqual(page.inner_text('#correctAnswerDisplay'), str(self.questions[index].correct_answer))
                self.assertTrue(page.locator('#setEndedState').is_hidden())
                self.assertTrue(page.locator('#quizEndedState').is_hidden())
                page.click('#continueRevealBtn')
            if index == 1:
                for page in players:
                    page.wait_for_selector('#setEndedState:not(.d-none)')
                host.click('.select-set-btn[data-set-number="2"]')
            if index < 3:
                self.next_question(host, index + 1)
        for page in players:
            page.wait_for_selector('#quizEndedState:not(.d-none)')
        self.assertEqual(self.operate(lambda: list(self.quiz.participants.values_list('overall_points', flat=True))), [4, 4])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dialogs, [])

    def test_names_with_less_than_and_trailing_backslash_can_submit(self):
        for participant, name in [(self.p1, 'Team <West'), (self.p2, 'Team ' + chr(92))]:
            self.hub.participants.filter(nickname=participant.name).update(nickname=name)
            participant.name = name
            participant.save()
        self.start()
        for participant in [self.p1, self.p2]:
            page = self.page(participant.name, 'vhs')
            self.answer(page, 9)
        self.assertEqual(self.operate(lambda: self.quiz.blackjack_answers.count()), 2)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dialogs, [])

    def test_legacy_ampersand_name_uses_correct_status_url(self):
        self.hub.participants.filter(nickname=self.p1.name).update(nickname='Team & Beta')
        self.p1.name = 'Team & Beta'
        self.p1.save()
        self.start()
        page = self.page(self.p1.name, 'vhs')
        with page.expect_response(lambda response: '/blackjack/status/' in response.url) as response:
            # The real server rejects the mismatching identity; the client's
            # recovery path then fetches its authenticated HTTP status URL.
            page.evaluate("""() => window.testBlackjackSocket.send(JSON.stringify({
                type: 'participant_submit_answer', participant_name: 'Another player', answer: 9
            }))""")
        self.assertEqual(response.value.status, 200)
        self.assertNotIn('&amp;', response.value.url)
        self.assertEqual(self.operate(lambda: self.quiz.blackjack_answers.count()), 0)
        self.assertEqual(self.errors, [])

    def test_saved_draft_survives_reload_and_is_graded_only_when_host_ends(self):
        self.start(limit=8)
        page = self.page(theme='vhs')
        page.wait_for_selector('#answerInput:not([disabled])')
        with page.expect_response(lambda response: '/blackjack/pending/' in response.url):
            page.fill('#answerInput', '9')
        page.reload()
        page.wait_for_selector('#answerInput:not([disabled])')
        self.assertEqual(page.input_value('#answerInput'), '9')
        page.wait_for_selector('#answerInput[disabled]', timeout=12000)
        self.assertEqual(self.operate(lambda: self.quiz.blackjack_answers.count()), 0)
        host = self.page(host=True)
        host.click('#endQuestionBtn')
        page.wait_for_selector('#correctAnswerState:not(.d-none)')
        self.assertEqual(page.inner_text('#userAnswerDisplay'), '9')
        self.assertEqual(self.operate(lambda: self.quiz.blackjack_answers.get().points_earned), 1)
        self.assertEqual(self.errors, [])

    def test_server_clock_and_locked_input_after_reload(self):
        self.start()
        page = self.page(clock_offset=120000)
        self.assertGreater(int(page.inner_text('#playerTimeLeft')), 40)
        self.answer(page, '1e2')
        page.reload()
        page.wait_for_selector('#questionSubmitFeedback:not(.d-none)')
        self.assertTrue(page.locator('#answerInput').is_disabled())
        self.assertEqual(page.input_value('#answerInput'), '100')
        self.assertEqual(self.operate(lambda: self.quiz.blackjack_answers.get().user_answer), 100)
        self.assertEqual(self.errors, [])

    def test_pause_resume_restores_draft_and_input_and_keeps_locked_answer(self):
        self.start()
        host = self.page(host=True)
        first, second = self.page(), self.page('Max', 'vhs')
        self.answer(first, 9)
        with second.expect_response(lambda response: '/blackjack/pending/' in response.url):
            second.fill('#answerInput', '8')
        host.click('#setInactiveBtn')
        host.wait_for_selector('#startQuizBtn')
        second.wait_for_selector('#waitingQuestionState:not(.d-none)')
        before = self.operate(lambda: m.BlackJackQuiz.objects.get(pk=self.quiz.pk).started_at)
        host.click('#startQuizBtn')
        second.wait_for_selector('#answerInput:not([disabled])')
        self.assertEqual(second.input_value('#answerInput'), '8')
        first.wait_for_selector('#questionSubmitFeedback:not(.d-none)')
        self.assertTrue(first.locator('#answerInput').is_disabled())
        self.assertGreater(int(second.inner_text('#playerTimeLeft')), 30)
        self.assertEqual(self.operate(lambda: m.BlackJackQuiz.objects.get(pk=self.quiz.pk).started_at), before)
        self.answer(second, 8)
        self.assertEqual(self.errors, [])
        self.assertFalse(any('abgelehnt' in text for text in self.dialogs))

    def test_repeated_set_adds_score_row_and_spectator_uses_same_progress(self):
        self.start(index=1)
        host, page = self.page(host=True), self.page(theme='vhs')
        context = self.browser.new_context()
        self.contexts.append(context)
        install_browser_test_stubs(context)
        spectator = context.new_page()
        spectator.goto(self.live_server_url + reverse('games_hub:spectate_session', args=[self.hub.code]))
        spectator.wait_for_function('document.getElementById("progressText").textContent.includes("RUNDE 1")')
        self.answer(page, 19)
        host.click('#endQuestionBtn')
        page.wait_for_selector('#correctAnswerState:not(.d-none)')
        page.click('#continueRevealBtn')
        self.next_question(host, 0)
        self.answer(page, 9)
        host.click('#endQuestionBtn')
        page.wait_for_selector('#correctAnswerState:not(.d-none)')
        page.click('#continueRevealBtn')
        page.wait_for_selector('#setEndedState:not(.d-none)')
        host.click('.select-set-btn[data-set-number="1"]')
        self.next_question(host, 0)
        self.answer(page, 9)
        host.click('#endQuestionBtn')
        page.wait_for_selector('#correctAnswerState:not(.d-none)')
        self.assertEqual(page.locator('#blackjackScoreList .blackjack-score-row').count(), 3)
        self.assertEqual(page.locator('.play-container').get_attribute('data-blackjack-current-set'), '2')
        self.assertEqual(page.locator('.play-container').get_attribute('data-blackjack-total-sets'), '3')
        spectator.wait_for_function('document.getElementById("progressText").textContent.includes("SET 2 / 3")')
        self.assertEqual(self.errors, [])
