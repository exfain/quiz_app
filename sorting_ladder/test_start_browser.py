import os
import random
import string
import time

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubSession
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser

from .models import (
    SortingItem,
    SortingLadderGame,
    SortingLadderSession,
    SortingQuestion,
)

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserLiveServerTestCase
except ImportError:
    from django.test import LiveServerTestCase as _BrowserLiveServerTestCase


def _random_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


class SortingLadderStartBrowserFlowTests(_BrowserLiveServerTestCase):
    TIMEOUT = 20_000
    LONG_TIMEOUT = 40_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls._playwright, cls._browser = start_chromium_browser(headless=True)
            cls._playwright_available = True
        except Exception as exc:
            cls._playwright_available = False
            cls._playwright_error = exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, '_playwright_available', False):
            cls._browser.close()
            cls._playwright.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._playwright_available:
            self.skipTest(f'Playwright/Chromium nicht verfuegbar: {self._playwright_error}')

        self.password = 'testpass123'
        self.admin = User.objects.create_superuser(
            username=f'sorting_admin_{_random_code()}',
            password=self.password,
            email='',
        )
        self.quiz = SortingLadderGame.objects.create(
            title='Sorting Browser Start',
            room_code=''.join(random.choices(string.digits, k=4)),
            creator=self.admin,
            status='waiting',
            tutorial_enabled=True,
        )
        self.tutorial = self._create_question('Tutorial T')
        self.regular_questions = [
            self._create_question(f'Regular {name}')
            for name in ('A', 'B', 'C')
        ]
        self.quiz.tutorial_question = self.tutorial
        self.quiz.current_question = self.tutorial
        self.quiz.tutorial_active = True
        self.quiz.selected_questions.set([self.tutorial, *self.regular_questions])
        self.quiz.question_order = [question.id for question in self.regular_questions]
        self.quiz.save(update_fields=[
            'tutorial_question',
            'current_question',
            'tutorial_active',
            'question_order',
        ])
        tutorial_items = list(self.tutorial.elements.order_by('correct_rank'))
        stale_session = SortingLadderSession.objects.create(
            quiz=self.quiz,
            current_round=1,
            is_round_active=True,
            active_element=tutorial_items[1],
            round_start_time=timezone.now(),
            round_end_time=timezone.now() + timezone.timedelta(seconds=30),
            shuffled_item_ids=','.join(str(item.id) for item in tutorial_items),
        )
        stale_session.placed_elements.add(tutorial_items[0])

        self.session = HubSession.objects.create(
            code=_random_code(),
            name='Sorting Browser Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='sorting_ladder',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )

        self.host_context = self._browser.new_context()
        self.participant_contexts = [self._browser.new_context(), self._browser.new_context()]
        for context in [self.host_context, *self.participant_contexts]:
            install_browser_test_stubs(context)
        self.host_page = self.host_context.new_page()
        self.participant_pages = [context.new_page() for context in self.participant_contexts]
        self.participant_pages[1].route(
            '**/sorting-ladder/join/',
            lambda route: (time.sleep(1.0), route.continue_()),
        )
        self._login_host()
        self._join_lobby(self.participant_pages[0], 'Alice')
        self._join_lobby(self.participant_pages[1], 'Bob')

    def tearDown(self):
        for page in [getattr(self, 'host_page', None), *getattr(self, 'participant_pages', [])]:
            if page:
                page.close()
        for context in [getattr(self, 'host_context', None), *getattr(self, 'participant_contexts', [])]:
            if context:
                context.close()

    def _create_question(self, name):
        question = SortingQuestion.objects.create(
            question_text=name,
            description=f'Order {name}.',
            upper_label='High',
            lower_label='Low',
            points=10,
            round_time_limit=30,
            created_by=self.admin,
        )
        items = [
            SortingItem.objects.create(
                topic=question,
                text=f'{name} {index}',
                correct_rank=index,
            )
            for index in range(1, 4)
        ]
        question.starting_item = items[1]
        question.save(update_fields=['starting_item'])
        return question

    def _login_host(self):
        self.host_page.goto(f'{self.live_server_url}{reverse("admin_dashboard:login")}')
        self.host_page.fill("input[name='username']", self.admin.username)
        self.host_page.fill("input[name='password']", self.password)
        self.host_page.click("button[type='submit']")
        self.host_page.wait_for_url(f'**{reverse("admin_dashboard:home")}**', timeout=self.TIMEOUT)

    def _join_lobby(self, page, nickname):
        page.goto(f'{self.live_server_url}{reverse("games_hub:lobby", args=[self.session.code])}')
        page.wait_for_selector('#nickname', timeout=self.TIMEOUT)
        page.fill('#nickname', nickname)
        page.click('#joinBtn')
        page.wait_for_selector('#joinCard', state='hidden', timeout=self.TIMEOUT)

    def _monitor_url(self):
        return (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:sorting_ladder_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )

    def _wait_host_socket(self):
        self.host_page.wait_for_function(
            '() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN',
            timeout=self.TIMEOUT,
        )

    def _capture_host_messages(self):
        self.host_page.evaluate(
            """() => {
                window.__sortingHostMessages = [];
                const monitor = window.adminGameMonitor;
                const original = monitor.handleWebSocketMessage.bind(monitor);
                monitor.handleWebSocketMessage = (payload) => {
                    window.__sortingHostMessages.push(payload);
                    return original(payload);
                };
            }"""
        )

    def _start_diagnostics(self):
        self.quiz.refresh_from_db()
        return {
            'status': self.quiz.status,
            'current_question_id': self.quiz.current_question_id,
            'messages': self.host_page.evaluate('() => window.__sortingHostMessages || []'),
            'button_disabled': self.host_page.locator('#startQuizBtn').is_disabled()
            if self.host_page.locator('#startQuizBtn').count()
            else None,
            'lobby_modal': self.host_page.locator('#lobbyReturnGuardModal.show').count(),
            'conflict_modal': self.host_page.locator('#activeGameConflictModal.show').count(),
        }

    def _start_normal_game(self):
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.assertFalse(self.host_page.is_checked('#playTutorialToggle'))
        self.assertEqual(self.host_page.locator('#activeQuestion').count(), 0)
        for question in self.regular_questions:
            self.host_page.get_by_text(question.question_text, exact=True).wait_for(
                state='visible', timeout=self.TIMEOUT
            )
        self._wait_host_socket()
        self._capture_host_messages()
        self.host_page.click('#startQuizBtn')
        try:
            self.host_page.wait_for_selector('#startQuizBtn', state='detached', timeout=self.TIMEOUT)
        except Exception as exc:
            self.fail(f'Normal start did not complete: {self._start_diagnostics()}; {exc}')
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.assertEqual(self.host_page.locator('#activeQuestion').count(), 0)
        for page in self.participant_pages:
            page.wait_for_url(
                f'**/sorting-ladder/play/{self.quiz.room_code}/**',
                timeout=self.LONG_TIMEOUT,
            )

    def _send_question(self, question, expected_question=None):
        expected_question = expected_question or question
        self._wait_host_socket()
        self.host_page.locator(
            f'.send-question-btn[data-question-id="{question.id}"]'
        ).click()
        self.host_page.wait_for_selector(
            f'#activeQuestion [class="question-text"]:has-text("{expected_question.question_text}")',
            timeout=self.TIMEOUT,
        )

    def _open_round(self):
        self._wait_host_socket()
        self.host_page.wait_for_selector(
            '#revealSortingContentBtn:not([disabled])', timeout=self.TIMEOUT
        )
        self.host_page.click('#revealSortingContentBtn')
        self.host_page.wait_for_selector(
            '#openSortingRoundBtn:not([disabled])', timeout=self.TIMEOUT
        )
        self.host_page.click('#openSortingRoundBtn')
        self.host_page.wait_for_selector(
            '#startNextRoundBtn:not(.d-none), #endQuestionBtn:not(.d-none)',
            timeout=self.TIMEOUT,
        )

    def _submit_round(self, page):
        active_card = "#topicLayout:not(.round-locked) .answer-card[draggable='true']"
        page.wait_for_selector(active_card, timeout=self.LONG_TIMEOUT)
        source = page.locator(active_card).first
        source_text = source.locator('.option-text').inner_text().strip()
        source_rank = int(source_text.rsplit(' ', 1)[1])
        source_id = int(source.get_attribute('data-item-id'))
        ladder_ranks = [
            int(text.strip().rsplit(' ', 1)[1])
            for text in page.locator('.ladder-row-text').all_inner_texts()
        ]
        target_position = sum(rank < source_rank for rank in ladder_ranks)
        page.evaluate(
            '([itemId, position]) => window.sortingPlayer.placeSortingItemAtPosition(itemId, position)',
            [source_id, target_position],
        )
        page.wait_for_selector('#submitRoundBtn:not([disabled])', timeout=self.TIMEOUT)
        page.click('#submitRoundBtn')

    def _play_question(self, question):
        self._send_question(question)
        for expected_round in (1, 2):
            self._open_round()
            for page in self.participant_pages:
                page.wait_for_function(
                    '(roundNumber) => window.sortingPlayer?.currentRound === roundNumber',
                    arg=expected_round,
                    timeout=self.LONG_TIMEOUT,
                )
                self._submit_round(page)
            self.host_page.wait_for_function(
                '() => window.adminGameMonitor?.allAnswersInRound === true',
                timeout=self.TIMEOUT,
            )
            if expected_round == 1:
                self.host_page.wait_for_selector(
                    '#startNextRoundBtn:not([disabled]):not(.d-none)',
                    timeout=self.TIMEOUT,
                )
                self.host_page.click('#startNextRoundBtn')

        self.host_page.wait_for_selector('#endQuestionBtn:not(.d-none)', timeout=self.TIMEOUT)
        self.host_page.click('#endQuestionBtn')
        self.host_page.wait_for_selector('#showSolutionBtn', timeout=self.TIMEOUT)
        self.host_page.click('#showSolutionBtn')
        self.host_page.wait_for_selector('#endQuestionBtn', timeout=self.TIMEOUT)
        self.host_page.click('#endQuestionBtn')
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)

    def test_normal_start_lists_three_regular_questions_and_plays_two(self):
        self._start_normal_game()

        self.quiz.refresh_from_db()
        self.quiz.session.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.assertIsNone(self.quiz.current_question_id)
        self.assertFalse(self.quiz.tutorial_active)
        self.assertEqual(self.quiz.session.current_round, 0)
        self.assertFalse(self.quiz.session.is_round_active)

        self._play_question(self.regular_questions[0])
        self._play_question(self.regular_questions[1])

    def test_explicit_tutorial_uses_tutorial_question(self):
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.check('#playTutorialToggle')
        self._wait_host_socket()
        self._capture_host_messages()
        self.host_page.click('#startQuizBtn')
        try:
            self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        except Exception as exc:
            self.fail(f'Tutorial start did not complete: {self._start_diagnostics()}; {exc}')
        self._send_question(self.regular_questions[0], expected_question=self.tutorial)

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.tutorial.id)
        self.host_page.get_by_text(self.tutorial.question_text, exact=True).wait_for(
            state='visible', timeout=self.TIMEOUT
        )
