from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
CINEMATOGRAFICA_FONT_PATH = REPO_ROOT / "static" / "fonts" / "Cinematografica-Regular-trial.ttf"
WALKWAY_FONT_PATH = REPO_ROOT / "static" / "fonts" / "Walkway Condensed SemiBold.ttf"
NOISE_PATH = REPO_ROOT / "static" / "neutral_transparent_noise_overlay_512.png"

THEME_TITLE_SELECTORS = [
    ".quiz-title",
    ".step-name-primary",
    ".session-game-start-intro__title",
    ".qa-game-title-font",
    ".host-monitor-content .quiz-title",
]

PLAYER_TITLE_TEMPLATES = [
    "templates/quiz/play.html",
    "templates/estimation/play.html",
    "templates/where_is_this/play.html",
    "templates/who_is_that/play.html",
    "templates/clue_rush/play.html",
    "templates/assign/play.html",
    "templates/sorting_ladder/play.html",
    "templates/black_jack_quiz/play.html",
    "templates/who_is_lying/play.html",
]

LOCAL_TITLE_FONT_TEMPLATES = [
    "templates/buzzer/play.html",
    "templates/host_points/play.html",
    "templates/wann_war_das/play.html",
    "templates/wer_weiss_mehr/play.html",
]

LOCAL_HOST_TITLE_TEMPLATES = [
    "templates/admin_dashboard/buzzer_monitor.html",
    "templates/admin_dashboard/host_points_monitor.html",
    "templates/admin_dashboard/wann_war_das_monitor.html",
    "templates/admin_dashboard/wer_weiss_mehr_monitor.html",
]


def read_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class GameTitleFontTests(unittest.TestCase):
    def test_cinematografica_font_file_and_theme_font_face_exist(self):
        content = read_text("static/theme.css")

        self.assertTrue(CINEMATOGRAFICA_FONT_PATH.exists())
        self.assertIn('@font-face', content)
        self.assertIn('font-family: "Cinematografica"', content)
        self.assertIn('url("/static/fonts/Cinematografica-Regular-trial.ttf")', content)
        self.assertIn('font-display: swap', content)
        self.assertIn('--font-family-game-title: "Cinematografica", system-ui, sans-serif', content)

    def test_walkway_condensed_is_default_ui_font(self):
        content = read_text("static/theme.css")

        self.assertTrue(WALKWAY_FONT_PATH.exists())
        self.assertIn('font-family: "Walkway Condensed"', content)
        self.assertIn('url("/static/fonts/Walkway%20Condensed%20SemiBold.ttf")', content)
        self.assertNotIn('url("/static/fonts/Walkway%20Condensed.ttf")', content)
        self.assertIn('--font-family-sans: "Walkway Condensed", system-ui, sans-serif', content)
        self.assertRegex(content, re.compile(r"(?s)body\s*\{[^}]*font-family: var\(--font-family-sans\)"))

    def test_global_background_uses_noise_overlay(self):
        content = read_text("static/theme.css")

        self.assertTrue(NOISE_PATH.exists())
        self.assertIn('--app-background: #d0d6b4', content)
        self.assertIn('url("/static/neutral_transparent_noise_overlay_512.png")', content)
        self.assertIn("body::before", content)
        self.assertIn("background-repeat: repeat", content)
        self.assertIn("pointer-events: none", content)
        self.assertIn("opacity: .32", content)

    def test_theme_applies_font_only_to_title_selectors(self):
        content = read_text("static/theme.css")

        for selector in THEME_TITLE_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIn(selector, content)
        self.assertIn("font-family: var(--font-family-game-title)", content)
        self.assertIn("font-size: clamp(2rem, 4.4vw, 3.15rem)", content)
        self.assertNotRegex(content, re.compile(r"(?s)body\s*\{[^}]*Cinematografica"))
        self.assertNotRegex(content, re.compile(r"(?s)(button|\.btn|table)\s*\{[^}]*Cinematografica"))

    def test_participant_titles_and_waiting_title_use_game_title_font(self):
        waiting_include = read_text("templates/includes/_participant_start_waiting.html")
        intro_include = read_text("templates/includes/_session_game_start_intro.html")

        self.assertIn('qa-start-waiting-game-name', waiting_include)
        self.assertIn('qa-start-waiting-subtitle', waiting_include)
        self.assertIn('session-game-start-intro__title', intro_include)

        for relative_path in PLAYER_TITLE_TEMPLATES:
            with self.subTest(template=relative_path):
                self.assertIn("quiz-title", read_text(relative_path))

    def test_local_templates_without_theme_scope_font_to_game_title_class(self):
        for relative_path in LOCAL_TITLE_FONT_TEMPLATES:
            with self.subTest(template=relative_path):
                content = read_text(relative_path)

                self.assertIn('font-family: "Cinematografica"', content)
                self.assertIn('font-family: "Walkway Condensed"', content)
                self.assertIn('Walkway%20Condensed%20SemiBold.ttf', content)
                self.assertNotIn('Walkway%20Condensed.ttf', content)
                self.assertIn('"Cinematografica", system-ui, sans-serif', content)
                self.assertIn('"Walkway Condensed", system-ui, sans-serif', content)
                self.assertIn("neutral_transparent_noise_overlay_512.png", content)
                self.assertIn("qa-game-title-font", content)
                self.assertNotRegex(content, re.compile(r"(?s)body\s*\{[^}]*Cinematografica"))

        for relative_path in LOCAL_HOST_TITLE_TEMPLATES:
            with self.subTest(template=relative_path):
                self.assertIn("qa-game-title-font", read_text(relative_path))


if __name__ == "__main__":
    unittest.main()
