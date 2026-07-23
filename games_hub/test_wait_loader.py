from pathlib import Path
import re
import unittest

from django.template.loader import render_to_string


REPO_ROOT = Path(__file__).resolve().parent.parent

WAITING_LOADER_TEMPLATES = [
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

START_WAITING_INCLUDE_TEMPLATES = [
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

LOCAL_START_WAITING_TEMPLATES = [
    "templates/buzzer/play.html",
    "templates/host_points/play.html",
    "templates/wann_war_das/play.html",
    "templates/wer_weiss_mehr/play.html",
]


def read_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def extract_start_waiting_block(content: str) -> str:
    start = content.index('id="waitingQuizState"')
    end_markers = [
        '<div id="waitingQuestionState"',
        '<!-- Correct Answer State',
        '<!-- Question State',
        '<!-- Topic',
    ]
    end_candidates = [content.find(marker, start) for marker in end_markers]
    end_candidates = [index for index in end_candidates if index != -1]
    end = min(end_candidates) if end_candidates else start + 1200
    return content[start:end]


class WaitLoaderTemplateTests(unittest.TestCase):
    def test_wait_loader_css_is_namespaced_and_reduced_motion_safe(self):
        content = read_text("static/theme.css")

        self.assertIn(".qa-wait-loader", content)
        self.assertIn('.qa-wait-loader:not([class*="qa-wait-loader-"]):not(.qa-arcade-wait-loader)', content)
        self.assertIn('.qa-wait-loader:not([class*="qa-wait-loader-"]):not(.qa-arcade-wait-loader)::before', content)
        self.assertIn('.qa-wait-loader:not([class*="qa-wait-loader-"]):not(.qa-arcade-wait-loader)::after', content)
        self.assertIn("@keyframes qa-wait-loader-spin", content)
        self.assertIn('font-family: "QA Arcade Heading"', content)
        self.assertIn('src: url("/static/fonts/ka1.ttf") format("truetype")', content)
        self.assertIn('font-family: "QA Arcade Text"', content)
        self.assertIn('src: url("/static/fonts/RETROTECH.ttf") format("truetype")', content)
        self.assertIn("@media (prefers-reduced-motion: reduce)", content)
        self.assertIn("animation: qa-wait-loader-spin 2s infinite linear", content)
        self.assertIn("animation-duration: 1s", content)
        self.assertIn("display: inline-block", content)
        self.assertIn("flex: 0 0 auto", content)
        self.assertIn("z-index: 1", content)
        self.assertIn("transform-origin: left", content)
        self.assertIn("color: var(--participant-effective-text-color, var(--participant-text-color, currentColor))", content)
        self.assertIn("color: currentColor", content)
        self.assertIn("overflow: visible", content)
        self.assertIn(".waiting-animation:has(.qa-wait-loader)", content)
        self.assertIn("justify-content: center", content)
        self.assertIn("animation-duration: 0.001ms !important", content)
        self.assertIn("animation-iteration-count: 1 !important", content)
        self.assertNotRegex(content, re.compile(r"(?m)^\.loader\s*\{"))

        expected_variants = [
            "01",
            "03",
            "04",
            "05",
            "06",
            "07",
            "08",
            "09",
            "10",
            "11",
            "12",
            "14",
            "15",
            "16",
            "17",
            "18",
            "19",
        ]
        for variant in expected_variants:
            with self.subTest(loader_variant=variant):
                self.assertIn(f".qa-wait-loader-{variant}", content)
                self.assertIn(f"@keyframes qa-wait-loader-{variant}-", content)

        self.assertNotIn(".qa-wait-loader-02", content)

    def test_start_waiting_loader_wrapper_has_no_background_badge(self):
        content = read_text("static/theme.css")
        start = content.index(".qa-start-waiting-animation")
        block = content[start:content.index(".qa-start-waiting-title", start)]

        self.assertIn("display: flex", block)
        self.assertIn("align-items: center", block)
        self.assertIn("justify-content: center", block)
        self.assertIn("background: transparent", block)
        self.assertIn("border: 0", block)
        self.assertIn("box-shadow: none", block)
        self.assertNotIn("rgba(255, 255, 255", block)

    def test_loader_positioning_uses_outer_frame_not_inner_transform_override(self):
        content = read_text("static/theme.css")
        start = content.index(".qa-wait-loader-frame")
        block = content[start:content.index(".qa-start-waiting-title", start)]

        self.assertIn("width: 80px", block)
        self.assertIn("display: flex", block)
        self.assertIn("align-items: center", block)
        self.assertIn("justify-content: center", block)
        self.assertIn('.qa-wait-loader-frame:has(.qa-wait-loader:not([class*="qa-wait-loader-"]):not(.qa-arcade-wait-loader))', block)
        self.assertIn("justify-content: flex-end", block)
        self.assertIn(".qa-wait-loader-frame:has(.qa-arcade-wait-loader)", block)
        self.assertIn("overflow: visible", block)
        self.assertNotIn("transform:", block)

    def test_shared_start_waiting_include_has_short_german_copy(self):
        content = read_text("templates/includes/_participant_start_waiting.html")
        rendered = render_to_string(
            "includes/_participant_start_waiting.html",
            {"waiting_game_title": "Schätzrunde 1"},
        )

        self.assertIn("qa-wait-loader", content)
        self.assertIn("qa-wait-loader-frame", content)
        self.assertIn("qa-start-waiting-title", content)
        self.assertIn("qa-start-waiting-game-name", content)
        self.assertIn("qa-start-waiting-subtitle", content)
        self.assertIn("beginnt gleich!", content)
        self.assertIn('class="qa-game-title-font qa-start-waiting-game-name"', rendered)
        self.assertIn(">Schätzrunde 1</span>", rendered)
        self.assertIn('<span class="qa-start-waiting-subtitle">beginnt gleich!</span>', rendered)
        self.assertNotIn("Waiting for", content)
        self.assertNotIn("participants", content)

    def test_participant_widget_randomizes_wait_loader_variants_decoratively(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn("standardLoaderVariants", rendered)
        self.assertIn("arcadeLoaderVariants", rendered)
        self.assertIn("qa-wait-loader-01", rendered)
        self.assertIn("qa-wait-loader-19", rendered)
        self.assertIn("Math.random()", rendered)
        self.assertIn("MutationObserver", rendered)
        self.assertIn("qaWaitLoaderVariantApplied", rendered)
        self.assertIn("root.querySelectorAll(waitLoaderSelector)", rendered)

    def test_arcade_loader_pool_and_theme_cleanup_are_strictly_separated(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        pool_match = re.search(
            r"var arcadeLoaderVariants = \[(.*?)\];",
            rendered,
            re.DOTALL,
        )

        self.assertIsNotNone(pool_match)
        arcade_pool = re.findall(r"'([^']+)'", pool_match.group(1))
        self.assertEqual(
            arcade_pool,
            [f"qa-arcade-wait-loader-{index:02d}" for index in range(1, 11)],
        )
        self.assertFalse(any(name.startswith("qa-wait-loader-") for name in arcade_pool))
        self.assertFalse(any("hypnotic" in name for name in arcade_pool))
        self.assertIn("function clearWaitLoaderVariantClasses(loader)", rendered)
        self.assertIn("className === 'qa-wait-loader-stage'", rendered)
        self.assertIn("className === 'qa-wait-loader-frame'", rendered)
        self.assertIn("className.startsWith('qa-wait-loader-')", rendered)
        self.assertIn("className.startsWith('qa-arcade-wait-loader-')", rendered)
        self.assertIn("className.startsWith('qa-hypnotic-loader-')", rendered)
        self.assertIn("className.startsWith('qa-hypnotic-wait-loader-')", rendered)
        self.assertIn(
            "loader.classList.remove('qa-wait-loader', 'qa-arcade-wait-loader', 'loader')",
            rendered,
        )
        self.assertIn("loader.classList.add('qa-arcade-wait-loader', arcadeVariant)", rendered)
        self.assertIn("loader.classList.add('qa-wait-loader')", rendered)

    def test_arcade_loader_css_has_exactly_ten_namespaced_variants(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        css_start = rendered.index("Arcade waiting-loader collection")
        css_end = rendered.index("</style>", css_start)
        arcade_css = rendered[css_start:css_end]

        defined_variants = sorted(set(re.findall(r"\.qa-arcade-wait-loader-(\d{2})", arcade_css)))
        self.assertEqual(defined_variants, [f"{index:02d}" for index in range(1, 11)])
        for index in range(1, 11):
            with self.subTest(arcade_loader=index):
                self.assertIn(f"@keyframes qa-arcade-loader-{index:02d}", arcade_css)
        for index in (8, 9, 10):
            with self.subTest(loading_label=index):
                variant_start = arcade_css.index(f".qa-arcade-wait-loader-{index:02d}")
                self.assertIn('content: "Loading";', arcade_css[variant_start:])
        self.assertNotRegex(arcade_css, re.compile(r"(?m)^\.loader(?:\s|[:.#\[])"))
        self.assertNotRegex(arcade_css, re.compile(r"@keyframes\s+l\d+\b"))
        self.assertIn(".qa-arcade-wait-loader::before", arcade_css)
        self.assertIn(".qa-arcade-wait-loader::after", arcade_css)

    def test_arcade_loading_labels_remain_black_independent_of_participant_text_color(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        expected_selector = (
            'html[data-participant-theme="arcade"] .qa-arcade-wait-loader-08::before,\n'
            'html[data-participant-theme="arcade"] .qa-arcade-wait-loader-09::before,\n'
            'html[data-participant-theme="arcade"] .qa-arcade-wait-loader-10::before {'
        )

        self.assertIn(expected_selector, rendered)
        rule_start = rendered.index(expected_selector)
        rule_end = rendered.index("}", rule_start)
        rule = rendered[rule_start:rule_end]
        self.assertIn("color: #000;", rule)
        self.assertIn("-webkit-text-fill-color: #000;", rule)
        self.assertNotIn("!important", rule)
        self.assertNotIn("--participant-effective-text-color", rule)

    def test_visible_participant_waiting_states_use_new_loader(self):
        for relative_path in WAITING_LOADER_TEMPLATES:
            with self.subTest(template=relative_path):
                visible_markup = read_text(relative_path).split("<style>", 1)[0]

                self.assertTrue(
                    "qa-wait-loader" in visible_markup
                    or "_participant_start_waiting.html" in visible_markup
                )
                self.assertNotIn('class="pulse-ring"', visible_markup)

    def test_pre_game_waiting_cards_use_unified_start_copy(self):
        forbidden_snippets = [
            "Waiting for",
            "Get ready",
            "participants joined",
            "participants connected",
            "participants-count",
            "participant_count",
            "The quiz host will start",
            "The host will start",
        ]

        for relative_path in START_WAITING_INCLUDE_TEMPLATES:
            with self.subTest(template=relative_path):
                block = extract_start_waiting_block(read_text(relative_path))

                self.assertIn("_participant_start_waiting.html", block)
                for snippet in forbidden_snippets:
                    self.assertNotIn(snippet, block)

        for relative_path in LOCAL_START_WAITING_TEMPLATES:
            with self.subTest(template=relative_path):
                content = read_text(relative_path)

                self.assertIn("beginnt gleich!", content)
                self.assertNotIn("Warte darauf, dass der Host das Spiel startet.", content)
                self.assertNotIn("Warte auf Spielstart", content)


if __name__ == "__main__":
    unittest.main()
