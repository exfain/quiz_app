from pathlib import Path
import unittest

from django.template.loader import render_to_string

from games_hub.playwright_e2e import start_chromium_browser


REPO_ROOT = Path(__file__).resolve().parent.parent

PARTICIPANT_WIDGET_TEMPLATES = [
    "templates/quiz/play.html",
    "templates/estimation/play.html",
    "templates/where_is_this/play.html",
    "templates/who_is_that/play.html",
    "templates/clue_rush/play.html",
    "templates/assign/play.html",
    "templates/sorting_ladder/play.html",
    "templates/black_jack_quiz/play.html",
    "templates/who_is_lying/play.html",
    "templates/hub/lobby.html",
    "templates/buzzer/play.html",
    "templates/host_points/play.html",
    "templates/wann_war_das/play.html",
    "templates/wer_weiss_mehr/play.html",
]

PARTICIPANT_RESULT_TEMPLATES = [
    "templates/assign/result.html",
    "templates/black_jack_quiz/result.html",
    "templates/buzzer/result.html",
    "templates/estimation/result.html",
    "templates/host_points/result.html",
    "templates/quiz/result.html",
    "templates/wann_war_das/result.html",
    "templates/where_is_this/results.html",
    "templates/who_is_lying/result.html",
    "templates/who_is_that/result.html",
]


