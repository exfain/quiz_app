import json
import os
import random
import string

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from django.contrib.auth.models import User
from django.test import Client as DjangoClient
from django.urls import reverse

from QuizGame.models import Quiz, QuizQuestion, QuizSession
from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser

try:
    from channels.testing import ChannelsLiveServerTestCase as _BaseLiveServerTestCase
except ImportError:
    from django.test import LiveServerTestCase as _BaseLiveServerTestCase


def _rand_code(length=6):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


class SessionCheckInBrowserE2ETest(_BaseLiveServerTestCase):
    """Focused browser smoke test for Hub check-in and first-game routing."""

    TIMEOUT = 15_000
    LONG_TIMEOUT = 35_000

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
        if getattr(cls, "_playwright_available", False):
            cls._browser.close()
            cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfügbar: {self._playwright_error}")

        self.password = "testpass123"
        self.admin = User.objects.create_superuser(
            username=f"admin_{_rand_code()}",
            password=self.password,
            email="",
        )
        self.client_for_setup = DjangoClient()
        self.client_for_setup.force_login(self.admin)

        self.quiz = self._create_quick_quiz()
        self.session = self._create_hub_session(self.quiz)

        self.host_context = self._browser.new_context()
        self.participant_one_context = self._browser.new_context()
        self.participant_two_context = self._browser.new_context()
        install_browser_test_stubs(self.host_context)
        install_browser_test_stubs(self.participant_one_context)
        install_browser_test_stubs(self.participant_two_context)

        self.host_page = self.host_context.new_page()
        self.participant_one_page = self.participant_one_context.new_page()
        self.participant_two_page = self.participant_two_context.new_page()
        self.browser_errors = []
        for page_name, page in (
            ("host", self.host_page),
            ("participant_one", self.participant_one_page),
            ("participant_two", self.participant_two_page),
        ):
            page.on("console", lambda msg, page_name=page_name: self.browser_errors.append(
                f"{page_name} console {msg.type}: {msg.text}"
            ))
            page.on("pageerror", lambda exc, page_name=page_name: self.browser_errors.append(
                f"{page_name} pageerror: {exc}"
            ))

        self._admin_login()

    def tearDown(self):
        for page in (
            getattr(self, "host_page", None),
            getattr(self, "participant_one_page", None),
            getattr(self, "participant_two_page", None),
        ):
            if page:
                page.close()
        for context in (
            getattr(self, "host_context", None),
            getattr(self, "participant_one_context", None),
            getattr(self, "participant_two_context", None),
        ):
            if context:
                context.close()

    def _create_quick_quiz(self):
        quiz = Quiz.objects.create(
            title="E2E Check-in Quick Quiz",
            creator=self.admin,
            max_participants=20,
        )
        question = QuizQuestion.objects.create(
            question_text="Was ist 2 + 2?",
            question_type="multiple_choice",
            option_a="4",
            option_b="3",
            option_c="5",
            option_d="6",
            correct_answer="A",
            points=1,
            time_limit=30,
            created_by=self.admin,
        )
        quiz.selected_questions.set([question])
        quiz.question_order = [question.id]
        quiz.save(update_fields=["question_order", "updated_at"])
        QuizSession.objects.get_or_create(quiz=quiz)
        return quiz

    def _create_hub_session(self, quiz):
        while True:
            code = _rand_code()
            if not HubSession.objects.filter(code=code).exists():
                break
        session = HubSession.objects.create(
            code=code,
            name=f"E2E Check-in {code}",
            is_active=False,
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key="quiz",
            room_code=quiz.room_code,
            title=quiz.title,
        )
        return session

    def _admin_login(self):
        self.host_page.goto(f"{self.live_server_url}{reverse('admin_dashboard:login')}")
        self.host_page.fill("input[name='username']", self.admin.username)
        self.host_page.fill("input[name='password']", self.password)
        self.host_page.click("button[type='submit']")
        self.host_page.wait_for_url(f"**{reverse('admin_dashboard:home')}**", timeout=self.TIMEOUT)

    def _join_lobby(self, page, nickname):
        page.goto(f"{self.live_server_url}{reverse('games_hub:lobby', args=[self.session.code])}")
        page.wait_for_selector("#nickname", timeout=self.TIMEOUT)
        page.fill("#nickname", nickname)
        page.wait_for_selector("#joinBtn:not([disabled])", timeout=self.TIMEOUT)
        page.click("#joinBtn")
        page.wait_for_selector("#joinCard", state="hidden", timeout=self.TIMEOUT)
        page.wait_for_selector("#checkInCard", state="visible", timeout=self.TIMEOUT)

    def _start_session_from_monitor(self):
        self.host_page.goto(f"{self.live_server_url}{reverse('games_hub:monitor', args=[self.session.code])}")
        self.host_page.wait_for_selector("#startSessionBtn", timeout=self.TIMEOUT)
        try:
            self.host_page.wait_for_function(
                "() => document.querySelector('#wsStatus .status-dot')?.classList.contains('connected')",
                timeout=self.TIMEOUT,
            )
        except Exception as exc:
            status_text = self.host_page.locator("#wsStatus").inner_text(timeout=1000)
            diagnostics = "\n".join(self.browser_errors[-20:])
            raise AssertionError(
                f"Hub monitor WebSocket did not connect. status={status_text!r}\n{diagnostics}"
            ) from exc
        self.host_page.click("#startSessionBtn")
        self.host_page.wait_for_selector("#startSessionBtn", state="detached", timeout=self.TIMEOUT)
        self.host_page.wait_for_selector(".launch-btn:not(.disabled)", timeout=self.TIMEOUT)

    def _assert_activation_blocked_before_check_in(self):
        response = self.client_for_setup.post(
            reverse("games_hub:activate_session_game", args=[self.session.code]),
            data=json.dumps({
                "game_key": "quiz",
                "room_code": self.quiz.room_code,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 428)
        payload = response.json()
        self.assertTrue(payload["check_in_required"])
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, "waiting")

    def _wait_until_host_check_in_row(self, nickname, expected_text):
        self.host_page.wait_for_function(
            """([nickname, expected]) => {
                const rows = Array.from(document.querySelectorAll('#checkInParticipantRows tr'));
                return rows.some((row) => row.textContent.includes(nickname) && row.textContent.includes(expected));
            }""",
            arg=[nickname, expected_text],
            timeout=self.TIMEOUT,
        )

    def _start_quiz_from_monitor(self):
        monitor_url = (
            f"{self.live_server_url}"
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.quiz.room_code])}"
            f"?hub_session={self.session.code}"
        )
        self.host_page.goto(monitor_url)
        self.host_page.wait_for_selector("#startQuizBtn:not([disabled])", timeout=self.TIMEOUT)
        self.host_page.wait_for_timeout(1000)
        self.host_page.click("#startQuizBtn")

    def test_check_in_blocks_first_game_until_completed_and_routes_participants(self):
        self._join_lobby(self.participant_one_page, "Alice")
        self._join_lobby(self.participant_two_page, "Bob")

        self._start_session_from_monitor()
        self._assert_activation_blocked_before_check_in()

        self.host_page.click('[data-session-panel-target="checkInPanel"]')
        self.host_page.wait_for_selector("#checkInPanel.is-open", timeout=self.TIMEOUT)
        self.host_page.click("#startCheckInBtn")
        self.participant_one_page.wait_for_selector("#readyCheckInBtn:not([disabled])", timeout=self.TIMEOUT)
        self.participant_two_page.wait_for_selector("#readyCheckInBtn:not([disabled])", timeout=self.TIMEOUT)

        self.participant_one_page.click("#readyCheckInBtn")
        self.participant_one_page.wait_for_function(
            "() => document.querySelector('#readyCheckInBtn')?.textContent.includes('Bereit')",
            timeout=self.TIMEOUT,
        )
        self._wait_until_host_check_in_row("Alice", "Bereit")

        self.participant_one_page.reload()
        self.participant_one_page.wait_for_selector("#readyCheckInBtn:disabled", timeout=self.TIMEOUT)
        self.participant_one_page.wait_for_function(
            "() => document.querySelector('#readyCheckInBtn')?.textContent.includes('Bereit')",
            timeout=self.TIMEOUT,
        )

        self.participant_two_page.click("#readyCheckInBtn")
        self._wait_until_host_check_in_row("Bob", "Bereit")
        self.host_page.wait_for_selector("#checkInReadyBadge", timeout=self.TIMEOUT)
        self.host_page.wait_for_function(
            "() => document.querySelector('#checkInReadyBadge')?.textContent.includes('Bereit: 2')",
            timeout=self.TIMEOUT,
        )

        self.host_page.click("#completeCheckInBtn")
        self.host_page.wait_for_function(
            "() => document.querySelector('#checkInLockedBadge')?.textContent.includes('Locked: 2')",
            timeout=self.TIMEOUT,
        )
        self.session.refresh_from_db()
        self.assertEqual(self.session.check_in_status, HubSession.CHECK_IN_COMPLETED)
        self.assertEqual(self.session.locked_participant_count, 2)

        self._start_quiz_from_monitor()
        play_pattern = f"**/quiz/play/{self.quiz.room_code}/**"
        self.participant_one_page.wait_for_url(play_pattern, timeout=self.LONG_TIMEOUT)
        self.participant_two_page.wait_for_url(play_pattern, timeout=self.LONG_TIMEOUT)

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, "active")
        self.assertIsNotNone(self.quiz.started_at)
        self.assertEqual(HubParticipant.objects.filter(session=self.session).count(), 2)
        self.assertEqual(self.quiz.participants.filter(hub_session_code=self.session.code).count(), 2)

        self.participant_one_page.reload()
        self.participant_one_page.wait_for_url(play_pattern, timeout=self.TIMEOUT)

    def test_lobby_join_requires_token_for_rejoin_and_uses_vhs_states(self):
        lobby_url = f"{self.live_server_url}{reverse('games_hub:lobby', args=[self.session.code])}"
        self.participant_one_page.add_init_script(
            "localStorage.setItem('participant_interface_theme', 'vhs')"
        )
        self.participant_one_page.goto(lobby_url)
        self.participant_one_page.wait_for_selector(
            "html[data-participant-theme='vhs'] #nickname",
            timeout=self.TIMEOUT,
        )

        status = self.participant_one_page.locator("#nicknameStatus")
        join_button = self.participant_one_page.locator("#joinBtn")
        self.assertTrue(status.is_hidden())
        self.assertTrue(join_button.is_disabled())

        self.participant_one_page.fill("#nickname", "ab")
        self.participant_one_page.wait_for_function(
            "() => document.querySelector('#nicknameStatus')?.textContent === 'Der Name ist zu kurz.'",
            timeout=self.TIMEOUT,
        )
        self.assertTrue(join_button.is_disabled())

        disabled_transform = join_button.evaluate(
            "(element) => getComputedStyle(element).transform"
        )
        disabled_box = join_button.bounding_box()
        self.assertIsNotNone(disabled_box)
        self.participant_one_page.mouse.move(
            disabled_box["x"] + disabled_box["width"] / 2,
            disabled_box["y"] + disabled_box["height"] / 2,
        )
        self.assertEqual(
            join_button.evaluate("(element) => getComputedStyle(element).transform"),
            disabled_transform,
        )

        self.participant_one_page.fill("#nickname", "Alice")
        self.participant_one_page.wait_for_function(
            "() => document.querySelector('#nicknameStatus')?.textContent === 'Verfügbar'",
            timeout=self.TIMEOUT,
        )
        self.assertFalse(join_button.is_disabled())
        default_transform = join_button.evaluate(
            "(element) => getComputedStyle(element).transform"
        )
        join_button.hover()
        self.participant_one_page.wait_for_timeout(250)
        hover_transform = join_button.evaluate(
            "(element) => getComputedStyle(element).transform"
        )
        self.assertNotEqual(hover_transform, default_transform)
        button_box = join_button.bounding_box()
        self.participant_one_page.mouse.down()
        self.participant_one_page.wait_for_timeout(80)
        active_transform = join_button.evaluate(
            "(element) => getComputedStyle(element).transform"
        )
        self.assertNotEqual(active_transform, hover_transform)
        self.participant_one_page.mouse.move(0, 0)
        self.participant_one_page.mouse.up()
        self.assertIsNotNone(button_box)

        styles = self.participant_one_page.evaluate(
            """() => {
                const card = document.querySelector('#joinCard');
                const status = document.querySelector('#nicknameStatus');
                const recDot = document.querySelector('.vhs-theme-rec-dot');
                const cardStyle = getComputedStyle(card);
                return {
                    backgroundColor: cardStyle.backgroundColor,
                    backgroundImage: cardStyle.backgroundImage,
                    borderTopWidth: cardStyle.borderTopWidth,
                    boxShadow: cardStyle.boxShadow,
                    paddingTop: cardStyle.paddingTop,
                    statusFont: getComputedStyle(status).fontFamily,
                    recColor: getComputedStyle(recDot).backgroundColor,
                };
            }"""
        )
        self.assertEqual(styles["backgroundColor"], "rgba(0, 0, 0, 0)")
        self.assertEqual(styles["backgroundImage"], "none")
        self.assertEqual(styles["borderTopWidth"], "0px")
        self.assertEqual(styles["boxShadow"], "none")
        self.assertEqual(styles["paddingTop"], "0px")
        self.assertIn("Courier New", styles["statusFont"])

        self.participant_one_page.fill("#nickname", "ab")
        self.participant_one_page.wait_for_function(
            "() => document.querySelector('#nicknameStatus')?.textContent === 'Der Name ist zu kurz.'",
            timeout=self.TIMEOUT,
        )
        error_color = status.evaluate("(element) => getComputedStyle(element).color")
        self.assertEqual(error_color, styles["recColor"])

        self.participant_one_page.fill("#nickname", "Alice")
        self.participant_one_page.wait_for_selector("#joinBtn:not([disabled])", timeout=self.TIMEOUT)
        self.participant_one_page.click("#joinBtn")
        self.participant_one_page.wait_for_selector("#joinCard", state="hidden", timeout=self.TIMEOUT)
        credential = self.participant_one_page.evaluate(
            f"localStorage.getItem('hub_lobby_rejoin:{self.session.code}')"
        )
        self.assertTrue(credential)

        self.participant_two_page.goto(lobby_url)
        self.participant_two_page.fill("#nickname", "Alice")
        self.participant_two_page.wait_for_function(
            "() => document.querySelector('#nicknameStatus')?.textContent === 'Dieser Name ist bereits vergeben.'",
            timeout=self.TIMEOUT,
        )
        self.assertTrue(self.participant_two_page.locator("#joinBtn").is_disabled())

        self.participant_one_page.reload()
        self.participant_one_page.wait_for_selector("#joinCard", state="hidden", timeout=self.TIMEOUT)
        self.assertEqual(
            HubParticipant.objects.filter(session=self.session, nickname="Alice").count(),
            1,
        )
