from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import time
import unittest

from django.template.loader import render_to_string
from django.test import TestCase
from django.utils import timezone

from games_hub.game_intro import (
    GAME_INTRO_DURATION_MS,
    GAME_INTRO_TITLES,
    serialize_game_intro,
    start_game_intro,
)
from games_hub.models import HubGameStep, HubSession
from games_hub.playwright_e2e import start_chromium_browser


REPO_ROOT = Path(__file__).resolve().parent.parent

PLAYER_TEMPLATES = [
    "templates/quiz/play.html",
    "templates/estimation/play.html",
    "templates/where_is_this/play.html",
    "templates/who_is_that/play.html",
    "templates/clue_rush/play.html",
    "templates/wann_war_das/play.html",
    "templates/buzzer/play.html",
    "templates/host_points/play.html",
    "templates/assign/play.html",
    "templates/sorting_ladder/play.html",
    "templates/black_jack_quiz/play.html",
    "templates/wer_weiss_mehr/play.html",
    "templates/who_is_lying/play.html",
]


def read_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class SessionGameIntroPersistenceTests(TestCase):
    def setUp(self):
        self.session = HubSession.objects.create(code="INTRO", name="Intro")

    def test_intro_window_is_absolute_persistent_and_idempotent(self):
        step = HubGameStep.objects.create(
            session=self.session,
            order=2,
            game_key="clue_rush",
            room_code="CLUE1",
            title="Hinweise der Nacht",
        )

        first = start_game_intro("INTRO", "clue_rush", "CLUE1")
        second = start_game_intro("INTRO", "clue_rush", "CLUE1")
        step.refresh_from_db()

        self.assertEqual(first["intro"]["state_revision"], 1)
        self.assertEqual(second["intro"]["state_revision"], 1)
        self.assertEqual(
            step.intro_ends_at - step.intro_started_at,
            timedelta(milliseconds=GAME_INTRO_DURATION_MS),
        )
        self.assertEqual(first["intro"]["intro_started_at"], second["intro"]["intro_started_at"])
        self.assertEqual(first["intro"]["intro_ends_at"], second["intro"]["intro_ends_at"])
        self.assertEqual(first["intro"]["game_number"], 3)
        self.assertEqual(first["intro"]["game_title"], "HINWEISE DER NACHT")
        self.assertTrue(first["intro"]["intro_active"])

    def test_all_participant_game_types_have_stable_visible_titles(self):
        self.assertEqual(set(GAME_INTRO_TITLES), {choice[0] for choice in HubGameStep.GAME_CHOICES})
        for order, (game_key, expected_title) in enumerate(GAME_INTRO_TITLES.items()):
            step = HubGameStep.objects.create(
                session=self.session,
                order=order,
                game_key=game_key,
                room_code=f"R{order}",
            )
            intro = serialize_game_intro(step)
            with self.subTest(game_key=game_key):
                self.assertEqual(intro["game_number"], order + 1)
                self.assertEqual(intro["game_title"], expected_title)
                self.assertNotIn("_", intro["game_title"])

    def test_custom_step_title_overrides_readable_game_type_fallback(self):
        step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key="quiz",
            room_code="QUIZ-CUSTOM",
            title="Natur & Wissenschaft",
        )

        intro = serialize_game_intro(step)

        self.assertEqual(intro["game_title"], "NATUR & WISSENSCHAFT")
        self.assertNotEqual(intro["game_title"], GAME_INTRO_TITLES["quiz"])

    def test_blank_custom_step_title_uses_readable_type_fallback(self):
        step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key="sorting_ladder",
            room_code="SORT-FALLBACK",
            title="   ",
        )

        intro = serialize_game_intro(step)

        self.assertEqual(intro["game_title"], "SORTING LADDER")
        self.assertNotEqual(intro["game_title"], step.game_key.upper())

    def test_expired_intro_is_not_reactivated_by_serialization(self):
        now = timezone.now()
        step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key="quiz",
            room_code="QUIZ1",
            intro_started_at=now - timedelta(seconds=9),
            intro_ends_at=now - timedelta(seconds=1),
            intro_state_revision=1,
        )

        intro = serialize_game_intro(step, server_now=now)

        self.assertFalse(intro["intro_active"])
        self.assertEqual(intro["state_revision"], 1)

    def test_same_game_type_in_two_instances_has_independent_intro_state(self):
        first_step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key="quiz",
            room_code="QUIZ1",
            title="Natur & Wissenschaft",
        )
        second_step = HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key="quiz",
            room_code="QUIZ2",
            title="Film & Fernsehen",
        )

        first = start_game_intro("INTRO", "quiz", "QUIZ1")
        second = start_game_intro("INTRO", "quiz", "QUIZ2")

        self.assertNotEqual(first_step.pk, second_step.pk)
        self.assertNotEqual(
            first["intro"]["game_instance_id"],
            second["intro"]["game_instance_id"],
        )
        self.assertTrue(first["intro"]["intro_active"])
        self.assertTrue(second["intro"]["intro_active"])
        self.assertEqual(first["intro"]["game_title"], "NATUR & WISSENSCHAFT")
        self.assertEqual(second["intro"]["game_title"], "FILM & FERNSEHEN")
        self.assertEqual(first["intro"]["game_number"], 1)
        self.assertEqual(second["intro"]["game_number"], 2)


