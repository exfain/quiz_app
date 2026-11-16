"""Real planning/Hub/browser flow with four independently authenticated contexts."""
import os
from unittest.mock import patch

from channels.testing import ChannelsLiveServerTestCase
from django.urls import reverse
from games_hub.models import HubSession
from games_hub.playwright_e2e import start_chromium_browser
from QuizGame.models import QuizAnswer, QuizParticipant
from .models import DuelGameInstance, DuelQuizRoundState, DuelScoreEntry
from .test_quiz import make_quiz_material
from . import test_playable_browser as reference


class PlayableQuickQuizDuelBrowserTests(ChannelsLiveServerTestCase):
    page = reference.PlayableEstimationBrowserTests.page
    click_response = reference.PlayableEstimationBrowserTests.click_response

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env=patch.dict(os.environ,{'DJANGO_ALLOW_ASYNC_UNSAFE':'1'});cls.env.start()
        cls.pw,cls.browser=start_chromium_browser(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.env.stop();super().tearDownClass()

    def setUp(self):
        make_quiz_material(self);self.contexts=[];self.errors=[]

    def tearDown(self):
        for context in self.contexts:context.close()
        super().tearDown()

    def public_json(self,page):
        return page.evaluate("""async()=>{
            const e=document.getElementById('duel-match'),headers={};
            if(e.dataset.role==='participant')headers.Authorization='Bearer '+JSON.parse(localStorage.getItem('hub_lobby_rejoin:'+e.dataset.code)).token;
            return(await fetch(e.dataset.api,{headers,cache:'no-store'})).json();
        }""")

    def prepare_start(self,host,question):
        host.wait_for_selector('#prepare:not([hidden])')
        host.select_option('#question-select',str(question.pk))
        self.click_response(host,'#prepare','/duel/api/quiz/')
        host.wait_for_selector('#start:not([hidden])')
        instance_id=host.locator('#duel-match').get_attribute('data-instance')
        self.assertIsNone(DuelQuizRoundState.objects.filter(unit__game_instance_id=instance_id).latest('unit__number').deadline)
        self.click_response(host,'#start','/units/')

    def answer(self,page,value):
        selector=f'#options [data-value="{value}"]:not([disabled])'
        page.wait_for_selector(selector)
        page.hover(selector);page.click(selector)
        self.assertEqual(page.locator(selector).get_attribute('aria-pressed'),'true')
        self.click_response(page,'#commit','/units/')
        page.wait_for_function('()=>document.getElementById("own-status").textContent.includes("eingeloggt")')
        self.assertTrue(page.locator('#commit').is_disabled())

    def finish_presentation(self,host):
        host.wait_for_selector('#complete:not([hidden])')
        self.click_response(host,'#complete','/units/')

    def reveal_choices(self,host,spectator):
        host.wait_for_selector('#show_answers:not([hidden])')
        self.assertTrue(spectator.locator('#answers-card').is_hidden())
        old=self.public_json(spectator)
        self.assertNotIn('answers',old['unit']);self.assertNotIn('solution',old['unit'])
        self.click_response(host,'#show_answers','/units/')
        spectator.wait_for_selector('#answers-card:not([hidden])')
        data=self.public_json(spectator)
        self.assertIn('answers',data['unit']);self.assertNotIn('solution',data['unit']);self.assertNotIn('points',data['unit'])
        self.click_response(host,'#show_solution','/units/')
        spectator.wait_for_selector('#solution-card:not([hidden])')
        self.assertTrue(spectator.locator('#result-card').is_hidden())
        # An old actual HTTP snapshot must never roll a newer reveal backwards.
        with spectator.expect_response(lambda r:'/duel/api/quiz/' in r.url and r.request.method=='GET'):
            spectator.route('**/duel/api/quiz/**/spectator/',lambda route:route.fulfill(json=old),times=1)
        self.assertTrue(spectator.locator('#solution-card').is_visible())
        self.click_response(host,'#show_result','/units/')
        spectator.wait_for_selector('#result-card:not([hidden])')

    def test_all_question_types_best_of_seven_then_target_score_via_regular_hub(self):
        host=self.page(host=True)
        host.goto(self.live_server_url+reverse('games_hub:create_session'))
        host.fill('#sessionName','Quick Quiz browser duel');host.fill('[name=code]','QUICK-BROWSER')
        host.select_option('#sessionType','duel')
        for _ in range(2):host.click(f'.game-instance-row[data-game-key="quiz"][data-room-code="{self.quiz.room_code}"] .add-game-btn')
        host.locator('.plan-mode').nth(0).select_option('best_of_n');host.locator('.plan-value').nth(0).fill('7')
        host.locator('.plan-mode').nth(1).select_option('target_score');host.locator('.plan-value').nth(1).fill('2')
        host.click('#submitBtn');host.wait_for_url('**/admin-dashboard/sessions/')
        hub=HubSession.objects.get(code='QUICK-BROWSER')
        host.goto(self.live_server_url+reverse('games_hub:monitor',args=[hub.code]))
        self.click_response(host,'#activate-session','/duel/api/session/')
        alice,bob,spectator=[self.page() for _ in range(3)]
        for page,name in ((alice,'Team Alpha'),(bob,'Team Beta')):
            page.goto(self.live_server_url+reverse('games_hub:lobby',args=[hub.code]))
            page.fill('#nickname',name);self.click_response(page,'#join-form button','/duel/api/session/')
            page.wait_for_selector('#joined:not([hidden])')
        self.click_response(host,'[data-check=start]','/check-in/start/')
        for page in (alice,bob):self.click_response(page,'#ready','/check-in/participant/')
        self.click_response(host,'[data-check=complete]','/check-in/complete/')
        self.click_response(host,'#save-plan','/duel/api/session/')
        host.reload();host.wait_for_selector('.start-slot')
        self.assertEqual(host.locator('.match-mode').nth(0).input_value(),'best_of_n')
        self.assertEqual(host.locator('.match-value').nth(0).input_value(),'7')
        spectator.goto(self.live_server_url+reverse('games_hub:spectate_session',args=[hub.code]))
        self.click_response(host,'.duel-slot:first-child .start-slot','/activate-game/')
        for page,role in ((host,'host'),(alice,'participant'),(bob,'participant'),(spectator,'spectator')):page.wait_for_url(f'**/duel/quiz/**/{role}/')
        alice.wait_for_selector('#sessionGameStartIntro:not([hidden])')
        alice.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        alice.wait_for_selector('#waiting:not([hidden])')
        self.assertTrue(alice.locator('#answer-form').is_hidden());self.assertTrue(alice.locator('#buzz').is_hidden())
        instance=DuelGameInstance.objects.get(hub_session=hub)
        self.assertFalse(instance.units.exists())
        host.wait_for_selector('#question-select option:nth-child(2)',state='attached')
        # Both browser contexts issue a real buzz close together. The DB result,
        # not browser scheduling or party ordering, determines the answer right.
        self.prepare_start(host,self.short)
        for page in (alice,bob):page.wait_for_selector('#buzz:not([disabled])')
        at=alice.evaluate('Date.now()+300')
        for page in (alice,bob):page.evaluate('(at)=>{setTimeout(()=>document.getElementById("buzz").click(),Math.max(0,at-Date.now()));}',at)
        host.wait_for_selector('#judgment:not([hidden])')
        state=DuelQuizRoundState.objects.get(unit__game_instance=instance)
        self.assertIsNotNone(state.buzz_id)
        parties=list(hub.participants.order_by('pk'))
        lead=0 if state.buzz_participant_id==parties[0].pk else 1
        pages=[alice,bob];names=['Team Alpha','Team Beta']
        for page in (alice,bob,spectator):
            page.wait_for_function('(name)=>document.getElementById("answer-right").textContent.includes(name)',arg=names[lead])
            public=self.public_json(page)['unit']
            self.assertNotIn('solution',public);self.assertNotIn('host_solution',public);self.assertNotIn('buzz_id',public)
        frozen=alice.inner_text('#question')
        self.assertLess(len(frozen),len(self.short.question_text))
        with alice.expect_response(lambda r:'/duel/api/quiz/' in r.url and r.request.method=='GET'):pass
        self.assertEqual(alice.inner_text('#question'),frozen)
        pages[1-lead].reload();pages[1-lead].wait_for_selector('#buzz[disabled]')
        self.click_response(host,'#correct','/units/')
        spectator.wait_for_selector('#result-card:not([hidden])')
        self.assertIn(names[lead],spectator.inner_text('#result'))
        self.finish_presentation(host)
        # A false oral answer gives the opponent the point, with a NEW attempt
        # identity even though the source question is intentionally repeated.
        self.prepare_start(host,self.short)
        pages[1-lead].wait_for_selector('#buzz:not([disabled])')
        self.click_response(pages[1-lead],'#buzz','/units/')
        host.wait_for_selector('#judgment:not([hidden])')
        self.click_response(host,'#incorrect','/units/')
        spectator.wait_for_function('name=>document.getElementById("result").textContent.includes("falsch")&&document.getElementById("result").textContent.includes(name)',arg=names[lead])
        self.finish_presentation(host)
        # Two consecutive TF questions exercise first selection, commitment,
        # reload after commit, independent second response, and ordered reveals.
        for index,question in enumerate((self.tf,self.tf2)):
            self.prepare_start(host,question)
            correct='True' if index==0 else 'False'
            if index==0:
                pages[lead].wait_for_selector('#options button:not([disabled])')
                old_own=self.public_json(pages[lead])
                # Hold subsequent GETs on a real pre-commit snapshot. The
                # successful POST projection itself must lock the answer.
                def stale_own(route):
                    route.fulfill(json=old_own) if route.request.method=='GET' else route.continue_()
                pages[lead].route('**/duel/api/quiz/**/participant/',stale_own)
            self.answer(pages[lead],correct)
            if index==0:
                with pages[lead].expect_response(lambda r:'/duel/api/quiz/' in r.url and r.request.method=='GET'):pass
                self.assertTrue(pages[lead].locator('#commit').is_disabled())
                self.assertIn('eingeloggt',pages[lead].inner_text('#own-status'))
                pages[lead].unroute('**/duel/api/quiz/**/participant/',stale_own)
                pages[lead].reload();pages[lead].wait_for_selector('#commit[disabled]')
                pages[lead].wait_for_function('()=>document.getElementById("own-status").textContent.includes("eingeloggt")')
                data=self.public_json(pages[1-lead])['unit']
                self.assertNotIn('own_answer',data);self.assertNotIn('answers',data);self.assertNotIn('solution',data)
            self.answer(pages[1-lead],'False' if correct=='True' else 'True')
            self.reveal_choices(host,spectator)
            self.finish_presentation(host)
        instance.refresh_from_db();self.assertEqual(instance.match_status,'completed')
        self.assertEqual(instance.match_outcome.winner_id,parties[lead].pk)
        self.assertEqual(instance.units.count(),4)
        tally=self.public_json(spectator)['unit']['match']
        self.assertEqual(tally['required_wins'],4);self.assertEqual(tally['party_a' if lead==0 else 'party_b']['round_wins'],4)
        for page in (alice,bob,spectator,host):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        for page in (alice,bob):page.wait_for_url('**/hub/lobby/QUICK-BROWSER/');page.wait_for_selector('#joined:not([hidden])')
        host.wait_for_url('**/hub/monitor/QUICK-BROWSER/')
        # Next planned slot: same quiz, distinct match, new intro/clock/answers.
        host.wait_for_selector('.duel-slot:nth-child(2) .start-slot')
        self.click_response(host,'.duel-slot:nth-child(2) .start-slot','/activate-game/')
        host.wait_for_url('**/duel/quiz/**/host/')
        for page in (alice,bob,spectator):page.wait_for_url('**/duel/quiz/**/')
        alice.wait_for_selector('#sessionGameStartIntro:not([hidden])')
        alice.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        alice.wait_for_selector('#waiting:not([hidden])')
        second=DuelGameInstance.objects.filter(hub_session=hub).exclude(pk=instance.pk).get()
        self.assertFalse(second.units.exists());self.assertEqual(second.participant_a_id,instance.participant_a_id)
        host.wait_for_selector('#question-select option:nth-child(2)',state='attached')
        for index in range(2):
            self.prepare_start(host,self.mc)
            self.answer(alice,'A');self.answer(bob,'A' if index==0 else 'B')
            self.reveal_choices(host,spectator)
            spectator.wait_for_selector('progress')
            self.assertEqual(spectator.locator('progress').count(),2)
            self.assertEqual(spectator.locator('progress').first.get_attribute('value'),str(index+1))
            if index==0:self.assertIn('Beide richtig',spectator.inner_text('#result'))
            self.finish_presentation(host)
        second.refresh_from_db();self.assertEqual(second.match_status,'completed')
        self.assertEqual(second.match_outcome.winner_id,parties[0].pk)
        self.assertEqual(second.units.count(),2)
        self.assertEqual(DuelScoreEntry.objects.filter(game_instance=second).count(),4)
        for page in (alice,bob,spectator,host):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        alice.wait_for_url('**/hub/lobby/QUICK-BROWSER/')
        self.assertFalse(QuizAnswer.objects.exists());self.assertFalse(QuizParticipant.objects.exists())
        self.assertFalse(self.errors,self.errors)
