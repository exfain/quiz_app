"""Planned Hub slots, actual Chromium clicks, four isolated browser identities."""
import os
from unittest.mock import patch
from channels.testing import ChannelsLiveServerTestCase
from django.urls import reverse
from games_hub.models import HubSession
from games_hub.playwright_e2e import start_chromium_browser
from . import test_playable_browser as reference
from . import test_quiz_browser as quiz_reference
from .test_inputs import make_material
from .models import DuelGameInstance, DuelInputRound, DuelScoreEntry


class DeviceDuelBrowserTests(ChannelsLiveServerTestCase):
    page = reference.PlayableEstimationBrowserTests.page
    click_response = reference.PlayableEstimationBrowserTests.click_response
    public_json = quiz_reference.PlayableQuickQuizDuelBrowserTests.public_json

    @classmethod
    def setUpClass(cls):
        super().setUpClass(); cls.env = patch.dict(os.environ, {'DJANGO_ALLOW_ASYNC_UNSAFE': '1'}); cls.env.start()
        cls.pw, cls.browser = start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.env.stop(); super().tearDownClass()

    def setUp(self): self.contexts = []; self.errors = []

    def tearDown(self):
        for context in self.contexts: context.close()
        super().tearDown()

    def wait_phase(self, page, state, phase, index=0):
        page.wait_for_function("v=>{const r=document.getElementById('duel-match');return r?.dataset.unit===v.unit&&r.dataset.phase===v.phase&&r.dataset.questionIndex===String(v.index);}",
            arg={'unit': str(state.unit_id), 'phase': phase, 'index': index})

    def marker(self, page, latitude, longitude):
        page.wait_for_selector('#map[data-loaded=true]')
        point = page.evaluate("p=>{const m=document.getElementById('map').duelMap;let [x,y]=m.project(...p);if(m.region==='world_mercator')x+=Math.round(m.box[0]+m.box[2]/2-x);const q=new DOMPoint(x,y).matrixTransform(m.svg.getScreenCTM());return {x:q.x,y:q.y};}", [latitude, longitude])
        with page.expect_response(lambda r: '/units/' in r.url and r.request.method == 'POST') as response:
            page.mouse.click(point['x'], point['y'])
        self.assertTrue(response.value.ok, response.value.text()); page.wait_for_selector('#commit:not(:disabled)')

    def start_question(self, host, state):
        host.wait_for_selector('#start:not([hidden])'); self.click_response(host, '#start', '/units/')
        state.refresh_from_db()

    def round(self, host, players, spectator, instance, key, number):
        host.wait_for_selector('#prepare:not([hidden])')
        q = self.questions[number % 3] if key == 'where' else None
        host.select_option('#question-select', str(q.pk if q else 1)); self.click_response(host, '#prepare', '/duel/api/')
        state = DuelInputRound.objects.get(unit=instance.units.latest('number'))
        self.assertIsNone(state.started_at); self.start_question(host, state)
        for page in (*players, spectator): self.wait_phase(page, state, 'answering')
        for index in range(1 if key == 'where' else 2):
            a, b = players
            if key == 'where':
                a.wait_for_selector('#map[data-loaded=true]'); self.assertEqual(a.locator('#map').evaluate('(n)=>n.duelMap.region'), q.map_type)
                if q.map_type == 'world_mercator': self.assertGreaterEqual(a.locator('#map [data-map-region]').count(), 2)
                a.click('#zoom-in'); a.click('#zoom-in')
                self.assertTrue(a.locator('#map').evaluate('(n)=>n.duelMap.box[2]<n.duelMap.defaultBox[2]'))
                self.marker(a, 51, 12); self.marker(a, 52.5, 13.4); self.marker(b, 50, 10)
                self.click_response(a, '#commit', '/units/')
                own = self.public_json(a)['unit']['own_answer']; a.reload(); self.wait_phase(a, state, 'answering')
                a.wait_for_selector('#map [data-marker=own]'); self.assertTrue(a.locator('#commit').is_disabled())
                self.assertEqual(self.public_json(a)['unit']['own_answer'], own)
                # Locked markers cannot be moved after reload; no write occurs.
                before = self.public_json(a)['state_revision']; a.locator('#map svg').click(position={'x': 100, 'y': 100})
                self.assertEqual(self.public_json(a)['state_revision'], before)
                self.click_response(b, '#commit', '/units/')
                spectator.wait_for_selector('#map[data-loaded=true]'); spectator.click('#zoom-in')
            else:
                # A: 10 + 11 = 21. B: 5 + 17 = 22 (Bust). Reverse arrival
                # order on alternate sets; never declare a win on first input.
                values = [110, 105] if index == 0 else [111, 117]
                order = (0, 1) if number % 2 == 0 else (1, 0)
                for side in order:
                    page = players[side]; page.fill('#estimate', str(values[side])); self.click_response(page, '#commit', '/units/')
                    if side == order[0]:
                        self.assertFalse(DuelScoreEntry.objects.filter(unit=state.unit).exists())
                        self.assertEqual(self.public_json(spectator)['unit']['stars'], {'a': 0, 'b': 0} if index == 0 else {'a': 10, 'b': 5})
                if number == 0 and index == 0:
                    a.reload(); self.wait_phase(a, state, 'awaiting_reveal'); self.assertEqual(a.locator('#own-status').inner_text(), 'Deine Antwort ist verbindlich eingeloggt.')
            for page in (*players, spectator):
                self.wait_phase(page, state, 'awaiting_reveal', index)
                public = self.public_json(page)['unit']; self.assertNotIn('answers', public); self.assertNotIn('target', public)
            if key == 'where':
                self.click_response(host, '#reveal', '/units/')
                spectator.wait_for_selector('#map [data-marker=target]')
                self.assertTrue(spectator.locator('#map').evaluate('(n)=>Math.abs(n.duelMap.box[2]-n.duelMap.defaultBox[2])<1e-10'))
                self.assertEqual(set(spectator.locator('#map [data-marker]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.marker)')), {'a', 'b', 'target'})
            else:
                self.click_response(host, '#show_answers', '/units/')
                for page in (*players, spectator):
                    self.wait_phase(page, state, 'show_answers', index); public = self.public_json(page)['unit']
                    self.assertIn('answers', public); self.assertNotIn('target', public)
                spectator.reload(); self.wait_phase(spectator, state, 'show_answers', index)
                self.click_response(host, '#show_solution', '/units/')
            for page in (*players, spectator): self.wait_phase(page, state, 'show_solution', index)
            self.click_response(host, '#evaluate', '/units/')
            if key == 'blackjack' and index == 0:
                for page in (*players, spectator): self.wait_phase(page, state, 'question_result')
                self.assertFalse(DuelScoreEntry.objects.filter(unit=state.unit).exists())
                self.click_response(host, '#next', '/units/'); self.start_question(host, state)
                for page in (*players, spectator): self.wait_phase(page, state, 'answering', 1)
        final_index = 0 if key == 'where' else 1
        for page in (*players, spectator): self.wait_phase(page, state, 'show_result', final_index)
        result = self.public_json(spectator)['unit']; self.assertEqual(result['points'], {'a': 1, 'b': 0})
        if key == 'blackjack': self.assertEqual(result['stars'], {'a': 21, 'b': 22}); self.assertIn('BUST', spectator.inner_text('#stars'))
        if instance.rule_config['match']['mode'] == 'target_score':
            self.assertEqual(spectator.locator('progress').count(), 2); self.assertEqual(spectator.locator('progress').first.get_attribute('value'), '1')
        if number == 0: spectator.screenshot(path=os.path.join(os.environ['TEMP'], f'duel-{key}-result.png'), full_page=True)
        self.click_response(host, '#complete', '/units/')

    def play(self, key):
        make_material(self, key)
        host = self.page(host=True); host.goto(self.live_server_url+reverse('games_hub:create_session'))
        host.fill('#sessionName', 'Device browser'); host.fill('[name=code]', 'INPUT-BROWSER'); host.select_option('#sessionType', 'duel')
        for _ in range(2): host.click(f'.game-instance-row[data-game-key="{key}"][data-room-code="{self.game.room_code}"] .add-game-btn')
        host.locator('.plan-mode').nth(0).select_option('best_of_n'); host.locator('.plan-value').nth(0).fill('7')
        host.locator('.plan-mode').nth(1).select_option('target_score'); host.locator('.plan-value').nth(1).fill('1')
        host.click('#submitBtn'); host.wait_for_url('**/admin-dashboard/sessions/')
        hub = HubSession.objects.get(code='INPUT-BROWSER')
        host.goto(self.live_server_url+reverse('games_hub:monitor', args=[hub.code])); self.click_response(host, '#activate-session', '/duel/api/session/')
        a, b, spectator = [self.page() for _ in range(3)]; players = [a, b]
        for page, name in ((a, 'Team Alpha'), (b, 'Team Beta')):
            page.goto(self.live_server_url+reverse('games_hub:lobby', args=[hub.code])); page.fill('#nickname', name)
            self.click_response(page, '#join-form button', '/duel/api/session/'); page.wait_for_selector('#joined:not([hidden])')
        self.click_response(host, '[data-check=start]', '/check-in/start/')
        for page in players: self.click_response(page, '#ready', '/check-in/participant/')
        self.click_response(host, '[data-check=complete]', '/check-in/complete/')
        self.click_response(host, '#save-plan', '/duel/api/session/'); host.reload(); host.wait_for_selector('.start-slot')
        self.assertEqual(host.locator('.match-value').first.input_value(), '7')
        spectator.goto(self.live_server_url+reverse('games_hub:spectate_session', args=[hub.code]))
        for slot in range(2):
            self.click_response(host, f'.duel-slot:nth-child({slot+1}) .start-slot', '/activate-game/')
            for page, role in ((host, 'host'), (a, 'participant'), (b, 'participant'), (spectator, 'spectator')): page.wait_for_url(f'**/duel/{key}/**/{role}/')
            a.wait_for_selector('#sessionGameStartIntro:not([hidden])'); a.wait_for_selector('#sessionGameStartIntro[hidden]', state='attached', timeout=25000)
            a.wait_for_selector('#waiting:not([hidden])'); self.assertTrue(a.locator('#answer-form').is_hidden())
            instance = DuelGameInstance.objects.get(hub_game_step=hub.steps.order_by('order', 'pk')[slot]); self.assertFalse(instance.units.exists())
            host.wait_for_selector('#question-select option:nth-child(2)', state='attached')
            for number in range(4 if slot == 0 else 1): self.round(host, players, spectator, instance, key, number)
            instance.refresh_from_db(); self.assertEqual(instance.match_status, 'completed')
            self.assertEqual(instance.match_outcome.winner_id, hub.participants.order_by('pk').first().pk)
            if slot == 0:
                tally = self.public_json(spectator)['unit']['match']; self.assertEqual(tally['required_wins'], 4); self.assertEqual(tally['party_a']['round_wins'], 4)
            for page in (a, b, spectator, host): page.wait_for_selector('#return:not([hidden])'); page.click('#return')
            for page in players: page.wait_for_url('**/hub/lobby/INPUT-BROWSER/'); page.wait_for_selector('#joined:not([hidden])')
            host.wait_for_url('**/hub/monitor/INPUT-BROWSER/')
        self.game.refresh_from_db(); self.assertEqual(self.game.status, 'waiting'); self.assertEqual(self.game.participants.count(), 0)
        self.assertFalse(self.errors, self.errors)

    def test_where_three_maps_zoom_reveal_lock_rejoin_both_formats(self): self.play('where')
    def test_blackjack_whole_set_both_orders_bust_both_formats(self): self.play('blackjack')
