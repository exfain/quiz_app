"""One real Hub evening: normal game, team game, final duel, tournament."""
import time
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from QuizGame.models import Quiz, QuizQuestion
from Estimation.models import EstimationQuiz, EstimationQuestion
from target_game.models import TargetGame, Run
from duel.models import DuelGameInstance, DuelTournament
from .models import HubSession, HubGameStep, HubParticipant
from .test_session_repairs_browser import SessionBrowserBase
from .views import get_leaderboard_data


class FullHubEveningBrowserTests(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        self.db(self.material)
        self.client.force_login(self.user)

    def material(self):
        self.user=User.objects.create_superuser('evening-host','','pw')
        self.hub=HubSession.objects.create(code='EVENING',creator=self.user,is_active=True,started_at=timezone.now(),
            check_in_status='completed',locked_participant_count=4,overall_scoring_mode='simple',overall_weighting_mode='none')
        self.parties=[HubParticipant.objects.create(session=self.hub,nickname=name,is_active=True,
            scoring_eligible=True,checked_in_at=timezone.now()) for name in ('Alpha','Beta','Gamma','Delta')]
        self.quiz=Quiz.objects.create(creator=self.user,title='Quiz am Anfang')
        self.mc=QuizQuestion.objects.create(created_by=self.user,question_text='Zwei plus zwei ist vier?',
            question_type='true_false',correct_answer='True',time_limit=120,points=1)
        self.quiz.selected_questions.add(self.mc)
        self.target=TargetGame.objects.create(creator=self.user,title='Städte gemeinsam',scoring_mode='simple',answer_seconds=6)
        self.task=self.target.tasks.create(question_text='Nennt Städte.',solutions=[{'text':'Berlin','aliases':['BERLIN']}])
        self.estimate=EstimationQuestion.objects.create(created_by=self.user,question_text='Wie hoch?',correct_answer=100,unit='meters')
        self.final=EstimationQuiz.objects.create(creator=self.user,title='Unser Finalduell')
        self.tournament=EstimationQuiz.objects.create(creator=self.user,title='Gemeinsames Turnier')
        for game in (self.final,self.tournament):game.selected_questions.add(self.estimate)
        self.steps=[HubGameStep.objects.create(session=self.hub,order=i,game_key=key,room_code=game.room_code,title=game.title)
            for i,(key,game) in enumerate((('quiz',self.quiz),('target',self.target),('estimation',self.final),('estimation',self.tournament)))]

    def click_post(self,page,selector,path):
        with page.expect_response(lambda r:path in r.url and r.request.method=='POST') as result:page.click(selector)
        # Successful Hub starts navigate immediately; Chromium may already have
        # discarded that response body. The status belongs to the captured POST.
        self.assertEqual(result.value.status,200,result.value.url)

    def wait_db(self,page,predicate):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if self.db(predicate):return
            page.wait_for_timeout(50)
        self.fail('Der erwartete bestätigte Datenbankzustand wurde nicht erreicht.')

    def intro(self,page,index):
        step=self.steps[index]
        page.wait_for_function('n=>document.getElementById("sessionGameStartIntro")?.dataset.gameNumber===String(n)',arg=index+1)
        intro=page.locator('#sessionGameStartIntro')
        self.assertEqual(intro.get_attribute('data-game-title'),step.title.upper())
        self.assertIn(str(step.pk),intro.get_attribute('data-game-id'))
        page.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)

    def lobby(self,pages):
        for page in pages:
            page.wait_for_url('**/hub/lobby/EVENING/**')
            page.wait_for_function("document.getElementById('joinCard').style.display==='none'")

    def estimation_round(self,host,players):
        host.select_option('#question-select',str(self.estimate.pk));self.click_post(host,'#prepare','/duel/api/estimation/')
        host.wait_for_selector('#start:not([hidden])');host.fill('#duration','60');self.click_post(host,'#start','/units/')
        for page,value in zip(players,('100','120')):
            page.wait_for_selector('#estimate:not([disabled])');page.fill('#estimate',value);self.click_post(page,'#commit','/units/')
        players[0].reload();players[0].wait_for_selector('#estimate[disabled]',state='attached')
        self.assertTrue(players[0].locator('#sessionGameStartIntro').is_hidden())
        for action in ('show_answers','show_solution','show_result','complete'):
            host.wait_for_selector('#'+action+':not([hidden])');self.click_post(host,'#'+action,'/units/')

    def test_normal_quiz_target_manual_duel_tournament_then_evening_scores(self):
        lobby=reverse('games_hub:lobby',args=[self.hub.code])
        players=[self.page(lobby,player=p,theme='vhs') for p in self.parties]
        self.lobby(players)
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True)
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]),theme='vhs')
        # 1. Normal multiplayer start, even though later slots use duel formats.
        host.click(f'.launch-btn[data-room-code="{self.quiz.room_code}"]');host.wait_for_url('**/quiz/**/monitor/**')
        host.wait_for_function('window.adminGameMonitor?.websocket?.readyState===WebSocket.OPEN')
        host.click('#startQuizBtn')
        for page in players:page.wait_for_url('**/quiz/play/**')
        self.intro(players[0],0)
        host.click(f'.send-question-btn[data-question-id="{self.mc.pk}"]')
        host.wait_for_selector('#sendPreparedQuestionBtn');host.click('#sendPreparedQuestionBtn')
        host.wait_for_selector('#revealQuestionContentBtn:not([disabled])');host.click('#revealQuestionContentBtn')
        for page in players:
            page.wait_for_selector('#answerOptions [data-value="True"]:not([disabled])')
            page.click('#answerOptions [data-value="True"]');page.click('#submitAnswerBtn')
            page.wait_for_selector('#answerSubmittedState:not(.d-none)')
        players[0].reload();players[0].wait_for_selector('#answerSubmittedState:not(.d-none)')
        self.assertTrue(players[0].locator('#sessionGameStartIntro').is_hidden())
        host.click('#endQuestionBtn')
        self.wait_db(host,lambda:self.quiz.quiz_answers.count()==4)
        host.click('#endQuizBtn')
        for page in players:
            page.wait_for_selector('#returnToLobbyBtn:not([disabled])');page.click('#returnToLobbyBtn')
        self.lobby(players)
        host.goto(self.live_server_url+reverse('games_hub:monitor',args=[self.hub.code]))
        # 2. Same four identities take part in the team game.
        host.click(f'.launch-btn[data-room-code="{self.target.room_code}"]');host.wait_for_url('**/target/monitor/**')
        self.click_post(host,'#startGameBtn','/activate-game/')
        for page in players:page.wait_for_url('**/target/play/**')
        self.intro(players[0],1)
        host.select_option('#task',str(self.task.pk));self.click_post(host,'#prepare','/target/api/')
        for page in players:
            page.wait_for_selector('#nomination:not([hidden])');self.click_post(page,'#ready','/target/api/')
            page.fill('#goal','1');self.click_post(page,'#commit-goal','/target/api/')
        host.wait_for_selector('#start:not([disabled])');self.click_post(host,'#start','/target/api/')
        for page in players:
            page.wait_for_selector('#answer:not([disabled])');page.fill('#answer','Berlin');self.click_post(page,'#commit-answer','/target/api/')
        players[1].goto(self.live_server_url+lobby);players[1].wait_for_url('**/target/play/**')
        players[1].wait_for_function("document.getElementById('own-answers').children.length===1")
        frame=spectator.frame_locator('#target-spectator-frame');frame.locator('#question').wait_for()
        host.wait_for_selector('#publish:not([hidden])');self.click_post(host,'#publish','/target/api/')
        frame.locator('#results:not([hidden])').wait_for()
        self.click_post(host,'#finish','/target/api/')
        for page in players:page.wait_for_selector('#return:not([hidden])');page.click('#return')
        self.lobby(players);host.click('#return');host.wait_for_url('**/hub/monitor/EVENING/')
        before=self.db(lambda:get_leaderboard_data(self.hub))
        self.assertEqual({p['total_score'] for p in before['participants']},{2})
        # 3. Only the selected two leave the lobby; the pairing survives rejoin.
        form=host.locator('#step-2 .encounter-config')
        for select,party in zip(form.locator('.encounter-party').all(),self.parties[:2]):
            select.locator(f'option[value="{party.pk}"]').wait_for(state='attached');select.select_option(str(party.pk))
        form.locator('.encounter-mode').select_option('target_score');form.locator('.encounter-value').fill('1')
        self.click_post(host,'#step-2 .encounter-start','/activate-game/');host.wait_for_url('**/duel/estimation/**/host/')
        for page in players[:2]:page.wait_for_url('**/duel/estimation/**/participant/')
        spectator.wait_for_url('**/duel/estimation/**/spectator/');self.intro(players[0],2)
        self.lobby(players[2:]);self.estimation_round(host,players[:2])
        spectator.wait_for_selector('#return:not([hidden])');self.assertIn('Matchsieg',spectator.inner_text('#result'))
        for page in (host,spectator,*players[:2]):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        self.lobby(players);host.wait_for_url('**/hub/monitor/EVENING/');spectator.wait_for_url('**/hub/spectate/EVENING/')
        # 4. All four join two parallel pairings in the same tournament set.
        self.click_post(host,'#step-3 .tournament-start','/activate-game/');host.wait_for_url('**/duel/tournament/**/host/')
        for page in players:page.wait_for_url('**/duel/tournament/**/participant/')
        spectator.wait_for_url('**/duel/tournament/**/spectator/')
        host.select_option('#material',str(self.estimate.pk));host.fill('#duration','60')
        self.click_post(host,'#draw','/api/tournament/')
        pairs=self.db(lambda:list(DuelTournament.objects.get(step=self.steps[3]).sets.get().pairs.select_related('game_instance').order_by('position')))
        self.assertEqual(len(pairs),2)
        by_id={party.pk:page for party,page in zip(self.parties,players)}
        controls=[]
        for pair in pairs:
            control=host.context.new_page();control.on('pageerror',lambda error:self.errors.append(str(error)))
            control.goto(self.live_server_url+reverse('duel:match_page',args=[self.hub.code,pair.game_instance_id,'host']))
            controls.append(control)
            for pk in (pair.game_instance.participant_a_id,pair.game_instance.participant_b_id):by_id[pk].wait_for_url(f'**/{pair.game_instance_id}/participant/')
        self.intro(players[0],3)
        for control in controls:control.wait_for_selector('#start:not([hidden])');self.click_post(control,'#start','/units/')
        for pair in pairs:
            for pk,value in ((pair.game_instance.participant_a_id,'100'),(pair.game_instance.participant_b_id,'120')):
                page=by_id[pk];page.wait_for_selector('#estimate:not([disabled])');page.fill('#estimate',value);self.click_post(page,'#commit','/units/')
        players[2].goto(self.live_server_url+lobby);players[2].wait_for_url('**/duel/estimation/**/participant/')
        players[2].wait_for_selector('#estimate[disabled]',state='attached')
        for control in controls:
            for action in ('show_answers','show_solution','show_result','complete'):
                control.wait_for_selector('#'+action+':not([hidden])');self.click_post(control,'#'+action,'/units/')
            control.close()
        host.wait_for_selector('#end:not([disabled])');self.click_post(host,'#end','/api/tournament/')
        for page in players:
            page.click('#tournament-pair a');page.wait_for_url('**/duel/tournament/**/participant/')
            page.wait_for_selector('#return:not([hidden])');page.click('#return')
        self.lobby(players)
        for page in (host,spectator):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        host.wait_for_url('**/hub/monitor/EVENING/');spectator.wait_for_url('**/hub/spectate/EVENING/')
        after=self.db(lambda:get_leaderboard_data(self.hub))
        self.assertEqual([(p['name'],p['total_score']) for p in before['participants']],[(p['name'],p['total_score']) for p in after['participants']])
        self.assertEqual({g['game_key'] for g in after['games']},{'quiz','target'})
        self.assertEqual(self.db(lambda:Run.objects.get(step=self.steps[1]).teams.filter(is_active=True).count()),0)
        self.assertEqual(self.db(lambda:DuelGameInstance.objects.filter(hub_session=self.hub).count()),3)
        host.wait_for_function('window.hubMonitorSocket?.readyState===WebSocket.OPEN')
        host.click('#endSessionBtn');host.wait_for_url('**/hub/session/EVENING/leaderboard/**')
        self.wait_db(host,lambda:HubSession.objects.get(pk=self.hub.pk).ended_at is not None)
        rows=host.locator('.ranking-row')
        self.assertEqual(rows.count(),4)
        self.assertEqual(set(rows.locator('.player-name').all_text_contents()),{p.nickname for p in self.parties})
        for row in rows.all():self.assertEqual(float(row.locator('td').nth(2).inner_text()),2)
        self.assertFalse(self.errors,self.errors)
