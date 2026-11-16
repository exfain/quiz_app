"""Real content dashboard, planning, Hub, four Chromium contexts and deadlines."""
import os
import subprocess
from tempfile import TemporaryFile
from unittest.mock import patch
from channels.testing import ChannelsLiveServerTestCase
from django.urls import reverse
from games_hub.models import HubSession
from games_hub.playwright_e2e import start_chromium_browser
from . import test_playable_browser as reference
from . import test_quiz_browser as quiz_reference
from .test_control import make_material
from .test_deadline_worker import worker_environment
from .models import BidGame, DuelGameInstance, DuelControlRound, DuelScoreEntry, DuelSubmission


class ControlBrowserTests(ChannelsLiveServerTestCase):
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

    def wait_phase(self, page, state, phase):
        page.wait_for_function("v=>{const r=document.getElementById('duel-match');return r?.dataset.unit===v.unit&&r.dataset.phase===v.phase;}", arg={'unit': str(state.unit_id), 'phase': phase})

    def configure(self, key):
        make_material(self, key); host = self.page(host=True)
        if key == 'bid':
            # Use actual dashboard forms, including editing/reloading the task.
            host.goto(self.live_server_url+reverse('admin_dashboard:manage_games')); host.get_by_role('link', name='Gebotsspiel: Spiele und Aufgaben verwalten').click()
            host.fill('#create-bid-game [name=title]', 'Browser Gebotsspiel'); host.click('#create-bid-game button')
            self.game = BidGame.objects.get(title='Browser Gebotsspiel')
            for text, seconds in [('Nenne Länder.', 120), ('Zeitablauf.', 5)]:
                host.fill('#new-bid-task [name=text]', text); host.fill('#new-bid-task [name=bid_seconds]', '120'); host.fill('#new-bid-task [name=answer_seconds]', str(seconds))
                host.check('#new-bid-task [name=is_active]'); host.click('#new-bid-task button')
            self.question, self.timeout_task = list(self.game.tasks.order_by('pk'))
            host.locator('.edit-bid-task').first.locator('[name=text]').fill('Nenne Länder weltweit.')
            host.locator('.edit-bid-task').first.get_by_role('button', name='Änderungen speichern').click(); host.reload()
            self.assertEqual(host.locator('.edit-bid-task').first.locator('[name=text]').input_value(), 'Nenne Länder weltweit.')
        host.goto(self.live_server_url+reverse('games_hub:create_session')); host.fill('#sessionName', 'Control browser'); host.fill('[name=code]', 'CONTROL-BROWSER'); host.select_option('#sessionType', 'duel')
        for _ in range(1 if key == 'host_points' else 2): host.click(f'.game-instance-row[data-game-key="{key}"][data-room-code="{self.game.room_code}"] .add-game-btn')
        if key == 'host_points':
            # Check native OPTION.disabled; server rejection is tested separately.
            self.assertTrue(host.locator('.plan-mode option[value=best_of_n]').evaluate('(o)=>o.disabled')); host.locator('.plan-mode').first.select_option('target_score'); host.locator('.plan-value').first.fill('3')
        else:
            host.locator('.plan-mode').nth(0).select_option('best_of_n'); host.locator('.plan-value').nth(0).fill('7')
            host.locator('.plan-mode').nth(1).select_option('target_score'); host.locator('.plan-value').nth(1).fill('1')
        host.click('#submitBtn'); host.wait_for_url('**/admin-dashboard/sessions/')
        self.hub = HubSession.objects.get(code='CONTROL-BROWSER'); host.goto(self.live_server_url+reverse('games_hub:monitor', args=[self.hub.code])); self.click_response(host, '#activate-session', '/duel/api/session/')
        a, b, spectator = [self.page() for _ in range(3)]
        for page, name in ((a, 'Team Alpha'), (b, 'Team Beta')):
            page.goto(self.live_server_url+reverse('games_hub:lobby', args=[self.hub.code])); page.fill('#nickname', name); self.click_response(page, '#join-form button', '/duel/api/session/'); page.wait_for_selector('#joined:not([hidden])')
        self.click_response(host, '[data-check=start]', '/check-in/start/')
        for page in (a, b): self.click_response(page, '#ready', '/check-in/participant/')
        self.click_response(host, '[data-check=complete]', '/check-in/complete/'); self.click_response(host, '#save-plan', '/duel/api/session/')
        spectator.goto(self.live_server_url+reverse('games_hub:spectate_session', args=[self.hub.code]))
        return host, a, b, spectator

    def enter(self, pages, key, slot):
        host, a, b, spectator = pages
        self.click_response(host, f'.duel-slot:nth-child({slot+1}) .start-slot', '/activate-game/')
        for page, role in ((host, 'host'), (a, 'participant'), (b, 'participant'), (spectator, 'spectator')): page.wait_for_url(f'**/duel/{key}/**/{role}/')
        a.wait_for_selector('#sessionGameStartIntro:not([hidden])'); a.wait_for_selector('#sessionGameStartIntro[hidden]', state='attached', timeout=25000); a.wait_for_selector('#waiting:not([hidden])')
        return DuelGameInstance.objects.get(hub_game_step=self.hub.steps.order_by('order', 'pk')[slot])

    def prepare(self, host, instance, selection):
        host.wait_for_selector('#prepare:not([hidden])'); host.select_option('#question-select', str(selection)); self.click_response(host, '#prepare', '/duel/api/')
        self.click_response(host, '#start', '/units/'); return DuelControlRound.objects.get(unit=instance.units.latest('number'))

    def return_all(self, pages):
        for page in pages: page.wait_for_selector('#return:not([hidden])'); page.click('#return')
        pages[0].wait_for_url('**/hub/monitor/CONTROL-BROWSER/')
        for page in pages[1:3]: page.wait_for_url('**/hub/lobby/CONTROL-BROWSER/'); page.wait_for_selector('#joined:not([hidden])')

    def test_bid_equal_sequence_success_timeout_duplicate_counter_rejoin_both_formats(self):
        pages = self.configure('bid'); host, a, b, spectator = pages
        with worker_environment() as (command, options), TemporaryFile() as output:
            worker = subprocess.Popen(command+['--interval', '0.1'], stdout=output, stderr=output,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **options)
            try:
                for slot in range(2):
                    instance = self.enter(pages, 'bid', slot)
                    for number in range(4 if slot == 0 else 1):
                        timeout = slot == 0 and number == 1
                        state = self.prepare(host, instance, self.timeout_task.pk if timeout else self.question.pk)
                        for page in (a, b, spectator): self.wait_phase(page, state, 'bidding')
                        # A wins equal bids by actual server receipt order; a
                        # larger B bid instead gets B the timed answer right.
                        for page, value in ((a, 2), (b, 3 if timeout else 2)):
                            page.fill('#bid', str(value)); self.click_response(page, '#commit', '/units/')
                            if page == a:
                                for viewer in (b, spectator, host):
                                    private = self.public_json(viewer)['unit']; self.assertNotIn('bids', private); self.assertNotIn('winning_bid', private)
                        if timeout:
                            self.wait_phase(spectator, state, 'answering'); self.assertIn('Team Beta', spectator.inner_text('#right'))
                            self.wait_phase(spectator, state, 'show_result')
                        else:
                            for page in (a, b, spectator): self.wait_phase(page, state, 'answering')
                            self.assertIn('Team Alpha', spectator.inner_text('#right'))
                            a.reload(); self.wait_phase(a, state, 'answering'); self.assertTrue(a.locator('#bid-form').is_hidden())
                            with host.expect_response(lambda r: '/units/' in r.url and r.request.method == 'POST') as response:
                                host.evaluate("()=>{const b=document.getElementById('increment');b.click();b.click();}")
                            self.assertTrue(response.value.ok); state.refresh_from_db(); self.assertEqual(state.counter, 1)
                            self.click_response(host, '#increment', '/units/'); self.wait_phase(spectator, state, 'show_result')
                        state.refresh_from_db(); self.assertEqual(state.unit.outcome.winner_id, instance.participant_a_id)
                        self.assertEqual(DuelScoreEntry.objects.filter(unit=state.unit, delta=1).count(), 1)
                        if slot == 1: self.assertEqual(spectator.locator('progress').count(), 2)
                        self.click_response(host, '#complete', '/units/')
                    instance.refresh_from_db(); self.assertEqual(instance.match_status, 'completed'); self.assertEqual(instance.match_outcome.winner_id, instance.participant_a_id)
                    if slot == 0: self.assertEqual(self.public_json(spectator)['match']['required_wins'], 4)
                    self.return_all(pages)
            finally:
                worker.terminate(); worker.wait(timeout=10)
        self.assertFalse(self.errors, self.errors)

    def test_buzzer_hub_wrong_lock_other_party_rejoin_both_formats_and_return(self):
        pages = self.configure('buzzer'); host, a, b, spectator = pages
        for slot in range(2):
            instance = self.enter(pages, 'buzzer', slot)
            for number in range(4 if slot == 0 else 1):
                state = self.prepare(host, instance, 1); self.click_response(host, '#open', '/units/')
                for page in (a, b): self.wait_phase(page, state, 'open')
                if number == 0:
                    self.click_response(b, '#buzz', '/units/'); self.click_response(host, '#incorrect', '/units/')
                    b.reload(); self.wait_phase(b, state, 'open'); self.assertTrue(b.locator('#buzz').is_disabled()); self.assertIn('gesperrt', b.inner_text('#own-status'))
                self.click_response(a, '#buzz', '/units/'); self.click_response(host, '#correct', '/units/'); self.wait_phase(spectator, state, 'show_result')
                self.assertEqual(self.public_json(spectator)['unit']['winner_id'], instance.participant_a_id)
                self.click_response(host, '#complete', '/units/')
            instance.refresh_from_db(); self.assertEqual(instance.match_status, 'completed'); self.return_all(pages)
        self.game.refresh_from_db(); self.assertEqual(self.game.status, 'waiting'); self.assertFalse(self.game.participants.exists()); self.assertFalse(self.errors, self.errors)

    def test_host_points_target_adjust_correction_doubleclick_rejoin_and_return(self):
        pages = self.configure('host_points'); host, a, b, spectator = pages
        instance = self.enter(pages, 'host_points', 0); state = self.prepare(host, instance, 1)
        host.select_option('#party', str(instance.participant_a_id)); host.fill('#delta', '2')
        with host.expect_response(lambda r: '/units/' in r.url and r.request.method == 'POST') as response:
            host.evaluate("()=>{const b=document.getElementById('adjust');b.click();b.click();}")
        self.assertTrue(response.value.ok); self.assertEqual(DuelScoreEntry.objects.count(), 1)
        a.reload(); self.wait_phase(a, state, 'answering'); a.wait_for_function("()=>document.getElementById('score').textContent.includes('2 Punkte')")
        for delta in (-1, 2): host.fill('#delta', str(delta)); self.click_response(host, '#adjust', '/units/')
        self.wait_phase(spectator, state, 'show_result'); self.assertEqual(spectator.locator('progress').count(), 2)
        self.assertEqual(self.public_json(spectator)['match']['party_a']['round_wins'], 0)
        self.click_response(host, '#complete', '/units/'); self.return_all(pages)
        self.game.refresh_from_db(); self.assertEqual(self.game.status, 'waiting'); self.assertFalse(self.game.participants.exists()); self.assertFalse(self.errors, self.errors)
