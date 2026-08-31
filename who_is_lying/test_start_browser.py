import os
import random
import string
import time
from unittest.mock import patch

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from django.contrib.auth.models import User
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubSession
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser

from .models import WhoParticipant, WhoQuestion, WhoQuiz, WhoSession

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserLiveServerTestCase
except ImportError:
    from django.test import LiveServerTestCase as _BrowserLiveServerTestCase


def _random_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


class WhoLyingStartBrowserFlowTests(_BrowserLiveServerTestCase):
    TIMEOUT = 15_000
    LONG_TIMEOUT = 35_000

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
            username=f'who_admin_{_random_code()}',
            password=self.password,
            email='',
        )
        self.setup_client = DjangoClient()
        self.setup_client.force_login(self.admin)
        self.quiz = WhoQuiz.objects.create(
            title='Who Browser Start',
            room_code=''.join(random.choices(string.digits, k=4)),
            creator=self.admin,
            status='waiting',
        )
        self.questions = [
            WhoQuestion.objects.create(
                statement=f'Who browser set {index}',
                points=10,
                time_limit=5,
                people=[
                    {'name': 'Ada', 'is_lying': False},
                    {'name': 'Bob', 'is_lying': True},
                ],
                created_by=self.admin,
            )
            for index in (1, 2)
        ]
        self.quiz.selected_questions.set(self.questions)
        self.quiz.question_order = [question.id for question in self.questions]
        self.quiz.save(update_fields=['question_order', 'updated_at'])
        WhoSession.objects.create(quiz=self.quiz)
        self.session = HubSession.objects.create(
            code=_random_code(),
            name='Who Browser Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='who',
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
            '**/who/join/',
            lambda route: (time.sleep(1.0), route.continue_()),
        )
        self.browser_errors = []
        for label, page in [('host', self.host_page), ('participant-a', self.participant_pages[0]), ('participant-b', self.participant_pages[1])]:
            page.on('pageerror', lambda exc, label=label: self.browser_errors.append(f'{label}: {exc}'))
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
        page.wait_for_selector('#joinBtn:not([disabled])', timeout=self.TIMEOUT)
        page.click('#joinBtn')
        page.wait_for_selector('#joinCard', state='hidden', timeout=self.TIMEOUT)

    def _monitor_url(self):
        return (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:who_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )

    def _send_and_start_set(self, question):
        send_button = self.host_page.locator(
            f'.send-question-btn[data-question-id="{question.id}"]'
        )
        send_button.wait_for(state='visible', timeout=self.TIMEOUT)
        self.host_page.wait_for_timeout(500)
        send_button.click()
        self.host_page.wait_for_selector('#startSetBtn', timeout=self.TIMEOUT)
        self.host_page.wait_for_selector('#startSetBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#startSetBtn')
        self.host_page.wait_for_selector('#endQuestionBtn', timeout=self.TIMEOUT)

    def test_start_reload_and_two_sets_use_running_monitor(self):
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.wait_for_timeout(500)
        self.host_page.click('#startQuizBtn')

        self.host_page.wait_for_selector('#startQuizBtn', state='detached', timeout=self.TIMEOUT)
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.assertIsNotNone(self.quiz.started_at)

        play_pattern = f'**/who/play/{self.quiz.room_code}/**'
        for page in self.participant_pages:
            page.wait_for_url(play_pattern, timeout=self.LONG_TIMEOUT)

        self.session.refresh_from_db()
        self.assertEqual(self.session.current_step_index, 0)
        self.assertEqual(
            set(
                WhoParticipant.objects.filter(
                    quiz=self.quiz,
                    hub_session_code=self.session.code,
                    is_active=True,
                ).values_list('name', flat=True)
            ),
            {'Alice', 'Bob'},
        )

        self.host_page.reload()
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.assertEqual(self.host_page.locator('#startQuizBtn').count(), 0)

        self._send_and_start_set(self.questions[0])
        self.assertEqual(self.host_page.locator('#startQuizBtn').count(), 0)
        self.host_page.click('#endQuestionBtn')
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)

        self._send_and_start_set(self.questions[1])
        self.assertEqual(self.host_page.locator('#startQuizBtn').count(), 0)
        self.assertFalse(any('updateQuizStatus is not a function' in error for error in self.browser_errors), self.browser_errors)

    def test_reused_game_start_routes_participants_and_spectator(self):
        previous_started_at = self.session.started_at - timezone.timedelta(hours=1)
        self.quiz.started_at = previous_started_at
        self.quiz.save(update_fields=['started_at', 'updated_at'])
        for nickname in ('Alice', 'Bob'):
            WhoParticipant.objects.get_or_create(
                quiz=self.quiz,
                name=nickname,
                hub_session_code=self.session.code,
                defaults={'is_active': False},
            )

        spectator_context = self._browser.new_context()
        install_browser_test_stubs(spectator_context)
        spectator_page = spectator_context.new_page()
        try:
            spectator_page.goto(
                f'{self.live_server_url}'
                f'{reverse("games_hub:spectate_session", args=[self.session.code])}'
            )
            self.host_page.goto(self._monitor_url())
            self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
            self.host_page.click('#startQuizBtn')

            self.host_page.wait_for_selector('#startQuizBtn', state='detached', timeout=self.TIMEOUT)
            play_pattern = f'**/who/play/{self.quiz.room_code}/**'
            for page in self.participant_pages:
                page.wait_for_url(play_pattern, timeout=self.LONG_TIMEOUT)
            spectator_page.wait_for_selector(
                '[data-spectator-game="who"]',
                timeout=self.LONG_TIMEOUT,
            )

            self.host_page.reload()
            self.host_page.wait_for_selector(
                '.send-question-btn:not([disabled])',
                timeout=self.TIMEOUT,
            )
            self.participant_pages[0].reload()
            self.participant_pages[0].wait_for_selector(
                '#waitingQuestionState',
                state='attached',
                timeout=self.TIMEOUT,
            )
            spectator_page.reload()
            spectator_page.wait_for_selector(
                '[data-spectator-game="who"]',
                timeout=self.LONG_TIMEOUT,
            )

            self._send_and_start_set(self.questions[0])
            self.participant_pages[0].wait_for_selector(
                '#questionState:not(.d-none)',
                timeout=self.LONG_TIMEOUT,
            )

            self.quiz.refresh_from_db()
            self.assertEqual(self.quiz.status, 'active')
            self.assertGreater(self.quiz.started_at, previous_started_at)
            self.assertGreaterEqual(self.quiz.started_at, self.session.started_at)
        finally:
            spectator_page.close()
            spectator_context.close()

    def test_recall_happens_once_before_retrying_start(self):
        stale_participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.wait_for_timeout(500)
        self.host_page.click('#startQuizBtn')
        self.host_page.wait_for_function(
            "() => document.querySelector('#startQuizBtn')?.disabled === true",
            timeout=self.TIMEOUT,
        )

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'waiting')
        self.assertIsNone(self.quiz.started_at)

        self.host_page.evaluate(
            "const button = document.querySelector('#lobbyReturnGuardPrimaryBtn'); button.disabled = false; button.click()"
        )
        with patch('games_hub.views.broadcast_players_recalled_to_lobby'):
            recall_response = self.setup_client.post(
                reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
            )
        self.assertEqual(recall_response.status_code, 200)
        self.assertTrue(recall_response.json()['all_in_lobby'])
        stale_participant.refresh_from_db()
        self.assertFalse(stale_participant.is_active)
        self.host_page.evaluate(
            "document.querySelector('#lobbyReturnGuardModal').dispatchEvent(new Event('hidden.bs.modal'))"
        )

        self.host_page.wait_for_function(
            "() => document.querySelector('#startQuizBtn')?.disabled === false",
            timeout=self.TIMEOUT,
        )
        self.host_page.click('#startQuizBtn')
        self.host_page.wait_for_selector('#startQuizBtn', state='detached', timeout=self.TIMEOUT)

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.assertIsNotNone(self.quiz.started_at)
