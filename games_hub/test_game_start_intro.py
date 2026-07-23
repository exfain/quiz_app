from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import unittest

from django.template.loader import render_to_string

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


def read_template(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class SessionGameStartIntroTemplateTests(unittest.TestCase):
    def test_lobby_start_navigation_adds_intro_metadata(self):
        content = read_template("templates/hub/lobby.html")

        self.assertIn("appendGameStartIntroParams(playUrl, step);", content)
        self.assertIn("findStepMetadata(step)", content)
        self.assertIn("playUrl.searchParams.set('hub_session', code);", content)
        self.assertIn("game_start_intro", content)
        self.assertIn("game_start_nonce", content)
        self.assertIn("game_start_order", content)
        self.assertIn("game_start_title", content)

        rejoin_section = content.split("if (data.type === 'lobby_join_success')", 1)[1].split("if (data.type === 'vote_update')", 1)[0]
        self.assertNotIn("game_start_intro", rejoin_section)

    def test_shared_intro_include_is_one_time_and_state_interruptible(self):
        content = read_template("templates/includes/_session_game_start_intro.html")

        self.assertIn("id=\"sessionGameStartIntro\"", content)
        self.assertIn("sessionGameStartIntro", content)
        self.assertIn("window.history.replaceState", content)
        self.assertIn("let cancelled = false", content)
        self.assertIn("cancelled = true", content)
        self.assertIn("sessionStorage.setItem(storageKey, 'shown')", content)
        self.assertIn("const INTRO_PREVIOUS_DURATION_MS = 5000", content)
        self.assertIn("const INTRO_DURATION_EXTENSION_MS = 3000", content)
        self.assertIn(
            "const INTRO_DISPLAY_DURATION_MS = INTRO_PREVIOUS_DURATION_MS + INTRO_DURATION_EXTENSION_MS",
            content,
        )
        self.assertIn("window.setTimeout(hide, INTRO_DISPLAY_DURATION_MS)", content)
        self.assertEqual(content.count("window.setTimeout(hide, INTRO_DISPLAY_DURATION_MS)"), 1)
        self.assertNotIn("window.setTimeout(hide, 5000)", content)
        self.assertIn("prefers-reduced-motion", content)
        self.assertIn("handleState", content)
        self.assertIn("'question_started'", content)
        self.assertIn("'round_started'", content)
        self.assertIn("'set_started'", content)
        self.assertIn("'buzzer_opened'", content)
        self.assertEqual(content.count('data-vhs-intro-layer="'), 5)
        self.assertEqual(content.count('class="vhs-interference__scanlines"'), 1)
        self.assertIn("aria-hidden=\"true\"", content)
        self.assertIn("document.documentElement.classList.add('is-session-game-intro-active')", content)
        self.assertIn("clearVhsIntroState(overlay)", content)
        self.assertIn("mountVhsIntro(overlay)", content)
        self.assertIn("restoreIntroMount(overlay)", content)
        self.assertIn("element.textContent = gameName", content)
        self.assertIn("layerElements.length !== 5", content)
        self.assertIn("index < 10", content)
        self.assertIn("toLocaleUpperCase('de-DE')", content)
        self.assertIn("is-vhs-intro-playing", content)
        self.assertNotIn("innerHTML", content)
        self.assertNotIn("setInterval", content)
        self.assertNotIn("requestAnimationFrame", content)

        vhs_css = read_template("static/themes/vhs/vhs.css")
        self.assertIn('html[data-participant-theme="vhs"] .session-game-start-intro', vhs_css)
        self.assertIn("vhs-intro-number-in 420ms", vhs_css)
        self.assertIn("vhs-interference-reveal 760ms", vhs_css)
        self.assertIn("vhs-interference-blur-pulse 30ms", vhs_css)
        self.assertIn("vhs-interference-micro-jerk 50ms", vhs_css)
        self.assertIn("vhs-interference-green-jump 1s", vhs_css)
        self.assertIn("vhs-interference-blue-jump 1s", vhs_css)
        self.assertIn("vhs-interference-whole 5s", vhs_css)
        self.assertIn("translate(-100px, 0) scale(1, 1.2) skew(50deg, 0deg)", vhs_css)
        self.assertIn("translate(100px, 0) scale(1, 1.2) skew(-80deg, 0deg)", vhs_css)
        self.assertIn("vhs-interference-whole-mobile", vhs_css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", vhs_css)
        self.assertIn("font-style: normal !important", vhs_css)
        self.assertIn(".vhs-interference__layer--main", vhs_css)
        self.assertIn("filter: none", vhs_css)
        for legacy_rule in (
            ".session-game-start-intro__title-effect",
            ".vhs-game-intro-name",
            ".vhs-game-intro-streaks",
            "@keyframes vhs-game-number-in",
            "@keyframes vhs-game-name-reveal",
            "@keyframes vhs-game-channel-in",
            "@keyframes vhs-game-glow-in",
            "@keyframes vhs-game-channel-jitter",
            "@keyframes vhs-game-streak-in",
            "@keyframes vhs-game-streak-pulse",
            "@keyframes vhs-game-intro-fade-in",
        ):
            self.assertNotIn(legacy_rule, vhs_css)
        for active_keyframe in (
            "vhs-intro-number-in",
            "vhs-interference-reveal",
            "vhs-interference-blur-pulse",
            "vhs-interference-micro-jerk",
            "vhs-interference-blue-jump",
            "vhs-interference-green-jump",
            "vhs-interference-whole",
            "vhs-interference-scanline-jitter",
        ):
            self.assertEqual(vhs_css.count(f"@keyframes {active_keyframe} {{"), 1)

    def test_player_templates_include_intro_and_forward_live_state(self):
        for relative_path in PLAYER_TEMPLATES:
            with self.subTest(template=relative_path):
                content = read_template(relative_path)
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

    def build_page(self, context, *, theme="vhs"):
        intro = render_to_string(
            "includes/_session_game_start_intro.html",
            {"participant": SimpleNamespace(name="Mia")},
        )
        vhs_css = read_template("static/themes/vhs/vhs.css")
        noise = (REPO_ROOT / "static/themes/vhs/noise.png").read_bytes()
        body = f"""
          <!doctype html>
          <html data-participant-theme="{theme}">
            <head><style>{vhs_css}</style></head>
            <body>
              <div class="vhs-theme-shell"><div class="play-container"></div></div>
              <aside class="qa-score-widget">Punkte</aside>
              <div id="participant-options-menu-root">Optionen</div>
              {intro}
            </body>
          </html>
        """
        context.route(
            "http://intro.test/static/themes/vhs/noise.png",
            lambda route: route.fulfill(status=200, content_type="image/png", body=noise),
        )
        context.route(
            "http://intro.test/intro*",
            lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body),
        )
        return urlencode(
            {
                "game_start_intro": "1",
                "game_start_order": "3",
                "game_start_title": "Geografie Schätzungen",
                "game_start_room": "ROOM1",
                "game_start_nonce": "intro-test",
            }
        )

    def test_vhs_intro_layers_timeline_cleanup_and_mobile_layout(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfügbar: {self._playwright_error}")

        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script(
            """
            (() => {
                const nativeSetTimeout = window.setTimeout.bind(window);
                window.__introScheduledDelays = [];
                window.setTimeout = (callback, delay, ...args) => {
                    window.__introScheduledDelays.push(Number(delay));
                    return nativeSetTimeout(callback, delay, ...args);
                };
            })();
            """
        )
        query = self.build_page(context)
        page = context.new_page()
        browser_errors = []
        external_requests = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        page.on(
            "request",
            lambda request: external_requests.append(request.url)
            if not request.url.startswith("http://intro.test/")
            else None,
        )
        try:
            page.goto(f"http://intro.test/intro?{query}")
            intro = page.locator("#sessionGameStartIntro")
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")

            self.assertEqual(page.evaluate("window.__introScheduledDelays.filter(delay => delay === 8000).length"), 1)
            self.assertEqual(page.evaluate("window.__introScheduledDelays.includes(5000)"), False)
            self.assertEqual(page.locator("#sessionGameStartIntroNumber").inner_text(), "SPIEL 3")
            self.assertEqual(page.locator("#sessionGameStartIntroTitle").inner_text(), "GEOGRAFIE SCHÄTZUNGEN")
            self.assertEqual(page.locator("[data-vhs-intro-layer]").count(), 5)
            self.assertEqual(page.locator("[data-vhs-intro-layer]").all_inner_texts(), ["GEOGRAFIE SCHÄTZUNGEN"] * 5)
            self.assertEqual(
                page.locator("[data-vhs-intro-layer]:not([data-vhs-intro-layer='main'])").evaluate_all(
                    "layers => layers.length === 4 && layers.every(layer => layer.getAttribute('aria-hidden') === 'true')"
                ),
                True,
            )
            self.assertEqual(page.locator(".vhs-interference__scanline").count(), 10)
            self.assertEqual(page.locator(".vhs-game-intro-app").inner_text(), "QuizMaster")
            self.assertEqual(page.locator(".vhs-game-intro-rec").inner_text(), "REC · LIVE")
            self.assertEqual(page.locator("#sessionGameStartIntroParticipant").inner_text(), "LIVE · MIA")
            self.assertEqual(page.locator(".qa-score-widget").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(page.locator("#participant-options-menu-root").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(intro.evaluate("el => el.parentElement.classList.contains('vhs-theme-shell')"), True)
            self.assertEqual(page.locator(".session-game-start-intro__frame").evaluate("el => getComputedStyle(el).backgroundImage"), "none")
            self.assertIn("noise.png", page.locator("body").evaluate("el => getComputedStyle(el).backgroundImage"))
            self.assertIn(
                "noise.png",
                page.locator(".vhs-theme-shell").evaluate(
                    "el => getComputedStyle(el, '::after').backgroundImage"
                ),
            )
            self.assertEqual(page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).fontStyle"), "normal")
            self.assertEqual(page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).filter"), "none")
            self.assertEqual(page.locator("#sessionGameStartIntroNumber").evaluate("el => getComputedStyle(el).animationDelay"), "0.18s")
            self.assertEqual(
                page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).animationDelay.split(',')[0].trim()"),
                "0.88s",
            )
            self.assertEqual(
                page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).animationDuration"),
                "0.76s, 0.03s, 0.05s",
            )
            self.assertEqual(
                page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).animationName"),
                "vhs-interference-reveal, vhs-interference-blur-pulse, vhs-interference-micro-jerk",
            )
            self.assertEqual(
                page.locator(".vhs-interference__layer--green").evaluate("el => getComputedStyle(el).animationDuration"),
                "0.76s, 0.03s, 0.05s, 1s",
            )
            self.assertIn(
                "vhs-interference-green-jump",
                page.locator(".vhs-interference__layer--green").evaluate("el => getComputedStyle(el).animationName"),
            )
            self.assertEqual(
                page.locator(".vhs-interference__layer--blue").evaluate("el => getComputedStyle(el).animationDuration"),
                "0.76s, 0.03s, 0.05s, 1s",
            )
            self.assertIn(
                "vhs-interference-blue-jump",
                page.locator(".vhs-interference__layer--blue").evaluate("el => getComputedStyle(el).animationName"),
            )
            self.assertEqual(
                page.locator(".vhs-interference__stack").evaluate("el => getComputedStyle(el).animationDuration"),
                "5s",
            )
            self.assertEqual(
                page.locator(".vhs-interference__stack").evaluate("el => getComputedStyle(el).animationName"),
                "vhs-interference-whole",
            )
            self.assertEqual(
                page.locator(".vhs-interference__scanline").first.evaluate("el => getComputedStyle(el).animationDuration"),
                "0.11s",
            )
            self.assertEqual(
                page.locator(".vhs-interference__scanline").first.evaluate("el => getComputedStyle(el).backgroundColor"),
                "rgb(0, 0, 0)",
            )
            self.assertEqual(
                page.locator(".vhs-interference__layer--red").evaluate("el => getComputedStyle(el).color"),
                "rgba(207, 99, 56, 0.72)",
            )
            self.assertEqual(
                page.locator(".vhs-interference__layer--green").evaluate("el => getComputedStyle(el).color"),
                "rgba(115, 145, 107, 0.56)",
            )
            self.assertEqual(
                page.locator(".vhs-interference__layer--blue").evaluate("el => getComputedStyle(el).color"),
                "rgba(85, 125, 155, 0.74)",
            )
            self.assertEqual(
                page.locator(".vhs-interference__layer--glow").evaluate("el => getComputedStyle(el).filter"),
                "blur(15px)",
            )

            intro.evaluate(
                """el => {
                    el.classList.remove('is-vhs-intro-playing');
                    void el.offsetWidth;
                    el.classList.add('is-vhs-intro-playing');
                }"""
            )
            page.wait_for_timeout(650)
            self.assertGreater(float(page.locator("#sessionGameStartIntroNumber").evaluate("el => getComputedStyle(el).opacity")), 0.9)
            self.assertLess(float(page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).opacity")), 0.1)
            page.wait_for_timeout(1050)
            self.assertGreater(float(page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).opacity")), 0.75)

            page.evaluate(
                """() => {
                    const root = document.querySelector('[data-vhs-game-intro]');
                    window.initializeVhsGameIntro({
                        root,
                        gameId: 'next-game',
                        gameNumber: 4,
                        gameName: 'Ein besonders langer Spielname mit Umlauten'
                    });
                    window.initializeVhsGameIntro({
                        root,
                        gameId: 'next-game',
                        gameNumber: 4,
                        gameName: 'Ein besonders langer Spielname mit Umlauten'
                    });
                }"""
            )
            self.assertEqual(page.locator(".vhs-interference__scanline").count(), 10)
            self.assertEqual(page.locator("[data-vhs-intro-layer]").count(), 5)
            self.assertEqual(page.locator(".vhs-interference").evaluate("el => el.classList.contains('vhs-interference--very-long')"), True)
            self.assertEqual(page.locator("#sessionGameStartIntroNumber").inner_text(), "SPIEL 4")

            for width, height in (
                (1024, 768),
                (900, 600),
                (800, 1000),
                (844, 390),
                (390, 844),
            ):
                with self.subTest(viewport=(width, height)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertEqual(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)
                    frame_box = page.locator(".session-game-start-intro__frame").bounding_box()
                    title_box = page.locator("#sessionGameStartIntroTitle").bounding_box()
                    header_box = page.locator(".vhs-game-intro-header").bounding_box()
                    footer_box = page.locator(".vhs-game-intro-footer").bounding_box()
                    self.assertGreaterEqual(title_box["x"], frame_box["x"] - 1)
                    self.assertLessEqual(title_box["x"] + title_box["width"], frame_box["x"] + frame_box["width"] + 1)
                    self.assertLessEqual(header_box["y"] + header_box["height"], footer_box["y"])
                    self.assertLessEqual(footer_box["y"] + footer_box["height"], frame_box["y"] + frame_box["height"] + 1)

            self.assertEqual(
                page.locator(".vhs-interference__stack").evaluate("el => getComputedStyle(el).animationName"),
                "vhs-interference-whole-mobile",
            )
            self.assertIn(
                "vhs-interference-green-jump-mobile",
                page.locator(".vhs-interference__layer--green").evaluate("el => getComputedStyle(el).animationName"),
            )
            self.assertIn(
                "vhs-interference-blue-jump-mobile",
                page.locator(".vhs-interference__layer--blue").evaluate("el => getComputedStyle(el).animationName"),
            )

            page.evaluate("window.sessionGameStartIntro.handleState({ type: 'question_started' })")
            page.wait_for_timeout(220)
            self.assertEqual(intro.get_attribute("hidden"), "")
            self.assertEqual(page.locator("html").evaluate("el => el.classList.contains('is-session-game-intro-active')"), False)
            self.assertEqual(intro.evaluate("el => el.parentElement === document.body"), True)
            self.assertEqual(browser_errors, [])
            self.assertEqual(external_requests, [])
        finally:
            page.close()
            context.close()

    def test_vhs_intro_reduced_motion_is_stable_within_half_a_second(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfügbar: {self._playwright_error}")

        context = self._browser.new_context(
            viewport={"width": 800, "height": 1000},
            reduced_motion="reduce",
        )
        query = self.build_page(context)
        page = context.new_page()
        try:
            page.goto(f"http://intro.test/intro?{query}")
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            page.wait_for_timeout(450)
            self.assertGreater(float(page.locator("#sessionGameStartIntroNumber").evaluate("el => getComputedStyle(el).opacity")), 0.9)
            self.assertGreater(float(page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).opacity")), 0.9)
            self.assertEqual(page.locator(".vhs-interference__layer--red").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(page.locator(".vhs-interference__scanlines").evaluate("el => getComputedStyle(el).display"), "none")
        finally:
            page.close()
            context.close()

    def test_vhs_intro_styles_do_not_leak_into_standard_or_arcade(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfügbar: {self._playwright_error}")

        context = self._browser.new_context(viewport={"width": 1024, "height": 768})
        query = self.build_page(context, theme="standard")
        page = context.new_page()
        try:
            page.goto(f"http://intro.test/intro?{query}")
            page.wait_for_selector("#sessionGameStartIntro:not([hidden])")
            self.assertEqual(page.locator(".vhs-game-intro-header").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(page.locator(".session-game-start-intro__card").evaluate("el => getComputedStyle(el).borderRadius"), "28px")
            self.assertNotEqual(page.locator(".qa-score-widget").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(page.locator("#sessionGameStartIntroTitle").evaluate("el => getComputedStyle(el).textTransform"), "none")

            page.locator("html").evaluate("el => { el.dataset.participantTheme = 'arcade'; }")
            self.assertEqual(page.locator(".vhs-game-intro-header").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(page.locator(".session-game-start-intro__card").evaluate("el => getComputedStyle(el).borderRadius"), "28px")
            self.assertNotEqual(page.locator(".qa-score-widget").evaluate("el => getComputedStyle(el).display"), "none")
        finally:
            page.close()
            context.close()


if __name__ == "__main__":
    unittest.main()
