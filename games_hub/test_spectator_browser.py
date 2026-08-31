import json
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.utils import timezone

from games_hub.playwright_e2e import start_chromium_browser


REPO_ROOT = Path(__file__).resolve().parent.parent


class HubSpectatorBrowserTests(unittest.TestCase):
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

    def _open_game(self, viewport, game_state=None):
        session = SimpleNamespace(code='SPEC1', name='Spectator Session')
        body = render_to_string(
            'hub/spectate.html',
            {
                'session': session,
                'join_url': 'http://spectator.test/hub/join/SPEC1/',
            },
        )
        vhs_css = (REPO_ROOT / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        spectator_css = (REPO_ROOT / 'static' / 'themes' / 'vhs' / 'spectator.css').read_text(encoding='utf-8')
        qr_css = (REPO_ROOT / 'static' / 'css' / 'session_join_qr.css').read_text(encoding='utf-8')
        now = timezone.now()
        deadline = now + timedelta(seconds=30)
        default_game_state = {
            'game_key': 'quiz',
            'label': 'Quick Quiz',
            'room_code': '1001',
            'title': 'Grossbild Quick Quiz',
            'game_number': 1,
            'game_instance_id': 'quiz:1001:1',
            'state_revision': 4,
            'phase': 'question',
            'message': 'Aktuelle Frage',
            'question_phase': 'answering_open',
            'answering_started_at': now.isoformat(),
            'answering_deadline_at': deadline.isoformat(),
            'timer': {
                'active': True,
                'seconds_left': 30,
                'ends_at': deadline.isoformat(),
            },
            'question': {
                'text': 'Welche Antwort gehoert zu dieser bewusst langen Frage auf dem grossen Bildschirm?',
                'options': [
                    {'key': 'A', 'text': 'Eine ausfuehrliche erste Antwort'},
                    {'key': 'B', 'text': 'Eine ausfuehrliche zweite Antwort'},
                    {'key': 'C', 'text': 'Eine ausfuehrliche dritte Antwort'},
                    {'key': 'D', 'text': 'Eine ausfuehrliche vierte Antwort'},
                ],
            },
            'progress': {'kind': 'question', 'current': 3, 'total': 10},
            'response_count': 2,
            'intro': {'state_revision': 0},
        }
        resolved_game_state = default_game_state if game_state is None else game_state
        state = {
            'success': True,
            'server_now': now.isoformat(),
            'phase': 'question' if resolved_game_state else 'waiting',
            'message': 'Aktuelle Frage' if resolved_game_state else 'Warte auf das naechste Spiel',
            'game': resolved_game_state,
        }
        context = self._browser.new_context(viewport=viewport)
        context.route(
            'http://spectator.test/static/themes/vhs/vhs.css',
            lambda route: route.fulfill(status=200, content_type='text/css', body=vhs_css),
        )
        context.route(
            'http://spectator.test/static/themes/vhs/spectator.css',
            lambda route: route.fulfill(status=200, content_type='text/css', body=spectator_css),
        )
        context.route(
            'http://spectator.test/static/css/session_join_qr.css',
            lambda route: route.fulfill(status=200, content_type='text/css', body=qr_css),
        )
        context.route(
            'http://spectator.test/static/theme.css',
            lambda route: route.fulfill(status=200, content_type='text/css', body=''),
        )
        context.route(
            'http://spectator.test/hub/api/spectate/SPEC1/state/',
            lambda route: route.fulfill(
                status=200,
                content_type='application/json',
                body=json.dumps(state),
            ),
        )
        context.route(
            'http://spectator.test/spectate',
            lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=body),
        )
        page = context.new_page()
        page.goto('http://spectator.test/spectate')
        if state['game']:
            if state['game'].get('scoreboard', {}).get('available'):
                page.wait_for_selector('[data-spectator-scoreboard]')
            else:
                page.wait_for_selector(f'[data-spectator-game="{state["game"]["game_key"]}"]')
        else:
            page.wait_for_selector('.spectator-join-area [data-session-join-toggle]')
        return context, page

    def test_waiting_qr_is_prominent_isolated_and_above_the_join_link(self):
        for width, height in ((1920, 1080), (2560, 1440), (3840, 2160)):
            with self.subTest(viewport=(width, height)):
                context, page = self._open_game({'width': width, 'height': height}, False)
                try:
                    disclosure = page.locator('[data-session-join-disclosure]')
                    toggle = page.locator('[data-session-join-toggle]')
                    qr = page.locator('[data-session-join-qr]')
                    link = page.locator('[data-session-join-url]')
                    stage_box = page.locator('.spectator-stage-frame').bounding_box()
                    toggle_box = toggle.bounding_box()
                    self.assertFalse(disclosure.get_attribute('open') is not None)
                    self.assertFalse(qr.is_visible())
                    self.assertAlmostEqual(
                        toggle_box['x'] + (toggle_box['width'] / 2),
                        stage_box['x'] + (stage_box['width'] / 2),
                        delta=2,
                    )
                    self.assertGreater(
                        toggle_box['y'],
                        stage_box['y'] + (stage_box['height'] * 0.6),
                    )
                    toggle.click()
                    qr_box = qr.bounding_box()
                    link_box = link.bounding_box()
                    styles = qr.evaluate(
                        """element => {
                            const style = getComputedStyle(element);
                            const svgStyle = getComputedStyle(element.querySelector('svg'));
                            return {
                                animation: style.animationName,
                                filter: style.filter,
                                transform: style.transform,
                                mixBlendMode: style.mixBlendMode,
                                svgAnimation: svgStyle.animationName,
                                svgFilter: svgStyle.filter,
                                svgTransform: svgStyle.transform,
                            };
                        }"""
                    )

                    self.assertGreaterEqual(qr_box['width'], 260)
                    self.assertLessEqual(qr_box['width'], 361)
                    self.assertGreaterEqual(link_box['y'], qr_box['y'] + qr_box['height'])
                    self.assertEqual(styles['animation'], 'none')
                    self.assertEqual(styles['filter'], 'none')
                    self.assertEqual(styles['transform'], 'none')
                    self.assertEqual(styles['mixBlendMode'], 'normal')
                    self.assertEqual(styles['svgAnimation'], 'none')
                    self.assertEqual(styles['svgFilter'], 'none')
                    self.assertEqual(styles['svgTransform'], 'none')
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                    toggle.click()
                    self.assertFalse(qr.is_visible())
                    page.wait_for_timeout(1700)
                    self.assertFalse(disclosure.get_attribute('open') is not None)
                finally:
                    page.close()
                    context.close()

    def test_scoreboard_updates_do_not_open_closed_spectator_drawer(self):
        scoreboard_game = {
            'game_key': 'quiz',
            'label': 'Quick Quiz',
            'room_code': 'SCORE1',
            'title': 'Scoreboard Quiz',
            'game_number': 1,
            'game_instance_id': 'quiz:SCORE1:1',
            'state_revision': 8,
            'phase': 'complete',
            'message': 'Spiel beendet',
            'timer': {'active': False, 'seconds_left': None, 'ends_at': None},
            'scoreboard': {
                'available': True,
                'title': 'Scoreboard Quiz',
                'rows': [
                    {'rank': 1, 'participant': 'Anna', 'game_points': 12},
                    {'rank': 2, 'participant': 'Max', 'game_points': 8},
                ],
            },
            'progress': {'kind': 'question', 'current': 2, 'total': 2},
            'response_count': 0,
            'intro': {'state_revision': 0},
        }
        context, page = self._open_game({'width': 1920, 'height': 1080}, scoreboard_game)
        try:
            disclosure = page.locator('[data-session-join-disclosure]')
            toggle = page.locator('[data-session-join-toggle]')
            scoreboard = page.locator('[data-spectator-scoreboard]')
            self.assertTrue(scoreboard.is_visible())
            self.assertFalse(disclosure.get_attribute('open') is not None)
            page.wait_for_timeout(300)
            scoreboard_closed = scoreboard.bounding_box()
            page.wait_for_timeout(1700)
            self.assertFalse(disclosure.get_attribute('open') is not None)

            toggle.click()
            self.assertTrue(page.locator('[data-session-join-qr]').is_visible())
            scoreboard_open = scoreboard.bounding_box()
            for dimension in ('x', 'y', 'width', 'height'):
                self.assertAlmostEqual(
                    scoreboard_closed[dimension],
                    scoreboard_open[dimension],
                    delta=1,
                    msg=dimension,
                )

            page.set_viewport_size({'width': 900, 'height': 500})
            page.wait_for_timeout(1700)
            self.assertTrue(disclosure.get_attribute('open') is not None)
            scoreboard_small_open = scoreboard.bounding_box()
            toggle.click()
            self.assertFalse(disclosure.get_attribute('open') is not None)
            scoreboard_small_closed = scoreboard.bounding_box()
            for dimension in ('x', 'y', 'width', 'height'):
                self.assertAlmostEqual(
                    scoreboard_small_open[dimension],
                    scoreboard_small_closed[dimension],
                    delta=1,
                    msg=f'small-{dimension}',
                )

            toggle.click()
            page.set_viewport_size({'width': 1920, 'height': 1080})
            page.wait_for_timeout(100)
            toggle.click()
            page.wait_for_timeout(1700)
            self.assertFalse(disclosure.get_attribute('open') is not None)
            self.assertTrue(scoreboard.is_visible())
            scoreboard_reclosed = scoreboard.bounding_box()
            for dimension in ('x', 'y', 'width', 'height'):
                self.assertAlmostEqual(
                    scoreboard_closed[dimension],
                    scoreboard_reclosed[dimension],
                    delta=1,
                    msg=dimension,
                )
        finally:
            page.close()
            context.close()

    def test_small_viewport_qr_header_remains_hit_testable_and_closable(self):
        for width, height in ((1366, 768), (1200, 700), (1024, 650), (800, 600), (900, 500)):
            with self.subTest(viewport=(width, height)):
                context, page = self._open_game({'width': width, 'height': height}, False)
                try:
                    disclosure = page.locator('[data-session-join-disclosure]')
                    toggle = page.locator('[data-session-join-toggle]')
                    toggle.click()
                    self.assertTrue(disclosure.get_attribute('open') is not None)

                    toggle_box = toggle.bounding_box()
                    area_box = page.locator('.spectator-join-area').bounding_box()
                    hit_test = page.evaluate(
                        """({x, y}) => {
                            const toggle = document.querySelector('[data-session-join-toggle]');
                            const hit = document.elementFromPoint(x, y);
                            return {
                                receivesClick: Boolean(hit && (hit === toggle || toggle.contains(hit))),
                                hitTag: hit?.tagName || null,
                                hitClass: hit?.className || null,
                            };
                        }""",
                        {
                            'x': toggle_box['x'] + (toggle_box['width'] / 2),
                            'y': toggle_box['y'] + (toggle_box['height'] / 2),
                        },
                    )
                    self.assertGreaterEqual(toggle_box['y'], 0)
                    self.assertLessEqual(toggle_box['y'] + toggle_box['height'], height)
                    self.assertGreaterEqual(toggle_box['y'], area_box['y'])
                    self.assertLessEqual(
                        toggle_box['y'] + toggle_box['height'],
                        area_box['y'] + area_box['height'],
                    )
                    self.assertTrue(hit_test['receivesClick'], hit_test)

                    page.mouse.click(
                        toggle_box['x'] + (toggle_box['width'] / 2),
                        toggle_box['y'] + (toggle_box['height'] / 2),
                    )
                    self.assertFalse(disclosure.get_attribute('open') is not None)
                    toggle.click()
                    toggle.click()
                    self.assertFalse(disclosure.get_attribute('open') is not None)
                finally:
                    page.close()
                    context.close()

    def test_passive_quiz_layout_at_large_screen_viewports(self):
        for width, height in ((1366, 768), (1920, 1080), (2560, 1440), (3840, 2160)):
            with self.subTest(viewport=(width, height)):
                context, page = self._open_game({'width': width, 'height': height})
                try:
                    self.assertEqual(page.locator('.spectator-grid--options .spectator-card').count(), 4)
                    self.assertEqual(page.locator('input, textarea, select, form, button, [draggable="true"]').count(), 0)
                    self.assertTrue(page.locator('#timerShell').is_visible())
                    self.assertEqual(page.locator('#progressText').inner_text(), 'FRAGE 3 / 10')
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                    shell = page.locator('.spectator-shell').bounding_box()
                    self.assertLessEqual(shell['x'] + shell['width'], width + 1)
                    self.assertLessEqual(shell['y'] + shell['height'], height + 1)
                    self.assertEqual(
                        page.locator('#stage > article').evaluate(
                            'element => getComputedStyle(element).animationName'
                        ),
                        'spectator-screen-in',
                    )
                finally:
                    page.close()
                    context.close()

    def test_unchanged_poll_does_not_replace_the_spectator_stage(self):
        context, page = self._open_game({'width': 1920, 'height': 1080})
        try:
            article = page.locator('#stage > article')
            article.evaluate("element => { element.dataset.renderIdentity = 'stable'; }")

            page.wait_for_timeout(1700)

            self.assertEqual(article.get_attribute('data-render-identity'), 'stable')
        finally:
            page.close()
            context.close()

    def test_quiz_answer_and_resolution_labels_only_render_during_reveal(self):
        base_game = {
            'game_key': 'quiz',
            'label': 'Quick Quiz',
            'room_code': 'PHASE1',
            'title': 'Natur & Wissenschaft',
            'game_number': 1,
            'game_instance_id': 'quiz:PHASE1:1',
            'state_revision': 5,
            'phase': 'question',
            'message': 'Aktuelle Frage',
            'question_phase': 'answering_open',
            'timer': {'active': False, 'seconds_left': None, 'ends_at': None},
            'question': {
                'text': 'Welche Antwort ist korrekt?',
                'options': [
                    {'key': 'A', 'text': 'Korrekt'},
                    {'key': 'B', 'text': 'Falsch'},
                ],
                # Deliberately model leaked data: the renderer must still honor the phase.
                'correct_answer': 'A / Korrekt',
                'explanation': 'Nur in der Aufloesung.',
            },
            'progress': {'kind': 'question', 'current': 1, 'total': 3},
            'response_count': 0,
            'intro': {'state_revision': 0},
        }
        context, page = self._open_game({'width': 1920, 'height': 1080}, base_game)
        try:
            self.assertEqual(
                page.locator('.spectator-kicker').text_content().strip(),
                'Spiel 1 - Natur & Wissenschaft',
            )
            self.assertEqual(page.locator('.spectator-game__title').count(), 0)
            self.assertEqual(page.locator('.spectator-panel--reveal').count(), 0)
            self.assertEqual(page.locator('.spectator-card--correct').count(), 0)
            self.assertNotIn('Quick Quiz /', page.locator('#stage').inner_text())
        finally:
            page.close()
            context.close()

        reveal_game = {**base_game, 'phase': 'reveal', 'message': 'Aufloesung'}
        context, page = self._open_game({'width': 1920, 'height': 1080}, reveal_game)
        try:
            self.assertEqual(page.locator('.spectator-panel--reveal').count(), 1)
            self.assertEqual(page.locator('.spectator-card--correct').count(), 1)
            self.assertEqual(page.locator('#progressText').inner_text(), 'FRAGE 1 / 3')
            self.assertTrue(page.locator('#phaseText').is_hidden())
            self.assertNotIn('AUFLOESUNG', page.locator('.vhs-theme-footer').inner_text())
        finally:
            page.close()
            context.close()

    def test_quiz_future_answer_start_reconstructs_partial_option_reveal(self):
        now = timezone.now()
        game = {
            'game_key': 'quiz',
            'label': 'Quick Quiz',
            'room_code': 'TIMED1',
            'title': 'Zeitgesteuertes Quiz',
            'game_number': 1,
            'game_instance_id': 'quiz:TIMED1:1',
            'state_revision': 7,
            'phase': 'question',
            'message': 'Aktuelle Frage',
            'question_phase': 'answering_open',
            'content_revealed_at': (now + timedelta(seconds=2)).isoformat(),
            'answering_started_at': (now + timedelta(seconds=5)).isoformat(),
            'answering_deadline_at': (now + timedelta(seconds=35)).isoformat(),
            'answer_reveal_stagger_ms': 300,
            'timer': {
                'active': False,
                'seconds_left': None,
                'starts_at': (now + timedelta(seconds=5)).isoformat(),
                'ends_at': (now + timedelta(seconds=35)).isoformat(),
            },
            'question': {
                'text': 'Welche Optionen sind bereits sichtbar?',
                'options': [
                    {'key': 'A', 'text': 'Erste Antwort'},
                    {'key': 'B', 'text': 'Zweite Antwort'},
                    {'key': 'C', 'text': 'Dritte Antwort'},
                    {'key': 'D', 'text': 'Vierte Antwort'},
                ],
                'correct_answer': 'D',
            },
            'progress': {'kind': 'question', 'current': 1, 'total': 2},
            'response_count': 0,
            'intro': {'state_revision': 0},
        }
        context, page = self._open_game({'width': 1920, 'height': 1080}, game)
        try:
            self.assertEqual(page.locator('.spectator-card').count(), 0)
            self.assertTrue(page.locator('#timerShell').is_hidden())
            self.assertEqual(page.locator('.spectator-card--correct').count(), 0)
        finally:
            page.close()
            context.close()

    def test_all_solution_renderers_require_authoritative_reveal_phase(self):
        cases = {
            'quiz': ({'text': 'Quiz', 'options': [], 'correct_answer': 'SECRET QUIZ'}, None),
            'estimation': ({'text': 'Estimation', 'correct_answer': 'SECRET ESTIMATION', 'zones': {'zones': []}}, None),
            'where': ({'text': 'Where', 'correct_location': {'latitude': 'SECRET WHERE', 'longitude': '1'}}, None),
            'who_that': ({'text': 'Who That', 'correct_answer': 'SECRET WHO THAT'}, None),
            'blackjack': ({'text': 'Blackjack', 'correct_answer': 'SECRET BLACKJACK'}, None),
            'who': ({'statement': 'Who', 'liars': ['SECRET WHO'], 'truth_tellers': []}, None),
            'clue_rush': ({'text': 'Clue', 'clues': [], 'correct_answer': 'SECRET CLUE'}, None),
            'assign': ({'text': 'Assign', 'left_items': [], 'right_items': [], 'correct_pairs': [{'left': 'SECRET ASSIGN', 'right': 'Target'}]}, None),
            'sorting_ladder': ({'text': 'Sorting', 'items': [], 'placed_elements': [], 'final_order': [{'text': 'SECRET SORTING'}]}, None),
            'wann_war_das': ({'text': 'Wann', 'correct_answer': 'SECRET WANN'}, None),
            'wer_weiss_mehr': ({
                'text': 'Wer weiss mehr',
                'round': 1,
                'answer_count': 1,
                'revealed_count': 0,
                'tiles': [{'revealed': False, 'text': 'SECRET WWM', 'presentation_index': 0}],
            }, {'revealed_count': 1, 'tiles': [{'revealed': True, 'text': 'SECRET WWM', 'presentation_index': 0}]}),
        }
        for index, (game_key, (question, reveal_override)) in enumerate(cases.items(), start=1):
            with self.subTest(game_key=game_key):
                game = {
                    'game_key': game_key,
                    'label': game_key,
                    'room_code': str(3000 + index),
                    'title': game_key,
                    'game_number': index,
                    'game_instance_id': f'{game_key}:{3000 + index}:{index}',
                    'state_revision': 1,
                    'phase': 'question',
                    'message': 'Live',
                    'question_phase': 'answering_open',
                    'timer': {'active': False, 'seconds_left': None, 'ends_at': None},
                    'question': question,
                    'response_count': 0,
                    'intro': {'state_revision': 0},
                }
                context, page = self._open_game({'width': 1920, 'height': 1080}, game)
                try:
                    secret = next(
                        token
                        for token in (
                            'SECRET QUIZ', 'SECRET ESTIMATION', 'SECRET WHERE',
                            'SECRET WHO THAT', 'SECRET BLACKJACK', 'SECRET WHO',
                            'SECRET CLUE', 'SECRET ASSIGN', 'SECRET SORTING',
                            'SECRET WANN', 'SECRET WWM',
                        )
                        if token in json.dumps(question)
                    )
                    self.assertNotIn(secret, page.locator('#stage').inner_text())
                    self.assertNotIn(secret, page.locator('#stage').inner_html())
                finally:
                    page.close()
                    context.close()

                reveal_question = dict(question)
                if reveal_override:
                    reveal_question.update(reveal_override)
                reveal_game = {**game, 'phase': 'reveal', 'question': reveal_question}
                context, page = self._open_game({'width': 1920, 'height': 1080}, reveal_game)
                try:
                    self.assertIn(secret, page.locator('#stage').inner_text())
                finally:
                    page.close()
                    context.close()

    def test_estimation_result_fits_all_zones_and_footer_without_scrolling(self):
        zones = [
            {
                'zone_number': number,
                'min_percentage': (number - 1) * 20,
                'max_percentage': number * 20,
                'absolute_range_display': f'{5000000000 - number * 100000000}-{5000000000 + number * 100000000}',
                'points': 6 - number,
            }
            for number in range(1, 6)
        ]
        estimation_game = {
            'game_key': 'estimation',
            'label': 'Estimation',
            'room_code': 'ESTR1',
            'title': 'Gewichte und sehr grosse Schaetzwerte',
            'game_number': 2,
            'game_instance_id': 'estimation:ESTR1:2',
            'state_revision': 7,
            'phase': 'reveal',
            'message': 'Aufloesung',
            'question_phase': 'completed',
            'timer': {'active': False, 'seconds_left': None, 'ends_at': None},
            'question': {
                'text': 'Wie schwer ist dieses aussergewoehnlich grosse Testobjekt?',
                'unit': 'Kilogramm Gesamtgewicht',
                'correct_answer': '5.000.000.000 Kilogramm',
                'zones': {
                    'special_case': None,
                    'outside_points': 0,
                    'zones': zones,
                },
            },
            'progress': {'kind': 'question', 'current': 1, 'total': 3},
            'response_count': 2,
            'intro': {'state_revision': 0},
        }

        for width, height in ((1366, 768), (1920, 1080), (2560, 1440), (3840, 2160)):
            with self.subTest(viewport=(width, height)):
                context, page = self._open_game({'width': width, 'height': height}, estimation_game)
                try:
                    stage = page.locator('#stage')
                    footer = page.locator('.vhs-theme-footer')
                    result = page.locator('[data-spectator-estimation-result]')
                    zone_list = page.locator('[data-spectator-estimation-zones]')
                    rows = page.locator('.spectator-estimation-zone')
                    stage_box = stage.bounding_box()
                    result_box = result.bounding_box()
                    footer_box = footer.bounding_box()

                    self.assertEqual(rows.count(), 6)
                    self.assertIn('Zone 5', rows.nth(4).inner_text())
                    self.assertIn('Außerhalb aller Zonen', rows.nth(5).inner_text())
                    self.assertEqual(zone_list.locator('.spectator-card').count(), 0)
                    self.assertTrue(rows.nth(5).is_visible())
                    self.assertGreaterEqual(result_box['y'], stage_box['y'])
                    self.assertLessEqual(
                        result_box['y'] + result_box['height'],
                        stage_box['y'] + stage_box['height'] + 1,
                    )
                    self.assertLessEqual(footer_box['y'] + footer_box['height'], height + 1)
                    self.assertTrue(
                        stage.evaluate('element => element.scrollHeight <= element.clientHeight + 1')
                    )
                    self.assertTrue(
                        zone_list.evaluate('element => element.scrollHeight <= element.clientHeight + 1')
                    )
                finally:
                    page.close()
                    context.close()

    def test_spectator_noise_uses_shared_texture_behind_qr_layer(self):
        context, page = self._open_game({'width': 1920, 'height': 1080}, False)
        try:
            styles = page.locator('.spectator-shell').evaluate(
                """element => {
                    const noise = getComputedStyle(element, '::after');
                    const stageFrame = getComputedStyle(element.querySelector('.spectator-stage-frame'));
                    const qr = getComputedStyle(element.querySelector('.spectator-join-area'));
                    return {
                        backgroundImage: noise.backgroundImage,
                        opacity: noise.opacity,
                        pointerEvents: noise.pointerEvents,
                        noiseZIndex: Number(noise.zIndex),
                        stageZIndex: Number(stageFrame.zIndex),
                        qrZIndex: Number(qr.zIndex),
                    };
                }"""
            )
            self.assertIn('noise.png', styles['backgroundImage'])
            self.assertEqual(styles['opacity'], '1')
            self.assertEqual(styles['pointerEvents'], 'none')
            self.assertLess(styles['noiseZIndex'], styles['stageZIndex'])
            self.assertLess(styles['noiseZIndex'], styles['qrZIndex'])
        finally:
            page.close()
            context.close()

    def test_long_question_and_answers_do_not_create_horizontal_overflow(self):
        repeated = 'Ein absichtlich sehr langer Inhalt fuer die Grossbilddarstellung '
        long_game = {
            'game_key': 'quiz',
            'label': 'Quick Quiz',
            'room_code': 'LONG1',
            'title': 'Ein sehr langer, aber echter Spielname fuer den Fernseher',
            'game_number': 1,
            'game_instance_id': 'quiz:LONG1:1',
            'state_revision': 2,
            'phase': 'question',
            'message': 'Aktuelle Frage',
            'question_phase': 'answering_open',
            'timer': {'active': False, 'seconds_left': None, 'ends_at': None},
            'question': {
                'text': repeated * 4,
                'options': [
                    {'key': key, 'text': repeated * 2}
                    for key in ('A', 'B', 'C', 'D')
                ],
            },
            'progress': {'kind': 'question', 'current': 1, 'total': 2},
            'response_count': 0,
            'intro': {'state_revision': 0},
        }
        context, page = self._open_game({'width': 1366, 'height': 768}, long_game)
        try:
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
            self.assertTrue(
                page.locator('#stage').evaluate('element => element.scrollWidth <= element.clientWidth + 1')
            )
        finally:
            page.close()
            context.close()

    def test_reduced_motion_disables_state_and_reveal_animations(self):
        context, page = self._open_game({'width': 1920, 'height': 1080})
        try:
            page.emulate_media(reduced_motion='reduce')
            self.assertEqual(
                page.locator('#stage > article').evaluate(
                    'element => getComputedStyle(element).animationName'
                ),
                'none',
            )
            self.assertEqual(
                page.locator('.spectator-reveal-item').first.evaluate(
                    'element => getComputedStyle(element).animationName'
                ),
                'none',
            )
        finally:
            page.close()
            context.close()

    def test_every_registered_game_renderer_produces_a_passive_public_screen(self):
        cases = {
            'quiz': {'question': {'text': 'Quiz?', 'options': [{'key': 'A', 'text': 'A'}]}},
            'assign': {'question': {'text': 'Zuordnen', 'left_items': [{'text': 'Element'}], 'right_items': [{'text': 'Ziel'}]}},
            'estimation': {'question': {'text': 'Schaetzen', 'unit': 'km'}},
            'where': {'question': {'text': 'Wo?', 'image_url': ''}},
            'who': {'question': {'statement': 'Wer luegt?', 'current_person': 'Mia'}},
            'who_that': {'question': {'text': 'Wer ist das?', 'category': 'Person'}},
            'blackjack': {'question': {'text': 'Wie viel?', 'set_number': 1, 'total_sets': 2, 'question_in_set': 1, 'set_question_count': 3}},
            'sorting_ladder': {'question': {'text': 'Sortieren', 'upper_label': 'Oben', 'lower_label': 'Unten', 'placed_elements': [], 'items': [], 'total_rounds': 2}},
            'clue_rush': {'question': {'text': 'Begriff?', 'clues': [{'number': 1, 'text': 'Hinweis'}]}},
            'buzzer': {'question': {'text': 'Runde 1'}, 'round': {'number': 1, 'buzzer_open': True}, 'participants': []},
            'host_points': {'question': {'text': 'Runde 1'}, 'round': {'number': 1}, 'participants': []},
            'wann_war_das': {'question': {'text': 'Wann?', 'current_tolerance': 2}},
            'wer_weiss_mehr': {'question': {'text': 'Thema', 'round': 1, 'answer_count': 2, 'revealed_count': 0, 'tiles': [{'presentation_index': 0, 'revealed': False, 'text': ''}]}, 'question_phase': 'answering_open'},
        }
        for index, (game_key, payload) in enumerate(cases.items(), start=1):
            with self.subTest(game_key=game_key):
                game_state = {
                    'game_key': game_key,
                    'label': game_key.replace('_', ' ').title(),
                    'room_code': str(2000 + index),
                    'title': game_key.replace('_', ' ').title(),
                    'game_number': index,
                    'game_instance_id': f'{game_key}:{2000 + index}:{index}',
                    'state_revision': 1,
                    'phase': 'question',
                    'message': 'Live',
                    'timer': {'active': False, 'seconds_left': None, 'ends_at': None},
                    'response_count': 0,
                    'intro': {'state_revision': 0},
                    **payload,
                }
                context, page = self._open_game({'width': 1920, 'height': 1080}, game_state)
                try:
                    self.assertEqual(page.locator(f'[data-spectator-game="{game_key}"]').count(), 1)
                    self.assertEqual(page.locator('input, textarea, select, form, button, [draggable="true"]').count(), 0)
                    self.assertNotIn('fehlen oeffentliche Spectator-Daten', page.locator('#stage').inner_text())
                finally:
                    page.close()
                    context.close()
