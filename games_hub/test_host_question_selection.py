from pathlib import Path
import unittest

from django.test import SimpleTestCase

from games_hub.models import HubGameStep
from games_hub.playwright_e2e import start_chromium_browser


REPO_ROOT = Path(__file__).resolve().parent.parent


class HostQuestionSelectionContractTests(SimpleTestCase):
    overview_templates = {
        'assign': 'assign_monitor.html',
        'where': 'where_monitor.html',
        'who': 'who_lying_monitor.html',
        'who_that': 'who_that_monitor.html',
        'blackjack': 'blackjack_monitor.html',
        'sorting_ladder': 'sorting_ladder_monitor.html',
        'clue_rush': 'clue_rush_monitor.html',
        'wer_weiss_mehr': 'wer_weiss_mehr_monitor.html',
    }

    def test_every_registered_game_is_explicitly_classified(self):
        classified = set(self.overview_templates) | {
            'quiz',
            'estimation',
            'wann_war_das',
            'buzzer',
            'host_points',
        }
        self.assertEqual(
            classified,
            {game_key for game_key, _label in HubGameStep.GAME_CHOICES},
        )

    def test_question_overviews_opt_into_host_only_selection(self):
        for game_key, template_name in self.overview_templates.items():
            with self.subTest(game_key=game_key):
                source = (REPO_ROOT / 'templates' / 'admin_dashboard' / template_name).read_text(
                    encoding='utf-8'
                )
                self.assertIn('data-host-question-selection', source)
                self.assertIn('data-host-question-item', source)
                self.assertIn('data-host-question-select', source)
                self.assertRegex(source, r'(FRAGE|AUFGABE|SET(?:/FRAGE)?) WÄHLEN')

    def test_quick_quiz_uses_persisted_dedicated_question_screen(self):
        source = (REPO_ROOT / 'templates' / 'admin_dashboard' / 'quiz_monitor.html').read_text(
            encoding='utf-8'
        )
        self.assertIn('id="questionSelection"', source)
        self.assertIn('id="questionDetailScreen"', source)
        self.assertIn('id="sendPreparedQuestionBtn"', source)
        self.assertIn("type: 'admin_prepare_question'", source)
        self.assertNotIn('data-host-question-select data-host-select-label', source)

    def test_estimation_uses_persisted_dedicated_question_screen(self):
        source = (REPO_ROOT / 'templates' / 'admin_dashboard' / 'estimation_monitor.html').read_text(
            encoding='utf-8'
        )
        self.assertIn('id="questionSelection"', source)
        self.assertIn('id="preparedQuestion"', source)
        self.assertIn('id="sendPreparedQuestionBtn"', source)
        self.assertIn("type: 'admin_prepare_question'", source)
        self.assertNotIn('data-host-question-select data-host-select-label', source)

    def test_existing_special_cases_are_not_forced_into_question_selection(self):
        for template_name in ('buzzer_monitor.html', 'host_points_monitor.html'):
            source = (REPO_ROOT / 'templates' / 'admin_dashboard' / template_name).read_text(
                encoding='utf-8'
            )
            self.assertNotIn('data-host-question-select', source)

    def test_wann_war_das_keeps_local_selection_before_public_send(self):
        source = (
            REPO_ROOT / 'templates' / 'admin_dashboard' / 'wann_war_das_monitor.html'
        ).read_text(encoding='utf-8')
        self.assertIn('id="questionSelect"', source)
        self.assertIn('id="startQuestionBtn">FRAGE SENDEN', source)
        self.assertIn("'admin_start_question'", source)

    def test_response_action_labels_match_the_game_mechanic(self):
        quiz = (REPO_ROOT / 'templates' / 'admin_dashboard' / 'quiz_monitor.html').read_text(
            encoding='utf-8'
        )
        self.assertIn('ANTWORTEN ANZEIGEN', quiz)
        self.assertIn('FRAGE FREIGEBEN', quiz)
        for template_name in (
            'estimation_monitor.html',
            'blackjack_monitor.html',
            'wann_war_das_monitor.html',
            'wer_weiss_mehr_monitor.html',
        ):
            source = (REPO_ROOT / 'templates' / 'admin_dashboard' / template_name).read_text(
                encoding='utf-8'
            )
            self.assertIn('ANTWORT FREIGEBEN', source)


class HostQuestionSelectionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.playwright, cls.browser = start_chromium_browser(headless=True)
            cls.browser_error = None
        except Exception as exc:
            cls.playwright = None
            cls.browser = None
            cls.browser_error = exc

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.close()
        if cls.playwright:
            cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        if not self.browser:
            self.skipTest(f'Playwright/Chromium nicht verfuegbar: {self.browser_error}')

    def test_first_click_selects_locally_and_second_click_reaches_send_handler(self):
        script = (REPO_ROOT / 'static' / 'js' / 'host-question-selection.js').read_text(
            encoding='utf-8'
        )
        page = self.browser.new_page()
        try:
            page.set_content(f'''
                <div data-host-question-selection>
                    <h6>Fragenübersicht</h6>
                    <div data-host-question-item data-host-question-text="Vollständige Frage eins">
                        <div data-host-question-text-target>Vorschau eins…</div>
                        <button data-host-question-select data-host-select-label="FRAGE WÄHLEN"
                                data-host-send-label="FRAGE SENDEN" data-host-selection-noun="Frage">
                            FRAGE WÄHLEN
                        </button>
                    </div>
                    <div data-host-question-item data-host-question-text="Vollständige Frage zwei">
                        <div data-host-question-text-target>Vorschau zwei…</div>
                        <button data-host-question-select>FRAGE WÄHLEN</button>
                    </div>
                    <div data-host-question-item data-host-question-text="Beendete Frage">
                        <button data-host-question-select disabled>Beendet</button>
                    </div>
                </div>
                <script>window.lucide = {{createIcons() {{}}}}; {script}</script>
                <script>
                    window.sendCount = 0;
                    document.querySelector('[data-host-question-select]').addEventListener(
                        'click', () => window.sendCount += 1
                    );
                </script>
            ''')
            first = page.locator('[data-host-question-select]').first
            first.click()
            self.assertEqual(page.evaluate('window.sendCount'), 0)
            self.assertEqual(first.inner_text().strip(), 'FRAGE SENDEN')
            self.assertEqual(
                page.locator('[data-host-question-text-target]').first.inner_text(),
                'Vollständige Frage eins',
            )
            self.assertTrue(page.locator('[data-host-question-item]').nth(1).is_hidden())
            self.assertEqual(
                page.locator('[data-host-question-select]').nth(2).inner_text().strip(),
                'Beendet',
            )

            first.click()
            self.assertEqual(page.evaluate('window.sendCount'), 1)
        finally:
            page.close()