def read_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class ParticipantOptionsMenuTests(unittest.TestCase):
    def test_all_participant_result_templates_use_existing_theme_infrastructure(self):
        widget = read_text("templates/includes/accessibility_widget.html")
        vhs_css = read_text("static/themes/vhs/vhs.css")

        self.assertEqual(len(PARTICIPANT_RESULT_TEMPLATES), 10)
        for relative_path in PARTICIPANT_RESULT_TEMPLATES:
            with self.subTest(template=relative_path):
                content = read_text(relative_path)
                self.assertIn("participant-result-page", content)
                self.assertEqual(
                    content.count("{% include 'includes/accessibility_widget.html' with participant_options_menu=True %}"),
                    1,
                )
                self.assertIn("data-participant-name", content)

        self.assertIn("body > .result-container", widget)
        self.assertIn("function syncVhsLegacyLabels(shell)", widget)
        self.assertIn("function restoreVhsLegacyLabels()", widget)
        self.assertIn('body.participant-result-page .vhs-theme-shell', vhs_css)
        self.assertNotIn('body.participant-result-page', read_text("static/theme.css"))

    def test_open_game_types_have_scoped_vhs_component_rules(self):
        vhs_css = read_text("static/themes/vhs/vhs.css")
        expected_scopes = (
            "body.assign-play-page",
            "body.where-play-page",
            "body.who-play-page",
            "body.who-that-play-page",
            "body.blackjack-play-page",
            "body.clue-rush-play-page",
            "body.sorting-ladder-play-page",
            "body.buzzer-play-page",
            ".host-points-play-page",
            "body.wann-war-das-play-page",
            "body.wer-weiss-mehr-play-page",
        )
        for scope in expected_scopes:
            with self.subTest(scope=scope):
                self.assertIn(scope, vhs_css)

        self.assertIn("score-box__list", read_text("templates/wer_weiss_mehr/play.html"))
        self.assertIn("data-vhs-round-number", read_text("templates/wer_weiss_mehr/play.html"))
        self.assertIn("'New': 'NEU'", read_text("templates/includes/accessibility_widget.html"))

    def test_vhs_endscreen_uses_scoped_layout_and_semantic_result_columns(self):
        results_partial = read_text("templates/includes/_post_game_results.html")
        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")

        for column in ("player", "rank", "game-points", "factor", "overall"):
            self.assertIn(f"results-col-{column}", results_partial)
        self.assertIn("<colgroup>${columns.map", results_partial)
        self.assertIn("vhsEndscreenOriginalHtml", widget)
        self.assertIn("join(' \\u00b7 ')", widget)
        self.assertIn('.vhs-theme-shell:has(.vhs-end-screen)', vhs_css)
        self.assertIn('.vhs-end-screen .trophy-icon', vhs_css)
        self.assertIn('.vhs-end-screen .vhs-lobby-return', vhs_css)
        self.assertNotIn('html[data-participant-theme="standard"] .vhs-end-screen', vhs_css)
        self.assertNotIn('html[data-participant-theme="arcade"] .vhs-end-screen', vhs_css)

    def test_vhs_reveal_uses_existing_quick_quiz_values_and_reversible_theme_runtime(self):
        quiz_template = read_text("templates/quiz/play.html")
        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")

        self.assertIn('data-vhs-reveal-game-type="quiz"', quiz_template)
        self.assertIn("revealState.dataset.vhsRevealQuestionType", quiz_template)
        self.assertIn("function formatVhsRevealAnswer(value, questionType)", widget)
        self.assertIn("function getVhsPromptLabel(questionType, gameType)", widget)
        self.assertIn("function syncVhsRevealState(shell)", widget)
        self.assertIn("'DIE RICHTIGE ANTWORT IST'", widget)
        self.assertIn("'GEGEBENE ANTWORT'", widget)
        self.assertIn("return 'BEHAUPTUNG'", widget)
        self.assertIn("return 'AUFGABE'", widget)
        self.assertIn("return 'FRAGE'", widget)
        self.assertIn('#correctAnswerState .vhs-reveal-card', vhs_css)
        self.assertIn('#correctAnswerState :is(.vhs-reveal-vs, .vhs-reveal-duplicate)', vhs_css)
        self.assertNotIn('html[data-participant-theme="standard"] #correctAnswerState', vhs_css)
        self.assertNotIn('html[data-participant-theme="arcade"] #correctAnswerState', vhs_css)

    def test_vhs_submitted_screen_uses_existing_answer_and_score_data(self):
        quiz_template = read_text("templates/quiz/play.html")
        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")

        self.assertIn("data-points-earned", quiz_template)
        self.assertIn("data-max-points", quiz_template)
        self.assertIn("syncVhsSubmittedState(shell)", widget)
        self.assertIn("ANTWORT EINGELOGGT!", widget)
        self.assertIn("Warte auf die nächste Runde...", widget)
        self.assertIn("Eingeloggt nach ", widget)
        self.assertIn("vhs-points-fraction", widget)
        self.assertIn("vhs-points-unit", widget)
        self.assertIn("vhs-points-column", widget)
        self.assertIn('#answerSubmittedState .vhs-answer-submitted-icon', vhs_css)
        self.assertIn('#answerSubmittedState .vhs-submitted-answer-panel', vhs_css)
        self.assertNotIn('html[data-participant-theme="standard"] #answerSubmittedState', vhs_css)
        self.assertNotIn('html[data-participant-theme="arcade"] #answerSubmittedState', vhs_css)

    def test_vhs_quick_quiz_short_answer_uses_existing_fields_and_submit_handler(self):
        quiz_template = read_text("templates/quiz/play.html")
        vhs_css = read_text("static/themes/vhs/vhs.css")

        self.assertIn('class="quick-quiz-response-area"', quiz_template)
        self.assertIn("quick-quiz-short-answer-field", quiz_template)
        self.assertIn("label.htmlFor = inputId", quiz_template)
        self.assertIn("input.id = inputId", quiz_template)
        self.assertEqual(
            quiz_template.count("document.getElementById('submitAnswerBtn').addEventListener('click'"),
            1,
        )
        self.assertIn(
            'html[data-participant-theme="vhs"] body.quiz-play-page .vhs-theme-shell',
            vhs_css,
        )
        self.assertIn("#questionState:has(.quick-quiz-short-answer-form)", vhs_css)
        self.assertIn("padding-top: 215px", vhs_css)
        self.assertIn("padding-top: 365px", vhs_css)
        self.assertNotIn('html[data-participant-theme="standard"] .quick-quiz-short-answer-field', vhs_css)
        self.assertNotIn('html[data-participant-theme="arcade"] .quick-quiz-short-answer-field', vhs_css)

    def test_vhs_estimation_question_reuses_existing_form_and_scopes_layout(self):
        estimation_template = read_text("templates/estimation/play.html")
        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")

        self.assertIn('class="estimation-interaction-area"', estimation_template)
        self.assertIn('id="estimateInput"', estimation_template)
        self.assertIn('id="unitDisplay"', estimation_template)
        self.assertIn('id="submitAnswerBtn"', estimation_template)
        self.assertIn('submit-answer vhs-action-button', estimation_template)
        self.assertIn('class="zone-submit-label"', estimation_template)
        self.assertEqual(
            estimation_template.count("document.getElementById('submitAnswerBtn').addEventListener('click'"),
            1,
        )
        self.assertIn("#questionState .estimation-interaction-area", widget)
        self.assertIn("isVhsElementVisible", widget)
        self.assertIn(
            'html[data-participant-theme="vhs"] body.estimation-play-page .vhs-theme-shell',
            vhs_css,
        )
        self.assertIn(".estimate-input::placeholder", vhs_css)
        self.assertIn("color: var(--vhs-muted) !important", vhs_css)
        self.assertIn(".estimate-input::-webkit-inner-spin-button", vhs_css)
        self.assertIn(".estimate-input:hover::-webkit-inner-spin-button", vhs_css)
        self.assertIn("#answerSubmittedState .estimate-summary", vhs_css)
        self.assertIn("#correctAnswerState .zone-range-row", vhs_css)
        self.assertIn("rank-results-title__vhs", estimation_template)
        self.assertIn("RANGLISTE DIESER FRAGE", estimation_template)
        self.assertIn("rank-result-points__vhs", estimation_template)
        self.assertIn("this.formatPointLabel(result.points_earned)", estimation_template)
        self.assertIn("#correctAnswerState .rank-result-row", vhs_css)
        self.assertIn("grid-template-columns: 52px minmax(0, 1fr) max-content", vhs_css)
        self.assertIn("#correctAnswerState .rank-result-row.is-self", vhs_css)
        estimation_result = read_text("templates/estimation/result.html")
        self.assertIn("FINALE RANGLISTE", estimation_result)
        self.assertIn("ranking_participant.id == participant.id", estimation_result)
        self.assertIn(".estimation-final-ranking", vhs_css)
        self.assertIn("@media (max-width: 1180px)", vhs_css)
        self.assertNotIn('html[data-participant-theme="standard"] body.estimation-play-page', vhs_css)
        self.assertNotIn('html[data-participant-theme="arcade"] body.estimation-play-page', vhs_css)

    def test_vhs_lobby_uses_existing_state_and_handlers(self):
        lobby = read_text("templates/hub/lobby.html")

        self.assertIn("data-participant-lobby", lobby)
        self.assertIn("function syncParticipantLobbyMetadata(state)", lobby)
        self.assertIn("state.steps.find(step => Number(step.order) === currentIndex)", lobby)
        self.assertIn("String(currentOrder + 1)", lobby)
        self.assertIn("lobbyRoot.dataset.participantName = nickname", lobby)
        self.assertEqual(
            lobby.count("readyCheckInBtn.addEventListener('click', submitCheckIn)"),
            1,
        )
        self.assertNotIn("data-participant-lobby-ready-handler", lobby)

    def test_participant_widget_renders_options_menu_without_old_buttons(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn("participant-options-menu-root", rendered)
        self.assertIn("Interface", rendered)
        self.assertIn("participant-options-interface-panel", rendered)
        self.assertIn("Schriftgröße", rendered)
        self.assertIn("participant-font-size-decrease", rendered)
        self.assertIn("participant-font-size-increase", rendered)
        self.assertIn("participant-font-size-value", rendered)
        self.assertIn("participant-theme-select", rendered)
        self.assertIn('<option value="standard">Standard</option>', rendered)
        self.assertIn('<option value="arcade">Arcade</option>', rendered)
        self.assertIn('<option value="vhs">VHS</option>', rendered)
        self.assertIn("participant-score-position-select", rendered)
        self.assertIn('<option value="bottom-left">links unten</option>', rendered)
        self.assertIn('<option value="top-left">links oben</option>', rendered)
        self.assertIn('<option value="top-right">rechts oben</option>', rendered)
        self.assertIn('<option value="bottom-right">rechts unten</option>', rendered)
        self.assertIn("participant-interface-reset", rendered)
        self.assertIn("participant-custom-colors-enabled", rendered)
        self.assertIn("participant-background-color", rendered)
        self.assertIn("participant-text-color", rendered)
        self.assertIn("participant-accent-color", rendered)
        self.assertIn('id="participant-background-color" type="color" value="#d0d6b4" aria-label="Hintergrund" disabled', rendered)
        self.assertIn('id="participant-text-color" type="color" value="#111827" aria-label="Schrift" disabled', rendered)
        self.assertIn('id="participant-accent-color" type="color" value="#a3a88d" aria-label="Akzentfarbe 1" disabled', rendered)
        self.assertIn('id="participant-accent-color-2" type="color" value="#f24b3d" aria-label="Akzentfarbe 2" disabled', rendered)
        self.assertIn("participant-high-contrast", rendered)
        self.assertIn("participant-invert-colors", rendered)
        self.assertIn("--participant-font-scale", rendered)
        self.assertIn("--participant-root-font-size", rendered)
        self.assertIn("--participant-theme-background", rendered)
        self.assertIn("--participant-theme-text", rendered)
        self.assertIn("--participant-heading-font", rendered)
        self.assertIn("--participant-body-font", rendered)
        self.assertIn("--participant-accent-color", rendered)
        self.assertIn("--participant-accent-color-2", rendered)
        self.assertIn("--participant-bg-color", rendered)
        self.assertIn("--participant-text-color", rendered)
        self.assertIn("--participant-effective-bg-color", rendered)
        self.assertIn("--participant-effective-text-color", rendered)
        self.assertIn("--participant-effective-accent-color", rendered)
        self.assertIn("body.quiz-play-page", rendered)
        self.assertIn("font-size: 1.5rem", rendered)

    def test_vhs_theme_uses_local_noise_twice_and_existing_theme_runtime(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        noise_path = REPO_ROOT / "static" / "themes" / "vhs" / "noise.png"

        self.assertIn("themes/vhs/vhs.css", rendered)
        self.assertIn("var THEMES = ['standard', 'arcade', 'vhs']", rendered)
        self.assertIn("participant_interface_theme", rendered)
        self.assertIn("document.documentElement.dataset.participantTheme = normalized", rendered)
        self.assertIn("if (normalized === 'vhs') setupVhsTheme()", rendered)
        self.assertIn("else teardownVhsTheme()", rendered)
        self.assertIn("Im VHS-Theme fest positioniert", rendered)

        self.assertTrue(noise_path.is_file())
        noise = noise_path.read_bytes()
        self.assertEqual(noise[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(noise[16:20], "big"), 256)
        self.assertEqual(int.from_bytes(noise[20:24], "big"), 256)
        self.assertEqual(vhs_css.count("background-image: var(--vhs-noise-texture)"), 2)
        self.assertIn('--vhs-noise-texture: url("/static/themes/vhs/noise.png")', vhs_css)
        self.assertIn("background-size: 256px 256px", vhs_css)
        self.assertIn("background-blend-mode: overlay", vhs_css)
        self.assertIn("mix-blend-mode: overlay", vhs_css)
        self.assertIn("z-index: 2147483646", vhs_css)
        self.assertIn("pointer-events: none", vhs_css)
        self.assertNotIn("data:image", vhs_css)
        self.assertNotIn("http://", vhs_css)
        self.assertNotIn("https://", vhs_css)

        for variable, color in (
            ("--vhs-ink", "#191f1d"),
            ("--vhs-plastic-line", "#555b57"),
            ("--vhs-paper", "#e9dfca"),
            ("--vhs-answer-text", "#d8d8d1"),
            ("--vhs-muted", "#9ba19c"),
            ("--vhs-cream", "#efe4ba"),
            ("--vhs-yellow", "#e7c84d"),
            ("--vhs-orange", "#cf6338"),
            ("--vhs-violet", "#78627e"),
            ("--vhs-blue", "#557d9b"),
            ("--vhs-black", "#0d1110"),
        ):
            with self.subTest(variable=variable):
                self.assertIn(f"{variable}: {color}", vhs_css)

        self.assertIn("function syncVhsQuestion(shell, gameMeta)", rendered)
        self.assertIn("firstWord.toLocaleUpperCase('de-DE')", rendered)
        self.assertIn("question.dataset.vhsFullQuestion = originalQuestion", rendered)
        self.assertIn("function syncVhsScoreWidget(shell)", rendered)
        self.assertIn("roundCount <= 3 ? 1 : (roundCount <= 6 ? 2 : 3)", rendered)
        self.assertIn("function syncVhsEndscreen(shell)", rendered)
        self.assertIn("setReversibleVhsText(heading, 'SPIEL BEENDET')", rendered)
        self.assertIn("setReversibleVhsText(label, 'PUNKTE')", rendered)
        self.assertIn("panel.classList.add('vhs-results-panel')", rendered)
        self.assertIn("function syncVhsLobby(shell)", rendered)
        self.assertIn("? 'SPIEL ' + nextGameNumber", rendered)
        self.assertIn("nextGameState === 'complete' ? 'KEINE WEITEREN SPIELE'", rendered)
        self.assertIn("appName, 'Lobby · ' + lobbyName", rendered)
        self.assertIn("title.textContent = 'PUNKTE PRO RUNDE'", rendered)
        self.assertIn('html[data-participant-theme="vhs"] .vhs-final-score', vhs_css)
        self.assertIn('html[data-participant-theme="vhs"] .vhs-results-panel', vhs_css)
        self.assertIn('html[data-participant-theme="vhs"] .vhs-theme-shell.vhs-lobby-shell', vhs_css)
        self.assertIn('html[data-participant-theme="vhs"] .vhs-lobby-status-panel', vhs_css)
        self.assertIn('html[data-participant-theme="vhs"] .vhs-lobby-share', vhs_css)
        self.assertIn('html[data-participant-theme="vhs"] .qa-score-widget .vhs-points-unit', vhs_css)
        self.assertIn("function parseVhsTimerValue(value)", rendered)
        self.assertIn("transition: transform 1s linear", vhs_css)
        self.assertNotIn("requestAnimationFrame(tick)", rendered)
        self.assertIn("scorePositionSelect.disabled = true", rendered)
        self.assertIn("scorePositionSelect.disabled = false", rendered)
        self.assertIn("font-size: clamp(3.2rem, 8vw, 5.6rem)", rendered)
        self.assertIn("font-weight: 400 !important", rendered)
        self.assertIn("#participant-options-menu-root option", rendered)
        self.assertNotIn("<strong>Optionen</strong>", rendered)
        self.assertIn("--participant-theme-text: #000", rendered)
        self.assertIn("--participant-accent-color: #a3a88d", rendered)
        self.assertIn("color: var(--participant-theme-text, #000) !important", rendered)
        self.assertIn(".quiz-info", rendered)
        self.assertIn("text-align: center !important", rendered)
        self.assertIn(".quiz-header .header-content", rendered)
        self.assertIn("overflow-wrap: anywhere", rendered)
        self.assertIn(".quiz-header {", rendered)
        self.assertIn("padding: 0.45rem 0 0.2rem !important", rendered)
        self.assertIn("background: transparent !important", rendered)
        self.assertIn("border-bottom: 0 !important", rendered)
        self.assertIn("box-shadow: none !important", rendered)
        self.assertIn("top: auto !important", rendered)
        self.assertIn("grid-template-columns: minmax(2.5rem, 1fr) minmax(0, auto) minmax(2.5rem, 1fr)", rendered)
        self.assertIn("min-height: 0", rendered)
        self.assertIn("row-gap: 0.16rem", rendered)
        self.assertIn("position: relative", rendered)
        self.assertIn("flex-direction: column !important", rendered)
        self.assertIn("grid-row: 1", rendered)
        self.assertIn("max-width: min(38rem, calc(100vw - 8rem))", rendered)
        self.assertIn("padding: 0.58rem 1.35rem 0.66rem", rendered)
        self.assertIn("border: 2px solid var(--participant-theme-text, #000)", rendered)
        self.assertIn("border-radius: 999px", rendered)
        self.assertIn("background: var(--participant-effective-accent-color, var(--participant-accent-color, #a3a88d))", rendered)
        self.assertIn(".quiz-header .quiz-title", rendered)
        self.assertIn("display: block", rendered)
        self.assertIn("font-size: clamp(1.75rem, 5.4vw, 3.05rem) !important", rendered)
        self.assertIn("grid-column: 2", rendered)
        self.assertIn("grid-row: 2", rendered)
        self.assertIn(".quiz-header .participant-info", rendered)
        self.assertIn("position: static", rendered)
        self.assertIn("background: transparent", rendered)
        self.assertIn("overflow: visible", rendered)
        self.assertIn("text-align: center", rendered)
        self.assertIn(".quiz-header .participant-avatar", rendered)
        self.assertIn("display: none !important", rendered)
        self.assertIn(".participant-name", rendered)
        self.assertIn("font-size: clamp(0.9rem, 1.55vw, 1.08rem) !important", rendered)
        self.assertIn("text-overflow: ellipsis", rendered)
        self.assertNotIn("content: \"Du:\"", rendered)
        self.assertNotIn("transform: rotate(-90deg)", rendered)
        self.assertIn(".session-game-number", rendered)
        self.assertIn("display: block !important", rendered)
        self.assertIn("font-size: clamp(1rem, 2.45vw, 1.42rem) !important", rendered)
        self.assertIn("margin-top: 0.04rem !important", rendered)
        self.assertIn(".qa-start-waiting-subtitle", rendered)
        self.assertIn("margin-top: 0.25rem !important", rendered)
        self.assertIn(".waiting-card", rendered)
        self.assertIn(".question-card", rendered)
        self.assertIn("--participant-frame-color: var(--participant-theme-text, #000)", rendered)
        self.assertIn("--participant-frame-gap-color: var(--participant-theme-background, #d0d6b4)", rendered)
        self.assertIn("border: 3px solid var(--participant-frame-color) !important", rendered)
        self.assertIn("0 0 0 8px var(--participant-frame-color) !important", rendered)
        self.assertIn("--participant-frame-color: var(--participant-effective-text-color)", rendered)
        self.assertIn("--participant-frame-gap-color: var(--participant-effective-bg-color)", rendered)
        self.assertIn("border-color: var(--participant-frame-color) !important", rendered)
        self.assertIn(".waiting-card::before", rendered)
        self.assertIn("opacity: .32", rendered)
        self.assertIn("font-family: var(--participant-body-font", rendered)
        self.assertIn(".score-box__", rendered)
        self.assertIn(".qa-score-widget", rendered)
        self.assertIn("width: auto", rendered)
        self.assertIn("max-width: calc(100vw - 2rem)", rendered)
        self.assertIn(".qa-score-widget--bottom-left", rendered)
        self.assertIn(".qa-score-widget--top-left", rendered)
        self.assertIn(".qa-score-widget--top-right", rendered)
        self.assertIn(".qa-score-widget--bottom-right", rendered)
        self.assertIn("bottom: calc(max(1rem, env(safe-area-inset-bottom)) + 4.25rem)", rendered)
        self.assertIn(".qa-score-widget__toggle", rendered)
        self.assertIn("z-index: 2", rendered)
        self.assertIn("qa-score-widget__panel", rendered)
        self.assertIn(".qa-score-widget__body", rendered)
        self.assertIn("position: absolute", rendered)
        self.assertIn("width: max-content", rendered)
        self.assertIn("max-width: min(16rem, calc(100vw - 2rem))", rendered)
        self.assertIn("max-height: min(60vh, 28rem)", rendered)
        self.assertIn(".qa-score-widget--bottom-left .qa-score-widget__body", rendered)
        self.assertIn("bottom: calc(100% + 0.45rem)", rendered)
        self.assertIn(".qa-score-widget--top-left .qa-score-widget__body", rendered)
        self.assertIn("top: calc(100% + 0.45rem)", rendered)
        self.assertIn(".qa-score-widget--top-right .qa-score-widget__body", rendered)
        self.assertIn(".qa-score-widget--bottom-right .qa-score-widget__body", rendered)
        self.assertIn(".qa-score-widget.is-collapsed", rendered)
        self.assertIn(".qa-score-widget .score-box", rendered)
        self.assertIn("position: static !important", rendered)
        self.assertIn(".qa-score-widget .score-box__title", rendered)
        self.assertIn(".qa-score-widget .scorebox > .fw-bold:first-child", rendered)
        self.assertIn("display: none !important", rendered)
        self.assertIn("data-participant-theme", rendered)
        self.assertIn("html[data-participant-theme=\"arcade\"]", rendered)
        self.assertIn("QA Arcade Heading", rendered)
        self.assertIn("QA Arcade Text", rendered)
        self.assertIn("ka1.ttf", rendered)
        self.assertIn("RETROTECH.ttf", rendered)
        self.assertIn("--arcade-scanline-color: rgba(0, 0, 0, 0.16)", rendered)
        self.assertIn("--arcade-scanline-highlight: rgba(255, 255, 255, 0.025)", rendered)
        self.assertIn("--arcade-scanline-spacing: 4px", rendered)
        self.assertIn("--arcade-vignette-strength: rgba(0, 0, 0, 0.18)", rendered)
        self.assertIn('html[data-participant-theme="arcade"] body::before', rendered)
        self.assertIn("repeating-linear-gradient(", rendered)
        self.assertIn("var(--arcade-scanline-color) 0", rendered)
        self.assertIn("background-repeat: no-repeat, repeat !important", rendered)
        self.assertIn('html[data-participant-theme="arcade"] .waiting-card::before', rendered)
        self.assertIn("background-image: none !important", rendered)
        self.assertIn('html[data-participant-theme="arcade"][data-participant-high-contrast="true"] body::before', rendered)
        self.assertIn("data-participant-colors-applied", rendered)
        self.assertIn("data-participant-high-contrast", rendered)
        self.assertIn("participant_interface_font_scale", rendered)
        self.assertIn("participant_interface_theme", rendered)
        self.assertIn("THEMES = ['standard', 'arcade', 'vhs']", rendered)
        self.assertIn("participant_interface_score_position", rendered)
        self.assertIn("participant_interface_score_collapsed", rendered)
        self.assertIn("participant_interface_custom_colors_enabled", rendered)
        self.assertIn("participant_interface_background_color", rendered)
        self.assertIn("participant_interface_text_color", rendered)
        self.assertIn("participant_interface_accent_color", rendered)
        self.assertIn("participant_interface_accent_color_2", rendered)
        self.assertIn("participant_interface_high_contrast", rendered)
        self.assertIn("participant_interface_invert_colors", rendered)
        self.assertIn("invertHexColor", rendered)
        self.assertIn("applyColorPreferences", rendered)
        self.assertIn("findScoreElement", rendered)
        self.assertIn("ensureScoreWidget", rendered)
        self.assertIn("applyScoreWidgetPosition", rendered)
        self.assertIn("applyScoreWidgetCollapsed", rendered)
        self.assertIn("observeScoreWidgetTarget", rendered)
        self.assertIn("DEFAULT_SCORE_POSITION = 'bottom-left'", rendered)
        self.assertIn("SCORE_POSITIONS = ['bottom-left', 'top-left', 'top-right', 'bottom-right']", rendered)
        self.assertIn("scoreWidgetButton.textContent = 'Punkte'", rendered)
        self.assertIn("scoreWidgetBody.className = 'qa-score-widget__panel qa-score-widget__body'", rendered)
        self.assertIn("document.querySelectorAll('.score-box, .scorebox')", rendered)
        self.assertIn("document.getElementById('ownScore')", rendered)
        self.assertIn("ownScore.closest('.score-card, .metric')", rendered)
        self.assertIn("html[data-participant-high-contrast=\"true\"] body::before", rendered)
        self.assertIn('html[data-participant-high-contrast="true"] .waiting-card::before', rendered)
        self.assertIn("removeStoredValue(LS_CUSTOM_COLORS_ENABLED)", rendered)
        self.assertIn("removeStoredValue(LS_SCORE_POSITION)", rendered)
        self.assertIn("removeStoredValue(LS_SCORE_COLLAPSED)", rendered)
        self.assertIn("removeStoredValue(LS_ACCENT_COLOR)", rendered)
        self.assertIn("removeStoredValue(LS_ACCENT_COLOR_2)", rendered)
        self.assertIn("removeStoredValue(LS_HIGH_CONTRAST)", rendered)
        self.assertIn("removeStoredValue(LS_INVERT_COLORS)", rendered)
        self.assertIn("window.localStorage", rendered)
        self.assertNotIn("filter: invert", rendered)
        self.assertNotIn("a11y-font-down", rendered)
        self.assertNotIn("a11y-font-up", rendered)
        self.assertNotIn("a11y-contrast-btn", rendered)

    def test_custom_color_toggle_controls_accent_with_background_and_text(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn("accentColorInput.disabled = !enabled", rendered)
        self.assertIn("accentColor2Input.disabled = !enabled", rendered)
        self.assertIn("accentColorInput.closest('.participant-color-field').classList.toggle('is-disabled', !enabled)", rendered)
        self.assertIn("function clearAppliedColorVariables()", rendered)
        self.assertIn("document.documentElement.style.removeProperty(variableName)", rendered)
        self.assertIn("clearAppliedColorVariables();", rendered)
        self.assertIn("if (preferences.customColorsEnabled) {", rendered)
        self.assertIn("accentColor = preferences.accentColor", rendered)
        self.assertIn("document.documentElement.style.setProperty('--participant-accent-color', preferences.accentColor)", rendered)
        self.assertIn("document.documentElement.style.setProperty('--participant-accent-color-2', preferences.accentColor2)", rendered)
        self.assertIn("if (accentColorInput.disabled) return", rendered)
        self.assertIn("if (accentColor2Input.disabled) return", rendered)

        clear_index = rendered.index("clearAppliedColorVariables();")
        theme_accent_index = rendered.index("var accentColor = getThemeColor('--participant-accent-color'", clear_index)
        custom_accent_index = rendered.index("accentColor = preferences.accentColor", theme_accent_index)
        self.assertLess(clear_index, theme_accent_index)
        self.assertLess(theme_accent_index, custom_accent_index)
        self.assertIn("accentColor2 = preferences.accentColor2", rendered)
        self.assertIn("accentColor2 = invertHexColor(accentColor2)", rendered)
        self.assertIn("accentColor2 = textColor", rendered)

        unconditional_accent_assignment = rendered[
            rendered.index("function applyColorPreferences()"):
            rendered.index("function resetInterfaceOptions()")
        ].split("if (preferences.customColorsEnabled) {", 1)[0]
        self.assertNotIn("accentColor = preferences.accentColor", unconditional_accent_assignment)

    def test_interface_panel_toggle_and_compact_color_controls(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertEqual(rendered.count(">Interface</button>"), 1)
        self.assertNotIn("participant-options-panel-title", rendered)
        self.assertNotIn("Gilt nur für dieses Gerät", rendered)
        self.assertIn('aria-expanded="false" aria-controls="participant-options-interface-panel"', rendered)
        self.assertIn("function setInterfacePanelOpen(open)", rendered)
        self.assertIn("setInterfacePanelOpen(interfacePanel.hidden)", rendered)
        self.assertIn("interfaceButton.setAttribute('aria-expanded', open ? 'true' : 'false')", rendered)
        self.assertIn("Eigene Farben", rendered)
        self.assertNotIn("Eigene Farben aktivieren", rendered)

        for label in ("Hintergrund", "Schrift", "Akzentfarbe 1", "Akzentfarbe 2"):
            with self.subTest(color_label=label):
                self.assertIn(f'aria-label="{label}"', rendered)
                self.assertIn(f'aria-hidden="true">{label}</span>', rendered)

        self.assertIn("grid-template-columns: repeat(4, 2.5rem)", rendered)
        self.assertIn("border-radius: 50%", rendered)
        self.assertIn(".participant-color-field:focus-within .participant-color-tooltip", rendered)
        self.assertIn(".participant-color-field:active .participant-color-tooltip", rendered)

    def test_arcade_overlay_and_pixel_frames_are_theme_scoped(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        overlay_start = rendered.index('html[data-participant-theme="arcade"] body::before')
        overlay_end = rendered.index("}", overlay_start)
        overlay_rule = rendered[overlay_start:overlay_end]
        self.assertIn("pointer-events: none", overlay_rule)
        self.assertIn("z-index: 99998", overlay_rule)
        self.assertIn('html[data-participant-theme="arcade"] :where(', rendered)
        self.assertIn("--arcade-frame-color: var(--participant-effective-text-color", rendered)
        self.assertIn("border-radius: 0 !important", rendered)
        self.assertIn("2px 0 0 var(--arcade-frame-color)", rendered)
        self.assertIn(".quiz-header .quiz-info", rendered)
        self.assertIn(".draggable-item", rendered)
        self.assertIn(".drop-zone", rendered)
        self.assertIn(".qa-score-widget__panel", rendered)
        self.assertNotIn(".modal-backdrop", rendered)

    def test_participant_arcade_theme_uses_existing_theme_key_and_loader_pool(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn("var LS_THEME = 'participant_interface_theme';", rendered)
        self.assertIn("var ARCADE_THEME = 'arcade';", rendered)
        self.assertIn("var arcadeLoaderVariants = [", rendered)
        self.assertIn("qaParticipantWaitLoaders", rendered)
        self.assertIn("qaWaitLoaderTheme", rendered)
        self.assertIn("clearWaitLoaderVariantClasses", rendered)
        self.assertIn("applyRandomLoaderVariant(loader, force)", rendered)
        self.assertIn(
            "if ([DEFAULT_THEME, ARCADE_THEME, VHS_THEME].indexOf(datasetTheme) !== -1) return datasetTheme;",
            rendered,
        )
        self.assertIn("previousTheme && previousTheme !== normalized", rendered)
        self.assertIn("content: \"Loading\";", rendered)
        self.assertIn("@media (prefers-reduced-motion: reduce)", rendered)
        self.assertIn("animation: none !important", rendered)
        self.assertIn("qa-wait-loader-stage", rendered)
        self.assertNotIn("participant_interface_arcade_theme", rendered)
        self.assertNotRegex(rendered, r"(?m)^\\.loader\\s*\\{")

        for index in range(1, 11):
            variant = f"qa-arcade-wait-loader-{index:02d}"
            with self.subTest(arcade_loader=variant):
                self.assertIn(variant, rendered)
                self.assertIn(f"qa-arcade-loader-{index:02d}", rendered)

    def test_participant_question_and_timer_banners_use_accent_color(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn("body.quiz-play-page .question-header", rendered)
        self.assertIn("body.assign-play-page .question-header", rendered)
        self.assertIn("body.where-play-page .question-header", rendered)
        self.assertIn("body.who-that-play-page .question-header", rendered)
        self.assertIn("body.who-play-page .question-header", rendered)
        self.assertIn("body.blackjack-play-page .question-header", rendered)
        self.assertIn(".wwm-card .timer-box", rendered)
        self.assertIn(".buzzer-card .status-pill", rendered)
        self.assertIn(".points-card #gameArea .metric", rendered)
        self.assertIn(".game-card #questionArea .metric", rendered)
        self.assertIn("background: var(--participant-effective-accent-color, var(--participant-accent-color, #a3a88d))", rendered)
        self.assertIn("border-color: var(--participant-effective-text-color, var(--participant-theme-text, currentColor))", rendered)
        self.assertIn("body.quiz-play-page .question-header .timer-circle:not(.warning):not(.danger)", rendered)
        self.assertNotIn(".modal-backdrop", rendered)
        self.assertNotIn(".modal ", rendered)

    def test_quick_quiz_true_false_and_submit_button_are_localized_without_value_changes(self):
        content = read_text("templates/quiz/play.html")
        vhs_css = read_text("static/themes/vhs/vhs.css")

        self.assertIn("createMultipleChoiceOption('True', 'Stimmt', { singleLabel: true })", content)
        self.assertIn("createMultipleChoiceOption('False', 'Stimmt nicht', { singleLabel: true })", content)
        self.assertIn("answer-option-single-label", content)
        self.assertIn("quick-quiz-answer--boolean", content)
        self.assertIn('class="question-content quick-quiz-layout"', content)
        self.assertIn('class="answer-options quick-quiz-answer-list"', content)
        self.assertIn('class="quick-quiz-submit-row"', content)
        self.assertIn("vhs-quick-quiz-submit", content)
        self.assertIn("submitButton.classList.add('is-selected')", content)
        self.assertIn("submitButton.setAttribute('aria-pressed', 'true')", content)
        self.assertIn("this.selectedAnswer = key", content)
        self.assertIn("submittedAnswerDisplay = answerPayload === 'True' ? 'Stimmt' : 'Stimmt nicht';", content)
        self.assertNotIn("createMultipleChoiceOption('True', 'True')", content)
        self.assertNotIn("createMultipleChoiceOption('False', 'False')", content)
        self.assertNotIn("Submit Answer", content)
        self.assertIn("Einloggen", content)
        self.assertIn("grid-template-columns: minmax(240px, 1fr) minmax(320px, 500px);", vhs_css)
        self.assertIn("grid-template-columns: 48px minmax(0, 1fr);", vhs_css)
        self.assertIn(".quick-quiz-answer--boolean", vhs_css)
        self.assertIn("@media (max-width: 1050px)", vhs_css)
        self.assertIn(".vhs-quick-quiz-submit:not(:disabled):hover", vhs_css)
        self.assertIn(".vhs-quick-quiz-submit.is-selected", vhs_css)

    def test_participant_answer_submit_buttons_use_consistent_text_and_accent_style(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn(".submit-answer,", rendered)
        self.assertIn("#logRoundBtn,", rendered)
        self.assertIn(".wwm-card #submitBtn,", rendered)
        self.assertIn(".game-card #submitBtn", rendered)
        self.assertIn("width: auto !important", rendered)
        self.assertIn("background: var(--participant-effective-accent-color, var(--participant-accent-color, #a3a88d)) !important", rendered)
        self.assertIn(".submit-answer > svg", rendered)
        self.assertIn("display: none !important", rendered)

        submit_templates = [
            "templates/quiz/play.html",
            "templates/where_is_this/play.html",
            "templates/clue_rush/play.html",
            "templates/black_jack_quiz/play.html",
            "templates/who_is_that/play.html",
            "templates/assign/play.html",
            "templates/wer_weiss_mehr/play.html",
            "templates/wann_war_das/play.html",
        ]
        for relative_path in submit_templates:
            with self.subTest(template=relative_path):
                content = read_text(relative_path)
                self.assertIn("Einloggen", content)
                self.assertNotIn("Submit Location", content)
                self.assertNotIn('data-lucide="send"', content)

        self.assertNotIn("Antwort einloggen", read_text("templates/assign/play.html"))
        self.assertNotIn("Antwort abgeben", read_text("templates/wann_war_das/play.html"))

    def test_assign_round_progress_is_integrated_into_single_top_banner(self):
        content = read_text("templates/assign/play.html")

        self.assertEqual(content.count('id="roundProgressBar"'), 1)

        header_start = content.index('<div class="question-header">')
        question_content_start = content.index("<!-- Question Content -->")
        header_markup = content[header_start:question_content_start]

        self.assertIn('id="questionNumberDisplay"', header_markup)
        self.assertIn('id="roundProgressBar"', header_markup)
        self.assertIn('id="roundDots"', header_markup)
        self.assertLess(
            header_markup.index('id="questionNumberDisplay"'),
            header_markup.index('id="roundProgressBar"'),
        )
        self.assertIn(".question-round-status", content)
        self.assertIn(".round-progress {", content)
        self.assertIn("background: transparent;", content)
        self.assertIn("border: 0;", content)
        self.assertNotIn("Runden-Fortschritt (nur im rundenbasierten Modus)", content)

    def test_assign_dropzones_keep_complete_borders_and_items_are_compact(self):
        content = read_text("templates/assign/play.html")

        self.assertIn(".left-items, .drop-zones, .matched-panel", content)
        self.assertIn("overflow: visible;", content)
        self.assertIn("min-height: 52px;", content)
        self.assertIn("padding: 0.7rem 0.85rem;", content)
        self.assertIn("font-size: 0.95rem;", content)
        self.assertIn("box-sizing: border-box;", content)
        self.assertIn("flex-direction: column;", content)
        self.assertIn(".drop-zone-label {", content)
        self.assertIn("position: static;", content)
        self.assertIn("background: transparent;", content)
        self.assertIn("background: var(--participant-effective-bg-color, var(--participant-theme-background, var(--bg-secondary)))", content)
        self.assertIn("border: 2px dashed var(--participant-effective-text-color, var(--participant-theme-text, var(--border-color)))", content)
        self.assertIn("border-color: var(--participant-effective-text-color, var(--participant-theme-text, var(--border-color)))", content)
        self.assertIn("color: var(--participant-effective-text-color, var(--participant-theme-text, var(--text-primary)))", content)
        self.assertIn("background: var(--participant-effective-accent-color, var(--participant-accent-color, var(--bg-primary)))", content)
        self.assertIn("background: var(--participant-effective-bg-color, var(--participant-theme-background, var(--bg-primary)))", content)
        self.assertIn("background: var(--participant-effective-accent-color, var(--participant-accent-color, var(--secondary-100)))", content)

    def test_vhs_assign_uses_fixed_drop_slots_and_scoped_workspace_rules(self):
        content = read_text("templates/assign/play.html")
        vhs_css = read_text("static/themes/vhs/vhs.css")
        widget = read_text("templates/includes/accessibility_widget.html")

        self.assertIn('class="drag-drop-interface assign-workspace"', content)
        self.assertIn('class="drag-drop-container assign-workspace-grid"', content)
        self.assertIn('class="assign-action-row"', content)
        self.assertIn('class="btn btn-outline-secondary d-none assign-reset-button"', content)
        self.assertIn('class="btn btn-primary d-none assign-submit-button"', content)
        workspace_start = content.index('class="drag-drop-interface assign-workspace"')
        workspace_end = content.index('<!-- Warte-Nachricht nach Runden-Submit -->')
        workspace_markup = content[workspace_start:workspace_end]
        self.assertIn('id="resetRoundBtn"', workspace_markup)
        self.assertIn('id="logRoundBtn"', workspace_markup)
        self.assertEqual(content.count('id="resetRoundBtn"'), 1)
        self.assertEqual(content.count('id="logRoundBtn"'), 1)
        self.assertIn('class="left-items assign-source-panel"', content)
        self.assertIn('class="drop-zones assign-target-panel"', content)
        self.assertIn("div.className = 'draggable-item assign-item';", content)
        self.assertIn("dropZone.className = 'drop-zone assign-target';", content)
        self.assertIn("label.className = 'drop-zone-label assign-target__label';", content)
        self.assertIn("slot.className = 'drop-zone-slot assign-target__dropzone';", content)
        self.assertEqual(content.count("droppedItem.className = 'dropped-item assign-item';"), 2)
        self.assertIn('slot.appendChild(droppedItem);', content)
        self.assertIn("const dropSlot = dropZone.querySelector('.drop-zone-slot');", content)
        self.assertIn('(dropSlot || dropZone).appendChild(droppedItem);', content)
        self.assertEqual(content.count("slot.className = 'drop-zone-slot assign-target__dropzone';"), 1)
        self.assertIn('class="assign-round-status d-none', content)
        self.assertNotIn('style="color: var(--success-color);"', content)

        assign_scope = 'html[data-participant-theme="vhs"] body.assign-play-page'
        self.assertIn(f'{assign_scope} .vhs-theme-shell', vhs_css)
        self.assertIn('#questionState .assign-workspace-grid', vhs_css)
        self.assertIn('#questionState .assign-action-row', vhs_css)
        self.assertIn('min-height: 54px;', vhs_css)
        self.assertIn('.qa-theme-button-shell:has(> .assign-submit-button)', vhs_css)
        self.assertIn('grid-template-columns: repeat(2, minmax(270px, 1fr));', vhs_css)
        self.assertIn(':is(.assign-item, .assign-target__label, .assign-target__dropzone)', vhs_css)
        self.assertIn('--assign-box-width: 130px;', vhs_css)
        self.assertIn('--assign-box-height: 58px;', vhs_css)
        self.assertIn('grid-template-columns: repeat(auto-fit, var(--assign-box-width));', vhs_css)
        self.assertIn('.items-list:is([data-item-count="2"], [data-item-count="3"], [data-item-count="4"])', vhs_css)
        self.assertIn('leftItemsList.dataset.itemCount = String(availableLeft.length);', content)
        self.assertIn('zonesList.dataset.itemCount = String(normRight.length);', content)
        self.assertIn("'assign-box-text--long'", content)
        self.assertIn("String(text || '').trim().length > 24", content)
        self.assertIn('#questionState .draggable-item.matched', vhs_css)
        self.assertIn('display: none;', vhs_css)
        self.assertIn('grid-template-rows: repeat(2, 58px);', vhs_css)
        self.assertIn('#questionState .drop-zone-slot', vhs_css)
        self.assertIn('border: 0 !important;', vhs_css)
        self.assertIn('background: rgba(233, 223, 202, 0.9) !important;', vhs_css)
        self.assertIn('border: 1px solid rgba(216, 216, 209, 0.64) !important;', vhs_css)
        self.assertNotIn('border: 1px dashed rgba(216, 216, 209, 0.55) !important;', vhs_css)
        self.assertIn('@media (max-width: 1280px)', vhs_css)
        self.assertIn('#questionState .assign-round-status', vhs_css)
        self.assertNotIn('html[data-participant-theme="standard"] body.assign-play-page', vhs_css)
        self.assertNotIn('html[data-participant-theme="arcade"] body.assign-play-page', vhs_css)

        self.assertIn("shell.querySelectorAll('#questionState .assign-workspace')", widget)
        self.assertIn('assignResponse.parentNode.insertBefore(widget, assignResponse);', widget)

    def test_vhs_assign_reveal_uses_one_solved_matrix_and_existing_result_state(self):
        content = read_text("templates/assign/play.html")
        vhs_css = read_text("static/themes/vhs/vhs.css")

        self.assertEqual(content.count('id="assignVhsSolutionMatrix"'), 1)
        self.assertEqual(content.count('id="assignVhsSolutionTimeout"'), 1)
        self.assertIn('class="assign-legacy-solution row g-3 mt-1"', content)
        self.assertIn("vhsMatrix.replaceChildren();", content)
        self.assertIn("this.vhsRevealAnswers = {};", content)
        self.assertIn("this.vhsRevealAnswers[this._pendingHistoryEntry.leftIdx]", content)
        self.assertIn("correct: Boolean(data.is_correct)", content)
        self.assertIn("Object.keys(vhsParticipantAnswers).length === 0", content)
        self.assertIn("(this.eliminated && !this._wrongEntry)", content)
        self.assertIn("pair.dataset.result = 'correct';", content)
        self.assertIn("pair.dataset.result = 'wrong';", content)
        self.assertIn("pair.dataset.result = 'neutral';", content)
        self.assertIn("source.textContent = leftItem;", content)
        self.assertIn("target.textContent = correctRightText;", content)
        self.assertNotIn("assignVhsSolutionMatrix.innerHTML", content)

        assign_scope = 'html[data-participant-theme="vhs"] body.assign-play-page'
        self.assertIn(f'{assign_scope} .vhs-theme-shell', vhs_css)
        self.assertIn('#solutionState .assign-legacy-solution', vhs_css)
        self.assertIn('#solutionState .assign-vhs-solution', vhs_css)
        self.assertIn('--assign-reveal-correct: #789174;', vhs_css)
        self.assertIn('--assign-reveal-wrong: #a45f52;', vhs_css)
        self.assertIn('#solutionState .assign-reveal-pair--correct .assign-reveal-box', vhs_css)
        self.assertIn('#solutionState .assign-reveal-pair--wrong .assign-reveal-box', vhs_css)
        self.assertNotIn('html[data-participant-theme="standard"] body.assign-play-page', vhs_css)
        self.assertNotIn('html[data-participant-theme="arcade"] body.assign-play-page', vhs_css)

    def test_participant_theme_button_effects_are_scoped_and_use_local_mask(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn(".qa-theme-button-shell", rendered)
        self.assertIn(".qa-theme-button__label", rendered)
        self.assertIn("--participant-button-clicked-color: color-mix(", rendered)
        self.assertIn('html[data-participant-theme="standard"] .qa-theme-button-shell::after', rendered)
        self.assertIn("border: 4px solid var(--participant-effective-text-color", rendered)
        self.assertIn("border-radius: var(--qa-theme-button-radius", rendered)
        self.assertIn("top left / 0 var(--qa-theme-frame-depth) no-repeat", rendered)
        self.assertIn("bottom left / 0 var(--qa-theme-frame-depth) no-repeat", rendered)
        self.assertIn("var(--qa-theme-frame-depth) 100%", rendered)
        self.assertIn('-webkit-mask-image: url("/static/nature-sprite.png")', rendered)
        self.assertIn("qa-standard-button-mask-open", rendered)
        self.assertIn("qa-standard-button-mask-close", rendered)
        self.assertNotIn("raw.githubusercontent.com/robin-dela", rendered)

        mask_path = REPO_ROOT / "static" / "nature-sprite.png"
        self.assertTrue(mask_path.is_file())
        self.assertGreater(mask_path.stat().st_size, 100_000)
        self.assertEqual(mask_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_arcade_theme_buttons_use_fixed_shadow_and_exact_pixel_bands(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn('html[data-participant-theme="arcade"] .qa-theme-button-shell::before', rendered)
        self.assertIn("background: #000", rendered)
        self.assertIn("transform: translate(-2px, -2px)", rendered)
        self.assertIn("transition: transform 90ms steps(2, end)", rendered)
        self.assertIn("transition: transform 420ms steps(10, end)", rendered)
        self.assertIn("background-color: var(--participant-effective-accent-color-2", rendered)
        self.assertIn("background-color: var(--participant-effective-accent-color", rendered)
        self.assertIn("fill='%23000'", rendered)
        self.assertNotIn("fill='%23f24b3d'", rendered)
        self.assertNotIn("fill='%231a9bd7'", rendered)
        self.assertIn("shape-rendering='crispEdges'", rendered)
        self.assertIn("transform: translate(-118%, -118%)", rendered)
        self.assertIn("transform: translate(118%, 118%)", rendered)
        self.assertIn(".qa-theme-button--answer.is-theme-clicked:not(.loading)::before", rendered)
        self.assertIn(".qa-theme-button--answer.is-theme-clicked:not(.loading)::after", rendered)
        self.assertIn(".qa-theme-button--answer.is-selected:not(.loading)::before", rendered)
        self.assertIn(".qa-theme-button--answer.is-selected:not(.loading)::after", rendered)

    def test_theme_button_runtime_preserves_actions_and_excludes_non_game_controls(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn("button.classList.add('qa-theme-button')", rendered)
        self.assertIn("button.parentNode.insertBefore(shell, button)", rendered)
        self.assertIn("shell.appendChild(button)", rendered)
        self.assertIn("button.classList.toggle('qa-theme-button--answer', isAnswerButton)", rendered)
        self.assertIn("button.classList.toggle('qa-theme-button--action', !isAnswerButton)", rendered)
        self.assertIn("clearActionThemeButtonState(button)", rendered)
        self.assertIn("playActionThemeClick(themeButton)", rendered)
        self.assertNotIn("themeButton.classList.toggle('is-theme-clicked')", rendered)
        self.assertIn("shell.style.setProperty('--qa-theme-button-radius', buttonRadius)", rendered)
        self.assertIn("syncThemeButtonGeometry(document.body)", rendered)
        self.assertIn("!themeButton.disabled", rendered)
        self.assertIn("themeButton.getAttribute('aria-disabled') !== 'true'", rendered)
        self.assertIn("observeThemeButtons()", rendered)
        self.assertIn("'#participant-options-menu-root'", rendered)
        self.assertIn("'.qa-score-widget'", rendered)
        self.assertIn("'[draggable=\"true\"]'", rendered)
        self.assertIn('[aria-grabbed="true"]', rendered)
        self.assertIn(".is-dragging", rendered)
        self.assertIn("'.slot-clear-btn'", rendered)
        self.assertNotIn("preventDefault()", rendered[rendered.index("function isThemeButtonCandidate"):])
        self.assertNotIn("stopPropagation()", rendered[rendered.index("function isThemeButtonCandidate"):])
        theme_button_runtime = rendered[rendered.index("function isThemeButtonCandidate"):]
        self.assertIn("button.removeAttribute('aria-pressed')", theme_button_runtime)
        self.assertNotIn("button.setAttribute('aria-pressed'", theme_button_runtime)

    def test_quick_quiz_single_choice_owns_visual_selection_and_real_deselection(self):
        content = read_text("templates/quiz/play.html")
        selection_start = content.index("setSelectedAnswerOption(option, key) {")
        selection_end = content.index("startQuestionTimer(timeLimit)", selection_start)
        selection = content[selection_start:selection_end]

        self.assertIn("const wasSelected = option.classList.contains('selected')", selection)
        self.assertIn("answerOption.classList.remove('selected', 'is-theme-clicked')", selection)
        self.assertIn("answerOption.setAttribute('aria-pressed', 'false')", selection)
        self.assertIn("this.selectedAnswer = null", selection)
        self.assertIn("option.classList.add('selected', 'is-theme-clicked')", selection)
        self.assertIn("option.setAttribute('aria-pressed', 'true')", selection)
        self.assertIn("this.selectedAnswer = key", selection)
        self.assertIn("disabled = !this.selectedAnswer", selection)
        self.assertIn("option.dataset.qaAnswerSelectable = 'true'", content)
        self.assertNotIn("border-color: var(--color-primary)", content[content.index(".answer-option {"):content.index(".option-key {")])
        self.assertNotIn("color: var(--primary-700)", content[content.index(".answer-option {"):content.index(".option-key {")])

    def test_score_widget_source_layout_becomes_centered_participant_stage(self):
        rendered = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )

        self.assertIn(".qa-participant-stage {", rendered)
        self.assertIn("grid-template-columns: minmax(0, var(--qa-participant-stage-width)) !important", rendered)
        self.assertIn("justify-content: center !important", rendered)
        self.assertIn(".qa-participant-stage__content", rendered)
        self.assertIn(".qa-participant-stage__aux", rendered)
        self.assertIn("function configureParticipantStage(scoreElement)", rendered)
        self.assertIn("scoreElement.closest('.assign-play-layout, .who-that-play-layout, .row')", rendered)
        self.assertIn("configureParticipantStage(scoreElement);", rendered)
        self.assertIn("--qa-participant-stage-width: 66.666667%", rendered)
        self.assertIn("--qa-participant-stage-width: 75%", rendered)
        self.assertIn("calc(100% - 260px - 1.5rem)", rendered)
        self.assertIn("calc(100% - 220px - 1.5rem)", rendered)

    def test_default_widget_keeps_admin_accessibility_bar(self):
        rendered = render_to_string("includes/accessibility_widget.html", {})

        self.assertIn("a11y-bar", rendered)
        self.assertIn("a11y-font-down", rendered)
        self.assertIn("a11y-font-up", rendered)
        self.assertIn("a11y-contrast-btn", rendered)
        self.assertNotIn("participant-options-menu-root", rendered)
        self.assertNotIn("participant_interface_font_scale", rendered)
        self.assertNotIn("participant_interface_score_position", rendered)

    def test_participant_pages_opt_into_options_menu(self):
        for relative_path in PARTICIPANT_WIDGET_TEMPLATES:
            with self.subTest(template=relative_path):
                content = read_text(relative_path)

                self.assertIn(
                    "{% include 'includes/accessibility_widget.html' with participant_options_menu=True %}",
                    content,
                )

        admin_base = read_text("templates/admin_dashboard/base.html")
        self.assertIn("{% include 'includes/accessibility_widget.html' %}", admin_base)
        self.assertNotIn("participant_options_menu=True", admin_base)


class ParticipantThemeButtonBrowserTests(unittest.TestCase):
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

    def test_vhs_result_page_uses_shared_shell_and_restores_legacy_labels(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <div class="result-container" data-participant-name="Mia">
            <div class="performance-card">
              <h1>Result</h1>
              <span class="score-label">points</span>
              <span class="badge bg-primary">New</span>
              <input id="legacyInput" placeholder="Type the person's name...">
              <div class="list-group-item">Result row</div>
            </div>
          </div>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(
            "http://participant.test/result",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='participant-result-page assign-result-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.goto("http://participant.test/result")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell > .result-container")

            self.assertEqual(page.locator("html").get_attribute("data-participant-theme"), "vhs")
            self.assertEqual(page.locator(".score-label").inner_text(), "PUNKTE")
            self.assertEqual(page.locator(".badge").inner_text(), "NEU")
            self.assertEqual(page.locator("#legacyInput").get_attribute("placeholder"), "Name eingeben...")
            self.assertEqual(page.locator(".performance-card").evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(page.locator(".performance-card").evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(page.locator(".vhs-theme-participant").inner_text(), "LIVE · MIA")

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator("body > .result-container").count(), 1)
            self.assertEqual(page.locator(".score-label").inner_text(), "points")
            self.assertEqual(page.locator(".badge").inner_text(), "New")
            self.assertEqual(page.locator("#legacyInput").get_attribute("placeholder"), "Type the person's name...")

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'arcade'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator("html").get_attribute("data-participant-theme"), "arcade")
            self.assertEqual(page.locator(".score-label").inner_text(), "points")

            page.reload()
            self.assertEqual(page.locator("html").get_attribute("data-participant-theme"), "arcade")

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell > .result-container")
            self.assertEqual(page.locator("html").get_attribute("data-participant-theme"), "vhs")
            self.assertEqual(page.locator(".score-label").inner_text(), "PUNKTE")
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_theme_button_enhancement_and_clicks_in_browser(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfÃ¼gbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        fixture = """
          <button id="gameAction" class="btn">Einloggen</button>
          <button id="submitAnswerBtn" class="btn submit-answer is-theme-clicked is-selected" aria-pressed="true">Einloggen</button>
          <button id="logRoundBtn" class="btn">Einloggen</button>
          <button id="submitRoundBtn" class="btn submit-answer">Einloggen</button>
          <button id="submitBtn" class="btn">Einloggen</button>
          <button id="buzzButton" class="buzzer-button">BUZZ</button>
          <button id="accuseLiarBtn" class="btn">lÃ¼gt</button>
          <button id="returnToLobbyBtn" class="btn">Zur Lobby zurÃ¼ckkehren</button>
          <button id="disabledAction" class="btn" disabled>Gesperrt</button>
          <div id="answerAction" class="answer-option"><span>Stimmt</span></div>
          <button id="dragAction" class="btn draggable-item" draggable="true">Ziehen</button>
          <aside class="qa-score-widget"><button id="scoreAction" class="btn">Punkte</button></aside>
        """
        context = self._browser.new_context()
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.set_content(f"<!doctype html><html><body>{fixture}{widget}</body></html>")
            page.wait_for_selector("#gameAction.qa-theme-button")

            self.assertEqual(page.locator("#gameAction").evaluate("el => el.parentElement.className"), "qa-theme-button-shell")
            self.assertEqual(page.locator("#answerAction").evaluate("el => el.parentElement.className"), "qa-theme-button-shell qa-theme-button-shell--block")
            self.assertEqual(page.locator("#gameAction > .qa-theme-button__label").inner_text(), "Einloggen")
            for button_id in (
                "submitAnswerBtn",
                "logRoundBtn",
                "submitRoundBtn",
                "submitBtn",
                "buzzButton",
                "accuseLiarBtn",
                "returnToLobbyBtn",
            ):
                with self.subTest(button_id=button_id):
                    self.assertEqual(page.locator(f"#{button_id}.qa-theme-button").count(), 1)
                    self.assertEqual(page.locator(f"#{button_id}.qa-theme-button--action").count(), 1)
            self.assertEqual(page.locator("#answerAction.qa-theme-button--answer").count(), 1)
            self.assertFalse(page.locator("#submitAnswerBtn").evaluate("el => el.classList.contains('is-theme-clicked')"))
            self.assertFalse(page.locator("#submitAnswerBtn").evaluate("el => el.classList.contains('is-selected')"))
            self.assertIsNone(page.locator("#submitAnswerBtn").get_attribute("aria-pressed"))
            self.assertEqual(page.locator("#dragAction.qa-theme-button").count(), 0)
            self.assertEqual(page.locator("#scoreAction.qa-theme-button").count(), 0)
            self.assertEqual(
                page.locator("#disabledAction").evaluate("el => getComputedStyle(el, '::before').content"),
                "none",
            )

            game_box = page.locator("#gameAction").bounding_box()
            shell_box = page.locator("#gameAction").locator("xpath=..").bounding_box()
            self.assertAlmostEqual(game_box["width"], shell_box["width"], delta=0.1)
            self.assertAlmostEqual(game_box["height"], shell_box["height"], delta=0.1)
            page.locator("#gameAction").hover()
            page.wait_for_timeout(380)
            standard_edge_sizes = page.locator("#gameAction").locator("xpath=..").evaluate(
                "el => getComputedStyle(el, '::after').webkitMaskSize || getComputedStyle(el, '::after').maskSize"
            )
            self.assertEqual(standard_edge_sizes.count("100%"), 4)
            self.assertEqual(
                page.locator("#gameAction").evaluate("el => getComputedStyle(el).transform"),
                "none",
            )

            page.evaluate("window.qaButtonClicks = 0; document.getElementById('gameAction').addEventListener('click', () => window.qaButtonClicks += 1)")
            page.locator("#gameAction").click()
            self.assertEqual(page.evaluate("window.qaButtonClicks"), 1)
            self.assertTrue(page.locator("#gameAction").evaluate("el => el.classList.contains('is-theme-clicked')"))
            page.locator("#gameAction").click()
            self.assertEqual(page.evaluate("window.qaButtonClicks"), 2)
            self.assertFalse(page.locator("#gameAction").evaluate("el => el.classList.contains('is-theme-clicked')"))

            page.locator("#disabledAction").evaluate("el => el.click()")
            self.assertFalse(page.locator("#disabledAction").evaluate("el => el.classList.contains('is-theme-clicked')"))
            page.locator("#answerAction").click()
            self.assertFalse(page.locator("#answerAction").evaluate("el => el.classList.contains('is-theme-clicked')"))

            page.evaluate("document.body.insertAdjacentHTML('afterbegin', '<button id=dynamicAction class=btn>Weiter</button>')")
            page.wait_for_selector("#dynamicAction.qa-theme-button")
            page.locator("#dynamicAction").evaluate("el => { el.textContent = 'Noch einmal'; }")
            page.wait_for_selector("#dynamicAction > .qa-theme-button__label")
            self.assertEqual(page.locator("#dynamicAction > .qa-theme-button__label").inner_text(), "Noch einmal")

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'arcade'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator("html").get_attribute("data-participant-theme"), "arcade")
            self.assertEqual(
                page.locator("#gameAction").locator("xpath=..").evaluate(
                    "el => getComputedStyle(el, '::before').backgroundColor"
                ),
                "rgb(0, 0, 0)",
            )
            page.locator("#gameAction").hover()
            page.wait_for_timeout(120)
            self.assertEqual(
                page.locator("#gameAction").evaluate("el => getComputedStyle(el).transform"),
                "matrix(1, 0, 0, 1, -2, -2)",
            )
            self.assertEqual(page.locator("#gameAction").evaluate("el => getComputedStyle(el).boxShadow"), "none")
            page.locator("#gameAction").click()
            self.assertFalse(page.locator("#gameAction").evaluate("el => el.classList.contains('is-theme-clicked')"))
            self.assertFalse(page.locator("#gameAction").evaluate("el => el.classList.contains('is-selected')"))
            self.assertIsNone(page.locator("#gameAction").get_attribute("aria-pressed"))
            self.assertEqual(
                page.locator("#gameAction").evaluate("el => getComputedStyle(el, '::before').content"),
                "none",
            )
            page.locator("#answerAction").evaluate("el => el.classList.add('is-theme-clicked')")
            page.wait_for_timeout(450)
            red_band = page.locator("#answerAction").evaluate("el => getComputedStyle(el, '::before').backgroundColor")
            blue_band = page.locator("#answerAction").evaluate("el => getComputedStyle(el, '::after').backgroundColor")
            self.assertEqual(red_band, "rgb(242, 75, 61)")
            self.assertEqual(blue_band, "rgb(163, 168, 141)")
            self.assertIn(
                "data:image/svg+xml",
                page.locator("#answerAction").evaluate("el => getComputedStyle(el, '::before').webkitMaskImage"),
            )
            page.locator("#answerAction").evaluate("el => el.classList.remove('is-theme-clicked')")
            self.assertEqual(
                page.locator("#gameAction").evaluate("el => getComputedStyle(el).transform"),
                "matrix(1, 0, 0, 1, -2, -2)",
            )
            page.mouse.move(0, 0)
            page.wait_for_timeout(120)
            self.assertEqual(
                page.locator("#gameAction").evaluate("el => getComputedStyle(el).transform"),
                "none",
            )
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_theme_runtime_uses_existing_question_score_and_timer_dom(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfügbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        score_rows = "".join(
            f'<div class="score-box__row"><span class="score-box__badge">{index}</span><span class="score-box__value">{index % 3}/{2 if index % 2 else 1}</span></div>'
            for index in range(1, 6)
        )
        fixture = f"""
          <title>Quiz - QuizMaster</title>
          <div class="play-container">
            <header class="quiz-header">
              <div class="quiz-info">
                <h1 class="quiz-title">Weltraum Quiz</h1>
                <span class="session-game-number">Spiel 4</span>
              </div>
              <span class="participant-name">Mia Müller</span>
            </header>
            <main class="quiz-main">
              <div class="container">
                <div class="row">
                  <div class="col-lg-8">
                    <div id="questionState" class="game-state">
                      <div class="question-card">
                        <div class="question-header">
                          <span id="currentQuestionNumber">1</span>
                          <span id="playerTimeLeft">18</span>
                        </div>
                        <div class="question-content">
                          <div class="question-text" id="questionText">Welcher Planet besitzt die meisten bekannten Monde?</div>
                          <div class="answer-options">
                            <div class="answer-option" data-qa-answer-selectable="true" aria-pressed="false">
                              <span class="option-key">A</span><span class="option-text">Jupiter</span>
                            </div>
                            <div class="answer-option selected" data-qa-answer-selectable="true" aria-pressed="true">
                              <span class="option-key">B</span><span class="option-text">Saturn</span>
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                  <aside class="score-box">
                    <h6 class="score-box__title">Punkte</h6>
                    <div class="score-box__list">{score_rows}</div>
                  </aside>
                </div>
              </div>
            </main>
          </div>
        """
        context = self._browser.new_context()
        context.route(
            "http://participant.test/",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.goto("http://participant.test/")
            page.add_style_tag(content=vhs_css)
            page.wait_for_selector("#participant-theme-select", state="attached")
            page.evaluate("localStorage.setItem('participant_interface_score_position', 'top-left')")
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell")

            self.assertEqual(page.locator("html").get_attribute("data-participant-theme"), "vhs")
            self.assertEqual(
                page.evaluate("localStorage.getItem('participant_interface_theme')"),
                "vhs",
            )
            self.assertEqual(page.locator(".vhs-question-lead").inner_text(), "WELCHER")
            self.assertEqual(
                page.locator(".vhs-question-body").inner_text(),
                "Planet besitzt die meisten bekannten Monde?",
            )
            self.assertEqual(
                page.locator("#questionText").get_attribute("data-vhs-full-question"),
                "Welcher Planet besitzt die meisten bekannten Monde?",
            )
            self.assertEqual(
                page.locator(".vhs-question-kicker").inner_text(),
                "WELTRAUM QUIZ · SPIEL 4",
            )
            self.assertEqual(page.locator(".vhs-theme-rec-text").inner_text(), "REC · FRAGE 01 / 05")
            self.assertEqual(page.locator(".vhs-theme-participant").inner_text(), "LIVE · MIA MÜLLER")
            self.assertEqual(page.locator(".vhs-theme-time").inner_text(), "00:00:18")
            self.assertEqual(
                page.locator(".vhs-theme-shell").evaluate("el => getComputedStyle(el, '::after').pointerEvents"),
                "none",
            )
            self.assertIn(
                "noise.png",
                page.locator(".vhs-theme-shell").evaluate("el => getComputedStyle(el, '::after').backgroundImage"),
            )
            self.assertIn("noise.png", page.locator("body").evaluate("el => getComputedStyle(el).backgroundImage"))

            self.assertEqual(page.locator(".qa-score-widget__toggle").inner_text(), "P")
            self.assertTrue(page.locator("#participant-score-position-select").is_disabled())
            page.locator("#participant-options-toggle").click()
            page.locator("[data-participant-options-panel='interface']").click()
            self.assertTrue(page.locator(".participant-score-position-vhs-hint").is_visible())
            page.locator("#participant-options-toggle").click()
            self.assertEqual(
                page.locator(".score-box__list").evaluate("el => el.style.getPropertyValue('--vhs-score-columns')"),
                "2",
            )
            self.assertEqual(
                page.locator(".score-box__list").evaluate("el => el.style.getPropertyValue('--vhs-score-rows')"),
                "3",
            )
            self.assertEqual(page.locator(".qa-score-widget__body").get_attribute("aria-hidden"), "false")
            page.locator(".qa-score-widget__toggle").click()
            self.assertEqual(page.locator(".qa-score-widget__toggle").get_attribute("aria-expanded"), "false")
            self.assertEqual(page.locator(".qa-score-widget__body").get_attribute("aria-hidden"), "true")
            page.locator(".qa-score-widget__toggle").click()
            self.assertEqual(page.locator(".qa-score-widget__toggle").get_attribute("aria-expanded"), "true")

            page.locator("#playerTimeLeft").evaluate("el => { el.textContent = '9'; }")
            page.wait_for_function("document.querySelector('.vhs-theme-time').textContent === '00:00:09'")
            self.assertEqual(
                page.locator(".vhs-theme-timer").evaluate("el => el.style.getPropertyValue('--vhs-timer-progress')"),
                "0.5",
            )
            page.locator("#questionText").evaluate("el => { el.textContent = 'Äpfel'; }")
            page.wait_for_function("document.querySelector('.vhs-question-lead')?.textContent === 'ÄPFEL'")
            self.assertEqual(page.locator(".vhs-question-body").inner_text(), "")

            page.set_viewport_size({"width": 390, "height": 844})
            self.assertEqual(
                page.locator(".qa-score-widget__toggle").evaluate("el => getComputedStyle(el).width"),
                "124px",
            )
            self.assertEqual(
                page.locator(".score-box__list").evaluate("el => getComputedStyle(el).gridAutoFlow"),
                "row",
            )
            self.assertEqual(
                page.locator(".vhs-theme-footer").evaluate("el => getComputedStyle(el).justifyItems"),
                "center",
            )
            self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator(".vhs-theme-shell").count(), 0)
            self.assertEqual(page.locator("#questionText").inner_text(), "Äpfel")
            self.assertEqual(page.locator(".vhs-question-kicker").count(), 0)
            self.assertEqual(page.locator(".qa-score-widget__toggle").inner_text(), "Punkte")
            self.assertFalse(page.locator("#participant-score-position-select").is_disabled())
            self.assertEqual(
                page.evaluate("localStorage.getItem('participant_interface_score_position')"),
                "top-left",
            )
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_endscreen_restyles_existing_results_and_restores_standard_dom(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfügbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        score_rows = "".join(
            f'<div class="score-box__row"><span class="score-box__badge">{index}</span><span class="score-box__value">{index % 3}/{2 if index % 2 else 1}</span></div>'
            for index in range(1, 6)
        )
        fixture = f"""
          <title>Quiz - QuizMaster</title>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">Weltraum Quiz</h1>
              <span class="session-game-number">Spiel 4</span>
              <span class="participant-name">Mia Müller</span>
            </header>
            <main class="quiz-main">
              <div id="quizEndedState" class="game-state">
                <div class="ended-card">
                  <div class="ended-animation"><svg class="trophy-icon"><path d="M4 4h16v4"></path></svg></div>
                  <h2>QUIZ COMPLETED!</h2>
                  <p id="endedSubtitle">Great job! The quiz has ended.</p>
                  <div class="final-score">
                    <div class="score-circle">
                      <span class="score-value">8</span>
                      <span class="score-label">points</span>
                    </div>
                  </div>
                  <section class="post-game-results-card" data-post-game-results>
                    <div class="post-game-results-content">
                      <h3>Spiel beendet</h3>
                      <div class="post-game-results-meta">
                        <span>Weltraum Quiz</span><span>Quick Quiz</span><span>Spiel 4</span><span>Einfach</span><span>Faktor ×1.00</span>
                      </div>
                      <div class="post-game-results-table-wrap">
                        <table class="post-game-results-table">
                          <colgroup><col class="results-col-player"><col class="results-col-rank"><col class="results-col-game-points"><col class="results-col-factor"><col class="results-col-overall"></colgroup>
                          <thead><tr><th class="results-col-player">Teilnehmer</th><th class="is-number results-col-rank">Rang</th><th class="is-number results-col-game-points">Punkte im Spiel</th><th class="is-number results-col-factor">Faktor</th><th class="is-number results-col-overall">Punkte fuers Gesamtkonto</th></tr></thead>
                          <tbody><tr class="is-current-player"><td class="results-col-player">Mia Müller</td><td class="is-number results-col-rank">1</td><td class="is-number results-col-game-points">8</td><td class="is-number results-col-factor">×1.00</td><td class="is-number results-col-overall">18</td></tr></tbody>
                        </table>
                      </div>
                    </div>
                  </section>
                  <div class="results-actions"><button id="returnToLobbyBtn" type="button">Zur Lobby zurückkehren</button></div>
                </div>
              </div>
            </main>
            <aside class="score-box" style="background: white; border: 3px solid green; border-radius: 24px; padding: 20px">
              <h6 class="score-box__title">Punkte</h6>
              <div class="score-box__list">{score_rows}</div>
              <div id="quizScoreTotal">8/9</div>
            </aside>
          </div>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(
            "http://participant.test/endscreen",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.goto("http://participant.test/endscreen")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-end-screen")

            shell = page.locator(".vhs-theme-shell")
            self.assertEqual(shell.evaluate("el => getComputedStyle(el).borderTopWidth"), "0px")
            self.assertEqual(shell.evaluate("el => getComputedStyle(el).borderLeftWidth"), "0px")
            self.assertEqual(page.locator(".vhs-end-screen > h2").inner_text(), "SPIEL BEENDET")
            trophy = page.locator(".vhs-end-screen .trophy-icon")
            self.assertEqual(trophy.evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertEqual(trophy.evaluate("el => getComputedStyle(el).filter"), "none")
            self.assertEqual(page.locator("#endedSubtitle").count(), 0)
            self.assertEqual(page.locator(".post-game-results-content > h3").count(), 0)
            self.assertEqual(page.locator(".vhs-final-score-value").inner_text(), "8")
            self.assertEqual(page.locator(".vhs-final-score-label").inner_text(), "PUNKTE")
            self.assertEqual(page.locator(".vhs-final-score").evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertIn(
                "linear-gradient",
                page.locator(".vhs-final-score").evaluate("el => getComputedStyle(el, '::after').backgroundImage"),
            )

            results_panel = page.locator(".vhs-results-panel")
            self.assertEqual(results_panel.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(results_panel.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(page.locator(".vhs-results-meta").inner_text(), "WELTRAUM QUIZ QUICK QUIZ · SPIEL 4")
            self.assertNotIn("EINFACH", page.locator(".vhs-results-meta").inner_text().upper())
            self.assertNotIn("FAKTOR", page.locator(".vhs-results-meta").inner_text().upper())
            self.assertEqual(page.locator(".post-game-results-table tbody td").all_inner_texts(), ["Mia Müller", "1", "8", "×1.00", "18"])
            self.assertEqual(page.locator(".vhs-results-actions").evaluate("el => getComputedStyle(el).marginTop"), "32px")
            self.assertIn(
                "Arial",
                page.locator("#returnToLobbyBtn").evaluate("el => getComputedStyle(el).fontFamily"),
            )
            table_wrap = page.locator(".post-game-results-table-wrap")
            self.assertLessEqual(table_wrap.evaluate("el => el.scrollWidth"), table_wrap.evaluate("el => el.clientWidth"))
            for column_class in ("results-col-rank", "results-col-game-points", "results-col-factor", "results-col-overall"):
                header_center = page.locator(f"thead .{column_class}").evaluate(
                    "el => { const box = el.getBoundingClientRect(); return box.left + box.width / 2; }"
                )
                value_center = page.locator(f"tbody .{column_class}").evaluate(
                    "el => { const box = el.getBoundingClientRect(); return box.left + box.width / 2; }"
                )
                self.assertAlmostEqual(header_center, value_center, delta=0.2)
                self.assertEqual(page.locator(f"thead .{column_class}").evaluate("el => getComputedStyle(el).textAlign"), "center")
                self.assertEqual(page.locator(f"tbody .{column_class}").evaluate("el => getComputedStyle(el).textAlign"), "center")
            self.assertEqual(
                page.locator("tbody .results-col-factor").evaluate("el => getComputedStyle(el).fontVariantNumeric"),
                "tabular-nums",
            )

            score_panel = page.locator(".vhs-points-panel")
            self.assertEqual(score_panel.locator(":scope > .vhs-points-panel-title").inner_text(), "PUNKTE PRO RUNDE")
            self.assertEqual(score_panel.locator(":scope > .vhs-points-panel-title").count(), 1)
            self.assertEqual(
                page.locator(".qa-score-widget .score-box").evaluate("el => getComputedStyle(el).backgroundColor"),
                "rgba(0, 0, 0, 0)",
            )
            self.assertEqual(page.locator("#quizScoreTotal").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(
                page.locator(".vhs-points-round").first.evaluate("el => getComputedStyle(el, '::before').content"),
                '"R"',
            )
            self.assertEqual(page.locator(".vhs-points-unit").first.inner_text(), "P")
            self.assertEqual(page.locator(".vhs-points-list").evaluate("el => el.style.getPropertyValue('--vhs-score-columns')"), "2")

            score_toggle = page.locator(".qa-score-widget__toggle")
            if score_toggle.get_attribute("aria-expanded") == "true":
                score_toggle.click()
            page.mouse.move(0, 0)
            page.wait_for_timeout(250)
            self.assertEqual(score_toggle.evaluate("el => getComputedStyle(el).outlineStyle"), "none")
            self.assertEqual(score_toggle.evaluate("el => getComputedStyle(el).boxShadow"), "none")
            score_toggle.hover()
            self.assertEqual(score_toggle.evaluate("el => getComputedStyle(el).outlineStyle"), "none")
            self.assertNotEqual(score_toggle.evaluate("el => getComputedStyle(el).boxShadow"), "none")
            score_toggle.click()
            page.mouse.move(0, 0)
            self.assertEqual(score_toggle.get_attribute("aria-expanded"), "true")
            self.assertEqual(page.locator(".qa-score-widget__body").get_attribute("aria-hidden"), "false")
            self.assertEqual(score_toggle.evaluate("el => getComputedStyle(el).outlineStyle"), "none")
            self.assertNotEqual(score_toggle.evaluate("el => getComputedStyle(el).boxShadow"), "none")

            page.set_viewport_size({"width": 390, "height": 844})
            self.assertEqual(page.locator(".vhs-points-list").evaluate("el => getComputedStyle(el).gridAutoFlow"), "row")
            self.assertGreater(
                page.locator(".post-game-results-table-wrap").evaluate("el => el.scrollWidth"),
                page.locator(".post-game-results-table-wrap").evaluate("el => el.clientWidth"),
            )
            self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator(".vhs-end-screen").count(), 0)
            self.assertEqual(page.locator("#quizEndedState .ended-card > h2").inner_text(), "QUIZ COMPLETED!")
            self.assertEqual(page.locator("#endedSubtitle").inner_text(), "Great job! The quiz has ended.")
            self.assertEqual(page.locator(".post-game-results-content > h3").inner_text(), "Spiel beendet")
            self.assertEqual(
                page.locator(".post-game-results-meta > span").all_text_contents(),
                ["Weltraum Quiz", "Quick Quiz", "Spiel 4", "Einfach", "Faktor ×1.00"],
            )
            self.assertEqual(page.locator(".score-label").inner_text(), "points")
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_lobby_uses_live_metadata_and_restores_standard_dom(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Lobby · Filmabend</title>
          <div class="container" data-participant-lobby data-lobby-name="Filmabend" data-participant-name="Mia" data-vhs-next-game-number="4">
            <div class="header" data-participant-lobby-heading>
              <div class="title" id="lobbyTitle">Filmabend (Mia)</div>
            </div>
            <div class="card join-card" id="checkInCard" data-participant-lobby-status>
              <div class="lobby-top">
                <div>
                  <div class="section-title" data-participant-lobby-status-label>Check-in</div>
                  <div class="muted" id="checkInStatusText" data-participant-lobby-status-copy>Du bist eingecheckt.</div>
                </div>
                <button id="readyCheckInBtn" type="button">Ich bin bereit</button>
              </div>
              <div class="muted" id="checkInCounts" data-participant-lobby-status-count>Bereit: 1 / 1</div>
            </div>
            <div class="share" data-participant-lobby-share>
              <span data-participant-lobby-share-label>Share link:</span>
              <code data-participant-lobby-share-value>https://participant.test/hub/lobby/ABCD/</code>
            </div>
          </div>
          <script>
            window.readyClicks = 0;
            document.getElementById('readyCheckInBtn').addEventListener('click', function () {
              window.readyClicks += 1;
            });
          </script>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(
            "http://participant.test/lobby",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.goto("http://participant.test/lobby")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-lobby-shell")

            self.assertEqual(page.locator(".vhs-theme-app").inner_text(), "Lobby · Filmabend")
            self.assertEqual(page.locator("#lobbyTitle").count(), 0)
            self.assertEqual(page.locator(".vhs-lobby-next-label").inner_text(), "Als nächstes:")
            self.assertEqual(page.locator(".vhs-lobby-next-game").inner_text(), "SPIEL 4")
            self.assertEqual(page.locator(".vhs-theme-rec-text").inner_text(), "REC · LIVE")
            self.assertEqual(page.locator(".vhs-theme-participant").inner_text(), "LIVE · MIA")
            self.assertEqual(page.locator(".vhs-lobby-status-label").inner_text(), "CHECK-IN")
            self.assertEqual(page.locator("#checkInStatusText").inner_text(), "Du bist eingecheckt.")
            self.assertEqual(page.locator("#checkInCounts").inner_text(), "Bereit: 1 / 1")
            self.assertEqual(page.locator(".vhs-lobby-share-label").inner_text(), "LOBBY-LINK")
            self.assertEqual(
                page.locator(".vhs-lobby-share-value").inner_text(),
                "https://participant.test/hub/lobby/ABCD/",
            )
            self.assertEqual(
                page.locator(".vhs-lobby-shell").evaluate("el => getComputedStyle(el).borderTopWidth"),
                "0px",
            )
            self.assertEqual(
                page.locator(".vhs-lobby-shell").evaluate("el => getComputedStyle(el).borderLeftWidth"),
                "0px",
            )

            page.locator("#readyCheckInBtn").click()
            self.assertEqual(page.evaluate("window.readyClicks"), 1)
            page.locator("[data-participant-lobby]").evaluate(
                "el => { el.dataset.vhsNextGameNumber = '5'; el.dataset.participantName = 'Noah'; }"
            )
            page.wait_for_function("document.querySelector('.vhs-lobby-next-game').textContent === 'SPIEL 5'")
            self.assertEqual(page.locator(".vhs-theme-participant").inner_text(), "LIVE · NOAH")

            page.set_viewport_size({"width": 390, "height": 844})
            self.assertEqual(
                page.locator(".vhs-lobby-status-content").evaluate("el => getComputedStyle(el).gridTemplateColumns"),
                page.locator(".vhs-lobby-status-content").evaluate("el => getComputedStyle(el).width"),
            )
            self.assertEqual(
                page.locator(".vhs-lobby-share").evaluate("el => getComputedStyle(el).flexDirection"),
                "column",
            )
            self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator(".vhs-lobby-shell").count(), 0)
            self.assertEqual(page.locator("#lobbyTitle").inner_text(), "Filmabend (Mia)")
            self.assertEqual(page.locator("[data-participant-lobby-share-label]").inner_text(), "Share link:")
            self.assertEqual(page.locator(".vhs-lobby-next").count(), 0)
            page.locator("#readyCheckInBtn").click()
            self.assertEqual(page.evaluate("window.readyClicks"), 2)
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_assign_workspace_is_responsive_and_drop_modules_stay_stable(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        source_items = "".join(
            f'<div class="draggable-item assign-item{" assign-box-text--long" if len(text) > 24 else ""}" draggable="true" data-left-index="{index}" tabindex="0">{text}</div>'
            for index, text in enumerate((
                "Deutschland", "Spanien", "Frankreich", "Italien", "Portugal",
                "Niederlande", "Vereinigtes Koenigreich", "Daenemark", "Schweden",
            ))
        )
        target_items = "".join(
            f'<div class="drop-zone assign-target" data-right-index="{index}"><div class="drop-zone-label assign-target__label{" assign-box-text--long" if len(text) > 24 else ""}">{text}</div><div class="drop-zone-slot assign-target__dropzone"></div></div>'
            for index, text in enumerate((
                "Berlin", "Madrid", "Paris", "Rom", "Lissabon", "Amsterdam",
                "Kopenhagen", "Eine sehr lange Hauptstadtbezeichnung",
            ))
        )
        fixture = """
          <title>Assign - QuizMaster</title>
          <style>
            html, body { margin: 0; min-height: 100%; }
            .play-container { min-height: 100vh; }
            .question-content { padding: 32px; }
            .items-list, .zones-list { display: flex; flex-direction: column; }
            .d-none { display: none !important; }
          </style>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">Zuordnen</h1>
              <span class="session-game-number">Spiel 4</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main"><div class="container"><div class="row"><div class="assign-main-column">
              <div id="questionState" class="game-state"><div class="question-card">
                <div class="question-content">
                  <div class="question-text" id="questionText">A2</div>
                  <div class="drag-drop-interface assign-workspace" id="dragDropInterface">
                    <div class="drag-drop-container assign-workspace-grid">
                      <div class="left-items assign-source-panel" id="leftItems">
                        <h4>ZIEHE VON HIER</h4>
                        <div class="items-list" id="leftItemsList" data-item-count="9">__SOURCE_ITEMS__</div>
                      </div>
                      <div class="drop-zones assign-target-panel" id="dropZones">
                        <h4>ORDNE HIER ZU</h4>
                        <div class="zones-list assign-targets-list" id="zonesList" data-item-count="8">__TARGET_ITEMS__</div>
                      </div>
                    </div>
                    <div class="assign-action-row">
                      <button type="button" class="btn d-none assign-reset-button" id="resetRoundBtn">Zuruecksetzen</button>
                      <button type="button" class="btn d-none assign-submit-button" id="logRoundBtn">Einloggen</button>
                    </div>
                  </div>
                  <div id="roundSubmittedMessage" class="assign-round-status d-none">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l5 5L20 6"></path></svg>
                    <p>Antwort gespeichert!</p>
                    <p>Warte auf nächste Runde...</p>
                  </div>
                </div>
              </div></div>
            </div></div></div></main>
            <aside class="score-box"><div class="score-box__list">
              <div class="score-box__row"><span class="score-box__badge">1</span><span class="score-box__value">1/2</span></div>
              <div class="score-box__row"><span class="score-box__badge">2</span><span class="score-box__value">_/2</span></div>
            </div></aside>
          </div>
          <script>
            const assignSource = document.querySelector('.draggable-item[data-left-index="0"]');
            const assignTarget = document.querySelector('.drop-zone[data-right-index="0"]');
            assignSource.addEventListener('dragstart', event => {
              event.dataTransfer.setData('text/plain', assignSource.dataset.leftIndex);
            });
            assignTarget.addEventListener('dragover', event => event.preventDefault());
            assignTarget.addEventListener('drop', event => {
              event.preventDefault();
              const droppedItem = document.createElement('div');
              droppedItem.className = 'dropped-item assign-item';
              droppedItem.textContent = assignSource.textContent;
              assignTarget.querySelector('.drop-zone-slot').appendChild(droppedItem);
              assignTarget.classList.add('occupied');
              assignSource.classList.add('matched');
              document.getElementById('leftItemsList').dataset.itemCount = '8';
              document.getElementById('resetRoundBtn').classList.remove('d-none');
              document.getElementById('logRoundBtn').classList.remove('d-none');
            });
            document.getElementById('resetRoundBtn').addEventListener('click', () => {
              assignTarget.querySelector('.dropped-item')?.remove();
              assignTarget.classList.remove('occupied');
              assignSource.classList.remove('matched');
              document.getElementById('leftItemsList').dataset.itemCount = '9';
              document.getElementById('resetRoundBtn').classList.add('d-none');
              document.getElementById('logRoundBtn').classList.add('d-none');
            });
            window.assignLogClicks = 0;
            document.getElementById('logRoundBtn').addEventListener('click', () => { window.assignLogClicks += 1; });
          </script>
        """.replace("__SOURCE_ITEMS__", source_items).replace("__TARGET_ITEMS__", target_items)
        context = self._browser.new_context(viewport={"width": 1440, "height": 1200})
        context.route(
            "http://participant.test/assign-layout",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='assign-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))

        def overlaps(first, second):
            return not (
                first["x"] + first["width"] <= second["x"]
                or second["x"] + second["width"] <= first["x"]
                or first["y"] + first["height"] <= second["y"]
                or second["y"] + second["height"] <= first["y"]
            )

        try:
            page.goto("http://participant.test/assign-layout")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell .assign-workspace")

            self.assertEqual(page.locator(".drop-zone").count(), 8)
            self.assertEqual(page.locator(".drop-zone-slot").count(), 8)
            self.assertEqual(
                page.locator(".qa-score-widget").evaluate("el => el.nextElementSibling.id"),
                "dragDropInterface",
            )

            source_boxes = [
                element.bounding_box()
                for element in page.locator(".assign-source-panel .assign-item").all()
            ]
            label_boxes = [
                element.bounding_box()
                for element in page.locator(".assign-target__label").all()
            ]
            slot_boxes = [
                element.bounding_box()
                for element in page.locator(".assign-target__dropzone").all()
            ]
            reference_box = source_boxes[0]
            for box in source_boxes + label_boxes + slot_boxes:
                self.assertAlmostEqual(box["width"], reference_box["width"], delta=0.5)
                self.assertAlmostEqual(box["height"], 58, delta=0.5)
            self.assertAlmostEqual(reference_box["width"], 130, delta=0.5)
            overflowing_labels = page.locator(
                ".assign-source-panel .assign-item, .assign-target__label"
            ).evaluate_all(
                "elements => elements.filter(el => el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1).map(el => { const style = getComputedStyle(el); return { text: el.textContent, className: el.className, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, fontSize: style.fontSize, lineHeight: style.lineHeight, letterSpacing: style.letterSpacing, whiteSpace: style.whiteSpace, wordBreak: style.wordBreak }; })"
            )
            self.assertEqual(overflowing_labels, [])
            for label_box, slot_box in zip(label_boxes, slot_boxes):
                self.assertAlmostEqual(label_box["x"], slot_box["x"], delta=0.5)
                self.assertAlmostEqual(label_box["width"], slot_box["width"], delta=0.5)
                self.assertAlmostEqual(label_box["y"] + label_box["height"], slot_box["y"], delta=0.5)
            self.assertAlmostEqual(source_boxes[0]["y"], source_boxes[1]["y"], delta=0.5)
            self.assertGreater(source_boxes[2]["y"], source_boxes[0]["y"])
            self.assertAlmostEqual(label_boxes[0]["y"], label_boxes[1]["y"], delta=0.5)
            self.assertGreater(label_boxes[2]["y"], label_boxes[0]["y"])
            for box_selector in (".assign-target__label", ".assign-target__dropzone"):
                for box_element in page.locator(box_selector).all():
                    self.assertEqual(
                        box_element.evaluate("el => getComputedStyle(el).borderTopStyle"),
                        "solid",
                    )
            target_wrapper = page.locator('.assign-target[data-right-index="0"]')
            self.assertEqual(target_wrapper.evaluate("el => getComputedStyle(el).borderTopWidth"), "0px")
            self.assertEqual(target_wrapper.evaluate("el => getComputedStyle(el).paddingTop"), "0px")
            self.assertEqual(target_wrapper.evaluate("el => getComputedStyle(el).backgroundColor"), "rgba(0, 0, 0, 0)")
            self.assertEqual(target_wrapper.evaluate("el => getComputedStyle(el).boxShadow"), "none")
            self.assertEqual(
                page.locator('.assign-target__label').first.evaluate("el => getComputedStyle(el).borderRadius"),
                "0px",
            )
            self.assertEqual(
                page.locator('.assign-target__label').first.evaluate("el => getComputedStyle(el).backgroundColor"),
                "rgba(233, 223, 202, 0.9)",
            )

            initial_workspace_box = page.locator('.assign-workspace').bounding_box()
            initial_grid_box = page.locator('.assign-workspace-grid').bounding_box()
            initial_action_box = page.locator('.assign-action-row').bounding_box()
            self.assertAlmostEqual(initial_grid_box['x'], initial_action_box['x'], delta=0.5)
            self.assertAlmostEqual(initial_grid_box['width'], initial_action_box['width'], delta=0.5)
            self.assertGreaterEqual(initial_action_box['height'], 54)
            self.assertFalse(page.locator('#resetRoundBtn').is_visible())
            self.assertFalse(page.locator('#logRoundBtn').is_visible())

            initial_target_box = page.locator('.drop-zone[data-right-index="0"]').bounding_box()
            initial_slot_box = page.locator('.drop-zone[data-right-index="0"] .drop-zone-slot').bounding_box()
            page.locator('.draggable-item[data-left-index="0"]').drag_to(
                page.locator('.drop-zone[data-right-index="0"]')
            )
            page.wait_for_selector('.drop-zone[data-right-index="0"] .dropped-item')
            page.wait_for_selector('#logRoundBtn:not(.d-none)')
            filled_target_box = page.locator('.drop-zone[data-right-index="0"]').bounding_box()
            filled_slot_box = page.locator('.drop-zone[data-right-index="0"] .drop-zone-slot').bounding_box()
            dropped_item_box = page.locator('.drop-zone[data-right-index="0"] .dropped-item').bounding_box()
            filled_workspace_box = page.locator('.assign-workspace').bounding_box()
            filled_grid_box = page.locator('.assign-workspace-grid').bounding_box()
            filled_action_box = page.locator('.assign-action-row').bounding_box()
            self.assertAlmostEqual(initial_workspace_box['x'], filled_workspace_box['x'], delta=0.5)
            self.assertAlmostEqual(initial_workspace_box['width'], filled_workspace_box['width'], delta=0.5)
            self.assertAlmostEqual(initial_grid_box['x'], filled_grid_box['x'], delta=0.5)
            self.assertAlmostEqual(initial_grid_box['width'], filled_grid_box['width'], delta=0.5)
            self.assertAlmostEqual(filled_grid_box['x'], filled_action_box['x'], delta=0.5)
            self.assertAlmostEqual(filled_grid_box['width'], filled_action_box['width'], delta=0.5)
            submit_box = page.locator('#logRoundBtn').bounding_box()
            reset_box = page.locator('#resetRoundBtn').bounding_box()
            self.assertAlmostEqual(
                submit_box['x'] + submit_box['width'],
                filled_action_box['x'] + filled_action_box['width'],
                delta=0.5,
            )
            self.assertLess(reset_box['width'], filled_action_box['width'] / 2)
            self.assertAlmostEqual(initial_target_box["width"], filled_target_box["width"], delta=0.5)
            self.assertAlmostEqual(initial_target_box["height"], filled_target_box["height"], delta=0.5)
            self.assertAlmostEqual(initial_slot_box["width"], filled_slot_box["width"], delta=0.5)
            self.assertAlmostEqual(initial_slot_box["height"], filled_slot_box["height"], delta=0.5)
            self.assertAlmostEqual(filled_slot_box["width"], dropped_item_box["width"], delta=0.5)
            self.assertAlmostEqual(filled_slot_box["height"], dropped_item_box["height"], delta=0.5)
            self.assertFalse(page.locator('.draggable-item[data-left-index="0"]').is_visible())
            self.assertEqual(page.locator('#leftItemsList').get_attribute('data-item-count'), '8')
            self.assertEqual(
                page.locator('.drop-zone[data-right-index="0"] .dropped-item').evaluate(
                    "el => getComputedStyle(el).borderTopStyle"
                ),
                "solid",
            )
            self.assertEqual(
                page.locator('.drop-zone[data-right-index="0"] > .dropped-item').count(),
                0,
            )
            self.assertEqual(
                page.locator('.drop-zone[data-right-index="0"] .drop-zone-slot > .dropped-item').count(),
                1,
            )
            page.locator('#resetRoundBtn').hover()
            page.wait_for_timeout(220)
            self.assertEqual(
                page.locator('#resetRoundBtn').evaluate("el => getComputedStyle(el).transform"),
                "matrix(1, 0, 0, 1, -4, -3)",
            )
            page.locator('#resetRoundBtn').focus()
            page.keyboard.press('Tab')
            self.assertEqual(page.evaluate('document.activeElement.id'), 'logRoundBtn')
            self.assertNotEqual(
                page.locator('#logRoundBtn').evaluate("el => getComputedStyle(el).outlineStyle"),
                "none",
            )
            page.locator('#logRoundBtn').press('Enter')
            self.assertEqual(page.evaluate('window.assignLogClicks'), 1)
            page.locator('#resetRoundBtn').click()
            self.assertTrue(page.locator('.draggable-item[data-left-index="0"]').is_visible())
            self.assertEqual(page.locator('#leftItemsList').get_attribute('data-item-count'), '9')
            self.assertEqual(page.locator('.drop-zone .dropped-item').count(), 0)
            self.assertFalse(page.locator('#resetRoundBtn').is_visible())
            self.assertFalse(page.locator('#logRoundBtn').is_visible())

            source_item = page.locator('.draggable-item[data-left-index="1"]')
            source_item.hover()
            self.assertNotEqual(source_item.evaluate("el => getComputedStyle(el).transform"), "none")
            source_item.focus()
            self.assertNotEqual(source_item.evaluate("el => getComputedStyle(el).outlineStyle"), "none")

            page.locator('.draggable-item[data-left-index="0"]').drag_to(
                page.locator('.drop-zone[data-right-index="0"]')
            )
            page.wait_for_selector('#logRoundBtn:not(.d-none)')

            for viewport in (
                {"width": 1440, "height": 1200},
                {"width": 1280, "height": 1200},
                {"width": 1024, "height": 1300},
                {"width": 820, "height": 1400},
                {"width": 600, "height": 1500},
                {"width": 390, "height": 1800},
            ):
                page.set_viewport_size(viewport)
                page.wait_for_timeout(80)
                workspace_box = page.locator(".assign-workspace-grid").bounding_box()
                action_box = page.locator(".assign-action-row").bounding_box()
                score_box = page.locator(".qa-score-widget").bounding_box()
                self.assertFalse(overlaps(workspace_box, score_box), viewport)
                self.assertAlmostEqual(workspace_box["x"], action_box["x"], delta=0.5)
                self.assertAlmostEqual(workspace_box["width"], action_box["width"], delta=0.5)
                if viewport["width"] > 560:
                    responsive_submit_box = page.locator('#logRoundBtn').bounding_box()
                    self.assertAlmostEqual(
                        responsive_submit_box['x'] + responsive_submit_box['width'],
                        action_box['x'] + action_box['width'],
                        delta=0.5,
                    )
                else:
                    self.assertAlmostEqual(page.locator('#resetRoundBtn').bounding_box()['width'], action_box['width'], delta=0.5)
                    self.assertAlmostEqual(page.locator('#logRoundBtn').bounding_box()['width'], action_box['width'], delta=0.5)
                for interactive_selector in (".assign-source-panel", ".drop-zone"):
                    for element_box in page.locator(interactive_selector).all():
                        self.assertFalse(overlaps(element_box.bounding_box(), score_box), viewport)
                self.assertLessEqual(
                    page.evaluate("document.body.scrollWidth"),
                    page.evaluate("window.innerWidth"),
                    viewport,
                )

            page.locator('.qa-score-widget__toggle').click()
            page.wait_for_timeout(80)
            self.assertFalse(
                overlaps(
                    page.locator('.assign-workspace').bounding_box(),
                    page.locator('.qa-score-widget').bounding_box(),
                )
            )
            self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.locator(".assign-workspace").evaluate("el => el.style.display = 'none'")
            status = page.locator("#roundSubmittedMessage")
            status.evaluate("el => el.classList.remove('d-none')")
            self.assertEqual(status.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(status.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(
                status.locator("p").nth(1).evaluate("el => getComputedStyle(el).color"),
                "rgb(155, 161, 156)",
            )
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_assign_reveal_matrix_states_and_responsive_layout(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Assign Reveal - QuizMaster</title>
          <style>
            html, body { margin: 0; min-height: 100%; }
            .play-container { min-height: 100vh; }
            .d-none { display: none !important; }
          </style>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">Zuordnen</h1>
              <span class="session-game-number">Spiel 4</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main"><div class="container"><div class="row"><div>
              <div id="solutionState" class="game-state">
                <div class="waiting-card">
                  <h2>Aufloesung</h2>
                  <div class="assign-legacy-solution row"><div>Richtige Zuordnung</div><div>Deine Zuordnung</div></div>
                  <div class="assign-vhs-solution" style="display:none" aria-label="Vollstaendig geloeste Zuordnung">
                    <p id="assignVhsSolutionTimeout" class="assign-reveal-timeout" hidden>Zeit abgelaufen</p>
                    <div class="assign-reveal-headings"><span>Quellbegriff</span><span>Zielbegriff</span></div>
                    <div id="assignVhsSolutionMatrix" class="assign-reveal-matrix" data-answer-state="answered">
                      <div class="assign-reveal-pair assign-reveal-pair--correct" data-result="correct">
                        <div class="assign-reveal-box assign-reveal-box--source">Deutschland</div>
                        <div class="assign-reveal-box assign-reveal-box--target">Berlin</div>
                      </div>
                      <div class="assign-reveal-pair assign-reveal-pair--wrong" data-result="wrong">
                        <div class="assign-reveal-box assign-reveal-box--source">Spanien</div>
                        <div class="assign-reveal-box assign-reveal-box--target">Madrid</div>
                      </div>
                      <div class="assign-reveal-pair" data-result="neutral">
                        <div class="assign-reveal-box assign-reveal-box--source">Eine sehr lange Quellenbezeichnung</div>
                        <div class="assign-reveal-box assign-reveal-box--target">Eine sehr lange Zielbezeichnung</div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div></div></div></main>
            <aside class="score-box"><div class="score-box__list">
              <div class="score-box__row"><span class="score-box__badge">1</span><span class="score-box__value">1/3</span></div>
            </div></aside>
          </div>
        """
        context = self._browser.new_context(viewport={"width": 1440, "height": 1000})
        context.route(
            "http://participant.test/assign-reveal",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='assign-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))

        def overlaps(first, second):
            return not (
                first["x"] + first["width"] <= second["x"]
                or second["x"] + second["width"] <= first["x"]
                or first["y"] + first["height"] <= second["y"]
                or second["y"] + second["height"] <= first["y"]
            )

        try:
            page.goto("http://participant.test/assign-reveal")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell .assign-vhs-solution")

            self.assertFalse(page.locator(".assign-legacy-solution").is_visible())
            self.assertTrue(page.locator(".assign-vhs-solution").is_visible())
            self.assertEqual(page.locator(".assign-reveal-pair").count(), 3)
            self.assertEqual(page.locator('[data-result="correct"]').count(), 1)
            self.assertEqual(page.locator('[data-result="wrong"]').count(), 1)
            self.assertEqual(page.locator('[data-result="neutral"]').count(), 1)
            self.assertEqual(
                page.locator('[data-result="correct"] .assign-reveal-box').first.evaluate(
                    "el => getComputedStyle(el).borderTopColor"
                ),
                "rgb(120, 145, 116)",
            )
            self.assertEqual(
                page.locator('[data-result="wrong"] .assign-reveal-box').first.evaluate(
                    "el => getComputedStyle(el).borderTopColor"
                ),
                "rgb(164, 95, 82)",
            )
            self.assertEqual(
                page.locator('[data-result="neutral"] .assign-reveal-box').first.evaluate(
                    "el => getComputedStyle(el).borderTopStyle"
                ),
                "solid",
            )

            for pair in page.locator(".assign-reveal-pair").all():
                source_box = pair.locator(".assign-reveal-box--source").bounding_box()
                target_box = pair.locator(".assign-reveal-box--target").bounding_box()
                self.assertAlmostEqual(source_box["width"], target_box["width"], delta=0.5)
                self.assertAlmostEqual(source_box["height"], target_box["height"], delta=0.5)

            page.evaluate("""
              document.querySelectorAll('.assign-reveal-pair').forEach(pair => {
                pair.classList.remove('assign-reveal-pair--wrong');
                pair.classList.add('assign-reveal-pair--correct');
                pair.dataset.result = 'correct';
              });
            """)
            self.assertEqual(page.locator(".assign-reveal-pair--correct").count(), 3)
            self.assertEqual(page.locator(".assign-reveal-pair--wrong").count(), 0)

            page.evaluate("""
              document.querySelector('#assignVhsSolutionTimeout').hidden = false;
              document.querySelector('#assignVhsSolutionMatrix').dataset.answerState = 'unanswered';
              document.querySelectorAll('.assign-reveal-pair').forEach(pair => {
                pair.classList.remove('assign-reveal-pair--correct', 'assign-reveal-pair--wrong');
                pair.dataset.result = 'neutral';
              });
            """)
            self.assertTrue(page.locator("#assignVhsSolutionTimeout").is_visible())
            self.assertEqual(page.locator("#assignVhsSolutionTimeout").inner_text(), "Zeit abgelaufen")
            self.assertEqual(page.locator(".assign-reveal-pair--correct").count(), 0)
            self.assertEqual(page.locator(".assign-reveal-pair--wrong").count(), 0)

            if page.locator(".qa-score-widget").evaluate(
                "el => el.classList.contains('is-collapsed')"
            ):
                page.locator(".qa-score-widget__toggle").click()
            page.wait_for_function(
                "!document.querySelector('.qa-score-widget').classList.contains('is-collapsed')"
            )

            for viewport in (
                {"width": 1440, "height": 1000},
                {"width": 1280, "height": 1100},
                {"width": 1024, "height": 1100},
                {"width": 820, "height": 1200},
                {"width": 390, "height": 1000},
            ):
                page.set_viewport_size(viewport)
                page.wait_for_timeout(80)
                matrix_box = page.locator(".assign-vhs-solution").bounding_box()
                score_box = page.locator(".qa-score-widget").bounding_box()
                self.assertFalse(overlaps(matrix_box, score_box), viewport)
                self.assertLessEqual(
                    page.evaluate("document.body.scrollWidth"),
                    page.evaluate("window.innerWidth"),
                    viewport,
                )

            for theme in ("standard", "arcade"):
                page.locator("#participant-theme-select").evaluate(
                    "(el, value) => { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })); }",
                    theme,
                )
                page.wait_for_function(
                    "value => document.documentElement.dataset.participantTheme === value",
                    arg=theme,
                )
                self.assertTrue(page.locator(".assign-legacy-solution").is_visible(), theme)
                self.assertFalse(page.locator(".assign-vhs-solution").is_visible(), theme)
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_quick_quiz_answers_are_centered_responsive_and_boolean_labels_are_balanced(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Quiz - QuizMaster</title>
          <style>.quick-quiz-response-area { display: contents; }</style>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">QA Quick Quiz</h1>
              <span class="session-game-number">Spiel 2</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main"><div class="container"><div class="row"><div class="col-lg-8">
              <div id="questionState" class="game-state"><div class="question-card">
                <div class="question-content quick-quiz-layout">
                  <div class="question-text quick-quiz-question" id="questionText">Welche Antwort ist richtig?</div>
                  <div class="quick-quiz-response-area">
                    <div class="answer-options quick-quiz-answer-list" id="answerOptions">
                      <div class="answer-option quick-quiz-answer"><div class="option-key">A</div><div class="option-text">Kurz</div></div>
                      <div class="answer-option quick-quiz-answer"><div class="option-key">B</div><div class="option-text">Eine deutlich laengere Antwort, die bei wenig Platz sauber in mehrere Zeilen umbrechen muss</div></div>
                      <div class="answer-option quick-quiz-answer"><div class="option-key">C</div><div class="option-text">Antwort C</div></div>
                      <div class="answer-option quick-quiz-answer"><div class="option-key">D</div><div class="option-text">Antwort D</div></div>
                    </div>
                    <div class="quick-quiz-submit-row">
                      <button class="btn submit-answer quick-quiz-submit vhs-quick-quiz-submit" id="submitAnswerBtn" aria-pressed="false">Einloggen</button>
                    </div>
                  </div>
                </div>
              </div></div>
            </div></div></div></main>
            <aside class="score-box"><div class="score-box__list">
              <div class="score-box__row"><span class="score-box__badge">1</span><span class="score-box__value">0/1</span></div>
              <div class="score-box__row"><span class="score-box__badge">2</span><span class="score-box__value">_/1</span></div>
            </div></aside>
          </div>
          <script>
            window.quickQuizSubmits = 0;
            document.getElementById('submitAnswerBtn').addEventListener('click', function () {
              window.quickQuizSubmits += 1;
              this.classList.add('is-selected');
              this.setAttribute('aria-pressed', 'true');
              this.disabled = true;
            });
          </script>
        """
        context = self._browser.new_context(viewport={"width": 1440, "height": 1100})
        context.route(
            "http://participant.test/quick-quiz-answers",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='quiz-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))

        def overlaps(first, second):
            return not (
                first["x"] + first["width"] <= second["x"]
                or second["x"] + second["width"] <= first["x"]
                or first["y"] + first["height"] <= second["y"]
                or second["y"] + second["height"] <= first["y"]
            )

        try:
            page.goto("http://participant.test/quick-quiz-answers")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell .quick-quiz-answer-list")

            answers = page.locator("#answerOptions .quick-quiz-answer")
            answer_boxes = [answers.nth(index).bounding_box() for index in range(4)]
            response_box = page.locator(".quick-quiz-response-area").bounding_box()
            submit_box = page.locator("#submitAnswerBtn").bounding_box()
            widget_box = page.locator(".qa-score-widget").bounding_box()
            self.assertTrue(all(abs(box["width"] - answer_boxes[0]["width"]) <= 0.5 for box in answer_boxes))
            self.assertAlmostEqual(
                submit_box["x"] + submit_box["width"] / 2,
                response_box["x"] + response_box["width"] / 2,
                delta=1,
            )
            self.assertFalse(any(overlaps(box, widget_box) for box in answer_boxes))
            self.assertGreater(answer_boxes[1]["height"], 70)

            page.evaluate("""
              () => {
                const answers = Array.from(document.querySelectorAll('#answerOptions .quick-quiz-answer'));
                answers.slice(2).forEach(answer => answer.parentElement.remove());
                answers.slice(0, 2).forEach((answer, index) => {
                  answer.classList.add('answer-option-single-label', 'quick-quiz-answer--boolean');
                  answer.replaceChildren(Object.assign(document.createElement('div'), {
                    className: 'option-text',
                    textContent: index === 0 ? 'Stimmt' : 'Stimmt nicht'
                  }));
                });
              }
            """)
            page.wait_for_timeout(50)
            for index in range(2):
                button_box = answers.nth(index).bounding_box()
                label_box = answers.nth(index).locator(".option-text").bounding_box()
                self.assertAlmostEqual(
                    label_box["x"] + label_box["width"] / 2,
                    button_box["x"] + button_box["width"] / 2,
                    delta=1,
                )
                self.assertAlmostEqual(
                    label_box["y"] + label_box["height"] / 2,
                    button_box["y"] + button_box["height"] / 2,
                    delta=1,
                )

            submit = page.locator("#submitAnswerBtn")
            submit.hover()
            self.assertNotEqual(submit.evaluate("el => getComputedStyle(el).transform"), "none")
            submit.focus()
            self.assertNotEqual(submit.evaluate("el => getComputedStyle(el).outlineStyle"), "none")
            submit.press("Enter")
            self.assertEqual(page.evaluate("window.quickQuizSubmits"), 1)
            self.assertTrue(submit.is_disabled())
            self.assertTrue(submit.evaluate("el => el.classList.contains('is-selected')"))
            self.assertEqual(submit.get_attribute("aria-pressed"), "true")

            for viewport in (
                {"width": 1024, "height": 1100},
                {"width": 820, "height": 1100},
                {"width": 390, "height": 900},
            ):
                page.set_viewport_size(viewport)
                page.wait_for_timeout(50)
                self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))
                current_widget_box = page.locator(".qa-score-widget").bounding_box()
                current_answer_boxes = [answers.nth(index).bounding_box() for index in range(2)]
                self.assertFalse(any(overlaps(box, current_widget_box) for box in current_answer_boxes))

            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_quick_quiz_short_answer_layout_reserves_score_space(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Quiz - QuizMaster</title>
          <style>
            .quick-quiz-response-area { display: contents; }
            .form-label { color: rgb(30, 64, 175); }
            .short-answer-input { width: 100%; margin-bottom: 2rem; }
          </style>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">QA Quick Quiz</h1>
              <span class="session-game-number">Spiel 2</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main">
              <div class="container">
                <div class="row"><div class="col-lg-8">
                  <div id="questionState" class="game-state">
                    <div class="question-card">
                      <div class="question-content quick-quiz-layout">
                        <div class="question-text" id="questionText">Welchen Song hörst du und von wem ist er gesungen?</div>
                        <div class="quick-quiz-response-area">
                          <div class="answer-options quick-quiz-short-answer-form" id="answerOptions">
                            <div class="mb-3 quick-quiz-short-answer-field">
                              <label class="form-label fw-semibold" for="shortAnswerInput1">Titel</label>
                              <input type="text" class="short-answer-input" id="shortAnswerInput1" value="Song A" placeholder="Titel">
                            </div>
                            <div class="mb-3 quick-quiz-short-answer-field">
                              <label class="form-label fw-semibold" for="shortAnswerInput2">Künstler</label>
                              <input type="text" class="short-answer-input" id="shortAnswerInput2" value="Band B" placeholder="Künstler">
                            </div>
                          </div>
                          <button class="btn btn-primary btn-lg w-100 submit-answer quick-quiz-submit" id="submitAnswerBtn">Einloggen</button>
                        </div>
                      </div>
                    </div>
                  </div>
                </div></div>
              </div>
            </main>
            <aside class="score-box">
              <h6 class="score-box__title">Punkte</h6>
              <div class="score-box__list">
                <div class="score-box__row"><span class="score-box__badge">1</span><span class="score-box__value">1/1</span></div>
                <div class="score-box__row"><span class="score-box__badge">2</span><span class="score-box__value">0/1</span></div>
              </div>
            </aside>
          </div>
          <script>
            window.shortAnswerSubmits = 0;
            document.getElementById('submitAnswerBtn').addEventListener('click', function () {
              window.shortAnswerSubmits += 1;
            });
          </script>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(
            "http://participant.test/quick-quiz-short-answer",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='quiz-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))

        def overlaps(first, second):
            return not (
                first["x"] + first["width"] <= second["x"]
                or second["x"] + second["width"] <= first["x"]
                or first["y"] + first["height"] <= second["y"]
                or second["y"] + second["height"] <= first["y"]
            )

        try:
            page.goto("http://participant.test/quick-quiz-short-answer")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell")
            page.wait_for_function(
                "document.querySelector('.qa-score-widget')?.parentElement?.classList.contains('question-content')"
            )

            labels = page.locator(".quick-quiz-short-answer-field > label")
            inputs = page.locator(".quick-quiz-short-answer-field > input")
            first_label_box = labels.nth(0).bounding_box()
            second_label_box = labels.nth(1).bounding_box()
            first_input_box = inputs.nth(0).bounding_box()
            second_input_box = inputs.nth(1).bounding_box()
            score_toggle_box = page.locator(".qa-score-widget__toggle").bounding_box()
            score_panel_box = page.locator(".qa-score-widget__body").bounding_box()
            submit_box = page.locator("#submitAnswerBtn").bounding_box()

            self.assertEqual(labels.nth(0).evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertEqual(labels.nth(1).evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertAlmostEqual(first_label_box["x"], second_label_box["x"], delta=0.5)
            self.assertAlmostEqual(first_input_box["x"], second_input_box["x"], delta=0.5)
            self.assertAlmostEqual(first_input_box["width"], second_input_box["width"], delta=0.5)
            self.assertLessEqual(first_input_box["width"], 360.5)
            self.assertGreaterEqual(first_label_box["y"], max(score_toggle_box["y"] + score_toggle_box["height"], score_panel_box["y"] + score_panel_box["height"]) + 20)
            self.assertGreater(second_label_box["y"], first_input_box["y"] + first_input_box["height"])
            self.assertGreater(submit_box["y"], second_input_box["y"] + second_input_box["height"])
            self.assertLess(score_panel_box["x"], score_toggle_box["x"])

            inputs.nth(0).focus()
            self.assertNotEqual(inputs.nth(0).evaluate("el => getComputedStyle(el).boxShadow"), "none")
            inputs.nth(0).fill("Neuer Titel")
            self.assertEqual(inputs.nth(0).input_value(), "Neuer Titel")
            self.assertEqual(inputs.nth(1).input_value(), "Band B")
            page.locator("#submitAnswerBtn").click()
            self.assertEqual(page.evaluate("window.shortAnswerSubmits"), 1)

            score_toggle = page.locator(".qa-score-widget__toggle")
            score_toggle.click()
            self.assertEqual(score_toggle.get_attribute("aria-expanded"), "false")
            score_toggle.click()
            self.assertEqual(score_toggle.get_attribute("aria-expanded"), "true")

            for viewport in (
                {"width": 1280, "height": 1100},
                {"width": 1180, "height": 1100},
                {"width": 1024, "height": 1100},
                {"width": 900, "height": 1100},
                {"width": 820, "height": 1100},
                {"width": 390, "height": 1000},
            ):
                page.set_viewport_size(viewport)
                page.wait_for_timeout(100)
                current_widget_box = page.locator(".qa-score-widget").bounding_box()
                content_boxes = [
                    labels.nth(0).bounding_box(),
                    inputs.nth(0).bounding_box(),
                    labels.nth(1).bounding_box(),
                    inputs.nth(1).bounding_box(),
                    page.locator("#submitAnswerBtn").bounding_box(),
                ]
                self.assertFalse(any(overlaps(box, current_widget_box) for box in content_boxes))
                self.assertAlmostEqual(inputs.nth(0).bounding_box()["width"], inputs.nth(1).bounding_box()["width"], delta=0.5)
                self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator(".vhs-theme-shell").count(), 0)
            self.assertEqual(labels.nth(0).evaluate("el => getComputedStyle(el).color"), "rgb(30, 64, 175)")
            self.assertEqual(page.locator(".quick-quiz-response-area").evaluate("el => getComputedStyle(el).display"), "contents")
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_estimation_question_layout_and_existing_submit_in_browser(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Estimation Quiz - QuizMaster</title>
          <style>
            .estimation-interaction-area { display: contents; }
            .question-content { padding: 1rem 2.25rem 2.5rem; }
            .answer-interface { margin-bottom: 2rem; }
            .answer-input-container { max-width: 520px; margin: 0 auto; }
            .input-with-unit { display: flex; gap: .75rem; }
            .estimate-input { height: 78px; flex: 1; text-align: center; }
            .estimate-input::placeholder { color: rgb(33, 37, 41); opacity: .5; }
            .unit-display { min-width: 72px; height: 78px; padding: 0 1rem; background: white; border-radius: 12px; }
            .submit-answer { display: block; width: fit-content; margin: 1.5rem auto 0; }
          </style>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">Geografie Schaetzungen</h1>
              <span class="session-game-number">Spiel 3</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main">
              <div class="container">
                <div class="row"><div class="col-lg-8">
                  <div id="questionState" class="game-state">
                    <div class="question-card qa-active-answer-card estimation-answer-card">
                      <div class="question-header">
                        <div class="question-number"><span id="currentQuestionNumber">1</span></div>
                        <div class="question-timer"><span id="playerTimeLeft">90</span></div>
                      </div>
                      <div class="question-content">
                        <div class="question-text" id="questionText">Wie lang ist der Nil in Kilometern?</div>
                        <div class="estimation-interaction-area">
                          <div class="answer-interface" id="answerInterface">
                            <div class="answer-input-container">
                              <div class="input-with-unit">
                                <input type="number" class="form-control estimate-input" id="estimateInput" placeholder="Antwort" step="any" autocomplete="off">
                                <span class="unit-display" id="unitDisplay">km</span>
                              </div>
                            </div>
                          </div>
                          <button class="btn btn-lg submit-answer vhs-action-button" id="submitAnswerBtn" aria-pressed="false">Einloggen</button>
                          <div class="zone-submit-feedback d-none" id="zoneSubmitFeedback"></div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div></div>
              </div>
            </main>
            <aside class="score-box">
              <h6 class="score-box__title">Punkte</h6>
              <div class="score-box__list">
                <div class="score-box__row" data-points-earned="1" data-max-points="2"><span class="score-box__badge">1</span><span class="score-box__value">1/2</span></div>
                <div class="score-box__row" data-points-earned="0" data-max-points="2"><span class="score-box__badge">2</span><span class="score-box__value">0/2</span></div>
              </div>
            </aside>
          </div>
          <script>
            window.estimationSubmits = 0;
            document.getElementById('submitAnswerBtn').addEventListener('click', function () {
              window.estimationSubmits += 1;
              this.setAttribute('aria-pressed', 'true');
            });
          </script>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 1000})
        context.route(
            "http://participant.test/estimation-question",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='estimation-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.goto("http://participant.test/estimation-question")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell")
            page.wait_for_function(
                "document.querySelector('.qa-score-widget')?.parentElement?.classList.contains('question-content')"
            )

            estimate_input = page.locator("#estimateInput")
            unit = page.locator("#unitDisplay")
            submit = page.locator("#submitAnswerBtn")
            interaction = page.locator(".estimation-interaction-area")
            score_widget = page.locator(".qa-score-widget")
            input_box = estimate_input.bounding_box()
            unit_box = unit.bounding_box()
            submit_box = submit.bounding_box()
            score_box = score_widget.bounding_box()

            self.assertEqual(
                estimate_input.evaluate("el => getComputedStyle(el, '::placeholder').color"),
                "rgb(155, 161, 156)",
            )
            self.assertEqual(estimate_input.evaluate("el => getComputedStyle(el, '::placeholder').opacity"), "1")
            self.assertEqual(estimate_input.evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertEqual(estimate_input.evaluate("el => getComputedStyle(el).colorScheme"), "dark")
            self.assertEqual(estimate_input.evaluate("el => getComputedStyle(el).height"), "76px")
            self.assertEqual(estimate_input.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertEqual(unit.evaluate("el => getComputedStyle(el).height"), "76px")
            self.assertEqual(unit.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(unit.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertAlmostEqual(input_box["y"], unit_box["y"], delta=0.5)
            self.assertAlmostEqual(input_box["height"], unit_box["height"], delta=0.5)
            self.assertGreater(unit_box["x"], input_box["x"] + input_box["width"])
            self.assertAlmostEqual(submit_box["x"], input_box["x"], delta=0.5)
            self.assertGreater(submit_box["y"], input_box["y"] + input_box["height"])
            self.assertGreaterEqual(input_box["y"], score_box["y"] + score_box["height"])
            self.assertLessEqual(interaction.bounding_box()["width"], 360.5)

            estimate_input.focus()
            self.assertNotEqual(estimate_input.evaluate("el => getComputedStyle(el).boxShadow"), "none")
            estimate_input.fill("123.5")
            self.assertEqual(estimate_input.input_value(), "123.5")
            resting_shadow = submit.evaluate("el => getComputedStyle(el).boxShadow")
            submit.hover()
            self.assertNotEqual(submit.evaluate("el => getComputedStyle(el).boxShadow"), resting_shadow)
            submit.click()
            self.assertEqual(page.evaluate("window.estimationSubmits"), 1)
            self.assertEqual(submit.get_attribute("aria-pressed"), "true")
            self.assertNotEqual(submit.evaluate("el => getComputedStyle(el).transform"), "none")

            for width in (1180, 1024, 900):
                page.set_viewport_size({"width": width, "height": 1100})
                page.wait_for_timeout(100)
                current_widget_box = score_widget.bounding_box()
                current_interaction_box = interaction.bounding_box()
                self.assertGreaterEqual(
                    current_interaction_box["y"],
                    current_widget_box["y"] + current_widget_box["height"],
                )
                self.assertLessEqual(page.evaluate("document.body.scrollWidth"), width)

            page.set_viewport_size({"width": 820, "height": 1100})
            page.wait_for_timeout(100)
            tablet_widget_box = score_widget.bounding_box()
            tablet_input_box = estimate_input.bounding_box()
            self.assertGreaterEqual(tablet_input_box["y"], tablet_widget_box["y"] + tablet_widget_box["height"])
            self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.set_viewport_size({"width": 390, "height": 900})
            page.wait_for_timeout(100)
            phone_input_box = estimate_input.bounding_box()
            phone_unit_box = unit.bounding_box()
            phone_submit_box = submit.bounding_box()
            self.assertGreaterEqual(phone_unit_box["y"], phone_input_box["y"] + phone_input_box["height"])
            self.assertAlmostEqual(phone_submit_box["width"], interaction.bounding_box()["width"], delta=0.5)
            self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator(".vhs-theme-shell").count(), 0)
            self.assertEqual(interaction.evaluate("el => getComputedStyle(el).display"), "contents")
            self.assertEqual(unit.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_estimation_submitted_and_reveal_panels_avoid_score_widget(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Estimation - QuizMaster</title>
          <style>.d-none { display: none !important; }</style>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">Geografie Schaetzungen</h1>
              <span class="session-game-number">Spiel 3</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main">
              <div id="answerSubmittedState" class="game-state">
                <div class="submitted-card">
                  <div class="submitted-animation"><div class="check-animation"><span class="check-icon">OK</span></div></div>
                  <h2>Estimate Submitted!</h2>
                  <p>Waiting for other participants and the next question...</p>
                  <div class="submitted-details" style="background: white; border-radius: 24px">
                    <div class="estimate-summary">
                      <div class="summary-item"><span class="summary-label">Your Estimate:</span><span class="summary-value">445 km</span></div>
                      <div class="summary-item"><span class="summary-label">Points Earned:</span><span class="summary-value points">2</span></div>
                      <div class="summary-item"><span class="summary-label">Accuracy:</span><span class="summary-value accuracy">72.50%</span></div>
                    </div>
                  </div>
                </div>
              </div>
              <div id="correctAnswerState" class="game-state d-none">
                <div class="correct-answer-card">
                  <div class="comparison-display" style="background: rgb(255, 220, 40); border-radius: 24px">
                    <div class="comparison-item"><div class="comparison-label">Schaetzung</div><div class="comparison-value user-estimate">445 km</div></div>
                    <div class="comparison-vs">VS</div>
                    <div class="comparison-item"><div class="comparison-label">Korrekte Antwort</div><div class="comparison-value correct-answer">570 km</div></div>
                  </div>
                  <div class="performance-summary">
                    <div class="performance-badge poor" style="background: red; border-radius: 24px">
                      <span class="performance-text"><span class="performance-points">0 Punkte</span><span class="performance-deviation">Abweichung: 21.9%</span></span>
                    </div>
                    <div class="zone-explanation-card" style="display: block; background: white; border-radius: 24px">
                      <div class="zone-explanation-summary">Punktezonen</div>
                      <div class="zone-range-list">
                        <div class="zone-range-row active-zone"><span class="zone-range-label">0 bis 5 % Abweichung</span><span class="zone-range-points">5 Punkte</span></div>
                        <div class="zone-range-row"><span class="zone-range-label">5 bis 15 % Abweichung</span><span class="zone-range-points">3 Punkte</span></div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </main>
            <aside class="score-box">
              <h6 class="score-box__title">Punkte</h6>
              <div class="score-box__list">
                <div class="score-box__row" data-points-earned="2" data-max-points="5"><span class="score-box__badge">1</span><span class="score-box__value">2/5</span></div>
                <div class="score-box__row" data-points-earned="" data-max-points="5"><span class="score-box__badge">2</span><span class="score-box__value">__/5</span></div>
              </div>
            </aside>
          </div>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 1100})
        context.route(
            "http://participant.test/estimation-results",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='estimation-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))

        def overlaps(first, second):
            return not (
                first["x"] + first["width"] <= second["x"]
                or second["x"] + second["width"] <= first["x"]
                or first["y"] + first["height"] <= second["y"]
                or second["y"] + second["height"] <= first["y"]
            )

        try:
            page.goto("http://participant.test/estimation-results")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell")
            page.wait_for_function(
                "document.querySelector('#answerSubmittedState h2')?.textContent === 'ANTWORT EINGELOGGT!'"
            )

            submitted = page.locator("#answerSubmittedState .submitted-card")
            details = submitted.locator(".submitted-details")
            score_widget = page.locator(".qa-score-widget")
            self.assertEqual(submitted.locator("h2").inner_text(), "ANTWORT EINGELOGGT!")
            self.assertEqual(submitted.locator(".summary-label").first.inner_text(), "EINGELOGGTE ANTWORT")
            self.assertEqual(details.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(details.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(submitted.locator(".summary-value").first.evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertFalse(overlaps(submitted.bounding_box(), score_widget.bounding_box()))

            page.evaluate("""
              document.querySelector('#answerSubmittedState').classList.add('d-none');
              document.querySelector('#correctAnswerState').classList.remove('d-none');
            """)
            page.wait_for_function(
                "document.querySelector('.qa-score-widget')?.nextElementSibling?.id === 'correctAnswerState'"
                " || document.querySelector('#correctAnswerState')?.classList.contains('d-none') === false"
            )
            page.wait_for_timeout(100)

            reveal = page.locator("#correctAnswerState .correct-answer-card")
            comparison = reveal.locator(".comparison-display")
            performance = reveal.locator(".performance-badge")
            zones = reveal.locator(".zone-explanation-card")
            self.assertEqual(comparison.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(comparison.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 220, 40)")
            self.assertEqual(reveal.locator(".comparison-value").first.evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertEqual(performance.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(performance.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 0, 0)")
            self.assertEqual(zones.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(zones.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(reveal.locator(".zone-range-row").first.evaluate("el => getComputedStyle(el).borderBottomStyle"), "dashed")

            for width, height in ((1280, 1100), (1180, 1100), (900, 1200), (480, 1200)):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(100)
                self.assertFalse(overlaps(reveal.bounding_box(), score_widget.bounding_box()))
                self.assertLessEqual(page.evaluate("document.body.scrollWidth"), width)

            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_submitted_screen_and_score_rows_use_vhs_presentation_only(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Quiz - QuizMaster</title>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">QA Quick Quiz</h1>
              <span class="session-game-number">Spiel 2</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main">
              <div id="answerSubmittedState" class="game-state">
                <div class="submitted-card">
                  <div class="question-timer submitted-timer"><span id="playerTimeLeftSubmitted">12</span></div>
                  <div class="submitted-animation">
                    <svg class="check-icon" viewBox="0 0 24 24" style="color: rgb(22, 163, 74); filter: drop-shadow(0 0 4px green)">
                      <path d="M5 12l4 4L19 6" fill="none" stroke="currentColor"></path>
                    </svg>
                  </div>
                  <h2>Answer Submitted!</h2>
                  <p>Waiting for other participants and the next question...</p>
                  <div class="submitted-details" style="background: white; border-radius: 24px">
                    <div class="submitted-answer">Your answer: <strong id="submittedAnswerText">Titel: asad | Künstler: fasd</strong></div>
                    <div class="submitted-time">Submitted in <strong id="submittedTime">35.0</strong> seconds</div>
                  </div>
                </div>
              </div>
            </main>
            <aside class="quiz-score-box score-box" id="quizScoreBox">
              <h6 class="score-box__title">Punkte</h6>
              <div class="score-box__list" id="quizScoreList">
                <div class="score-box__row is-played" data-points-earned="0" data-max-points="5"><span class="score-box__badge">1</span><span class="score-box__value"><span>0/5</span></span></div>
                <div class="score-box__row is-upcoming" data-points-earned="" data-max-points="5"><span class="score-box__badge">2</span><span class="score-box__value"><span>__/5</span></span></div>
                <div class="score-box__row is-upcoming" data-points-earned="" data-max-points=""><span class="score-box__badge">3</span><span class="score-box__value"><span class="score-box__empty">____</span></span></div>
                <div class="score-box__row is-played" data-points-earned="10" data-max-points="10"><span class="score-box__badge">4</span><span class="score-box__value"><span>10/10</span></span></div>
                <div class="score-box__row is-played" data-points-earned="2" data-max-points="2"><span class="score-box__badge">5</span><span class="score-box__value"><span>2/2</span></span></div>
              </div>
            </aside>
          </div>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(
            "http://participant.test/quick-quiz-submitted",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='quiz-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.goto("http://participant.test/quick-quiz-submitted")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-answer-submitted-card")
            page.wait_for_function("document.querySelectorAll('.vhs-points-column').length === 2")

            card = page.locator("#answerSubmittedState .submitted-card")
            icon = card.locator(".check-icon")
            panel = card.locator(".submitted-details")
            self.assertEqual(card.locator(":scope > h2").inner_text(), "ANTWORT EINGELOGGT!")
            self.assertEqual(card.locator(":scope > p").inner_text(), "Warte auf die nächste Runde...")
            self.assertEqual(card.locator(":scope > p").evaluate("el => getComputedStyle(el).color"), "rgb(155, 161, 156)")
            self.assertEqual(icon.evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertEqual(icon.evaluate("el => getComputedStyle(el).filter"), "none")
            self.assertEqual(panel.locator(".vhs-submitted-answer-title").text_content(), "Eingeloggte Antwort")
            self.assertEqual(
                " ".join(panel.locator(".vhs-submitted-answer-values").inner_text().split()),
                "Titel: asad · Kuenstler: fasd",
            )
            self.assertEqual(panel.locator(".vhs-submitted-answer-time").inner_text(), "Eingeloggt nach 35.0 Sekunden")
            self.assertEqual(panel.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(panel.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")

            rows = page.locator(".vhs-points-row")
            self.assertEqual(rows.count(), 5)
            self.assertEqual(rows.nth(0).locator(".vhs-points-fraction").inner_text(), "0/5")
            self.assertEqual(rows.nth(1).locator(".vhs-points-fraction").inner_text(), "_/5")
            self.assertEqual(rows.nth(2).locator(".vhs-points-fraction").inner_text(), "_/_")
            self.assertEqual(rows.nth(3).locator(".vhs-points-fraction").inner_text(), "10/10")
            self.assertEqual(rows.nth(4).locator(".vhs-points-fraction").inner_text(), "2/2")
            self.assertEqual(page.locator(".vhs-points-unit").all_inner_texts(), ["P"] * 5)
            for index in range(rows.count()):
                fraction = rows.nth(index).locator(".vhs-points-fraction")
                unit = rows.nth(index).locator(".vhs-points-unit")
                self.assertEqual(fraction.evaluate("el => getComputedStyle(el).color"), "rgb(216, 216, 209)")
                self.assertEqual(unit.evaluate("el => getComputedStyle(el).color"), "rgb(216, 216, 209)")
                self.assertLess(abs(fraction.bounding_box()["y"] - unit.bounding_box()["y"]), 0.6)
                self.assertEqual(rows.nth(index).evaluate("el => getComputedStyle(el).whiteSpace"), "nowrap")
            first_column_units = page.locator(".vhs-points-column").nth(0).locator(".vhs-points-unit")
            unit_x = [first_column_units.nth(index).bounding_box()["x"] for index in range(first_column_units.count())]
            self.assertLess(max(unit_x) - min(unit_x), 0.6)
            first_column_fractions = page.locator(".vhs-points-column").nth(0).locator(".vhs-points-fraction")
            fraction_x = [first_column_fractions.nth(index).bounding_box()["x"] for index in range(first_column_fractions.count())]
            self.assertLess(max(fraction_x) - min(fraction_x), 0.6)
            self.assertEqual(rows.nth(0).evaluate("el => getComputedStyle(el).borderBottomStyle"), "dashed")
            self.assertEqual(page.locator(".vhs-points-column").nth(0).locator(".vhs-points-row:last-child").evaluate("el => getComputedStyle(el).borderBottomStyle"), "none")
            self.assertEqual(page.locator(".vhs-points-column").nth(1).evaluate("el => getComputedStyle(el).borderLeftStyle"), "dashed")
            self.assertEqual(rows.nth(0).evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertEqual(rows.nth(0).evaluate("el => getComputedStyle(el).boxShadow"), "none")

            toggle = page.locator(".qa-score-widget__toggle")
            toggle.click()
            self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
            toggle.click()
            self.assertEqual(toggle.get_attribute("aria-expanded"), "true")

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(100)
            columns = page.locator(".vhs-points-column")
            self.assertAlmostEqual(columns.nth(0).bounding_box()["x"], columns.nth(1).bounding_box()["x"], delta=0.6)
            self.assertGreater(columns.nth(1).bounding_box()["y"], columns.nth(0).bounding_box()["y"])
            self.assertEqual(columns.nth(1).evaluate("el => getComputedStyle(el).borderLeftStyle"), "none")
            self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(card.locator(":scope > h2").inner_text(), "Answer Submitted!")
            self.assertEqual(card.locator(":scope > p").inner_text(), "Waiting for other participants and the next question...")
            self.assertEqual(panel.locator(".vhs-submitted-answer-title").count(), 0)
            self.assertEqual(page.locator("#quizScoreList > .vhs-points-column").count(), 0)
            self.assertEqual(page.locator("#quizScoreList > .score-box__row").count(), 5)
            self.assertEqual(page.locator("#quizScoreList > .score-box__row").nth(0).locator(".score-box__value").inner_text(), "0/5")
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_vhs_reveal_localizes_values_and_removes_duplicate_comparison(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <title>Quiz - QuizMaster</title>
          <div class="play-container">
            <header class="quiz-header">
              <h1 class="quiz-title">QA Quick Quiz</h1>
              <span class="session-game-number">Spiel 3</span>
              <span class="participant-name">Mia</span>
            </header>
            <main class="quiz-main">
              <div class="row"><div class="col-lg-8">
                <div id="correctAnswerState" class="game-state" data-vhs-reveal-game-type="quiz" data-vhs-reveal-question-type="true_false">
                  <div class="correct-answer-card" style="background: white; border-radius: 24px">
                    <div class="answer-reveal">
                      <div class="reveal-icon" style="color: blue; filter: drop-shadow(0 0 4px green)">
                        <svg viewBox="0 0 24 24"><path d="M12 2v20" fill="none" stroke="currentColor"></path></svg>
                      </div>
                      <h2>Correct Answer Revealed!</h2>
                      <div class="answer-display"><span class="answer-value" id="correctAnswerDisplay">True</span></div>
                    </div>
                    <div class="revealed-question" style="background: white; border-radius: 20px">
                      <div class="revealed-question-label">Question</div>
                      <div class="revealed-question-text" id="revealedQuestionText">Die Erde ist eine Scheibe.</div>
                    </div>
                    <div class="comparison-display">
                      <div class="comparison-item" style="background: white; border-radius: 20px">
                        <div class="comparison-label">Your Answer</div>
                        <div class="comparison-value user-answer" id="userAnswerDisplay">False</div>
                      </div>
                      <div class="comparison-vs">VS</div>
                      <div class="comparison-item" style="background: white; border-radius: 20px">
                        <div class="comparison-label">Correct Answer</div>
                        <div class="comparison-value correct-answer" id="correctAnswerComparison">True</div>
                      </div>
                    </div>
                  </div>
                </div>
              </div></div>
            </main>
            <aside class="quiz-score-box score-box" id="quizScoreBox">
              <h6 class="score-box__title">Punkte</h6>
              <div class="score-box__list" id="quizScoreList">
                <div class="score-box__row is-played" data-points-earned="0" data-max-points="1"><span class="score-box__badge">1</span><span class="score-box__value"><span>X</span></span></div>
                <div class="score-box__row is-played" data-points-earned="1" data-max-points="1"><span class="score-box__badge">2</span><span class="score-box__value"><span>✓</span></span></div>
              </div>
            </aside>
          </div>
        """
        context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(
            "http://participant.test/quick-quiz-reveal",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=f"<!doctype html><html><head></head><body class='quiz-play-page'>{fixture}{widget}</body></html>",
            ),
        )
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))

        def overlaps(first, second):
            return not (
                first["x"] + first["width"] <= second["x"]
                or second["x"] + second["width"] <= first["x"]
                or first["y"] + first["height"] <= second["y"]
                or second["y"] + second["height"] <= first["y"]
            )

        try:
            page.goto("http://participant.test/quick-quiz-reveal")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-reveal-card")
            page.wait_for_function("document.querySelector('.vhs-reveal-correct-value')?.textContent === 'Stimmt'")

            state = page.locator("#correctAnswerState")
            card = state.locator(".vhs-reveal-card")
            self.assertEqual(card.locator(".vhs-reveal-label").inner_text(), "DIE RICHTIGE ANTWORT IST")
            self.assertEqual(card.locator(".vhs-reveal-label").evaluate("el => getComputedStyle(el).color"), "rgb(155, 161, 156)")
            self.assertEqual(card.locator(".vhs-reveal-correct-value").inner_text(), "Stimmt")
            self.assertEqual(card.locator(".vhs-reveal-correct-value").evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertEqual(card.locator(".vhs-reveal-icon").evaluate("el => getComputedStyle(el).color"), "rgb(233, 223, 202)")
            self.assertEqual(card.locator(".vhs-reveal-icon").evaluate("el => getComputedStyle(el).filter"), "none")
            self.assertEqual(card.locator(".vhs-reveal-answer-panel .vhs-reveal-panel-label").inner_text(), "GEGEBENE ANTWORT")
            self.assertEqual(card.locator("#userAnswerDisplay").inner_text(), "Stimmt nicht")
            self.assertEqual(card.locator(".vhs-reveal-prompt-panel .vhs-reveal-panel-label").inner_text(), "BEHAUPTUNG")
            self.assertEqual(card.locator("#revealedQuestionText").inner_text(), "Die Erde ist eine Scheibe.")
            self.assertEqual(card.locator(".vhs-reveal-answer-panel").evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(card.locator(".vhs-reveal-answer-panel").evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(card.locator(".vhs-reveal-vs").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(card.locator(".vhs-reveal-duplicate").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(card.locator(".vhs-reveal-correct-value:visible").count(), 1)

            answer_box = card.locator(".vhs-reveal-answer-panel").bounding_box()
            prompt_box = card.locator(".vhs-reveal-prompt-panel").bounding_box()
            self.assertGreaterEqual(prompt_box["y"], answer_box["y"] + answer_box["height"] + 23)

            score_panel = page.locator(".qa-score-widget__body")
            self.assertEqual(page.locator(".vhs-points-fraction").all_inner_texts(), ["0/1", "1/1"])
            self.assertEqual(page.locator(".vhs-points-unit").all_inner_texts(), ["P", "P"])
            self.assertNotIn("X", score_panel.inner_text())
            self.assertNotIn("✓", score_panel.inner_text())
            self.assertEqual(page.locator(".vhs-points-row").first.evaluate("el => getComputedStyle(el).borderRadius"), "0px")

            state.evaluate("el => { el.dataset.vhsRevealQuestionType = 'multiple_choice'; }")
            page.wait_for_function("document.querySelector('.vhs-reveal-prompt-panel .vhs-reveal-panel-label').textContent === 'FRAGE'")
            state.evaluate("el => { el.dataset.vhsRevealQuestionType = ''; el.dataset.vhsRevealGameType = 'sorting_ladder'; }")
            page.wait_for_function("document.querySelector('.vhs-reveal-prompt-panel .vhs-reveal-panel-label').textContent === 'AUFGABE'")
            state.evaluate("el => { el.dataset.vhsRevealQuestionType = 'bool'; el.dataset.vhsRevealGameType = 'quiz'; }")
            page.wait_for_function("document.querySelector('.vhs-reveal-prompt-panel .vhs-reveal-panel-label').textContent === 'BEHAUPTUNG'")

            long_answer = "Eine sehr lange korrekte Freitextantwort mit mehreren Begriffen, die vollstaendig sichtbar bleibt und sauber in mehrere Zeilen umbricht"
            card.locator(".vhs-reveal-correct-value").evaluate(
                "(el, value) => { el.dataset.vhsOriginalText = value; el.textContent = value; }",
                long_answer,
            )

            for viewport in (
                {"width": 1280, "height": 1100},
                {"width": 1180, "height": 1100},
                {"width": 1024, "height": 1100},
                {"width": 900, "height": 1100},
                {"width": 820, "height": 1100},
                {"width": 390, "height": 1100},
            ):
                page.set_viewport_size(viewport)
                page.wait_for_timeout(100)
                widget_box = page.locator(".qa-score-widget").bounding_box()
                reveal_boxes = [
                    card.locator(".vhs-reveal-hero").bounding_box(),
                    card.locator(".vhs-reveal-answer-panel").bounding_box(),
                    card.locator(".vhs-reveal-prompt-panel").bounding_box(),
                ]
                self.assertFalse(any(overlaps(box, widget_box) for box in reveal_boxes))
                self.assertLessEqual(
                    card.locator(".vhs-reveal-correct-value").bounding_box()["width"],
                    card.bounding_box()["width"] + 0.5,
                )
                self.assertLessEqual(page.evaluate("document.body.scrollWidth"), page.evaluate("window.innerWidth"))

            card.locator(".vhs-reveal-correct-value").evaluate(
                "el => { el.dataset.vhsOriginalText = 'True'; el.textContent = 'Stimmt'; }"
            )

            toggle = page.locator(".qa-score-widget__toggle")
            toggle.click()
            self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
            toggle.click()
            self.assertEqual(toggle.get_attribute("aria-expanded"), "true")

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(state.locator(".vhs-reveal-label").count(), 0)
            self.assertEqual(state.locator(".answer-reveal > h2").inner_text(), "Correct Answer Revealed!")
            self.assertEqual(state.locator("#correctAnswerDisplay").inner_text(), "True")
            self.assertEqual(state.locator("#userAnswerDisplay").inner_text(), "False")
            self.assertEqual(state.locator(".revealed-question-label").inner_text(), "Question")
            self.assertNotEqual(state.locator(".comparison-vs").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_quick_quiz_selection_and_two_accent_colors_stay_synchronized(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfÃƒÂ¼gbar: {self._playwright_error}")

        quiz_content = read_text("templates/quiz/play.html")
        method_start = quiz_content.index("setSelectedAnswerOption(option, key) {")
        method_end = quiz_content.index("startQuestionTimer(timeLimit)", method_start)
        selection_method = quiz_content[method_start:method_end].strip()
        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        fixture = """
          <div id="answerOptions">
            <div id="answerA" class="answer-option" data-qa-answer-selectable="true" style="border: 2px solid #000; border-radius: 14px" aria-pressed="false">
              <span class="option-text">Antwort A</span>
            </div>
            <div id="answerB" class="answer-option" data-qa-answer-selectable="true" style="border: 2px solid #000; border-radius: 14px" aria-pressed="false">
              <span class="option-text">Antwort B</span>
            </div>
          </div>
          <button id="submitAnswerBtn" disabled>Einloggen</button>
        """
        html = f"<!doctype html><html><body>{fixture}{widget}</body></html>"
        context = self._browser.new_context()
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        page.route(
            "http://qa.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=html),
        )
        try:
            page.goto("http://qa.test/participant")
            page.wait_for_selector("#answerA.qa-theme-button")
            page.evaluate(f"window.quickQuizPlayer = {{ selectedAnswer: null, {selection_method} }}")

            page.evaluate("quickQuizPlayer.setSelectedAnswerOption(document.getElementById('answerA'), 'A')")
            self.assertEqual(page.locator(".answer-option.selected").count(), 1)
            self.assertEqual(page.locator(".answer-option.is-theme-clicked").count(), 1)
            self.assertEqual(page.locator("#answerA").get_attribute("aria-pressed"), "true")
            self.assertEqual(page.evaluate("quickQuizPlayer.selectedAnswer"), "A")
            self.assertFalse(page.locator("#submitAnswerBtn").is_disabled())
            self.assertEqual(page.locator("#answerA").evaluate("el => getComputedStyle(el).color"), "rgb(0, 0, 0)")
            self.assertEqual(page.locator("#answerA .option-text").evaluate("el => getComputedStyle(el).fontWeight"), "700")
            self.assertEqual(
                page.locator("#answerA").locator("xpath=..").evaluate("el => getComputedStyle(el, '::after').borderRadius"),
                page.locator("#answerA").evaluate("el => getComputedStyle(el).borderRadius"),
            )
            page.wait_for_timeout(380)
            selected_frame_mask = page.locator("#answerA").locator("xpath=..").evaluate(
                "el => getComputedStyle(el, '::after').webkitMaskSize || getComputedStyle(el, '::after').maskSize"
            )
            self.assertEqual(selected_frame_mask.count("100%"), 4)

            page.evaluate("quickQuizPlayer.setSelectedAnswerOption(document.getElementById('answerA'), 'A')")
            self.assertEqual(page.locator(".answer-option.selected").count(), 0)
            self.assertEqual(page.locator(".answer-option.is-theme-clicked").count(), 0)
            self.assertEqual(page.locator("#answerA").get_attribute("aria-pressed"), "false")
            self.assertIsNone(page.evaluate("quickQuizPlayer.selectedAnswer"))
            self.assertTrue(page.locator("#submitAnswerBtn").is_disabled())

            page.evaluate("quickQuizPlayer.setSelectedAnswerOption(document.getElementById('answerA'), 'A')")
            page.evaluate("quickQuizPlayer.setSelectedAnswerOption(document.getElementById('answerB'), 'B')")
            self.assertEqual(page.locator(".answer-option.selected").count(), 1)
            self.assertTrue(page.locator("#answerB").evaluate("el => el.classList.contains('selected')"))
            self.assertEqual(page.evaluate("quickQuizPlayer.selectedAnswer"), "B")

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'arcade'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.locator("#participant-custom-colors-enabled").evaluate(
                "el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            for input_id, value in (("participant-accent-color", "#123456"), ("participant-accent-color-2", "#abcdef")):
                page.locator(f"#{input_id}").evaluate(
                    "(el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); }",
                    value,
                )

            self.assertEqual(
                page.locator("#answerB").evaluate("el => getComputedStyle(el, '::after').backgroundColor"),
                "rgb(18, 52, 86)",
            )
            self.assertEqual(
                page.locator("#answerB").evaluate("el => getComputedStyle(el, '::before').backgroundColor"),
                "rgb(171, 205, 239)",
            )
            self.assertEqual(page.evaluate("localStorage.getItem('participant_interface_accent_color')"), "#123456")
            self.assertEqual(page.evaluate("localStorage.getItem('participant_interface_accent_color_2')"), "#abcdef")

            page.reload()
            page.wait_for_selector("#answerB.qa-theme-button")
            page.evaluate(f"window.quickQuizPlayer = {{ selectedAnswer: null, {selection_method} }}")
            page.evaluate("quickQuizPlayer.setSelectedAnswerOption(document.getElementById('answerB'), 'B')")
            self.assertEqual(page.locator("html").get_attribute("data-participant-theme"), "arcade")
            self.assertEqual(page.locator("#participant-accent-color").input_value(), "#123456")
            self.assertEqual(page.locator("#participant-accent-color-2").input_value(), "#abcdef")
            self.assertEqual(page.locator("#answerB").evaluate("el => getComputedStyle(el, '::after').backgroundColor"), "rgb(18, 52, 86)")
            self.assertEqual(page.locator("#answerB").evaluate("el => getComputedStyle(el, '::before').backgroundColor"), "rgb(171, 205, 239)")

            page.locator("#participant-high-contrast").evaluate(
                "el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator("#answerB").evaluate("el => getComputedStyle(el, '::after').backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(page.locator("#answerB").evaluate("el => getComputedStyle(el, '::before').backgroundColor"), "rgb(0, 0, 0)")
            page.locator("#participant-invert-colors").evaluate(
                "el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertEqual(page.locator("#answerB").evaluate("el => getComputedStyle(el, '::after').backgroundColor"), "rgb(0, 0, 0)")
            self.assertEqual(page.locator("#answerB").evaluate("el => getComputedStyle(el, '::before').backgroundColor"), "rgb(255, 255, 255)")

            page.locator("#participant-interface-reset").evaluate("el => el.click()")
            self.assertTrue(page.locator("#participant-accent-color").is_disabled())
            self.assertTrue(page.locator("#participant-accent-color-2").is_disabled())
            self.assertIsNone(page.evaluate("localStorage.getItem('participant_interface_accent_color')"))
            self.assertIsNone(page.evaluate("localStorage.getItem('participant_interface_accent_color_2')"))
            self.assertEqual(
                page.locator("#answerB").locator("xpath=..").evaluate("el => getComputedStyle(el, '::after').borderRadius"),
                page.locator("#answerB").evaluate("el => getComputedStyle(el).borderRadius"),
            )
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_who_that_vhs_states_are_aligned_responsive_and_theme_scoped(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        fixture = """
          <div class="play-container" data-participant-name="Mia">
            <main class="quiz-main">
              <div class="who-that-play-layout">
                <div class="who-that-main-column">
                  <div id="questionState" class="game-state">
                    <div class="question-card">
                      <div class="question-content">
                        <div class="question-text">Wer ist diese Person?</div>
                        <div class="who-that-interaction-stack">
                          <div class="photo-display" id="photoDisplay"><div class="question-photo"></div></div>
                          <div class="answer-interface"><div class="answer-input-container"><div class="input-container">
                            <input class="name-input" placeholder="Type the person's name...">
                            <span class="input-icon">ICON</span>
                          </div></div></div>
                          <button class="btn submit-answer vhs-who-that-submit">Einloggen</button>
                        </div>
                      </div>
                    </div>
                  </div>
                  <div id="answerSubmittedState" class="game-state d-none">
                    <div class="submitted-card">
                      <h2>Answer Submitted!</h2>
                      <p class="who-that-submitted-waiting">Waiting for the other participants before the answer is revealed.</p>
                      <div class="submitted-details"><div class="summary-item">
                        <span class="who-that-submitted-answer-label">Your Answer:</span>
                        <span class="who-that-submitted-answer-value">Ada</span>
                      </div></div>
                    </div>
                  </div>
                  <div id="correctAnswerState" class="game-state d-none">
                    <div class="correct-answer-card"><div class="answer-reveal">
                      <div class="reveal-icon">!</div><h2>Correct Answer Revealed!</h2>
                      <div class="photo-display reveal-photo-display"><div class="question-photo"></div></div>
                      <div class="answer-display who-that-reveal-answer"><span id="correctAnswerDisplay">Ada</span></div>
                    </div><div class="comparison-display who-that-reveal-comparison">
                      <div class="comparison-item"><div class="comparison-label">Your Answer</div><div id="userAnswerDisplay">Mia</div></div>
                      <div class="comparison-vs">VS</div>
                      <div class="comparison-item"><div class="comparison-label">Correct Answer</div><div id="correctAnswerComparison">Ada</div></div>
                    </div><div class="performance-summary"><div id="performanceBadge">Better Luck Next Time!</div></div></div>
                  </div>
                </div>
                <aside class="score-box">Punkte</aside>
              </div>
            </main>
          </div>
        """
        fixture_css = """
          <style>
            * { box-sizing: border-box; }
            body { margin: 0; }
            .d-none { display: none !important; }
            .play-container { width: min(100%, 1280px); margin-inline: auto; }
            .who-that-play-layout { display: grid; grid-template-columns: minmax(0, 1fr) 220px; gap: 24px; }
            .question-photo { width: 100%; height: 220px; }
          </style>
        """
        context = self._browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.set_content(f"<!doctype html><html><body class='who-that-play-page'>{fixture_css}{fixture}{widget}</body></html>")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell #questionState")
            self.assertEqual(page.locator(".input-icon").evaluate("el => getComputedStyle(el).display"), "none")
            page.locator(".vhs-who-that-submit").hover()
            page.wait_for_timeout(220)
            self.assertEqual(
                page.locator(".vhs-who-that-submit").evaluate("el => getComputedStyle(el).transform"),
                "matrix(1, 0, 0, 1, -4, -3)",
            )
            page.locator(".vhs-who-that-submit").evaluate("el => el.classList.add('loading')")
            page.wait_for_timeout(220)
            self.assertEqual(
                page.locator(".vhs-who-that-submit").evaluate("el => getComputedStyle(el).transform"),
                "matrix(1, 0, 0, 1, 3, 3)",
            )
            page.locator(".vhs-who-that-submit").evaluate("el => el.classList.remove('loading')")
            page.mouse.move(0, 0)
            page.wait_for_timeout(220)

            for width in (1440, 1180, 1000, 820, 640, 390):
                with self.subTest(width=width):
                    page.set_viewport_size({"width": width, "height": 1000})
                    centers = page.locator("#photoDisplay, .name-input, .vhs-who-that-submit").evaluate_all(
                        "els => els.map(el => { const r = el.getBoundingClientRect(); return r.left + r.width / 2; })"
                    )
                    self.assertLessEqual(max(centers) - min(centers), 1)
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"),
                        page.evaluate("document.documentElement.clientWidth") + 1,
                    )

            page.locator("#questionState").evaluate("el => el.classList.add('d-none')")
            page.locator("#answerSubmittedState").evaluate("el => el.classList.remove('d-none')")
            page.wait_for_function("document.querySelector('#answerSubmittedState h2').textContent === 'ANTWORT EINGELOGGT!'")
            self.assertEqual(
                page.locator(".who-that-submitted-waiting").inner_text(),
                "Warte auf die nächste Runde...",
            )
            self.assertEqual(
                page.locator(".who-that-submitted-answer-value").evaluate("el => getComputedStyle(el).color"),
                "rgb(233, 223, 202)",
            )

            page.locator("#answerSubmittedState").evaluate("el => el.classList.add('d-none')")
            page.locator("#correctAnswerState").evaluate("el => el.classList.remove('d-none')")
            page.wait_for_selector("#correctAnswerState .vhs-reveal-card")
            self.assertEqual(page.locator("#correctAnswerState h2").inner_text(), "DIE RICHTIGE ANTWORT IST")
            self.assertEqual(page.locator("#correctAnswerComparison").evaluate("el => getComputedStyle(el.parentElement).display"), "none")
            self.assertEqual(page.locator("#performanceBadge").evaluate("el => getComputedStyle(el).display"), "none")

            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'standard'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            self.assertNotEqual(page.locator(".input-icon").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertNotEqual(page.locator("#performanceBadge").evaluate("el => getComputedStyle(el).display"), "none")
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_estimation_rank_results_use_scoped_vhs_panel_without_overflow(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfuegbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        vhs_css = read_text("static/themes/vhs/vhs.css")
        rows = "".join(
            f"""
              <div class="rank-result-row{' is-self' if index == 0 else ''}">
                <div class="rank-result-position">#{index + 1}</div>
                <div class="rank-result-name">{'Teilnehmer mit einem sehr langen Namen' if index == 0 else f'Person {index + 1}'}</div>
                <div class="rank-result-points">
                  <span class="rank-result-points__default">{12 - index} pts</span>
                  <span class="rank-result-points__vhs">{12 - index} Punkte</span>
                </div>
              </div>
            """
            for index in range(12)
        )
        fixture = f"""
          <div class="play-container" data-participant-name="Teilnehmer mit einem sehr langen Namen">
            <main class="quiz-main">
              <div id="correctAnswerState" class="game-state">
                <div class="correct-answer-card">
                  <div class="rank-results-card" id="rankResultsCard">
                    <div class="rank-results-title">
                      <span class="rank-results-title__default">Ranking for this question</span>
                      <span class="rank-results-title__vhs">RANGLISTE DIESER FRAGE</span>
                    </div>
                    <div class="rank-results-list" id="rankResultsList">{rows}</div>
                  </div>
                </div>
              </div>
            </main>
            <aside class="score-box">Punkte</aside>
          </div>
        """
        base_css = """
          <style>
            * { box-sizing: border-box; }
            body { margin: 0; }
            .play-container { width: min(100%, 1280px); margin-inline: auto; }
            .rank-results-title__vhs, .rank-result-points__vhs { display: none; }
          </style>
        """
        context = self._browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        browser_errors = []
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        try:
            page.set_content(f"<!doctype html><html><body class='estimation-play-page'>{base_css}{fixture}{widget}</body></html>")
            page.add_style_tag(content=vhs_css)
            page.locator("#participant-theme-select").evaluate(
                "el => { el.value = 'vhs'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            page.wait_for_selector(".vhs-theme-shell #rankResultsCard")

            self.assertTrue(page.locator(".rank-results-title__vhs").is_visible())
            self.assertFalse(page.locator(".rank-results-title__default").is_visible())
            self.assertEqual(page.locator(".rank-results-title__vhs").inner_text(), "RANGLISTE DIESER FRAGE")
            self.assertEqual(page.locator(".rank-result-points__vhs").first.inner_text(), "12 Punkte")
            self.assertEqual(page.locator(".rank-result-row").first.evaluate("el => getComputedStyle(el).display"), "grid")
            self.assertEqual(page.locator(".rank-result-row").first.evaluate("el => getComputedStyle(el).borderRadius"), "0px")
            self.assertNotEqual(page.locator(".rank-result-row").first.evaluate("el => getComputedStyle(el).backgroundColor"), "rgb(255, 255, 255)")
            self.assertGreater(
                page.locator("#rankResultsList").evaluate("el => el.scrollHeight"),
                page.locator("#rankResultsList").evaluate("el => el.clientHeight"),
            )

            for width in (1440, 1180, 900, 768, 640, 390):
                with self.subTest(width=width):
                    page.set_viewport_size({"width": width, "height": 900})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"),
                        page.evaluate("document.documentElement.clientWidth") + 1,
                    )

            page.locator(".qa-score-widget__toggle").click()
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"),
                page.evaluate("document.documentElement.clientWidth") + 1,
            )
            self.assertEqual(browser_errors, [])
        finally:
            page.close()
            context.close()

    def test_score_source_layout_stays_centered_at_all_viewport_widths(self):
        if not self._playwright_available:
            self.skipTest(f"Playwright/Chromium nicht verfÃ¼gbar: {self._playwright_error}")

        widget = render_to_string(
            "includes/accessibility_widget.html",
            {"participant_options_menu": True},
        )
        fixtures = {
            "bootstrap-lg-8": """
              <div class="qa-test-container">
                <div id="stage" class="row">
                  <div class="col-lg-8"><div class="game-state"><div id="central">Spiel</div></div></div>
                  <div class="col-lg-4"><aside class="score-box">Punkte</aside></div>
                </div>
              </div>
            """,
            "bootstrap-xl-9": """
              <div class="qa-test-container qa-test-container-fluid">
                <div id="stage" class="row">
                  <div class="col-xl-9"><div class="game-state"><div id="central">Spiel</div></div></div>
                  <div class="col-xl-3"><aside class="score-box">Punkte</aside></div>
                </div>
              </div>
            """,
            "assign-grid": """
              <div class="qa-test-container">
                <div id="stage" class="assign-play-layout">
                  <div class="assign-main-column"><div class="game-state"><div id="central">Spiel</div></div></div>
                  <aside class="score-box">Punkte</aside>
                </div>
              </div>
            """,
            "who-that-grid": """
              <div class="qa-test-container">
                <div id="stage" class="who-that-play-layout">
                  <div class="who-that-main-column"><div class="game-state"><div id="central">Spiel</div></div></div>
                  <aside class="score-box">Punkte</aside>
                </div>
              </div>
            """,
        }
        fixture_css = """
          <style>
            * { box-sizing: border-box; }
            body { margin: 0; }
            .qa-test-container { width: min(100%, 1140px); margin-inline: auto; padding-inline: 12px; }
            .qa-test-container-fluid { width: 100%; max-width: none; }
            .row { display: flex; width: 100%; }
            .col-lg-8 { width: 66.666667%; }
            .col-lg-4 { width: 33.333333%; }
            .col-xl-9 { width: 75%; }
            .col-xl-3 { width: 25%; }
            .assign-play-layout { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 1.5rem; }
            .who-that-play-layout { display: grid; grid-template-columns: minmax(0, 1fr) 220px; gap: 1.5rem; }
            #central { width: 100%; min-height: 180px; border: 2px solid; }
          </style>
        """

        for fixture_name, markup in fixtures.items():
            with self.subTest(layout=fixture_name):
                context = self._browser.new_context()
                page = context.new_page()
                try:
                    page.set_content(f"<!doctype html><html><body>{fixture_css}{markup}{widget}</body></html>")
                    page.wait_for_selector("#stage.qa-participant-stage")
                    for viewport_width in (1440, 1200, 1024, 991, 768, 390):
                        page.set_viewport_size({"width": viewport_width, "height": 900})
                        center_delta = page.locator("#central").evaluate(
                            "el => { const rect = el.getBoundingClientRect(); return Math.abs((rect.left + rect.width / 2) - window.innerWidth / 2); }"
                        )
                        self.assertLessEqual(center_delta, 0.6, f"{fixture_name} at {viewport_width}px")

                    page.locator("#participant-score-position-select").evaluate(
                        "el => { el.value = 'bottom-right'; el.dispatchEvent(new Event('change', { bubbles: true })); }"
                    )
                    page.locator(".qa-score-widget__toggle").click()
                    page.locator("#participant-options-toggle").click()
                    center_delta = page.locator("#central").evaluate(
                        "el => { const rect = el.getBoundingClientRect(); return Math.abs((rect.left + rect.width / 2) - window.innerWidth / 2); }"
                    )
                    self.assertLessEqual(center_delta, 0.6)
                finally:
                    page.close()
                    context.close()


if __name__ == "__main__":
    unittest.main()
