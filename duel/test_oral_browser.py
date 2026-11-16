"""Four actual browser contexts, regular planning/Hub, both match formats."""
import os
import subprocess
from datetime import timedelta
from unittest.mock import patch
from channels.testing import ChannelsLiveServerTestCase
from django.urls import reverse
from games_hub.models import HubSession
from games_hub.playwright_e2e import start_chromium_browser
from .test_oral import make_material
from . import test_playable_browser as reference
from . import test_quiz_browser as quiz_reference
from .models import DuelGameInstance, DuelOralRound, DuelBuzz, DuelScoreEntry
from .test_deadline_worker import worker_environment


class OralDuelBrowserTests(ChannelsLiveServerTestCase):
    page=reference.PlayableEstimationBrowserTests.page
    click_response=reference.PlayableEstimationBrowserTests.click_response
    public_json=quiz_reference.PlayableQuickQuizDuelBrowserTests.public_json

    @classmethod
    def setUpClass(cls):
        super().setUpClass();cls.env=patch.dict(os.environ,{'DJANGO_ALLOW_ASYNC_UNSAFE':'1'});cls.env.start()
        cls.pw,cls.browser=start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.env.stop();super().tearDownClass()

    def setUp(self):self.contexts=[];self.errors=[]

    def tearDown(self):
        for context in self.contexts:context.close()
        super().tearDown()

    def prepare_start(self,host,instance):
        host.wait_for_selector('#prepare:not([hidden])');host.select_option('#question-select',str(self.question.pk))
        self.click_response(host,'#prepare','/duel/api/')
        host.wait_for_selector('#start:not([hidden])')
        state=DuelOralRound.objects.get(unit=instance.units.latest('number'))
        self.assertIsNone(state.started_at);self.assertIsNone(state.deadline)
        self.click_response(host,'#start','/units/')
        state.refresh_from_db();return state

    def finish(self,host):
        host.wait_for_selector('#complete:not([hidden])');self.click_response(host,'#complete','/units/')

    def play(self,key):
        make_material(self,key)
        if key=='who':
            self.question.people=[{'name':p['name'],'is_lying':True} for p in self.question.people]
            self.question.save()
        if key=='wann_war_das':
            self.question.max_tolerance=100;self.question.save()
        host=self.page(host=True);host.goto(self.live_server_url+reverse('games_hub:create_session'))
        host.fill('#sessionName','Oral duel browser');host.fill('[name=code]','ORAL-BROWSER');host.select_option('#sessionType','duel')
        for _ in range(2):host.click(f'.game-instance-row[data-game-key="{key}"][data-room-code="{self.game.room_code}"] .add-game-btn')
        host.locator('.plan-mode').nth(0).select_option('best_of_n');host.locator('.plan-value').nth(0).fill('7')
        host.locator('.plan-mode').nth(1).select_option('target_score');host.locator('.plan-value').nth(1).fill('1')
        host.click('#submitBtn');host.wait_for_url('**/admin-dashboard/sessions/')
        hub=HubSession.objects.get(code='ORAL-BROWSER')
        host.goto(self.live_server_url+reverse('games_hub:monitor',args=[hub.code]))
        self.click_response(host,'#activate-session','/duel/api/session/')
        alice,bob,spectator=[self.page() for _ in range(3)];players=[alice,bob]
        for page,name in ((alice,'Team Alpha'),(bob,'Team Beta')):
            page.goto(self.live_server_url+reverse('games_hub:lobby',args=[hub.code]));page.fill('#nickname',name)
            self.click_response(page,'#join-form button','/duel/api/session/');page.wait_for_selector('#joined:not([hidden])')
        self.click_response(host,'[data-check=start]','/check-in/start/')
        for page in players:self.click_response(page,'#ready','/check-in/participant/')
        self.click_response(host,'[data-check=complete]','/check-in/complete/')
        self.click_response(host,'#save-plan','/duel/api/session/');host.reload();host.wait_for_selector('.start-slot')
        self.assertEqual(host.locator('.match-mode').first.input_value(),'best_of_n');self.assertEqual(host.locator('.match-value').first.input_value(),'7')
        spectator.goto(self.live_server_url+reverse('games_hub:spectate_session',args=[hub.code]))
        self.click_response(host,'.duel-slot:first-child .start-slot','/activate-game/')
        for page,role in ((host,'host'),(alice,'participant'),(bob,'participant'),(spectator,'spectator')):page.wait_for_url(f'**/duel/{key}/**/{role}/')
        alice.wait_for_selector('#sessionGameStartIntro:not([hidden])');alice.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        alice.wait_for_selector('#waiting:not([hidden])');self.assertTrue(alice.locator('#buzz').is_hidden())
        instance=DuelGameInstance.objects.get(hub_session=hub);self.assertFalse(instance.units.exists())
        host.wait_for_selector('#question-select option:nth-child(2)',state='attached')
        parties=list(hub.participants.order_by('pk'));names=['Team Alpha','Team Beta'];lead=None
        for index in range(4):
            state=self.prepare_start(host,instance)
            for page in players:page.wait_for_selector('#buzz:not([disabled])')
            if index==0:
                deadline=state.deadline
                bob.reload();bob.wait_for_selector('#buzz:not([disabled])')
                self.assertEqual(DuelOralRound.objects.get(pk=state.pk).deadline,deadline)
                if key=='who':
                    spectator.wait_for_function('()=>document.getElementById("person").textContent.length>0')
                    first=spectator.inner_text('#person')
                    spectator.wait_for_function('name=>document.getElementById("person").textContent!==name',arg=first,timeout=5000)
                if key=='who_that':
                    spectator.wait_for_function('()=>document.getElementById("photo").complete&&document.getElementById("photo").naturalWidth===96')
                    self.assertNotIn('SecretPerson',str(self.public_json(spectator)))
                # Two real browser clients click close together. Which request
                # obtains the DB lock is deliberately NOT assumed by the test.
                at=alice.evaluate('Date.now()+250')
                for page in players:page.evaluate('(at)=>{setTimeout(()=>document.getElementById("buzz").click(),Math.max(0,at-Date.now()));}',at)
            else:
                side=(1-lead) if key=='clue_rush' or key in ('who_that','wann_war_das') and index%2 else lead
                self.click_response(players[side],'#buzz','/units/')
            if key=='who':
                spectator.wait_for_selector('#result-card:not([hidden])')
                attempt=state.unit.buzzes.get();computed=(attempt.accepted_at-state.started_at)//timedelta(microseconds=state.material['person_us'])
                self.assertEqual(attempt.captured['person_index'],computed)
                if index==0:
                    self.assertGreaterEqual(computed,1);lead=0 if attempt.participant_id==parties[0].pk else 1
                self.assertIn(attempt.captured['name'],spectator.inner_text('#result'))
            else:
                host.wait_for_selector('#judgment:not([hidden])')
                first_attempt=state.unit.buzzes.latest('sequence')
                first_side=0 if first_attempt.participant_id==parties[0].pk else 1
                if index==0:lead=1-first_side if key=='clue_rush' else first_side
                for page in (alice,bob,spectator):
                    page.wait_for_function('()=>document.getElementById("answer-right").textContent.length>0')
                    public=self.public_json(page)['unit'];self.assertNotIn('solution',public);self.assertNotIn('host_solution',public);self.assertNotIn('accepted_range',public)
                if key=='clue_rush':
                    frozen=self.public_json(spectator)['unit']['clues']
                    state.refresh_from_db();saved=state.elapsed_us
                    if index==0:
                        # Deliberately spend longer assessing than the entire
                        # original 6s schedule; this is the product scenario.
                        host.wait_for_timeout(7000)
                        self.assertEqual(self.public_json(spectator)['unit']['clues'],frozen)
                    self.click_response(host,'#incorrect','/units/')
                    state.refresh_from_db();self.assertEqual(state.elapsed_us,saved)
                    self.assertGreater(state.deadline,state.resumed_at)
                    self.assertEqual((state.deadline-state.resumed_at)//timedelta(microseconds=1),state.material['duration_us']-saved)
                    self.assertFalse(DuelScoreEntry.objects.filter(unit=state.unit).exists())
                    players[first_side].wait_for_selector('#buzz[disabled]')
                    players[lead].wait_for_selector('#buzz:not([disabled])');self.click_response(players[lead],'#buzz','/units/')
                    host.wait_for_selector('#judgment:not([hidden])');self.click_response(host,'#correct','/units/')
                else:
                    if key=='wann_war_das' and index==0:
                        frozen=first_attempt.captured['tolerance'];host.wait_for_timeout(1500)
                        self.assertEqual(self.public_json(alice)['unit']['tolerance'],frozen)
                        self.assertIn(frozen,alice.inner_text('#tolerance'));self.assertIn('Eingefrorener',host.inner_text('#range'))
                    self.click_response(host,'#incorrect' if index%2 else '#correct','/units/')
                spectator.wait_for_selector('#result-card:not([hidden])')
            self.assertIn(names[lead],spectator.inner_text('#result'))
            self.finish(host)
        instance.refresh_from_db();self.assertEqual(instance.match_status,'completed');self.assertEqual(instance.match_outcome.winner_id,parties[lead].pk)
        tally=self.public_json(spectator)['unit']['match'];self.assertEqual(tally['required_wins'],4)
        self.assertEqual(tally['party_a' if lead==0 else 'party_b']['round_wins'],4)
        for page in (alice,bob,spectator,host):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        for page in players:page.wait_for_url('**/hub/lobby/ORAL-BROWSER/');page.wait_for_selector('#joined:not([hidden])')
        host.wait_for_url('**/hub/monitor/ORAL-BROWSER/');host.wait_for_selector('.duel-slot:nth-child(2) .start-slot')
        self.click_response(host,'.duel-slot:nth-child(2) .start-slot','/activate-game/')
        host.wait_for_url(f'**/duel/{key}/**/host/')
        for page in (alice,bob,spectator):page.wait_for_url(f'**/duel/{key}/**/')
        alice.wait_for_selector('#sessionGameStartIntro:not([hidden])');alice.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        alice.wait_for_selector('#waiting:not([hidden])')
        second=DuelGameInstance.objects.filter(hub_session=hub).exclude(pk=instance.pk).get();self.assertFalse(second.units.exists())
        host.wait_for_selector('#question-select option:nth-child(2)',state='attached')
        # Confirmed no-buzz rule through the real local worker and HTTP views.
        # The first target-score unit ends 0:0; the next unit can still win.
        original_limit=self.question.time_limit
        if key=='clue_rush':self.question.clues.update(duration=1)
        else:self.question.time_limit=1;self.question.save()
        with worker_environment() as (command,options):
            worker=subprocess.Popen([*command,'--interval','0.05'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,**options)
            try:
                state=self.prepare_start(host,second)
                host.wait_for_selector('#show_result:not([hidden])',timeout=15000)
                public=self.public_json(spectator)['unit'];self.assertNotIn('solution',public);self.assertNotIn('points',public)
                self.assertFalse(DuelScoreEntry.objects.filter(unit=state.unit).exists())
                self.click_response(host,'#show_result','/units/')
                spectator.wait_for_selector('#result-card:not([hidden])')
                self.assertIn('Kein Rundensieger',spectator.inner_text('#result'))
                self.assertEqual(self.public_json(spectator)['unit']['points'],{'a':0,'b':0})
                self.finish(host)
            finally:worker.terminate();worker.wait(timeout=10)
        if key=='clue_rush':self.question.clues.update(duration=2)
        else:self.question.time_limit=original_limit;self.question.save()
        state=self.prepare_start(host,second);alice.wait_for_selector('#buzz:not([disabled])');self.click_response(alice,'#buzz','/units/')
        if key!='who':host.wait_for_selector('#judgment:not([hidden])');self.click_response(host,'#correct','/units/')
        spectator.wait_for_function("unit=>document.getElementById('duel-match').dataset.unit===unit&&!document.getElementById('result-card').hidden",arg=str(state.unit_id))
        spectator.wait_for_selector('progress')
        self.assertEqual(spectator.locator('progress').count(),2);self.assertEqual(spectator.locator('progress').first.get_attribute('value'),'1')
        self.finish(host);second.refresh_from_db();self.assertEqual(second.match_status,'completed');self.assertEqual(second.match_outcome.winner_id,parties[0].pk)
        for page in (alice,bob,spectator,host):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        alice.wait_for_url('**/hub/lobby/ORAL-BROWSER/')
        self.game.refresh_from_db();self.assertEqual(self.game.status,'waiting');self.assertEqual(self.game.participants.count(),0)
        self.assertFalse(self.errors,self.errors)

    def test_clue_rush_two_attempts_paused_assessment_and_both_formats(self):self.play('clue_rush')
    def test_who_is_lying_name_switch_and_both_formats(self):self.play('who')
    def test_who_is_that_image_reveal_and_both_formats(self):self.play('who_that')
    def test_wann_war_das_frozen_tolerance_and_both_formats(self):self.play('wann_war_das')
