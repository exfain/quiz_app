"""Full browser acceptance through planning, Hub, four contexts and real HTTP."""
import json
import os
import subprocess
from unittest.mock import patch

from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.urls import reverse
from games_hub.models import HubSession
from games_hub.playwright_e2e import start_chromium_browser, install_browser_test_stubs
from .models import DuelGameInstance, DuelUnit, DuelScoreEntry
from .test_hub_playable import make_material
from .test_deadline_worker import worker_environment


def has_key(value, key):
    if isinstance(value,dict):return key in value or any(has_key(v,key) for v in value.values())
    if isinstance(value,list):return any(has_key(v,key) for v in value)
    return False


class PlayableEstimationBrowserTests(ChannelsLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env=patch.dict(os.environ,{'DJANGO_ALLOW_ASYNC_UNSAFE':'1'});cls.env.start()
        cls.pw,cls.browser=start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.env.stop();super().tearDownClass()

    def setUp(self):
        make_material(self);self.contexts=[];self.errors=[]

    def tearDown(self):
        for context in self.contexts:context.close()
        super().tearDown()

    def page(self, host=False):
        context=self.browser.new_context(reduced_motion='reduce');self.contexts.append(context)
        install_browser_test_stubs(context)
        context.add_init_script("if(window.crypto)Object.defineProperty(window.crypto,'randomUUID',{value:undefined});")
        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(self.live_server_url) else route.abort())
        if host:
            self.client.force_login(self.user)
            context.add_cookies([{'name':settings.SESSION_COOKIE_NAME,'value':self.client.cookies[settings.SESSION_COOKIE_NAME].value,'url':self.live_server_url}])
        page=context.new_page();page.on('pageerror',lambda e:self.errors.append(str(e)));return page

    def click_response(self, page, selector, contains):
        with page.expect_response(lambda r:contains in r.url and r.request.method=='POST') as result:
            page.click(selector)
        response=result.value
        self.assertTrue(response.ok, f'HTTP {response.status} for {response.url}')

    def play(self, mode, threshold, rounds):
        host=self.page(host=True)
        host.goto(self.live_server_url+reverse('games_hub:create_session'))
        host.fill('#sessionName','Browser Duell');host.fill('[name=code]','BROWSER-DUEL')
        host.select_option('#sessionType','duel')
        host.click(f'.game-instance-row[data-room-code="{self.quiz.room_code}"] .add-game-btn')
        if mode=='best_of_n':
            host.click(f'.game-instance-row[data-room-code="{self.quiz.room_code}"] .add-game-btn')
            host.locator('.plan-mode').nth(1).select_option('target_score')
            host.locator('.plan-value').nth(1).fill('1')
        host.locator('.plan-mode').first.select_option(mode);host.locator('.plan-value').first.fill(str(threshold))
        host.click('#submitBtn');host.wait_for_url('**/admin-dashboard/sessions/')
        hub=HubSession.objects.get(code='BROWSER-DUEL')
        host.goto(self.live_server_url+reverse('games_hub:monitor',args=[hub.code]))
        self.click_response(host,'#activate-session','/duel/api/session/')
        alice,bob,spectator=[self.page() for _ in range(3)]
        for page,name in ((alice,'Team Alpha'),(bob,'Team Beta')):
            page.goto(self.live_server_url+reverse('games_hub:lobby',args=[hub.code]))
            page.fill('#nickname',name)
            self.click_response(page,'#join-form button','/duel/api/session/')
            page.wait_for_selector('#joined:not([hidden])')
        self.click_response(host,'[data-check=start]','/check-in/start/')
        for page in (alice,bob):self.click_response(page,'#ready','/check-in/participant/')
        self.click_response(host,'[data-check=complete]','/check-in/complete/')
        # Save/reload/re-edit via the regular Hub plan, not ORM test setup.
        host.locator('.match-mode').first.select_option(mode);host.locator('.match-value').first.fill(str(threshold))
        self.click_response(host,'#save-plan','/duel/api/session/')
        host.reload();host.wait_for_selector('.start-slot')
        self.assertEqual(host.locator('.match-value').first.input_value(),str(threshold))
        spectator.goto(self.live_server_url+reverse('games_hub:spectate_session',args=[hub.code]))
        self.click_response(host,'.duel-slot:first-child .start-slot','/activate-game/')
        host.wait_for_url('**/host/');alice.wait_for_url('**/participant/');bob.wait_for_url('**/participant/');spectator.wait_for_url('**/spectator/')
        alice.wait_for_selector('#sessionGameStartIntro:not([hidden])',timeout=10000)
        alice.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        alice.wait_for_selector('#waiting:not([hidden])')
        self.assertTrue(alice.locator('#answer-form').is_hidden())
        instance=DuelGameInstance.objects.get(hub_session=hub)
        self.assertEqual(instance.units.count(),0)
        host.wait_for_selector('#question-select option:nth-child(2)', state='attached')
        for index in range(rounds):
            host.select_option('#question-select',str(self.question.pk))
            self.click_response(host,'#prepare','/duel/api/estimation/')
            host.wait_for_selector('#start:not([hidden])')
            host.fill('#duration','60')
            # Actual delivered public JSON before answer/solution release.
            data=bob.evaluate("async()=>{const e=document.getElementById('duel-match');const c=JSON.parse(localStorage.getItem('hub_lobby_rejoin:'+e.dataset.code));return (await fetch(e.dataset.api,{headers:{Authorization:'Bearer '+c.token}})).json();}")
            self.assertFalse(has_key(data,'target'))
            self.assertFalse(has_key(data,'answers'))
            self.click_response(host,'#start','/units/')
            for page in (alice,bob):page.wait_for_selector('#estimate:not([disabled])')
            alice.fill('#estimate','95');self.click_response(alice,'#commit','/units/')
            alice.wait_for_selector('#estimate[disabled]')
            data=bob.evaluate("async()=>{const e=document.getElementById('duel-match');const c=JSON.parse(localStorage.getItem('hub_lobby_rejoin:'+e.dataset.code));return (await fetch(e.dataset.api,{headers:{Authorization:'Bearer '+c.token}})).json();}")
            self.assertFalse(has_key(data,'answers'));self.assertFalse(has_key(data,'target'))
            self.assertIsNone(data['unit']['own_answer'])
            bob.fill('#estimate','120');self.click_response(bob,'#commit','/units/')
            old_public=spectator.evaluate("async()=> (await fetch(document.getElementById('duel-match').dataset.api)).json()")
            host.wait_for_selector('#show_answers:not([hidden])')
            self.assertTrue(spectator.locator('#answers-card').is_hidden())
            self.click_response(host,'#show_answers','/units/')
            spectator.wait_for_selector('#answers-card:not([hidden])')
            self.assertIn('95',spectator.inner_text('#answers'))
            self.assertIn('120',spectator.inner_text('#answers'))
            self.assertTrue(spectator.locator('#solution-card').is_hidden())
            bob.reload();bob.wait_for_selector('#answers-card:not([hidden])')
            self.assertTrue(bob.locator('#solution-card').is_hidden())
            self.click_response(host,'#show_solution','/units/')
            spectator.wait_for_selector('#solution-card:not([hidden])')
            self.assertIn('100',spectator.inner_text('#solution'))
            # Deliver a real old HTTP response after a newer revision has painted.
            if index==0:
                spectator.route('**/duel/api/estimation/**/spectator/',lambda route:route.fulfill(json=old_public),times=1)
                with spectator.expect_response(lambda r:'/duel/api/estimation/' in r.url):
                    spectator.wait_for_timeout(700)
                self.assertTrue(spectator.locator('#solution-card').is_visible())
            self.assertTrue(spectator.locator('#result-card').is_hidden())
            self.click_response(host,'#show_result','/units/')
            spectator.wait_for_selector('#result-card:not([hidden])')
            self.assertIn('Team Alpha',spectator.inner_text('#result'))
            if mode=='target_score':
                self.assertEqual(spectator.locator('progress').count(),2)
                self.assertEqual(spectator.locator('progress').first.get_attribute('value'),str(index+1))
            self.click_response(host,'#complete','/units/')
            if index+1<rounds:host.wait_for_selector('#prepare:not([hidden])')
        instance.refresh_from_db()
        self.assertEqual(instance.match_status,'completed')
        self.assertEqual(instance.units.count(),rounds)
        self.assertEqual(DuelScoreEntry.objects.filter(game_instance=instance).count(),rounds*2)
        self.assertEqual(instance.match_outcome.winner_id,instance.participant_a_id)
        for page in (alice,bob,spectator,host):
            page.wait_for_selector('#return:not([hidden])')
            page.click('#return')
        alice.wait_for_url('**/hub/lobby/BROWSER-DUEL/')
        alice.wait_for_selector('#joined:not([hidden])')
        host.wait_for_url('**/hub/monitor/BROWSER-DUEL/')
        if mode=='best_of_n':
            # A second planned slot with the SAME material has fresh identity,
            # intro, round and clock, while signed rejoin keeps the same parties.
            host.wait_for_selector('.duel-slot:nth-child(2) .start-slot')
            self.click_response(host,'.duel-slot:nth-child(2) .start-slot','/activate-game/')
            host.wait_for_url('**/host/');alice.wait_for_url('**/participant/');bob.wait_for_url('**/participant/');spectator.wait_for_url('**/spectator/')
            alice.reload()
            alice.wait_for_selector('#sessionGameStartIntro:not([hidden])')
            alice.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
            alice.wait_for_selector('#waiting:not([hidden])')
            second=DuelGameInstance.objects.filter(hub_session=hub).exclude(pk=instance.pk).get()
            self.assertEqual((second.participant_a_id,second.participant_b_id),(instance.participant_a_id,instance.participant_b_id))
            self.assertFalse(second.units.exists())
            host.wait_for_selector('#question-select option:nth-child(2)',state='attached')
            host.select_option('#question-select',str(self.question.pk));self.click_response(host,'#prepare','/duel/api/estimation/')
            host.wait_for_selector('#start:not([hidden])');host.fill('#duration','5')
            with worker_environment() as (command, options):
                worker=subprocess.Popen([*command,'--interval','0.05'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,**options)
                try:
                    self.click_response(host,'#start','/units/')
                    alice.wait_for_selector('#estimate:not([disabled])');alice.fill('#estimate','95')
                    self.click_response(alice,'#commit','/units/')
                    host.wait_for_selector('#show_answers:not([hidden])',timeout=15000)
                    self.click_response(host,'#show_answers','/units/')
                    self.click_response(host,'#show_solution','/units/')
                    host.wait_for_selector('#show_result[disabled]')
                    self.assertIn('Mindestens eine Antwort fehlt',host.inner_text('#error'))
                    self.assertFalse(DuelScoreEntry.objects.filter(game_instance=second).exists())
                    second.refresh_from_db();self.assertEqual(second.match_status,'in_progress')
                    self.assertEqual(second.units.get().estimation_round_state.phase,'show_solution')
                finally:
                    worker.terminate();worker.wait(timeout=10)
        self.assertFalse(self.errors,self.errors)

    def test_best_of_seven_four_wins_through_regular_browser_entry_points(self):
        self.play('best_of_n',7,4)

    def test_target_score_progress_and_match_finish_through_regular_browser_entry_points(self):
        self.play('target_score',2,2)
