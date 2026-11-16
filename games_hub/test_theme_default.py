import json
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from .playwright_e2e import start_chromium_browser


class ParticipantThemeDefaultBrowserTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.playwright, cls.browser = start_chromium_browser(headless=True)
        cls.root = Path(settings.BASE_DIR)
        cls.bootstrap = (cls.root / 'static/js/authoritative-game-state.js').read_text(encoding='utf-8')
        intro = (cls.root / 'templates/includes/_session_game_start_intro.html').read_text(encoding='utf-8')
        cls.intro_helper = intro[intro.index('function isQaVhsParticipantTheme()'):intro.index('function initializeQaVhsGameIntro(')]
        cls.widget = render_to_string('includes/accessibility_widget.html', {'participant_options_menu': True})

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def open_page(self, *, stored=None, early=True, blocked=False):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        if blocked:
            context.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw new DOMException('Blocked','SecurityError')}})")
        elif stored is not None:
            context.add_init_script('localStorage.setItem("participant_interface_theme",' + json.dumps(stored) + ');')
        markup = ('<!doctype html><html><head>' + ('<script src="/bootstrap.js"></script>' if early else '')
            + '<script>' + self.intro_helper + ';window.initialTheme=document.documentElement.dataset.participantTheme;'
            + 'window.initialIntroIsVhs=isQaVhsParticipantTheme();</script></head><body class="quiz-play-page">'
            + '<main class="play-container"><h1 class="quiz-title">Testspiel</h1>'
            + '<div class="qa-wait-loader"></div><button class="btn">Weiter</button></main>'
            + self.widget + '</body></html>')
        def route(request):
            path = urlsplit(request.request.url).path
            if path == '/':
                request.fulfill(status=200, content_type='text/html; charset=utf-8', body=markup)
            elif path == '/bootstrap.js':
                request.fulfill(status=200, content_type='text/javascript', body=self.bootstrap)
            elif path.startswith('/static/') and '..' not in path:
                source = self.root / path.lstrip('/')
                request.fulfill(path=str(source)) if source.is_file() else request.fulfill(status=404, body='')
            else:
                request.fulfill(status=404, body='')
        context.route('**/*', route)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://participant.test/')
        page.wait_for_selector('#participant-theme-select', state='attached')
        return page, errors

    def test_first_visit_defaults_to_vhs_with_and_without_early_bootstrap(self):
        for early in [True, False]:
            with self.subTest(early=early):
                page, errors = self.open_page(early=early)
                self.assertEqual(page.get_attribute('html', 'data-participant-theme'), 'vhs')
                self.assertEqual(page.input_value('#participant-theme-select'), 'vhs')
                self.assertTrue(page.evaluate('window.initialIntroIsVhs'))
                if early:
                    self.assertEqual(page.evaluate('window.initialTheme'), 'vhs')
                self.assertIsNone(page.evaluate('localStorage.getItem("participant_interface_theme")'))
                self.assertFalse(errors)

    def test_invalid_preference_and_blocked_storage_fall_back_to_vhs(self):
        for options in [{'stored': 'unknown'}, {'stored': ''}, {'blocked': True}]:
            with self.subTest(options=options):
                page, errors = self.open_page(**options)
                self.assertEqual(page.get_attribute('html', 'data-participant-theme'), 'vhs')
                self.assertEqual(page.input_value('#participant-theme-select'), 'vhs')
                self.assertTrue(page.evaluate('window.initialIntroIsVhs'))
                self.assertFalse(errors)

    def test_saved_choices_remain_selected_in_bootstrap_menu_and_wait_loader(self):
        for theme in ['standard', 'arcade', 'vhs']:
            for early in [True, False]:
                with self.subTest(theme=theme, early=early):
                    page, errors = self.open_page(stored=theme, early=early)
                    self.assertEqual(page.get_attribute('html', 'data-participant-theme'), theme)
                    self.assertEqual(page.input_value('#participant-theme-select'), theme)
                    self.assertEqual(page.get_attribute('[data-qa-wait-loader-theme]', 'data-qa-wait-loader-theme'), theme)
                    self.assertEqual(page.evaluate('window.initialIntroIsVhs'), theme == 'vhs')
                    self.assertFalse(errors)

    def test_manual_choice_survives_reload_and_reset_returns_to_vhs(self):
        page, errors = self.open_page()
        page.locator('#participant-theme-select').evaluate("input=>{input.value='standard';input.dispatchEvent(new Event('change',{bubbles:true}));}")
        page.reload()
        self.assertEqual(page.input_value('#participant-theme-select'), 'standard')
        page.locator('#participant-interface-reset').evaluate('(button)=>button.click()')
        self.assertEqual(page.get_attribute('html', 'data-participant-theme'), 'vhs')
        self.assertEqual(page.input_value('#participant-theme-select'), 'vhs')
        self.assertIsNone(page.evaluate('localStorage.getItem("participant_interface_theme")'))
        page.reload()
        self.assertEqual(page.input_value('#participant-theme-select'), 'vhs')
        self.assertFalse(errors)