class SessionGameStartIntroTemplateTests(unittest.TestCase):
    def test_lobby_navigation_uses_only_server_intro_metadata(self):
        content = read_text("templates/hub/lobby.html")

        self.assertIn("appendGameStartIntroParams(playUrl, step);", content)
        self.assertIn("appendGameStartIntroParams(playRoute, data.step || {});", content)
        self.assertIn("intro?.intro_active !== true", content)
        self.assertIn("game_start_intro_started_at", content)
        self.assertIn("game_start_intro_ends_at", content)
        self.assertIn("game_start_server_now", content)
        self.assertIn("game_start_state_revision", content)
        self.assertIn("game_start_client_received_at", content)
        self.assertNotIn("game_start_nonce", content)

    def test_shared_intro_has_reference_structure_timing_and_revision_guard(self):
        content = read_text("templates/includes/_session_game_start_intro.html")
        css = read_text("static/themes/vhs/vhs.css")

        markup_layer_count = (
            content.count('data-qa-vhs-intro-layer="')
            - content.count('[data-qa-vhs-intro-layer="')
        )
        self.assertEqual(markup_layer_count, 5)
        self.assertEqual(content.count('class="qa-vhs-intro__scanline"'), 11)
        self.assertNotIn("document.createElement('span')", content)
        self.assertNotIn("scanlineContainer.replaceChildren()", content)
        self.assertIn("introStartedAt: params.get('game_start_intro_started_at')", content)
        self.assertIn("introEndsAt: params.get('game_start_intro_ends_at')", content)
        self.assertIn("serverNow: params.get('game_start_server_now')", content)
        self.assertIn("performance.now()", content)
        self.assertIn("nextState.stateRevision < highestIntroRevision", content)
        self.assertNotIn("startedAt: Date.now()", content)
        self.assertNotIn("data-vhs-intro-app", content)
        self.assertNotIn("vhs-game-intro-footer", content)
        self.assertNotIn("vhs-game-intro-screen", content)
        self.assertNotIn("vhs-interference__", content)
        self.assertNotIn("session-game-start-intro__card", content)
        self.assertNotIn("session-game-start-intro__text", content)

        self.assertIn("calc(160ms - var(--qa-vhs-intro-elapsed))", css)
        self.assertIn("calc(1640ms - var(--qa-vhs-intro-elapsed))", css)
        self.assertIn("qa-vhs-intro-number-build 420ms steps(5, end)", css)
        self.assertIn("grid-template-rows: repeat(11, 4px)", css)
        self.assertIn("background-color: #000", css)
        self.assertIn("background-image: none", css)
        self.assertIn("z-index: 10000", css)
        self.assertIn("animation: none", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("const VHS_TRANSITION_DURATION_MS = 2800", content)
        self.assertIn("startVhsTransition(serverNow - endsAt)", content)
        self.assertIn("--qa-vhs-transition-elapsed", content)
        self.assertIn("qa-vhs-transition-background 1100ms ease-in-out", css)
        self.assertIn("calc(1100ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(1100ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(1170ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(1240ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(1310ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(1380ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(2040ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(2200ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertIn("calc(2400ms - var(--qa-vhs-transition-elapsed))", css)
        self.assertNotIn("@keyframes qa-vhs-transition-intro-copy", css)
        self.assertNotIn("@keyframes qa-vhs-transition-shell", css)
        waiting = read_text("templates/includes/_participant_start_waiting.html")
        self.assertNotIn("qa-vhs-transition", waiting)
        self.assertIn("qa-wait-loader", waiting)
        self.assertIn("beginnt gleich!", waiting)

    def test_every_player_template_uses_the_shared_intro_and_live_state_hook(self):
        for relative_path in PLAYER_TEMPLATES:
            with self.subTest(template=relative_path):
                content = read_text(relative_path)
                self.assertIn("_session_game_start_intro.html", content)
                self.assertIn("window.sessionGameStartIntro?.handleState", content)


class SessionGameStartIntroBrowserTests(unittest.TestCase):
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
        if getattr(cls, "_playwright_available", False):
            cls._browser.close()
            cls._playwright.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

    def build_page(self, context, *, theme="vhs"):
        intro = render_to_string(
            "includes/_session_game_start_intro.html",
            {"participant": SimpleNamespace(name="Mia")},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        theme_attribute = f' data-participant-theme="{theme}"' if theme else ""
        body = f"""
          <!doctype html>
          <html{theme_attribute}>
            <head><style>{vhs_css}</style></head>
            <body>
              <div class="vhs-theme-shell">
                <header class="vhs-theme-header">
                  <div class="vhs-theme-app">APPNAME</div>
                  <div class="vhs-theme-rec">
                    <span class="vhs-theme-rec-dot"></span>
                    <span class="vhs-theme-rec-text">REC</span>
                  </div>
                </header>
                <div class="play-container">
                  <button id="followupAction" type="button">FOLGEAKTION</button>
                  <span class="followup-label">FOLGESCREEN</span>
                </div>
                <footer class="vhs-theme-footer">
                  <span class="vhs-theme-participant">LIVE · MIA</span>
                </footer>
              </div>
              <aside class="qa-score-widget">Punkte</aside>
              <div id="participant-options-menu-root">Optionen</div>
              {intro}
            </body>
          </html>
        """
        context.route(
            "http://intro.test/intro*",
            lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body),
        )

    def build_query(
        self,
        *,
        elapsed_ms=0,
        duration_ms=8000,
        game_key="estimation",
        room_code="ROOM1",
        game_number=3,
        game_title="ESTIMATION",
        revision=1,
    ):
        server_now = timezone.now()
        started_at = server_now - timedelta(milliseconds=elapsed_ms)
        ends_at = started_at + timedelta(milliseconds=duration_ms)
        return urlencode({
            "game_start_intro": "1",
            "hub_session": "HUB1",
            "game_start_key": game_key,
            "game_start_room": room_code,
            "game_start_instance": f"{game_key}:{room_code}:1",
            "game_start_order": str(game_number),
            "game_start_title": game_title,
            "game_start_intro_started_at": started_at.isoformat(),
            "game_start_intro_ends_at": ends_at.isoformat(),
            "game_start_server_now": server_now.isoformat(),
            "game_start_state_revision": str(revision),
            "game_start_client_received_at": str(round(time.time() * 1000)),
        })

    def open_intro(self, context, query):
        self.build_page(context)
        page = context.new_page()
        page.goto(f"http://intro.test/intro?{query}")
        page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
        return page

    def start_transition(self, page, elapsed_ms):
        return page.evaluate(
            """elapsedMs => {
                const state = window.sessionGameStartIntro.getState();
                const introEndsAt = Date.parse(state.introEndsAt);
                return window.sessionGameStartIntro.handleState({
                    intro_active: false,
                    game_instance_id: state.gameInstanceId,
                    intro_started_at: state.introStartedAt,
                    intro_ends_at: state.introEndsAt,
                    server_now: new Date(introEndsAt + elapsedMs).toISOString(),
                    state_revision: state.stateRevision
                });
            }""",
            elapsed_ms,
        )

    def test_custom_titles_for_consecutive_same_type_games_reach_participant_intro(self):
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        page = self.open_intro(
            context,
            self.build_query(
                elapsed_ms=2000,
                game_key="quiz",
                room_code="QUIZ1",
                game_number=1,
                game_title="NATUR & WISSENSCHAFT",
            ),
        )
        try:
            title = page.locator("[data-qa-vhs-intro-title]")
            self.assertEqual(title.get_attribute("aria-label"), "NATUR & WISSENSCHAFT")
            self.assertNotIn("QUICK QUIZ", title.inner_text())

            page.evaluate(
                """() => initializeQaVhsGameIntro({
                    root: document.getElementById('sessionGameStartIntro'),
                    gameId: 'quiz:QUIZ2:2',
                    gameKey: 'quiz',
                    roomCode: 'QUIZ2',
                    gameNumber: 2,
                    gameTitle: 'FILM & FERNSEHEN',
                    stateRevision: 2,
                    elapsedMs: 2000
                })"""
            )

            self.assertEqual(title.get_attribute("aria-label"), "FILM & FERNSEHEN")
            self.assertEqual(
                page.locator(".qa-vhs-intro__layer").evaluate_all(
                    "items => Array.from(new Set(items.map(item => item.textContent)))"
                ),
                ["FILM & FERNSEHEN"],
            )
        finally:
            page.close()
            context.close()

    def test_reference_layers_timing_blackout_and_all_viewports(self):
        context = self._browser.new_context(viewport={"width": 1366, "height": 768})
        page = self.open_intro(context, self.build_query())
        try:
            root = page.locator("#sessionGameStartIntro")
            number = page.locator("#sessionGameStartIntroNumber")
            title = page.locator("[data-qa-vhs-intro-title]")

            self.assertEqual(root.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(0, 0, 0)")
            self.assertEqual(root.evaluate("el => getComputedStyle(el).backgroundImage"), "none")
            self.assertEqual(root.evaluate("el => getComputedStyle(el).opacity"), "1")
            self.assertEqual(root.evaluate("el => getComputedStyle(el).zIndex"), "10000")
            self.assertEqual(root.evaluate("el => el.parentElement === document.body"), True)
            self.assertEqual(root.evaluate("el => getComputedStyle(el, '::before').content"), "none")
            self.assertEqual(root.evaluate("el => getComputedStyle(el, '::after').content"), "none")
            self.assertEqual(page.locator(".vhs-game-intro-header").count(), 0)
            self.assertEqual(page.locator(".vhs-game-intro-footer").count(), 0)
            self.assertEqual(page.locator(".session-game-start-intro__card").count(), 0)
            self.assertEqual(page.locator(".session-game-start-intro__text").count(), 0)
            self.assertEqual(page.locator(".vhs-game-intro-screen").count(), 0)
            self.assertEqual(page.locator("[class*='vhs-interference']").count(), 0)
            self.assertEqual(page.locator(".vhs-theme-shell").evaluate("el => getComputedStyle(el).visibility"), "hidden")
            self.assertEqual(page.locator(".qa-score-widget").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(page.locator("[data-qa-vhs-intro-layer]").count(), 5)
            self.assertEqual(page.locator(".qa-vhs-intro__scanline").count(), 11)
            self.assertEqual(
                page.locator(".qa-vhs-intro__layer--red").evaluate(
                    "el => getComputedStyle(el).color"
                ),
                "rgb(255, 0, 0)",
            )
            self.assertEqual(
                page.locator(".qa-vhs-intro__layer--green").evaluate(
                    "el => getComputedStyle(el).color"
                ),
                "rgb(0, 128, 0)",
            )
            self.assertEqual(
                page.locator(".qa-vhs-intro__layer--blue").evaluate(
                    "el => getComputedStyle(el).color"
                ),
                "rgb(0, 0, 255)",
            )
            self.assertIn(
                "blur(15px)",
                page.locator(".qa-vhs-intro__layer--blur").evaluate(
                    "el => getComputedStyle(el).filter"
                ),
            )
            self.assertEqual(
                page.locator(".qa-vhs-intro__scanline").first.evaluate(
                    "el => getComputedStyle(el).animationName"
                ),
                "none",
            )
            self.assertEqual(number.inner_text(), "SPIEL 3")
            self.assertEqual(title.get_attribute("aria-label"), "ESTIMATION")

            number_delay = float(
                page.locator(".qa-vhs-intro__number").evaluate(
                    "el => parseFloat(getComputedStyle(el).animationDelay) * 1000"
                )
            )
            title_delay = float(
                page.locator(".qa-vhs-intro__title").evaluate(
                    "el => parseFloat(getComputedStyle(el).animationDelay.split(',')[0]) * 1000"
                )
            )
            rendered_elapsed = float(
                root.evaluate(
                    "el => parseFloat(getComputedStyle(el).getPropertyValue('--qa-vhs-intro-elapsed'))"
                )
            )
            self.assertGreaterEqual(number_delay + rendered_elapsed, 120)
            self.assertLessEqual(number_delay + rendered_elapsed, 200)
            self.assertGreaterEqual(title_delay + rendered_elapsed, 1600)
            self.assertLessEqual(title_delay + rendered_elapsed, 1680)

            for width, height in (
                (360, 800),
                (390, 844),
                (768, 1024),
                (1366, 768),
                (1920, 1080),
            ):
                with self.subTest(viewport=(width, height)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                    root_box = root.bounding_box()
                    number_box = number.bounding_box()
                    title_box = title.bounding_box()
                    self.assertGreaterEqual(number_box["x"], root_box["x"])
                    self.assertLessEqual(number_box["x"] + number_box["width"], root_box["width"] + 1)
                    self.assertGreaterEqual(title_box["x"], root_box["x"])
                    self.assertLessEqual(title_box["x"] + title_box["width"], root_box["width"] + 1)
        finally:
            page.close()
            context.close()

    def test_server_timeline_reload_reconnect_and_followup_screen(self):
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        page = self.open_intro(
            context,
            self.build_query(elapsed_ms=500, game_title="CLUE RUSH"),
        )
        try:
            number = page.locator("#sessionGameStartIntroNumber")
            stack = page.locator(".qa-vhs-intro__title")
            self.assertGreater(float(number.evaluate("el => getComputedStyle(el).opacity")), 0.9)
            self.assertLess(float(stack.evaluate("el => getComputedStyle(el).opacity")), 0.1)
            elapsed_before = float(
                page.locator("#sessionGameStartIntro").evaluate(
                    "el => parseFloat(getComputedStyle(el).getPropertyValue('--qa-vhs-intro-elapsed'))"
                )
            )

            page.reload()
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            elapsed_after = float(
                page.locator("#sessionGameStartIntro").evaluate(
                    "el => parseFloat(getComputedStyle(el).getPropertyValue('--qa-vhs-intro-elapsed'))"
                )
            )
            self.assertGreaterEqual(elapsed_after, elapsed_before)
            self.assertGreater(float(number.evaluate("el => getComputedStyle(el).opacity")), 0.5)
            self.assertEqual(page.locator(".qa-vhs-intro__scanline").count(), 11)

            page.goto(f"http://intro.test/intro?{self.build_query(elapsed_ms=2000, game_title='CLUE RUSH')}")
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            self.assertGreater(float(number.evaluate("el => getComputedStyle(el).opacity")), 0.9)
            self.assertGreater(float(stack.evaluate("el => getComputedStyle(el).opacity")), 0.9)
            self.assertGreater(
                float(
                    page.locator(".qa-vhs-intro__scanlines").evaluate(
                        "el => getComputedStyle(el).opacity"
                    )
                ),
                0.9,
            )

            page.goto(f"http://intro.test/intro?{self.build_query(elapsed_ms=7850, game_title='CLUE RUSH')}")
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            page.wait_for_timeout(400)
            self.assertIsNone(page.locator("#sessionGameStartIntro").get_attribute("hidden"))
            self.assertTrue(
                page.evaluate("document.documentElement.classList.contains('qa-vhs-transition--active')")
            )
            self.assertEqual(
                page.locator("#followupAction").evaluate("el => getComputedStyle(document.body).pointerEvents"),
                "none",
            )
            page.wait_for_timeout(2600)
            self.assertEqual(page.locator("#sessionGameStartIntro").get_attribute("hidden"), "")
            self.assertEqual(page.locator(".qa-vhs-intro__scanline:visible").count(), 0)
            self.assertEqual(page.locator(".followup-label").inner_text(), "FOLGESCREEN")
            self.assertEqual(
                page.locator("#followupAction").evaluate("el => getComputedStyle(document.body).pointerEvents"),
                "auto",
            )
        finally:
            page.close()
            context.close()

    def test_reference_frames_have_only_the_namespaced_five_layer_intro(self):
        context = self._browser.new_context(viewport={"width": 1366, "height": 768})
        frames = (
            (0, False),
            (300, False),
            (1500, False),
            (1700, True),
            (3000, True),
            (7200, True),
        )
        pages = []
        try:
            for elapsed_ms, title_expected in frames:
                with self.subTest(elapsed_ms=elapsed_ms):
                    page = self.open_intro(
                        context,
                        self.build_query(
                            elapsed_ms=0,
                            duration_ms=20000,
                            game_title="SORTING LADDER",
                        ),
                    )
                    pages.append(page)
                    page.evaluate(
                        """elapsedMs => {
                            initializeQaVhsGameIntro({
                                root: document.getElementById('sessionGameStartIntro'),
                                gameId: 'visual-reference',
                                gameNumber: 3,
                                gameTitle: 'SORTING LADDER',
                                stateRevision: 1,
                                elapsedMs
                            });
                            document.getAnimations().forEach(animation => animation.pause());
                        }""",
                        elapsed_ms,
                    )
                    root = page.locator("#sessionGameStartIntro")
                    title = page.locator(".qa-vhs-intro__title")
                    layers = page.locator(".qa-vhs-intro__layer")
                    interference = page.locator(".qa-vhs-intro__interference")
                    scanlines = page.locator(".qa-vhs-intro__scanlines")
                    scanline_items = page.locator(".qa-vhs-intro__scanline")

                    self.assertEqual(
                        root.evaluate("el => getComputedStyle(el).backgroundColor"),
                        "rgb(0, 0, 0)",
                    )
                    self.assertEqual(root.evaluate("el => getComputedStyle(el).backgroundImage"), "none")
                    self.assertEqual(layers.count(), 5)
                    self.assertEqual(scanline_items.count(), 11)
                    self.assertEqual(page.locator(".session-game-start-intro__card").count(), 0)
                    self.assertEqual(page.locator("[class*='vhs-interference']").count(), 0)
                    self.assertEqual(interference.evaluate("el => getComputedStyle(el).overflow"), "hidden")
                    self.assertEqual(scanlines.evaluate("el => getComputedStyle(el).display"), "grid")
                    self.assertEqual(scanlines.evaluate("el => getComputedStyle(el).visibility"), "visible")
                    self.assertEqual(scanlines.evaluate("el => getComputedStyle(el).position"), "absolute")
                    self.assertEqual(scanlines.evaluate("el => getComputedStyle(el).zIndex"), "6")
                    self.assertTrue(
                        scanline_items.evaluate_all(
                            "items => items.every(item => getComputedStyle(item).animationName === 'none')"
                        )
                    )
                    self.assertTrue(
                        page.evaluate(
                            """() => {
                              const boundary = document.querySelector('.qa-vhs-intro__interference')
                                .getBoundingClientRect();
                              return Array.from(document.querySelectorAll('.qa-vhs-intro__scanline'))
                                .every(line => {
                                  const rect = line.getBoundingClientRect();
                                  return rect.left >= boundary.left - 0.5
                                    && rect.right <= boundary.right + 0.5
                                    && rect.top >= boundary.top - 0.5
                                    && rect.bottom <= boundary.bottom + 0.5;
                                });
                            }"""
                        )
                    )
                    if title_expected:
                        self.assertGreater(
                            float(title.evaluate("el => getComputedStyle(el).opacity")),
                            0.7,
                        )
                        self.assertGreater(
                            float(scanlines.evaluate("el => getComputedStyle(el).opacity")),
                            0.7,
                        )
                        self.assertGreater(
                            float(
                                page.locator(".qa-vhs-intro__number").evaluate(
                                    "el => getComputedStyle(el).opacity"
                                )
                            ),
                            0.9,
                        )
                    else:
                        self.assertLess(
                            float(title.evaluate("el => getComputedStyle(el).opacity")),
                            0.1,
                        )
                        self.assertLess(
                            float(scanlines.evaluate("el => getComputedStyle(el).opacity")),
                            0.1,
                        )
                    self.assertGreater(len(page.screenshot()), 1000)
        finally:
            for page in pages:
                page.close()
            context.close()

    def test_transition_reuses_shell_texture_bars_dot_and_gates_interaction(self):
        context = self._browser.new_context(viewport={"width": 1366, "height": 768})
        page = self.open_intro(
            context,
            self.build_query(elapsed_ms=2000, game_title="ASSIGN"),
        )
        try:
            self.assertTrue(self.start_transition(page, 300))
            root = page.locator("#sessionGameStartIntro")
            shell = page.locator(".vhs-theme-shell")
            dot = page.locator(".vhs-theme-rec-dot")
            self.assertTrue(
                page.evaluate("document.documentElement.classList.contains('qa-vhs-transition--active')")
            )
            self.assertGreaterEqual(
                float(
                    page.evaluate(
                        "parseFloat(getComputedStyle(document.documentElement)"
                        ".getPropertyValue('--qa-vhs-transition-elapsed'))"
                    )
                ),
                300,
            )
            self.assertIn(
                "qa-vhs-transition-background",
                root.evaluate("el => getComputedStyle(el).animationName"),
            )
            self.assertIn(
                "noise.png",
                page.locator("body").evaluate("el => getComputedStyle(el).backgroundImage"),
            )
            self.assertEqual(page.locator(".vhs-theme-shell").count(), 1)
            self.assertEqual(dot.count(), 1)
            self.assertEqual(page.locator("[class*='qa-vhs-transition__bar']").count(), 0)
            self.assertEqual(shell.get_attribute("inert"), "")
            self.assertEqual(
                shell.evaluate(
                    "el => getComputedStyle(el, '::before').animationName.split(',').length"
                ),
                5,
            )
            self.assertIn(
                "qa-vhs-transition-status-dot",
                dot.evaluate("el => getComputedStyle(el).animationName"),
            )
            self.assertEqual(
                root.locator(".qa-vhs-intro__content").evaluate(
                    "el => getComputedStyle(el).animationName"
                ),
                "none",
            )

            page.evaluate(
                """() => {
                    window.followupClicks = 0;
                    document.getElementById('followupAction').addEventListener(
                        'click',
                        () => { window.followupClicks += 1; }
                    );
                }"""
            )
            button_box = page.locator("#followupAction").bounding_box()
            page.mouse.click(
                button_box["x"] + button_box["width"] / 2,
                button_box["y"] + button_box["height"] / 2,
            )
            self.assertEqual(page.evaluate("window.followupClicks"), 0)

            page.wait_for_timeout(2750)
            self.assertEqual(root.get_attribute("hidden"), "")
            self.assertFalse(
                page.evaluate("document.documentElement.classList.contains('qa-vhs-transition--active')")
            )
            self.assertIsNone(shell.get_attribute("inert"))
            page.evaluate(
                "document.documentElement.classList.add('is-session-game-intro-active')"
            )
            self.assertEqual(root.evaluate("el => getComputedStyle(el).display"), "none")
            page.evaluate(
                "document.documentElement.classList.remove('is-session-game-intro-active')"
            )
            page.locator("#followupAction").click()
            self.assertEqual(page.evaluate("window.followupClicks"), 1)
            self.assertEqual(shell.evaluate("el => getComputedStyle(el).transform"), "none")
        finally:
            page.close()
            context.close()

    def test_transition_phases_are_ordered_and_preserve_existing_chrome(self):
        context = self._browser.new_context(viewport={"width": 1366, "height": 768})
        self.build_page(context)
        selectors = (
            ".vhs-theme-shell",
            ".vhs-theme-header",
            ".vhs-theme-app",
            ".vhs-theme-rec",
            ".vhs-theme-rec-dot",
            ".play-container",
            ".vhs-theme-footer",
        )
        baseline = context.new_page()
        baseline.goto("http://intro.test/intro")
        baseline.wait_for_load_state("load")
        final_rects = baseline.evaluate(
            """selectors => Object.fromEntries(selectors.map(selector => {
                const rect = document.querySelector(selector).getBoundingClientRect();
                return [selector, {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height
                }];
            }))""",
            selectors,
        )
        baseline.close()

        page = context.new_page()
        page.goto(
            f"http://intro.test/intro?{self.build_query(elapsed_ms=2000, game_title='ASSIGN')}"
        )
        page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
        try:
            self.assertTrue(self.start_transition(page, 0))
            schedule = page.evaluate(
                """() => {
                    const milliseconds = value => Number.parseFloat(value) * 1000;
                    const animation = (selector, pseudo = null) => {
                        const style = getComputedStyle(document.querySelector(selector), pseudo);
                        return {
                            delay: milliseconds(style.animationDelay.split(',')[0]),
                            duration: milliseconds(style.animationDuration.split(',')[0]),
                            name: style.animationName.split(',')[0]
                        };
                    };
                    return {
                        background: animation('#sessionGameStartIntro'),
                        introCopy: animation('.qa-vhs-intro__content'),
                        bars: animation('.vhs-theme-shell', '::before'),
                        header: animation('.vhs-theme-header'),
                        main: animation('.play-container'),
                        footer: animation('.vhs-theme-footer'),
                        dot: animation('.vhs-theme-rec-dot')
                    };
                }"""
            )
            self.assertEqual(schedule["background"]["name"], "qa-vhs-transition-background")
            self.assertEqual(schedule["bars"]["name"], "qa-vhs-transition-bar-1")
            self.assertAlmostEqual(schedule["background"]["duration"], 1100, delta=5)
            self.assertAlmostEqual(schedule["bars"]["delay"], 1100, delta=5)
            self.assertAlmostEqual(schedule["bars"]["duration"], 620, delta=5)
            self.assertAlmostEqual(schedule["header"]["delay"], 2040, delta=5)
            self.assertAlmostEqual(schedule["main"]["delay"], 2200, delta=5)
            self.assertAlmostEqual(schedule["footer"]["delay"], 2400, delta=5)
            self.assertAlmostEqual(schedule["dot"]["delay"], 2040, delta=5)
            self.assertLessEqual(
                schedule["background"]["delay"] + schedule["background"]["duration"],
                schedule["bars"]["delay"],
            )
            self.assertLessEqual(
                schedule["bars"]["delay"] + schedule["bars"]["duration"] + (4 * 70),
                schedule["header"]["delay"],
            )
            self.assertLess(schedule["header"]["delay"], schedule["main"]["delay"])
            self.assertLess(schedule["main"]["delay"], schedule["footer"]["delay"])

            self.assertEqual(page.locator(".vhs-theme-shell").count(), 1)
            self.assertEqual(page.locator(".vhs-theme-app").count(), 1)
            self.assertEqual(page.locator(".vhs-theme-rec").count(), 1)
            self.assertEqual(page.locator(".vhs-theme-rec-dot").count(), 1)
            self.assertEqual(page.locator(".vhs-theme-participant").count(), 1)
            self.assertEqual(page.locator("[class*='qa-vhs-transition__bar']").count(), 0)

            self.assertTrue(self.start_transition(page, 900))
            page.wait_for_timeout(20)
            self.assertLess(
                float(page.locator(".vhs-theme-header").evaluate("el => getComputedStyle(el).opacity")),
                0.05,
            )
            self.assertLess(
                float(page.locator(".play-container").evaluate("el => getComputedStyle(el).opacity")),
                0.05,
            )
            self.assertEqual(
                page.locator(".vhs-theme-shell").evaluate(
                    "el => getComputedStyle(el, '::before').clipPath"
                ),
                "none",
            )

            self.assertTrue(self.start_transition(page, 2150))
            page.wait_for_timeout(20)
            self.assertGreater(
                float(page.locator(".vhs-theme-header").evaluate("el => getComputedStyle(el).opacity")),
                0.05,
            )
            self.assertGreater(
                float(page.locator(".vhs-theme-rec-dot").evaluate("el => getComputedStyle(el).opacity")),
                0.05,
            )
            self.assertLess(
                float(page.locator(".vhs-theme-footer").evaluate("el => getComputedStyle(el).opacity")),
                0.05,
            )

            self.assertTrue(self.start_transition(page, 2600))
            page.wait_for_timeout(20)
            self.assertGreater(
                float(page.locator(".play-container").evaluate("el => getComputedStyle(el).opacity")),
                0.9,
            )
            self.assertGreater(
                float(page.locator(".vhs-theme-footer").evaluate("el => getComputedStyle(el).opacity")),
                0.5,
            )
            self.assertEqual(page.locator(".vhs-theme-shell").get_attribute("inert"), "")

            page.wait_for_timeout(250)
            self.assertEqual(page.locator("#sessionGameStartIntro").get_attribute("hidden"), "")
            self.assertIsNone(page.locator(".vhs-theme-shell").get_attribute("inert"))
            self.assertEqual(
                page.locator("#sessionGameStartIntro").evaluate("el => getComputedStyle(el).display"),
                "none",
            )
            for selector, expected in final_rects.items():
                actual = page.locator(selector).evaluate(
                    """el => {
                        const rect = el.getBoundingClientRect();
                        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
                    }"""
                )
                for key in ("x", "y", "width", "height"):
                    self.assertAlmostEqual(actual[key], expected[key], delta=1)
        finally:
            page.close()
            context.close()

    def test_transition_background_is_a_stationary_fullscreen_crossfade(self):
        context = self._browser.new_context(viewport={"width": 1366, "height": 768})
        page = self.open_intro(
            context,
            self.build_query(elapsed_ms=2000, game_title="ASSIGN"),
        )
        try:
            samples = []
            for elapsed_ms in (0, 275, 550, 825, 1100):
                self.assertTrue(self.start_transition(page, elapsed_ms))
                page.wait_for_timeout(20)
                samples.append(
                    page.evaluate(
                        """() => {
                            const root = document.getElementById('sessionGameStartIntro');
                            const rootStyle = getComputedStyle(root);
                            const shell = document.querySelector('.vhs-theme-shell');
                            const barsStyle = getComputedStyle(shell, '::before');
                            const backgroundAnimation = root.getAnimations().find(
                                animation => animation.animationName === 'qa-vhs-transition-background'
                            );
                            return {
                                opacity: Number.parseFloat(rootStyle.opacity),
                                backgroundColor: rootStyle.backgroundColor,
                                backgroundImage: rootStyle.backgroundImage,
                                clipPath: rootStyle.clipPath,
                                maskImage: rootStyle.maskImage,
                                transform: rootStyle.transform,
                                barsClipPath: barsStyle.clipPath,
                                barsTransform: barsStyle.transform,
                                frames: backgroundAnimation.effect.getKeyframes().map(frame => ({
                                    opacity: frame.opacity ?? null,
                                    clipPath: frame.clipPath ?? null,
                                    maskImage: frame.maskImage ?? null,
                                    transform: frame.transform ?? null,
                                })),
                            };
                        }"""
                    )
                )

            opacities = [sample["opacity"] for sample in samples]
            self.assertGreater(opacities[0], opacities[1])
            self.assertGreater(opacities[1], opacities[2])
            self.assertGreater(opacities[2], opacities[3])
            self.assertGreater(opacities[3], opacities[4])
            self.assertLess(opacities[4], 0.05)
            for sample in samples:
                self.assertEqual(sample["backgroundColor"], "rgb(0, 0, 0)")
                self.assertEqual(sample["backgroundImage"], "none")
                self.assertEqual(sample["clipPath"], "none")
                self.assertEqual(sample["maskImage"], "none")
                self.assertEqual(sample["transform"], "none")
                self.assertEqual(sample["barsClipPath"], "none")
                self.assertTrue(all(frame["clipPath"] is None for frame in sample["frames"]))
                self.assertTrue(all(frame["maskImage"] is None for frame in sample["frames"]))
                self.assertTrue(all(frame["transform"] is None for frame in sample["frames"]))

            self.assertEqual(samples[3]["barsTransform"], "none")
            self.assertEqual(samples[4]["barsTransform"], "none")
            self.assertGreater(len(page.screenshot()), 1000)
        finally:
            page.close()
            context.close()

    def test_transition_bars_are_staggered_from_lower_right_without_changing_geometry(self):
        context = self._browser.new_context(viewport={"width": 1366, "height": 768})
        page = self.open_intro(
            context,
            self.build_query(elapsed_ms=2000, game_title="ASSIGN"),
        )
        try:
            shell = page.locator(".vhs-theme-shell")
            baseline_rect = shell.bounding_box()
            baseline_background = shell.evaluate("el => getComputedStyle(el).backgroundImage")

            self.assertTrue(self.start_transition(page, 1000))
            page.wait_for_timeout(20)
            start_state = shell.evaluate(
                """el => {
                    const style = getComputedStyle(el, '::before');
                    const px = value => Number.parseFloat(value);
                    const positions = style.backgroundPosition.split(',').map(pair => {
                        const [x, y] = pair.trim().split(/\\s+/);
                        return {x: px(x), y: px(y)};
                    });
                    return {
                        names: style.animationName.split(',').map(value => value.trim()),
                        delays: style.animationDelay.split(',').map(value => px(value) * 1000),
                        durations: style.animationDuration.split(',').map(value => px(value) * 1000),
                        easings: style.animationTimingFunction.match(/cubic-bezier\\([^)]+\\)/g) || [],
                        positions,
                        sizes: style.backgroundSize.split(',').map(value => value.trim()),
                        transform: style.transform,
                        opacity: style.opacity,
                        angleCount: (style.backgroundImage.match(/linear-gradient\\(153deg/g) || []).length,
                        overflowX: document.documentElement.scrollWidth - window.innerWidth,
                    };
                }"""
            )
            self.assertEqual(
                start_state["names"],
                [f"qa-vhs-transition-bar-{index}" for index in range(1, 6)],
            )
            self.assertEqual(len(start_state["positions"]), 5)
            self.assertTrue(all(value >= baseline_rect["width"] for value in (
                position["x"] for position in start_state["positions"]
            )))
            self.assertTrue(all(value >= baseline_rect["height"] for value in (
                position["y"] for position in start_state["positions"]
            )))
            self.assertEqual(
                [round(start_state["delays"][index + 1] - start_state["delays"][index])
                 for index in range(4)],
                [70, 70, 70, 70],
            )
            self.assertTrue(all(abs(duration - 620) <= 5 for duration in start_state["durations"]))
            self.assertTrue(all(easing == "cubic-bezier(0.18, 0.72, 0.22, 1)"
                                for easing in start_state["easings"]))
            self.assertTrue(all(size == "100% 100%" for size in start_state["sizes"]))
            self.assertEqual(start_state["transform"], "none")
            self.assertEqual(start_state["opacity"], "1")
            self.assertEqual(start_state["angleCount"], 5)
            self.assertLessEqual(start_state["overflowX"], 0)
            self.assertGreater(len(page.screenshot()), 1000)

            self.assertTrue(self.start_transition(page, 1150))
            page.wait_for_timeout(20)
            self.assertGreater(len(page.screenshot()), 1000)

            self.assertTrue(self.start_transition(page, 1500))
            page.wait_for_timeout(20)
            moving_positions = shell.evaluate(
                """el => getComputedStyle(el, '::before').backgroundPosition
                    .split(',').map(pair => Number.parseFloat(pair.trim().split(/\\s+/)[0]))"""
            )
            self.assertEqual(moving_positions, sorted(moving_positions))
            self.assertGreater(len(page.screenshot()), 1000)

            self.assertTrue(self.start_transition(page, 1990))
            page.wait_for_timeout(20)
            self.assertGreater(len(page.screenshot()), 1000)

            self.assertTrue(self.start_transition(page, 2050))
            page.wait_for_timeout(20)
            end_state = shell.evaluate(
                """el => {
                    const style = getComputedStyle(el, '::before');
                    return {
                        positions: style.backgroundPosition,
                        transform: style.transform,
                        rect: (() => {
                            const rect = el.getBoundingClientRect();
                            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
                        })(),
                    };
                }"""
            )
            self.assertEqual(
                end_state["positions"],
                ", ".join(["0px 0px"] * 5),
            )
            self.assertEqual(end_state["transform"], "none")
            for key in ("x", "y", "width", "height"):
                self.assertAlmostEqual(end_state["rect"][key], baseline_rect[key], delta=1)
            self.assertGreater(len(page.screenshot()), 1000)

            self.assertTrue(self.start_transition(page, 2150))
            page.wait_for_timeout(20)
            self.assertGreater(len(page.screenshot()), 1000)
            page.wait_for_timeout(700)
            self.assertEqual(shell.evaluate("el => getComputedStyle(el).backgroundImage"), baseline_background)
        finally:
            page.close()
            context.close()

    def test_transition_bar_reconnect_resumes_each_staggered_layer(self):
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        page = self.open_intro(
            context,
            self.build_query(elapsed_ms=9500, game_title="CLUE RUSH"),
        )
        try:
            shell = page.locator(".vhs-theme-shell")
            read_positions = """el => getComputedStyle(el, '::before').backgroundPosition
                .split(',').map(pair => Number.parseFloat(pair.trim().split(/\\s+/)[0]))"""
            before = shell.evaluate(read_positions)
            self.assertEqual(before, sorted(before))
            self.assertGreater(before[-1], 0)

            page.reload()
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            after = shell.evaluate(read_positions)
            self.assertEqual(len(after), 5)
            self.assertTrue(all(current <= previous for current, previous in zip(after, before)))
            self.assertGreater(after[-1], 0)

            page.goto(
                f"http://intro.test/intro?{self.build_query(elapsed_ms=10100, game_title='CLUE RUSH')}"
            )
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            completed = shell.evaluate(read_positions)
            self.assertEqual(completed, [0, 0, 0, 0, 0])
        finally:
            page.close()
            context.close()

    def test_transition_reconnect_resumes_and_expired_transition_does_not_replay(self):
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        query = self.build_query(elapsed_ms=8550, game_title="CLUE RUSH")
        page = self.open_intro(context, query)
        try:
            before = float(
                page.evaluate(
                    "parseFloat(getComputedStyle(document.documentElement)"
                    ".getPropertyValue('--qa-vhs-transition-elapsed'))"
                )
            )
            before_opacity = float(
                page.locator("#sessionGameStartIntro").evaluate(
                    "el => getComputedStyle(el).opacity"
                )
            )
            page.reload()
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            after = float(
                page.evaluate(
                    "parseFloat(getComputedStyle(document.documentElement)"
                    ".getPropertyValue('--qa-vhs-transition-elapsed'))"
                )
            )
            after_opacity = float(
                page.locator("#sessionGameStartIntro").evaluate(
                    "el => getComputedStyle(el).opacity"
                )
            )
            self.assertGreaterEqual(after, before)
            self.assertGreater(after, 550)
            self.assertLessEqual(after_opacity, before_opacity + 0.05)
            self.assertLess(after_opacity, 0.9)
            self.assertIsNone(page.locator("#sessionGameStartIntro").get_attribute("hidden"))

            page.goto(
                f"http://intro.test/intro?{self.build_query(elapsed_ms=11000, game_title='CLUE RUSH')}"
            )
            page.wait_for_load_state("load")
            self.assertEqual(page.locator("#sessionGameStartIntro").get_attribute("hidden"), "")
            self.assertFalse(
                page.evaluate("document.documentElement.classList.contains('qa-vhs-transition--active')")
            )
        finally:
            page.close()
            context.close()

    def test_reduced_motion_uses_only_short_crossfade(self):
        context = self._browser.new_context(
            viewport={"width": 768, "height": 1024},
            reduced_motion="reduce",
        )
        page = self.open_intro(
            context,
            self.build_query(elapsed_ms=2000, game_title="WHO IS LYING"),
        )
        try:
            self.assertTrue(self.start_transition(page, 0))
            self.assertEqual(
                page.locator(".vhs-theme-shell").evaluate(
                    "el => getComputedStyle(el, '::before').animationName"
                ),
                "none",
            )
            self.assertNotIn(
                "qa-vhs-transition-status-dot",
                page.locator(".vhs-theme-rec-dot").evaluate(
                    "el => getComputedStyle(el).animationName"
                ),
            )
            page.wait_for_timeout(550)
            self.assertEqual(page.locator("#sessionGameStartIntro").get_attribute("hidden"), "")
            self.assertEqual(
                page.locator(".vhs-theme-shell").evaluate("el => getComputedStyle(el).transform"),
                "none",
            )
        finally:
            page.close()
            context.close()

    def test_transition_is_responsive_without_layout_shift_or_overflow(self):
        for width, height in (
            (320, 568),
            (360, 800),
            (390, 844),
            (768, 1024),
            (1024, 768),
            (1366, 768),
            (1920, 1080),
        ):
            with self.subTest(viewport=(width, height)):
                context = self._browser.new_context(viewport={"width": width, "height": height})
                page = self.open_intro(
                    context,
                    self.build_query(elapsed_ms=2000, game_title="HOST-PUNKTEVERGABE"),
                )
                try:
                    baseline_scroll_height = page.evaluate("document.documentElement.scrollHeight")
                    self.assertTrue(self.start_transition(page, 1000))
                    shell = page.locator(".vhs-theme-shell")
                    before = shell.bounding_box()
                    bar_offsets = shell.evaluate(
                        """el => getComputedStyle(el, '::before').backgroundPosition
                            .split(',').map(pair => {
                                const [x, y] = pair.trim().split(/\\s+/).map(Number.parseFloat);
                                return {x, y};
                            })"""
                    )
                    self.assertEqual(len(bar_offsets), 5)
                    self.assertTrue(all(offset["x"] >= before["width"] for offset in bar_offsets))
                    self.assertTrue(all(offset["y"] >= before["height"] for offset in bar_offsets))
                    self.assertTrue(
                        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    )
                    self.assertEqual(
                        page.evaluate("document.documentElement.scrollHeight"),
                        baseline_scroll_height,
                    )
                    self.assertGreaterEqual(before["x"], 0)
                    self.assertLessEqual(before["x"] + before["width"], width + 1)
                    self.assertGreater(len(page.screenshot()), 1000)
                    page.wait_for_timeout(1900)
                    after = shell.bounding_box()
                    self.assertLess(abs(before["width"] - after["width"]), 1)
                    self.assertLess(abs(before["height"] - after["height"]), 1)
                finally:
                    page.close()
                    context.close()

    def test_required_game_titles_share_the_same_intro_dom(self):
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        pages = []
        try:
            for index, (game_key, game_title) in enumerate(GAME_INTRO_TITLES.items(), start=1):
                with self.subTest(game_key=game_key, game_title=game_title):
                    page = self.open_intro(
                        context,
                        self.build_query(
                            elapsed_ms=2000,
                            game_key=game_key,
                            room_code=f"ROOM{index}",
                            game_number=index,
                            game_title=game_title,
                        ),
                    )
                    pages.append(page)
                    self.assertEqual(
                        page.locator("[data-qa-vhs-intro-title]").get_attribute("aria-label"),
                        game_title,
                    )
                    self.assertEqual(page.locator(".qa-vhs-intro__layer").count(), 5)
                    self.assertEqual(page.locator(".qa-vhs-intro__scanlines").count(), 1)
                    self.assertEqual(page.locator(".qa-vhs-intro__scanline").count(), 11)
                    self.assertGreater(
                        float(
                            page.locator(".qa-vhs-intro__scanlines").evaluate(
                                "el => getComputedStyle(el).opacity"
                            )
                        ),
                        0.7,
                    )
                    self.assertTrue(
                        page.locator(".qa-vhs-intro__scanline").evaluate_all(
                            "items => items.every(item => getComputedStyle(item).animationName === 'none')"
                        )
                    )
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                    self.assertTrue(self.start_transition(page, 1000))
                    self.assertEqual(page.locator(".vhs-theme-shell").count(), 1)
                    self.assertEqual(page.locator(".vhs-theme-rec-dot").count(), 1)
                    self.assertTrue(
                        page.evaluate(
                            "document.documentElement.classList.contains("
                            "'qa-vhs-transition--active')"
                        )
                    )
        finally:
            for page in pages:
                page.close()
            context.close()

    def test_first_second_and_third_game_reuse_static_scanlines_without_duplicates(self):
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        page = self.open_intro(
            context,
            self.build_query(
                elapsed_ms=2000,
                game_key="clue_rush",
                room_code="CLUE1",
                game_number=1,
                game_title="CLUE RUSH",
            ),
        )
        try:
            self.assertEqual(page.locator(".qa-vhs-intro__scanline").count(), 11)

            for game_number, game_id, game_title in (
                (2, "assign:ASSIGN2:2", "ASSIGN"),
                (3, "host_points:HOST3:3", "HOST-PUNKTEVERGABE"),
            ):
                with self.subTest(game_number=game_number, game_title=game_title):
                    page.evaluate(
                        """state => initializeQaVhsGameIntro({
                            root: document.getElementById('sessionGameStartIntro'),
                            gameId: state.gameId,
                            gameNumber: state.gameNumber,
                            gameTitle: state.gameTitle,
                            stateRevision: state.gameNumber,
                            elapsedMs: 2000
                        })""",
                        {
                            "gameId": game_id,
                            "gameNumber": game_number,
                            "gameTitle": game_title,
                        },
                    )

                    self.assertEqual(page.locator(".qa-vhs-intro__layer").count(), 5)
                    self.assertEqual(page.locator(".qa-vhs-intro__scanlines").count(), 1)
                    self.assertEqual(page.locator(".qa-vhs-intro__scanline").count(), 11)
                    self.assertGreater(
                        float(
                            page.locator(".qa-vhs-intro__scanlines").evaluate(
                                "el => getComputedStyle(el).opacity"
                            )
                        ),
                        0.9,
                    )
                    self.assertEqual(
                        page.locator(".qa-vhs-intro__layer").evaluate_all(
                            "items => Array.from(new Set(items.map(item => item.textContent)))"
                        ),
                        [game_title],
                    )
        finally:
            page.close()
            context.close()

    def test_early_snapshot_before_theme_bootstrap_initializes_vhs_scanlines(self):
        context = self._browser.new_context(viewport={"width": 390, "height": 844})
        context.add_init_script(
            "localStorage.setItem('participant_interface_theme', 'vhs')"
        )
        self.build_page(context, theme="")
        page = context.new_page()
        try:
            page.goto(
                f"http://intro.test/intro?{self.build_query(elapsed_ms=2000, game_title='WHO IS LYING')}"
            )
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")

            self.assertEqual(
                page.locator("#sessionGameStartIntro").get_attribute(
                    "data-qa-vhs-intro-initialized"
                ),
                "true",
            )
            page.evaluate("document.documentElement.dataset.participantTheme = 'vhs'")
            self.assertEqual(page.locator(".qa-vhs-intro__layer").count(), 5)
            self.assertEqual(page.locator(".qa-vhs-intro__scanlines").count(), 1)
            self.assertEqual(page.locator(".qa-vhs-intro__scanline").count(), 11)
            self.assertGreater(
                float(
                    page.locator(".qa-vhs-intro__scanlines").evaluate(
                        "el => getComputedStyle(el).opacity"
                    )
                ),
                0.9,
            )
        finally:
            page.close()
            context.close()

    def test_stale_revision_cannot_reactivate_completed_intro(self):
        context = self._browser.new_context(viewport={"width": 768, "height": 1024})
        page = self.open_intro(context, self.build_query(elapsed_ms=2000, revision=4))
        try:
            accepted = page.evaluate(
                """() => window.sessionGameStartIntro.handleState({
                    intro: {
                        intro_active: false,
                        game_number: 3,
                        game_title: 'ESTIMATION',
                        game_key: 'estimation',
                        game_instance_id: 'estimation:ROOM1:1',
                        intro_started_at: new Date(Date.now() - 2000).toISOString(),
                        intro_ends_at: new Date(Date.now() + 6000).toISOString(),
                        server_now: new Date().toISOString(),
                        state_revision: 3
                    }
                })"""
            )
            self.assertFalse(accepted)
            self.assertIsNone(page.locator("#sessionGameStartIntro").get_attribute("hidden"))

            page.evaluate("window.sessionGameStartIntro.hide()")
            page.wait_for_timeout(220)
            accepted = page.evaluate(
                """() => window.sessionGameStartIntro.handleState({
                    intro: {
                        intro_active: true,
                        game_number: 3,
                        game_title: 'ESTIMATION',
                        game_key: 'estimation',
                        game_instance_id: 'estimation:ROOM1:1',
                        intro_started_at: new Date(Date.now() - 2000).toISOString(),
                        intro_ends_at: new Date(Date.now() + 6000).toISOString(),
                        server_now: new Date().toISOString(),
                        state_revision: 4
                    }
                })"""
            )
            self.assertFalse(accepted)
            self.assertEqual(page.locator("#sessionGameStartIntro").get_attribute("hidden"), "")
        finally:
            page.close()
            context.close()

    def test_two_participants_render_the_same_authoritative_progress(self):
        context = self._browser.new_context(viewport={"width": 768, "height": 1024})
        query = self.build_query(elapsed_ms=2100, game_title="CLUE RUSH")
        first = self.open_intro(context, query)
        second = self.open_intro(context, query)
        try:
            for page in (first, second):
                self.assertEqual(
                    page.locator("#sessionGameStartIntroNumber").inner_text(),
                    "SPIEL 3",
                )
                self.assertEqual(
                    page.locator("[data-qa-vhs-intro-title]").get_attribute("aria-label"),
                    "CLUE RUSH",
                )
                self.assertGreater(
                    float(
                        page.locator(".qa-vhs-intro__title").evaluate(
                            "el => getComputedStyle(el).opacity"
                        )
                    ),
                    0.9,
                )

            effective_elapsed = """el => {
                const initialElapsed = parseFloat(
                    getComputedStyle(el).getPropertyValue('--qa-vhs-intro-elapsed')
                );
                const wholeAnimation = el.querySelector('.qa-vhs-intro__title')
                    .getAnimations()
                    .find(animation => animation.animationName.includes('whole'));
                return initialElapsed + (wholeAnimation?.currentTime || 0);
            }"""
            first_elapsed = float(
                first.locator("#sessionGameStartIntro").evaluate(effective_elapsed)
            )
            second_elapsed = float(
                second.locator("#sessionGameStartIntro").evaluate(effective_elapsed)
            )
            self.assertLess(abs(first_elapsed - second_elapsed), 250)
        finally:
            first.close()
            second.close()
            context.close()

    def test_long_title_second_game_and_reduced_motion(self):
        context = self._browser.new_context(
            viewport={"width": 360, "height": 800},
            reduced_motion="reduce",
        )
        page = self.open_intro(
            context,
            self.build_query(
                elapsed_ms=2000,
                game_key="host_points",
                room_code="HOST1",
                game_number=12,
                game_title="HOST-PUNKTEVERGABE",
            ),
        )
        try:
            self.assertEqual(page.locator("#sessionGameStartIntroNumber").inner_text(), "SPIEL 12")
            self.assertEqual(
                page.locator("[data-qa-vhs-intro-title]").get_attribute("aria-label"),
                "HOST-PUNKTEVERGABE",
            )
            self.assertGreater(
                float(page.locator(".qa-vhs-intro__title").evaluate("el => getComputedStyle(el).opacity")),
                0.9,
            )
            self.assertEqual(
                page.locator(".qa-vhs-intro__layer--white").evaluate(
                    "el => getComputedStyle(el).animationName"
                ),
                "none",
            )
            for width, height in (
                (360, 800),
                (390, 844),
                (768, 1024),
                (1366, 768),
                (1920, 1080),
            ):
                with self.subTest(viewport=(width, height)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertTrue(
                        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    )
                    title_box = page.locator(".qa-vhs-intro__title").bounding_box()
                    self.assertGreaterEqual(title_box["x"], 0)
                    self.assertLessEqual(title_box["x"] + title_box["width"], width + 1)
        finally:
            page.close()
            context.close()


if __name__ == "__main__":
    unittest.main()
