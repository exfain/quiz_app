"""Real Hub, four independent identities, target tiebreak and tournament draws."""
from django.urls import reverse
from Estimation.models import EstimationQuestion
from games_hub import test_session_repairs_browser as browser
from . import test_tournaments as fixtures
from . import test_tournament_browser as tournament
from .models import DuelGameInstance, DuelMatchOutcome
from .match_service import get_match_snapshot


class ScoringBrowserTests(browser.SessionBrowserBase):
    click_response=tournament.TournamentBrowserTests.click_response
    setup_pages=tournament.TournamentBrowserTests.setup_pages
    draw_browser=tournament.TournamentBrowserTests.draw_browser
    pair_hosts=tournament.TournamentBrowserTests.pair_hosts
    answers=tournament.TournamentBrowserTests.answers
    release=tournament.TournamentBrowserTests.release
    end_and_return=tournament.TournamentBrowserTests.end_and_return

    def setUp(self):
        super().setUp();hostname=self.host
        self.db(lambda:fixtures.TournamentTests.setUp(self));self.host=hostname
        self.client.force_login(self.user)
        def material():
            q=EstimationQuestion.objects.create(created_by=self.user,question_text='Stichfrage: neue Höhe?',correct_answer=200,unit='meters')
            self.quiz.selected_questions.add(q);return q
        self.second_question=self.db(material)

    def round(self,host,players,spectator,question,a,b):
        host.wait_for_selector('#prepare:not([hidden])');host.select_option('#question-select',str(question.pk))
        self.click_response(host,'#prepare','/api/estimation/')
        host.wait_for_selector('#start:not([hidden])');host.fill('#duration','60');self.click_response(host,'#start','/units/')
        for page,value in zip(players,(a,b)):
            page.wait_for_selector('#estimate:not([disabled])');page.fill('#estimate',str(value))
            self.click_response(page,'#commit','/units/');page.wait_for_selector('#estimate[disabled]',state='attached')
        spectator.wait_for_function("document.getElementById('phase').textContent.includes('geschlossen')")
        snapshot=spectator.evaluate('async()=> (await fetch(document.getElementById("duel-match").dataset.api)).json()')
        self.assertNotIn('answers',snapshot['unit']);self.assertNotIn('target',snapshot['unit'])
        self.assertIsNone(snapshot['match'])
        for action in ('show_answers','show_solution','show_result'):
            host.wait_for_selector('#'+action+':not([hidden])');self.click_response(host,'#'+action,'/units/')
            if action=='show_answers':
                spectator.wait_for_selector('#answers-card:not([hidden])');self.assertTrue(spectator.locator('#solution-card').is_hidden())
        spectator.wait_for_selector('#result-card:not([hidden])')

    def test_planned_target_matchball_enters_stichrunde_reload_then_solo_win(self):
        def plan():
            self.hub.session_type='duel';self.hub.locked_participant_count=2;self.hub.save()
            for party in self.parties[2:]:party.delete()
            self.step.game_format='duel';self.step.duel_match_config={'mode':'target_score','target_score':1};self.step.save()
        self.db(plan);self.parties=self.parties[:2]
        lobby=reverse('games_hub:lobby',args=[self.hub.code])
        players=[self.page(lobby,player=p,theme='vhs') for p in self.parties]
        for page in players:page.wait_for_selector('#joined:not([hidden])')
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True)
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]))
        self.click_response(host,'.start-slot','/activate-game/')
        for page in (*players,spectator):page.wait_for_url('**/duel/estimation/**')
        players[0].wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        self.round(host,players,spectator,self.question,90,110)
        spectator.wait_for_function("document.getElementById('duel-match-notice').textContent.includes('Stichrunden')")
        instance=DuelGameInstance.objects.get(hub_game_step=self.step)
        self.assertEqual(get_match_snapshot(instance.pk)['evaluation'],'sudden_death')
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.click_response(host,'#complete','/units/')
        for page in (host,*players,spectator):page.reload()
        spectator.wait_for_function("document.getElementById('duel-match-notice').textContent.includes('Stichrunden')")
        self.assertTrue(host.locator('#finish-match-draw').is_hidden())
        self.round(host,players,spectator,self.second_question,200,250)
        spectator.wait_for_function("document.getElementById('result').textContent.includes('Matchsieg')")
        self.click_response(host,'#complete','/units/')
        snap=get_match_snapshot(instance.pk)
        self.assertEqual((snap['party_a']['score'],snap['party_b']['score']),(2,1))
        self.assertEqual(snap['winner_id'],self.parties[0].pk)
        for page in (host,*players,spectator):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        for page in players:page.wait_for_url('**/hub/lobby/MIXED/**')
        self.assertFalse(self.errors,self.errors)

    def test_manual_duel_exhaustion_finishes_as_visible_draw_and_returns_to_lobby(self):
        self.db(lambda:self.quiz.selected_questions.remove(self.second_question))
        lobby=reverse('games_hub:lobby',args=[self.hub.code])
        players=[self.page(lobby,player=p,theme='vhs') for p in self.parties]
        for page in players:page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True)
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]))
        config=host.locator('#step-0 .encounter-config')
        config.locator('.encounter-party').nth(0).select_option(str(self.parties[0].pk));config.locator('.encounter-party').nth(1).select_option(str(self.parties[2].pk))
        config.locator('.encounter-mode').select_option('target_score');config.locator('.encounter-value').fill('1')
        self.click_response(host,'#step-0 .encounter-start','/activate-game/')
        selected=[players[0],players[2]]
        for page in (*selected,spectator):page.wait_for_url('**/duel/estimation/**')
        selected[0].wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        self.round(host,selected,spectator,self.question,90,110)
        self.click_response(host,'#complete','/units/');host.wait_for_selector('#finish-match-draw:not([hidden])')
        self.click_response(host,'#finish-match-draw','/api/estimation/')
        for page in (host,spectator):page.wait_for_function("document.getElementById('duel-match-notice').textContent.includes('unentschieden')")
        instance=DuelGameInstance.objects.get(hub_game_step=self.step)
        self.assertTrue(get_match_snapshot(instance.pk)['draw'])
        for page in (host,*selected,spectator):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        for page in players:page.wait_for_url('**/hub/lobby/MIXED/**')
        self.assertFalse(self.errors,self.errors)

    def test_tournament_draw_completes_pair_preserves_reveal_gate_and_next_draw(self):
        host,spectator,players=self.setup_pages()
        pairs=self.draw_browser(host,self.question);controls=self.pair_hosts(host,pairs,players)
        self.answers(pairs[0],players,100,tie=True)
        with controls[0].expect_response(lambda r:'/units/' in r.url and r.request.method=='POST') as blocked:controls[0].click('#show_answers')
        self.assertEqual(blocked.value.status,409)
        self.answers(pairs[1],players,100)
        for page in controls:self.release(page);page.close()
        snap=get_match_snapshot(pairs[0].game_instance_id)
        self.assertTrue(snap['draw']);self.assertEqual(snap['match_status'],'completed')
        spectator.wait_for_function("document.getElementById('results').textContent.includes('Unentschieden')")
        old={p.game_instance_id for p in pairs}
        host.wait_for_function("!document.getElementById('draw').disabled")
        new=self.draw_browser(host,self.second_question)
        self.assertTrue(old.isdisjoint({p.game_instance_id for p in new}))
        controls=self.pair_hosts(host,new,players)
        for pair in new:self.answers(pair,players,200)
        for page in controls:self.release(page);page.close()
        self.end_and_return(host,spectator,players)
        self.assertFalse(self.errors,self.errors)
