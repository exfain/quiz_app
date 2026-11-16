"""Three games through real planning/Hub, four Chromium identities, both modes."""
import os
from unittest.mock import patch
from channels.testing import ChannelsLiveServerTestCase
from django.urls import reverse
from games_hub.models import HubSession
from games_hub.playwright_e2e import start_chromium_browser
from . import test_playable_browser as reference
from . import test_quiz_browser as quiz_reference
from .test_turns import make_material, correct_selection
from .models import DuelGameInstance, DuelTurnRound, DuelDecision, DuelScoreEntry


class AlternatingDuelBrowserTests(ChannelsLiveServerTestCase):
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

    def prepare_start(self, host, instance, side):
        host.wait_for_selector('#prepare:not([hidden])'); host.select_option('#question-select', str(self.question.pk))
        self.click_response(host, '#prepare', '/duel/api/')
        host.wait_for_selector('#start:not([hidden])'); state = DuelTurnRound.objects.get(unit=instance.units.latest('number'))
        self.assertIsNone(state.started_at)
        host.select_option('#starting-party', str(self.parties[side].pk))
        self.click_response(host, '#start', '/units/')
        state.refresh_from_db(); return state

    def correct(self, host, key, state, double=False):
        selection = correct_selection(key, state)
        if key == 'assign':
            host.select_option('#left', str(selection['left_id'])); host.select_option('#right', str(selection['right_id'])); selector = '#record'
        elif key == 'sorting_ladder':
            host.select_option('#item', str(selection['item_id'])); host.select_option('#position', str(selection['position'])); selector = '#record'
        else:
            selector = f'button[data-answer="{selection["answer_id"]}"]'
            host.wait_for_selector(selector)
        if double:
            with host.expect_response(lambda r: '/units/' in r.url and r.request.method == 'POST') as response:
                host.evaluate('(selector)=>{const button=document.querySelector(selector);button.click();button.click();}', selector)
            self.assertTrue(response.value.ok, response.value.text())
        else: self.click_response(host, selector, '/units/')
        state.refresh_from_db()

    def wait_state(self, page, state, phase, turn=None):
        page.wait_for_function("value=>{const r=document.getElementById('duel-match');return r.dataset.unit===value.unit&&r.dataset.phase===value.phase;}",
            arg={'unit': str(state.unit_id), 'phase': phase})
        if turn is not None: page.wait_for_function('(turn)=>document.getElementById("turn").textContent.startsWith("Zug "+turn+":")', arg=turn)

    def play(self, key):
        make_material(self, key)
        host = self.page(host=True); host.goto(self.live_server_url+reverse('games_hub:create_session'))
        host.fill('#sessionName', 'Alternating browser'); host.fill('[name=code]', 'TURN-BROWSER'); host.select_option('#sessionType', 'duel')
        for _ in range(2): host.click(f'.game-instance-row[data-game-key="{key}"][data-room-code="{self.game.room_code}"] .add-game-btn')
        host.locator('.plan-mode').nth(0).select_option('best_of_n'); host.locator('.plan-value').nth(0).fill('7')
        host.locator('.plan-mode').nth(1).select_option('target_score'); host.locator('.plan-value').nth(1).fill('1')
        host.click('#submitBtn'); host.wait_for_url('**/admin-dashboard/sessions/')
        hub = HubSession.objects.get(code='TURN-BROWSER')
        host.goto(self.live_server_url+reverse('games_hub:monitor', args=[hub.code])); self.click_response(host, '#activate-session', '/duel/api/session/')
        alice, bob, spectator = [self.page() for _ in range(3)]; players = [alice, bob]
        for page, name in ((alice, 'Team Alpha'), (bob, 'Team Beta')):
            page.goto(self.live_server_url+reverse('games_hub:lobby', args=[hub.code])); page.fill('#nickname', name)
            self.click_response(page, '#join-form button', '/duel/api/session/'); page.wait_for_selector('#joined:not([hidden])')
        self.click_response(host, '[data-check=start]', '/check-in/start/')
        for page in players: self.click_response(page, '#ready', '/check-in/participant/')
        self.click_response(host, '[data-check=complete]', '/check-in/complete/')
        self.click_response(host, '#save-plan', '/duel/api/session/'); host.reload(); host.wait_for_selector('.start-slot')
        self.assertEqual(host.locator('.match-value').first.input_value(), '7')
        spectator.goto(self.live_server_url+reverse('games_hub:spectate_session', args=[hub.code]))
        self.click_response(host, '.duel-slot:first-child .start-slot', '/activate-game/')
        for page, role in ((host, 'host'), (alice, 'participant'), (bob, 'participant'), (spectator, 'spectator')):
            page.wait_for_url(f'**/duel/{key}/**/{role}/')
        alice.wait_for_selector('#sessionGameStartIntro:not([hidden])'); alice.wait_for_selector('#sessionGameStartIntro[hidden]', state='attached', timeout=25000)
        alice.wait_for_selector('#waiting:not([hidden])'); self.assertEqual(alice.locator('#record').count(), 0)
        self.parties = list(hub.participants.order_by('pk')); instance = DuelGameInstance.objects.get(hub_session=hub)
        self.assertFalse(instance.units.exists()); host.wait_for_selector('#question-select option:nth-child(2)', state='attached')
        for index in range(4):
            state = self.prepare_start(host, instance, 0)
            for page in (alice, bob, spectator):
                self.wait_state(page, state, 'playing', 1)
                public = self.public_json(page)['unit']; self.assertNotIn('solution', public); self.assertNotIn('correct_rank', str(public))
                if key == 'wer_weiss_mehr': self.assertTrue(all(c['text'] is None for c in public['cards']))
            self.correct(host, key, state, double=index == 0)
            self.assertEqual(len(state.actions), 1)
            self.assertEqual(DuelDecision.objects.filter(unit=state.unit).count(), 1)
            for page in (alice, bob, spectator): self.wait_state(page, state, 'playing', 2); self.assertIn('Team Beta', page.inner_text('#turn'))
            if index == 0:
                bob.reload(); self.wait_state(bob, state, 'playing', 2)
                spectator.reload(); self.wait_state(spectator, state, 'playing', 2)
                self.assertTrue(bob.locator('#sessionGameStartIntro').is_hidden())
                if key == 'wer_weiss_mehr':
                    cards = self.public_json(bob)['unit']['cards']; self.assertEqual(sum(c['text'] is not None for c in cards), 1)
                    self.assertEqual(host.locator('.turn-card.revealed').count(), 1)
            self.correct(host, key, state)
            for page in (alice, bob, spectator): self.wait_state(page, state, 'playing', 3); self.assertIn('Team Alpha', page.inner_text('#turn'))
            self.assertFalse(DuelScoreEntry.objects.filter(unit=state.unit).exists())
            self.click_response(host, '#wrong', '/units/')
            for page in (alice, bob, spectator):
                self.wait_state(page, state, 'awaiting_result'); self.assertTrue(page.locator('#result-card').is_hidden())
                self.assertNotIn('solution', self.public_json(page)['unit'])
            self.click_response(host, '#show_result', '/units/')
            for page in (alice, bob, spectator): self.wait_state(page, state, 'show_result'); self.assertIn('Team Beta', page.inner_text('#result'))
            self.assertEqual(self.public_json(spectator)['unit']['points'], {'a': 0, 'b': 1})
            self.click_response(host, '#complete', '/units/')
        instance.refresh_from_db(); self.assertEqual(instance.match_status, 'completed'); self.assertEqual(instance.match_outcome.winner_id, self.parties[1].pk)
        tally = self.public_json(spectator)['unit']['match']; self.assertEqual(tally['required_wins'], 4); self.assertEqual(tally['party_b']['round_wins'], 4)
        for page in (alice, bob, spectator, host): page.wait_for_selector('#return:not([hidden])'); page.click('#return')
        for page in players: page.wait_for_url('**/hub/lobby/TURN-BROWSER/'); page.wait_for_selector('#joined:not([hidden])')
        host.wait_for_url('**/hub/monitor/TURN-BROWSER/'); self.click_response(host, '.duel-slot:nth-child(2) .start-slot', '/activate-game/')
        for page in (host, alice, bob, spectator): page.wait_for_url(f'**/duel/{key}/**/')
        alice.wait_for_selector('#sessionGameStartIntro:not([hidden])'); alice.wait_for_selector('#sessionGameStartIntro[hidden]', state='attached', timeout=25000)
        alice.wait_for_selector('#waiting:not([hidden])')
        second = DuelGameInstance.objects.filter(hub_session=hub).exclude(pk=instance.pk).get(); self.assertFalse(second.units.exists())
        host.wait_for_selector('#question-select option:nth-child(2)', state='attached')
        state = self.prepare_start(host, second, 1)
        self.correct(host, key, state); self.click_response(host, '#wrong', '/units/'); self.click_response(host, '#show_result', '/units/')
        self.wait_state(spectator, state, 'show_result'); self.assertEqual(spectator.locator('progress').count(), 2)
        self.assertEqual(spectator.locator('progress').nth(1).get_attribute('value'), '1')
        self.click_response(host, '#complete', '/units/'); second.refresh_from_db()
        self.assertEqual(second.match_status, 'completed'); self.assertEqual(second.match_outcome.winner_id, self.parties[1].pk)
        for page in (alice, bob, spectator, host): page.wait_for_selector('#return:not([hidden])'); page.click('#return')
        alice.wait_for_url('**/hub/lobby/TURN-BROWSER/')
        self.game.refresh_from_db(); self.assertEqual(self.game.status, 'waiting'); self.assertEqual(self.game.participants.count(), 0)
        self.assertFalse(self.errors, self.errors)

    def test_assign_alternating_pairs_both_formats_and_lobby_return(self): self.play('assign')
    def test_sorting_live_insertion_hidden_solution_both_formats(self): self.play('sorting_ladder')
    def test_knowledge_cards_host_only_and_both_formats(self): self.play('wer_weiss_mehr')
