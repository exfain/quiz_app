"""Real content/planning/Hub/HTTP flow, independent Host, teams and spectator."""
from django.urls import reverse
from django.utils import timezone
from games_hub import test_session_repairs_browser as browser
from games_hub.models import HubSession,HubGameStep,HubParticipant
from .tests import Fixture
from .models import Answer,Run


class TargetBrowserTests(browser.SessionBrowserBase):
    def setUp(self):
        super().setUp();self.db(lambda:Fixture.setUp(self))

    def click_response(self,page,selector,path='/target/api/'):
        with page.expect_response(lambda response:path in response.url and response.request.method=='POST') as response:
            page.click(selector)
        self.assertEqual(response.value.status,200,response.value.text())

    def exercise(self,mode):
        host=self.page(reverse('target_game:manage')+f'?game={self.game.pk}',host=True,theme='vhs')
        host.locator('#game-form [name=scoring_mode]').select_option(mode)
        host.locator('#game-form [name=answer_seconds]').fill('12')
        with host.expect_navigation():host.locator('#game-form button').click()
        host.goto(self.live_server_url+reverse('games_hub:create_session'))
        host.fill('[name=name]','Zielansage Browser');host.fill('[name=code]','TARGETBROWSER')
        host.locator(f'.game-instance-row[data-room-code="{self.game.room_code}"] .add-game-btn').click()
        with host.expect_navigation():host.click('#submitBtn')
        def activate():
            self.hub=HubSession.objects.get(code='TARGETBROWSER');HubSession.activate_exclusive(self.hub.code)
            self.hub.refresh_from_db();self.hub.started_at=timezone.now();self.hub.check_in_status='completed';self.hub.locked_participant_count=3;self.hub.save()
            self.parties=[HubParticipant.objects.create(session=self.hub,nickname=name,is_active=True,scoring_eligible=True)
                for name in ['Team Alpha','Team Beta','Team Gamma']]
            self.step=HubGameStep.objects.get(session=self.hub)
        self.db(activate)
        players=[self.page(reverse('games_hub:lobby',args=[self.hub.code]),player=p,theme='vhs') for p in self.parties]
        for page in players:page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]),theme='vhs')
        host.goto(self.live_server_url+reverse('games_hub:monitor',args=[self.hub.code]))
        host.wait_for_function('window.hubMonitorSocket?.readyState===WebSocket.OPEN')
        host.click('.launch-btn')
        host.wait_for_url('**/target/monitor/**')
        self.click_response(host,'#startGameBtn','/activate-game/')
        for page in players:page.wait_for_url('**/target/play/**')
        players[0].wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        host.select_option('#task',str(self.task.pk));self.click_response(host,'#prepare')
        for side,page in enumerate(players):
            page.wait_for_selector('#nomination:not([hidden])');self.click_response(page,'#ready')
            page.wait_for_selector('#goal-form:not([hidden])');page.fill('#goal','3' if side==2 else '2');self.click_response(page,'#commit-goal')
            page.wait_for_selector('#goal-form[hidden]',state='attached')
        host.wait_for_selector('#start:not([disabled])');self.click_response(host,'#start')
        for side,answers in enumerate((['Munich','Muenchen','Bärlin'],['Munich','Berlin'],['Hamburg'])):
            page=players[side];page.wait_for_selector('#answer:not([disabled])')
            for value in answers:
                page.fill('#answer',value);self.click_response(page,'#commit-answer')
                page.wait_for_function('document.getElementById("answer").value===""')
        players[0].reload();players[0].wait_for_function("document.getElementById('own-answers').children.length===3")
        self.assertIn('2',players[0].inner_text('#target-label'))
        frame=spectator.frame_locator('#target-spectator-frame')
        frame.locator('#target-game').wait_for()
        secret=frame.locator('#target-game').evaluate('async root=>(await fetch(root.dataset.api)).json()')
        self.assertNotIn('solutions',secret['round']);self.assertNotIn('teams',secret['round']);self.assertNotIn('own',secret['round'])
        host.wait_for_selector('#publish:not([hidden])',timeout=25000)
        players[0].wait_for_function("document.getElementById('target-game').dataset.phase==='review'")
        self.assertTrue(players[0].locator('#answer').is_disabled())
        self.assertIn('1 unterschiedliche richtige',host.inner_text('#corrections'))
        answer_id=self.db(lambda:Answer.objects.get(text='Bärlin').pk)
        host.get_by_label('Richtige Lösung für Bärlin').select_option('1')
        self.click_response(host,f'[data-answer-id="{answer_id}"]')
        host.wait_for_function("document.getElementById('corrections').textContent.includes('Hostkorrektur')")
        self.click_response(host,'#publish')
        for page in (host,*players):page.wait_for_selector('#results:not([hidden])')
        frame.locator('#results:not([hidden])').wait_for()
        points=4 if mode=='ranking' else 2;failed=1 if mode=='ranking' else 0
        for page in (host,*players):
            text=page.inner_text('#result-rows')
            self.assertIn(f'Team Alpha: Ziel 2 · 2 richtig · Ziel erreicht · Platz 1 · {points} Punkte',text)
            self.assertIn(f'Team Beta: Ziel 2 · 2 richtig · Ziel erreicht · Platz 1 · {points} Punkte',text)
            self.assertIn(f'Team Gamma: Ziel 3 · 1 richtig · Ziel verfehlt · Platz 3 · {failed} Punkte',text)
        self.assertIn(f'{points} Punkte',frame.locator('#result-rows').inner_text())
        self.click_response(host,'#finish')
        for page in players:
            page.wait_for_selector('#return:not([hidden])');page.click('#return');page.wait_for_url('**/hub/lobby/TARGETBROWSER/**')
        host.click('#return');host.wait_for_url('**/hub/monitor/TARGETBROWSER/**')
        def verify():
            run=Run.objects.get(step=self.step);self.assertIsNotNone(run.completed_at)
            self.assertEqual(list(run.teams.order_by('pk').values_list('total_score',flat=True)),[points,points,failed])
            self.assertFalse(run.teams.filter(is_active=True).exists())
        self.db(verify);self.assertFalse(self.errors,self.errors)

    def test_simple_from_planned_hub_to_corrected_result_and_manual_return(self):self.exercise('simple')
    def test_ranking_tied_targets_rejoin_deadline_correction_and_manual_return(self):self.exercise('ranking')
