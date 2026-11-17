"""Complete planned evenings with 25 independent browser identities."""
from decimal import Decimal
import re
import os
from functools import partial
from pathlib import Path
from channels.testing.live import make_application, set_database_connection

from django.contrib.auth.models import User
from django.urls import reverse
from QuizGame.models import Quiz, QuizQuestion
from Estimation.models import EstimationQuiz, EstimationQuestion
from target_game.models import TargetGame, Answer, Run
from duel.models import DuelGameInstance, DuelTournament, DuelUnitOutcome, DuelScoreEntry
from .models import HubSession,HubGameStep,HubParticipant
from django.utils import timezone
from .test_session_repairs_browser import SessionBrowserBase
from . import test_evening_browser as reference
from .evening_test_server import ObservedStaticFilesHandler


class TwentyFivePlayerEveningBrowserTests(SessionBrowserBase):
    static_wrapper=ObservedStaticFilesHandler
    wait_db = reference.FullHubEveningBrowserTests.wait_db

    def click_post(self,page,selector,path):
        with page.expect_response(lambda response:path in response.url and response.request.method=='POST') as result:
            page.click(selector)
        if result.value.status!=200:
            self.fail(f'{result.value.status} {result.value.url}: {result.value.text()}')
        return result.value

    def setUp(self):
        super().setUp()
        # Each independent evening gets a fresh server before its fixture is
        # created. Never flush a DB while a previous 25-device request can still
        # be using it. In-night reloads/rejoins still use the same live server.
        cls=type(self)
        if not cls._server_process.is_alive():
            cls._server_process=cls.ProtocolServerProcess(cls.host,
                partial(make_application,static_wrapper=cls.static_wrapper),setup=set_database_connection)
            cls._server_process.start()
            if not cls._server_process.ready.wait(30):
                raise RuntimeError('The isolated evening server did not start.')
            cls._port=cls._server_process.port.value
        self.db(self.material)
        self.client.force_login(self.user)

    def page(self,path,**options):
        if '/hub/lobby/' in path:
            # Twenty-five phone-sized displays, plus desktop host/projector.
            # This avoids allocating 25 full desktop rendering surfaces locally.
            options['viewport']={'width':480,'height':640}
        page=super().page(path,**options)
        page.set_default_navigation_timeout(60000)
        if options.get('host'):
            # This 25-device load test measured up to 75 queued DB operations.
            # Await the exact host acknowledgement, with no retry or change to
            # server-side deadlines/handshake limits. Small-device tests retain
            # their normal timeout in SessionBrowserBase.
            page.set_default_timeout(60000)
        return page

    def context(self,**options):
        context=super().context(**options)
        # Do not synchronously proxy every internal poll through Python when 25
        # devices are open. The real server handles all same-origin traffic;
        # only the existing external UI-library stubs are retained.
        context.unroute('**/*')
        context.route(re.compile(r'^(?!'+re.escape(self.live_server_url)+r'(?:/|$)).*'),
            lambda route:route.fulfill(status=200,content_type='text/javascript',body=''))
        # Observe real reconnects across page reloads without modifying transport.
        context.add_init_script("""(() => {
          window.evening25Clicks=[];
          document.addEventListener('click',event=>{
            const b=event.target.closest('button'),leave=event.target.closest('[data-host-game-leave]'),start=event.target.closest('[data-host-game-start]');
            window.evening25Clicks.push({id:b?.id,classes:b?.className,leave:leave?.id,start:start?.id,selected:b?.dataset.hostQuestionReadyToSend});
          },true);
          const Native=window.WebSocket;
          window.WebSocket=class extends Native {
            constructor(...args) {
              super(...args);
              this.addEventListener('close',event=>{
                const key='evening25-close-events';
                const rows=JSON.parse(sessionStorage.getItem(key)||'[]');
                rows.push({path:new URL(this.url).pathname,code:event.code,reason:event.reason});
                sessionStorage.setItem(key,JSON.stringify(rows));
              });
              this.addEventListener('message',event=>{
                try {
                  const data=JSON.parse(event.data),key='evening25-message-events';
                  if(data.type==='quiz_started' && new URL(this.url).pathname.startsWith('/ws/quiz/')) window.evening25GameJoined=true;
                  const rows=JSON.parse(sessionStorage.getItem(key)||'[]');
                  rows.push({type:data.type,code:data.code,message:data.message,error:data.error,revision:data.state_revision});
                  sessionStorage.setItem(key,JSON.stringify(rows.slice(-60)));
                } catch (_) {}
              });
            }
            send(raw) {
              try {
                const data=JSON.parse(raw),key='evening25-outbound-events';
                const rows=JSON.parse(sessionStorage.getItem(key)||'[]');
                rows.push({type:data.type,revision:data.state_revision});
                sessionStorage.setItem(key,JSON.stringify(rows.slice(-30)));
              } catch (_) {}
              return super.send(raw);
            }
          };
        })();""")
        return context

    def material(self):
        self.user=User.objects.create_superuser('evening25-host','','pw')
        self.quiz=Quiz.objects.create(creator=self.user,title='25 Teams Quick Quiz')
        self.questions=[
            QuizQuestion.objects.create(created_by=self.user,question_text='Zwei plus zwei ist vier?',question_type='true_false',correct_answer='True',time_limit=120,points=1),
            QuizQuestion.objects.create(created_by=self.user,question_text='Welche Antwort ist vier?',question_type='multiple_choice',option_a='4',option_b='5',correct_answer='A',time_limit=120,points=1),
        ]
        self.quiz.selected_questions.set(self.questions)
        self.quiz.question_order=[q.pk for q in self.questions];self.quiz.save()
        self.target=TargetGame.objects.create(creator=self.user,title='25 Teams Zielansage',scoring_mode='simple',answer_seconds=120)
        self.task=self.target.tasks.create(question_text='Nennt deutsche Städte.',solutions=[
            {'text':'Berlin','aliases':[]},{'text':'München','aliases':['Munich','Muenchen']},{'text':'Hamburg','aliases':[]}])
        self.final=EstimationQuiz.objects.create(creator=self.user,title='25 Teams Finalduell')
        self.tournament=EstimationQuiz.objects.create(creator=self.user,title='25 Teams Turnier')
        self.estimates=[EstimationQuestion.objects.create(created_by=self.user,question_text=f'Höhe {n}?',correct_answer=n,unit='meters') for n in (100,200)]
        for game in (self.final,self.tournament):game.selected_questions.set(self.estimates)
        self.names=[f'Team {n:02d}' for n in range(1,26)]

    def mark(self,stage):
        self.stage=stage
        print(f'Evening25[{self.mode}]: {stage}',flush=True)

    def intro(self,page,index):
        step=self.steps[index]
        # Late devices resume the absolute server timeline; an already expired
        # intro must not be restarted merely to expose animation DOM attributes.
        page.wait_for_function('n=>window.sessionGameStartIntro?.getState().gameNumber===String(n)',arg=index+1,timeout=60000)
        state=page.evaluate('window.sessionGameStartIntro.getState()')
        self.assertEqual(state['gameTitle'].upper(),step.title.upper())
        self.assertIn(str(step.pk),state['gameInstanceId'])
        page.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)

    def tearDown(self):
        try:
            if any(case is self for case,_ in self._outcome.result.errors+self._outcome.result.failures):
                print('Evening25 javascript errors',self.errors,flush=True)
                for label,page in [('host',getattr(self,'host_page',None)),*[(f'player {i+1}',p) for i,p in enumerate(getattr(self,'player_pages',[])) if i in (0,24)]]:
                    if page and not page.is_closed():
                        try:data=page.evaluate("({body:document.body?.innerText.slice(0,4000),count:document.getElementById('participantCount')?.textContent,error:document.getElementById('quizHostError')?.textContent,revision:window.adminGameMonitor?.stateRevision,messages:sessionStorage.getItem('evening25-message-events')})")
                        except Exception as error:data={'diagnostic_unavailable':str(error)}
                        print('Evening25 failure',getattr(self,'stage','setup'),label,page.url,data,flush=True)
        finally:
            try:
                super().tearDown()
            finally:
                server=type(self)._server_process
                if server.is_alive():server.terminate()
                server.join()

    def lobby(self,pages):
        for page in pages:
            # A burst closes up to 75 old sockets before 25 signed re-joins.
            # Use the same bounded load-test budget as host acknowledgements.
            page.wait_for_url(f'**/hub/lobby/{self.hub.code}/**',timeout=60000)
            page.wait_for_function("document.getElementById('joinCard')?.style.display==='none'",timeout=60000)
            self.assertEqual(page.input_value('#nickname'),self.names[self.player_pages.index(page)])

    def setup_evening(self,mode,weighting,target_mode):
        self.mode=mode;self.mark('Planung')
        self.db(lambda:TargetGame.objects.filter(pk=self.target.pk).update(scoring_mode=target_mode,answer_seconds=120 if mode=='simple' else 90))
        host=self.page(reverse('games_hub:create_session'),host=True,theme='vhs')
        self.host_page=host
        host.fill('#sessionName','25 Teams '+mode);host.fill('[name=code]','EVENING25')
        host.select_option('[name=overall_scoring_mode]',mode)
        host.select_option('[name=overall_weighting_mode]',weighting)
        host.fill('[name=weighting_step]','0.15');host.fill('[name=weighting_cap]','1.2')
        for game in (self.quiz,self.target,self.final,self.tournament):
            host.click(f'.game-instance-row[data-room-code="{game.room_code}"] .add-game-btn')
        with host.expect_navigation():host.click('#submitBtn')
        self.hub=self.db(lambda:HubSession.objects.get(code='EVENING25'))
        self.steps=self.db(lambda:list(self.hub.steps.order_by('order')))
        self.assertEqual(len(self.steps),4);self.assertEqual(self.hub.overall_scoring_mode,mode)
        self.assertEqual(self.hub.overall_weighting_mode,weighting)
        host.goto(self.live_server_url+reverse('games_hub:monitor',args=[self.hub.code]))
        host.wait_for_function('window.hubMonitorSocket?.readyState===WebSocket.OPEN')
        with host.expect_navigation():host.click('#startSessionBtn')
        self.mark('25 getrennte Lobby-Beitritte und Check-in')
        players=[];self.player_pages=players
        for i,name in enumerate(self.names):
            self.mark('Beitritt '+name)
            page=self.page(reverse('games_hub:lobby',args=[self.hub.code]),theme='vhs' if i%2 else 'standard')
            page.fill('#nickname',name)
            try:
                page.wait_for_function("document.getElementById('joinCard')?.style.display==='none' || document.getElementById('joinBtn')?.disabled===false")
                if page.locator('#joinCard').is_visible():page.click('#joinBtn')
            except Exception:
                print('Lobby25 diagnostic',name,page.url,page.evaluate("({title:document.title,body:document.body?.innerText.slice(0,1000),status:document.getElementById('nicknameStatus')?.textContent,value:document.getElementById('nickname')?.value,joined:document.getElementById('joinCard')?.style.display==='none',socket:window.hubLobbySocket?.readyState})"),self.errors,flush=True)
                raise
            page.wait_for_function("document.getElementById('joinCard')?.style.display==='none'")
            self.assertEqual(page.evaluate("JSON.parse(localStorage.getItem('hub_lobby_rejoin:EVENING25')).nickname"),name)
            closes=page.evaluate("JSON.parse(sessionStorage.getItem('evening25-close-events')||'[]')")
            if closes:print('Lobby25 reconnect',name,closes,flush=True)
            players.append(page)
        self.parties=self.db(lambda:[self.hub.participants.get(nickname=name) for name in self.names])
        host.locator('[data-session-panel-target="checkInPanel"]').click()
        host.click('#startCheckInBtn')
        for name,page in zip(self.names,players):
            try:
                page.wait_for_selector('#readyCheckInBtn:not([disabled])');page.click('#readyCheckInBtn')
            except Exception:
                print('Evening25 check-in diagnostic',name,page.url,page.evaluate("({body:document.body?.innerText.slice(0,1500),socket:window.hubLobbySocket?.readyState,closes:sessionStorage.getItem('evening25-close-events')})"),flush=True)
                raise
        host.wait_for_function("document.getElementById('checkInReadyBadge').textContent.includes('25')")
        host.click('#completeCheckInBtn')
        self.wait_db(host,lambda:HubSession.objects.get(pk=self.hub.pk).locked_participant_count==25)
        self.hub=self.db(lambda:HubSession.objects.get(pk=self.hub.pk))
        self.assertEqual(self.db(lambda:self.hub.get_official_participants().count()),25)
        host.locator('#checkInPanel .session-panel-close').click()
        host.wait_for_selector('#sessionPanelBackdrop:not(.is-active)',state='attached')
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]),theme='vhs')
        return host,spectator,players

    def quiz_game(self,host,players):
        self.mark('Quick Quiz: zwei Fragen, 50 verbindliche Antworten')
        host.click(f'.launch-btn[data-room-code="{self.quiz.room_code}"]');host.wait_for_url('**/quiz/**/monitor/**')
        host.wait_for_function('window.adminGameMonitor?.websocket?.readyState===WebSocket.OPEN');host.click('#startQuizBtn')
        players[0].wait_for_url('**/quiz/play/**')
        self.intro(players[0],0)
        for page in players:
            page.wait_for_url('**/quiz/play/**')
            # Navigation is not the game socket's join acknowledgement. Wait
            # for each actual device, rather than starting during its handshake.
            page.wait_for_function("window.evening25GameJoined===true")
            # On phone-sized displays use the existing collapsible scorebox;
            # do not force clicks through its open floating panel.
            page.wait_for_selector('.qa-score-widget__toggle')
            if page.locator('.qa-score-widget:not(.is-collapsed)').count():
                page.locator('.qa-score-widget__toggle').click()
            page.wait_for_selector('.qa-score-widget.is-collapsed')
        host.wait_for_function("document.getElementById('participantCount')?.textContent==='25'")
        for number,question in enumerate(self.questions):
            host.click(f'.send-question-btn[data-question-id="{question.pk}"]')
            try:host.wait_for_selector('#sendPreparedQuestionBtn')
            except Exception:
                host.screenshot(path=str(Path(os.environ['QUIZ_QA_ROOT']).parent/'evening25-quiz-failure.png'))
                print('Evening25 quiz prepare diagnostic',host.evaluate("({error:document.getElementById('quizHostError')?.textContent,revision:window.adminGameMonitor?.stateRevision,active:window.adminGameMonitor?.isActive,socket:window.adminGameMonitor?.websocket?.readyState,button:document.querySelector('.send-question-btn')?.outerHTML,clicks:window.evening25Clicks,outbound:sessionStorage.getItem('evening25-outbound-events'),messages:sessionStorage.getItem('evening25-message-events')})"),flush=True)
                raise
            host.click('#sendPreparedQuestionBtn')
            host.wait_for_selector('#revealQuestionContentBtn:not([disabled])');host.click('#revealQuestionContentBtn')
            for i,page in enumerate(players):
                correct=i%(3 if number==0 else 4)!=0
                value=('True' if correct else 'False') if number==0 else ('A' if correct else 'B')
                option=f'#answerOptions [data-value="{value}"]:not([disabled])'
                page.wait_for_selector(option);page.click(option)
                self.assertTrue(page.locator(f'#answerOptions [data-value="{value}"]').evaluate("el=>el.classList.contains('selected')"))
                page.click('#submitAnswerBtn')
            for page in players:page.wait_for_selector('#answerSubmittedState:not(.d-none)')
            if number==0:
                players[12].reload();players[12].wait_for_selector('#answerSubmittedState:not(.d-none)')
                self.assertTrue(players[12].locator('#sessionGameStartIntro').is_hidden())
            host.click('#endQuestionBtn')
            host.wait_for_selector('#returnToQuestionOverviewBtn:not(.d-none)')
            self.wait_db(host,lambda:self.quiz.quiz_answers.count()==25*(number+1))
            if number==0:
                with host.expect_navigation():host.click('#returnToQuestionOverviewBtn')
                host.wait_for_selector(f'.send-question-btn[data-question-id="{self.questions[1].pk}"]:not([disabled])')
        self.quiz_scores=[int(i%3!=0)+int(i%4!=0) for i in range(25)]
        self.assertEqual(self.db(lambda:dict(self.quiz.participants.values_list('name','total_score'))),dict(zip(self.names,self.quiz_scores)))
        for i in (0,1):
            players[i].locator('.qa-score-widget__toggle').click()
            players[i].wait_for_function('n=>document.getElementById("quizScoreTotal").textContent.trim()===`${n}/2`',arg=self.quiz_scores[i])
        host.click('#endQuizBtn')
        for page in players:page.wait_for_selector('#returnToLobbyBtn:not([disabled])');page.click('#returnToLobbyBtn')
        self.lobby(players);host.goto(self.live_server_url+reverse('games_hub:monitor',args=[self.hub.code]))

    def target_game(self,host,spectator,players,target_mode):
        self.mark('Zielansage: Ziele, Duplikate, Frist, Korrektur')
        host.click(f'.launch-btn[data-room-code="{self.target.room_code}"]');host.wait_for_url('**/target/monitor/**')
        self.click_post(host,'#startGameBtn','/activate-game/')
        players[0].wait_for_url('**/target/play/**')
        self.intro(players[0],1)
        for page in players:page.wait_for_url('**/target/play/**')
        host.select_option('#task',str(self.task.pk));self.click_post(host,'#prepare','/target/api/')
        self.goals=[i%3+1 for i in range(25)]
        self.counts=[goal-int(i%5==0) for i,goal in enumerate(self.goals)]
        for i,page in enumerate(players):
            page.wait_for_selector('#nomination:not([hidden])');self.click_post(page,'#ready','/target/api/')
            page.fill('#goal',str(self.goals[i]));self.click_post(page,'#commit-goal','/target/api/')
        host.wait_for_selector('#start:not([disabled])');self.click_post(host,'#start','/target/api/')
        words=('Berlin','Munich','Hamburg')
        answers_by_team=[];answer_posts=[]
        def record(response):
            if '/target/api/' in response.url and response.request.method=='POST':answer_posts.append(response.status)
        for i,page in enumerate(players):
            page.wait_for_selector('#answer:not([disabled])')
            answers=[*words[:self.counts[i]],'Manuelle Korrektur Team 01' if i==0 else 'Unbekannte Antwort ohne Stadt']
            if self.counts[i]>=2:answers.append('Muenchen')
            answers_by_team.append(answers)
            page.on('response',record)
        # Independent devices submit in bursts; do not serialize all 25 teams'
        # network round trips inside a single team's real answer deadline.
        for turn in range(max(map(len,answers_by_team))):
            active=[i for i,answers in enumerate(answers_by_team) if turn<len(answers)]
            for i in active:
                page=players[i];page.fill('#answer',answers_by_team[i][turn]);page.click('#commit-answer')
            for i in active:
                page=players[i]
                page.wait_for_function("document.getElementById('answer').value===''")
        self.assertEqual(answer_posts,[200]*sum(map(len,answers_by_team)))
        for page in players:page.remove_listener('response',record)
        players[7].goto(self.live_server_url+reverse('games_hub:lobby',args=[self.hub.code]));players[7].wait_for_url('**/target/play/**')
        players[7].wait_for_function("document.getElementById('own-answers').children.length>0")
        frame=spectator.frame_locator('#target-spectator-frame');frame.locator('#target-game').wait_for()
        data=frame.locator('#target-game').evaluate('async root=>(await fetch(root.dataset.api)).json()')
        self.assertNotIn('solutions',data['round']);self.assertNotIn('teams',data['round'])
        configured_seconds=self.db(lambda:Run.objects.get(step=self.steps[1]).answer_seconds)
        host.wait_for_selector('#publish:not([hidden])',timeout=(configured_seconds+15)*1000)
        for page in players:page.wait_for_function("document.getElementById('target-game').dataset.phase==='review'")
        answer=self.db(lambda:Answer.objects.get(text='Manuelle Korrektur Team 01'))
        host.get_by_label('Richtige Lösung für Manuelle Korrektur Team 01').select_option('0')
        self.click_post(host,f'[data-answer-id="{answer.pk}"]','/target/api/');self.counts[0]=1
        self.click_post(host,'#publish','/target/api/');frame.locator('#results:not([hidden])').wait_for()
        successful=[i for i in range(25) if self.counts[i]>=self.goals[i]]
        self.target_scores=[self.goals[i] if i in successful else 0 for i in range(25)] if target_mode=='simple' else [
            26-sum(self.goals[j]>self.goals[i] for j in successful) if i in successful else 1 for i in range(25)]
        self.assertEqual(self.db(lambda:dict(Run.objects.get(step=self.steps[1]).teams.values_list('participant__nickname','total_score'))),dict(zip(self.names,self.target_scores)))
        self.click_post(host,'#finish','/target/api/')
        for page in players:page.wait_for_selector('#return:not([hidden])');page.click('#return')
        self.lobby(players);host.click('#return');host.wait_for_url('**/hub/monitor/EVENING25/')

    def manual_duel(self,host,spectator,players,mode):
        self.mark('Manuelles Duell: zwei Parteien, 23 bleiben in der Lobby')
        form=host.locator('#step-2 .encounter-config');selected=(0,24)
        for select,i in zip(form.locator('.encounter-party').all(),selected):
            select.locator(f'option[value="{self.parties[i].pk}"]').wait_for(state='attached');select.select_option(str(self.parties[i].pk))
        form.locator('.encounter-mode').select_option(mode);form.locator('.encounter-value').fill('3' if mode=='best_of_n' else '2')
        self.click_post(host,'#step-2 .encounter-start','/activate-game/');host.wait_for_url('**/duel/estimation/**/host/')
        selected_pages=[players[i] for i in selected]
        for page in selected_pages:page.wait_for_url('**/duel/estimation/**/participant/')
        spectator.wait_for_url('**/duel/estimation/**/spectator/');self.intro(selected_pages[0],2)
        self.lobby([p for i,p in enumerate(players) if i not in selected])
        for _ in range(2):
            host.wait_for_selector('#prepare:not([hidden])');host.select_option('#question-select',str(self.estimates[0].pk))
            self.click_post(host,'#prepare','/duel/api/estimation/');host.wait_for_selector('#start:not([hidden])');host.fill('#duration','120')
            self.click_post(host,'#start','/units/')
            for page,value in zip(selected_pages,('100','130')):
                page.wait_for_selector('#estimate:not([disabled])');page.fill('#estimate',value);self.click_post(page,'#commit','/units/')
            for action in ('show_answers','show_solution','show_result','complete'):
                host.wait_for_selector('#'+action+':not([hidden])');self.click_post(host,'#'+action,'/units/')
        spectator.wait_for_function("document.getElementById('result')?.textContent.includes('Matchsieg')")
        self.assertIn(self.parties[selected[0]].nickname,spectator.inner_text('#result'))
        for page in (host,spectator,*selected_pages):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        self.lobby(players);host.wait_for_url('**/hub/monitor/EVENING25/');spectator.wait_for_url('**/hub/spectate/EVENING25/')

    def tournament_game(self,host,spectator,players,expected_instances=25):
        self.mark('Turnier: zwei Sets, je 12 parallele Paarungen und ein Freilos')
        self.click_post(host,'#step-3 .tournament-start','/activate-game/');host.wait_for_url('**/duel/tournament/**/host/')
        for page in players:page.wait_for_url('**/duel/tournament/**/participant/')
        spectator.wait_for_url('**/duel/tournament/**/spectator/')
        run=self.db(lambda:DuelTournament.objects.get(step=self.steps[3]))
        by_id={p.pk:page for p,page in zip(self.parties,players)}
        control=host.context.new_page();control.on('pageerror',lambda error:self.errors.append(str(error)))
        control.set_default_timeout(60000);control.set_default_navigation_timeout(60000)
        previous_instances=set()
        for number,question in enumerate(self.estimates,1):
            self.mark(f'Turnier Set {number}: auslosen und zwölf Paarungen starten')
            host.select_option('#material',str(question.pk));host.fill('#duration','180')
            self.click_post(host,'#draw','/api/tournament/')
            host.wait_for_function('n=>document.getElementById("tournament").dataset.set===String(n)',arg=number)
            current=self.db(lambda:run.sets.get(number=number))
            pairs=self.db(lambda:list(current.pairs.select_related('game_instance').order_by('position')))
            self.assertEqual(len(pairs),12);self.assertEqual(self.db(lambda:current.seats.count()),25)
            bye=self.db(lambda:current.seats.get(pair__isnull=True))
            self.assertEqual(len({pk for pair in pairs for pk in (pair.game_instance.participant_a_id,pair.game_instance.participant_b_id)}),24)
            self.assertTrue(previous_instances.isdisjoint({pair.game_instance_id for pair in pairs}))
            previous_instances.update(pair.game_instance_id for pair in pairs)
            host.reload();host.wait_for_function('n=>document.getElementById("tournament").dataset.set===String(n)',arg=number)
            self.assertEqual(self.db(lambda:list(current.pairs.order_by('position').values_list('game_instance_id',flat=True))),[p.game_instance_id for p in pairs])
            by_id[bye.participant_id].wait_for_function("document.getElementById('bye').textContent.includes('Freilos')")
            for pair in pairs:
                for pk in (pair.game_instance.participant_a_id,pair.game_instance.participant_b_id):by_id[pk].wait_for_url(f'**/{pair.game_instance_id}/participant/')
                control.goto(self.live_server_url+reverse('duel:match_page',args=[self.hub.code,pair.game_instance_id,'host']))
                control.wait_for_selector('#start:not([hidden])')
                if number==1 and pair==pairs[0]:self.intro(by_id[pair.game_instance.participant_a_id],3)
                self.click_post(control,'#start','/units/')
            for position,pair in enumerate(pairs):
                self.mark(f'Turnier Set {number}: Abgaben der Paarung {position+1}/12')
                value=100 if number==1 else 200
                for pk,answer in ((pair.game_instance.participant_a_id,value),(pair.game_instance.participant_b_id,value if number==2 else value+20)):
                    page=by_id[pk];page.wait_for_selector('#estimate:not([disabled])');page.fill('#estimate',str(answer));self.click_post(page,'#commit','/units/')
                if position==0:
                    control.goto(self.live_server_url+reverse('duel:match_page',args=[self.hub.code,pair.game_instance_id,'host']))
                    control.wait_for_selector('#show_answers:not([hidden])')
                    with control.expect_response(lambda r:'/units/' in r.url and r.request.method=='POST') as blocked:control.click('#show_answers')
                    self.assertEqual(blocked.value.status,409)
                    self.assertEqual(blocked.value.json()['error']['code'],'tournament_reveal_locked')
                    early=spectator.evaluate('async()=> (await fetch(document.getElementById("tournament").dataset.api)).json()')
                    self.assertFalse(any('points' in pair for pair in early['pairs']))
                    self.assertEqual(len(early['results']),(number-1)*13)  # 12 encounters plus the visible bye
                    rejoining=by_id[pair.game_instance.participant_a_id]
                    rejoining.goto(self.live_server_url+reverse('games_hub:lobby',args=[self.hub.code]))
                    rejoining.wait_for_url(f'**/{pair.game_instance_id}/participant/');rejoining.wait_for_selector('#estimate[disabled]',state='attached')
            for pair in pairs:
                control.goto(self.live_server_url+reverse('duel:match_page',args=[self.hub.code,pair.game_instance_id,'host']))
                for action in ('show_answers','show_solution','show_result','complete'):
                    control.wait_for_selector('#'+action+':not([hidden])');self.click_post(control,'#'+action,'/units/')
            self.mark(f'Turnier Set {number}: alle Ergebnisse und das Freilos veröffentlicht')
            host.wait_for_function("!document.getElementById('draw').disabled")
            spectator.wait_for_function('n=>document.querySelectorAll("#results p").length===n',arg=number*13)
            self.assertFalse(self.db(lambda:DuelScoreEntry.objects.filter(game_instance__in=[p.game_instance_id for p in pairs],participant_id=bye.participant_id).exists()))
            if number==2:
                outcomes=self.db(lambda:list(DuelUnitOutcome.objects.filter(unit__game_instance_id__in=[p.game_instance_id for p in pairs])))
                self.assertEqual(len(outcomes),12);self.assertTrue(all(o.winner_id is None for o in outcomes))
        control.close()
        host.wait_for_selector('#end:not([disabled])');self.click_post(host,'#end','/api/tournament/')
        for page in players:
            if '/duel/estimation/' in page.url:page.click('#tournament-pair a')
            page.wait_for_url('**/duel/tournament/**/participant/');page.wait_for_selector('#return:not([hidden])');page.click('#return')
        self.lobby(players)
        for page in (host,spectator):page.wait_for_selector('#return:not([hidden])');page.click('#return')
        host.wait_for_url('**/hub/monitor/EVENING25/');spectator.wait_for_url('**/hub/spectate/EVENING25/')
        self.assertEqual(self.db(lambda:DuelGameInstance.objects.filter(hub_session=self.hub).count()),expected_instances)

    def expected_overall(self,mode,weighting):
        quiz=self.quiz_scores;target=self.target_scores
        if mode=='ranking':
            quiz=[25-sum(other>value for other in self.quiz_scores) for value in self.quiz_scores]
            target=[25-sum(other>value for other in self.target_scores) for value in self.target_scores]
        weight=Decimal('1.15') if weighting=='linear_cap' else Decimal(1)
        return {name:Decimal(a)+Decimal(b)*weight for name,a,b in zip(self.names,quiz,target)}

    def play_evening(self,mode,weighting,target_mode,duel_mode):
        host,spectator,players=self.setup_evening(mode,weighting,target_mode)
        self.quiz_game(host,players);self.target_game(host,spectator,players,target_mode)
        expected=self.expected_overall(mode,weighting)
        api=f'/hub/api/session/{self.hub.code}/leaderboard/'
        before=host.evaluate('async url=>(await fetch(url)).json()',api)
        self.assertEqual({p['name']:Decimal(str(p['total_with_adjustment'])) for p in before['participants']},expected)
        self.manual_duel(host,spectator,players,duel_mode);self.tournament_game(host,spectator,players)
        after=host.evaluate('async url=>(await fetch(url)).json()',api)
        self.assertEqual({p['name']:Decimal(str(p['total_with_adjustment'])) for p in after['participants']},expected)
        self.assertEqual({g['game_key'] for g in after['games']},{'quiz','target'})
        host.wait_for_function('window.hubMonitorSocket?.readyState===WebSocket.OPEN');host.click('#endSessionBtn')
        host.wait_for_url('**/hub/session/EVENING25/leaderboard/**')
        self.assertEqual(host.locator('.ranking-row').count(),25)
        for row in host.locator('.ranking-row').all():
            name=row.locator('.player-name').inner_text()
            self.assertEqual(Decimal(row.locator('td').nth(2).inner_text().replace(',','.')),expected[name])
        self.assertFalse(self.errors,self.errors);self.mark('Abend einschließlich sichtbarer Endwertung bestanden')

    def test_25_simple_evening_target_simple_best_of_three_two_tournament_sets(self):
        self.play_evening('simple','none','simple','best_of_n')

    def test_25_ranking_evening_linear_weights_target_ranking_target_score_two_sets(self):
        self.play_evening('ranking','linear_cap','ranking','target_score')

    def test_25_quiz_entry_after_previously_completed_check_in(self):
        self.mode='entry'
        def roster():
            self.hub=HubSession.objects.create(code='EVENING25',creator=self.user,is_active=True,started_at=timezone.now(),check_in_status='completed',locked_participant_count=25)
            self.steps=[HubGameStep.objects.create(session=self.hub,order=i,game_key=key,room_code=game.room_code,title=game.title)
                for i,(key,game) in enumerate((('quiz',self.quiz),('target',self.target),('estimation',self.final),('estimation',self.tournament)))]
            self.parties=[HubParticipant.objects.create(session=self.hub,nickname=name,is_active=True,scoring_eligible=True,checked_in_at=timezone.now()) for name in self.names]
        self.db(roster)
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True)
        self.host_page=host
        players=[self.page(reverse('games_hub:lobby',args=[self.hub.code]),player=p) for p in self.parties]
        self.player_pages=players;self.lobby(players)
        self.quiz_game(host,players)
        self.assertFalse(self.errors,self.errors)

    def test_25_tournament_after_previously_completed_check_in(self):
        self.mode='tournament-entry'
        def roster():
            self.hub=HubSession.objects.create(code='EVENING25',creator=self.user,is_active=True,started_at=timezone.now(),check_in_status='completed',locked_participant_count=25)
            self.steps=[HubGameStep.objects.create(session=self.hub,order=i,game_key=key,room_code=game.room_code,title=game.title)
                for i,(key,game) in enumerate((('quiz',self.quiz),('target',self.target),('estimation',self.final),('estimation',self.tournament)))]
            self.parties=[HubParticipant.objects.create(session=self.hub,nickname=name,is_active=True,scoring_eligible=True,checked_in_at=timezone.now()) for name in self.names]
            self.quiz.status='completed';self.quiz.ended_at=timezone.now();self.quiz.save()
        self.db(roster)
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True);self.host_page=host
        players=[self.page(reverse('games_hub:lobby',args=[self.hub.code]),player=p) for p in self.parties]
        self.player_pages=players;self.lobby(players)
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]),theme='vhs')
        # No earlier manual encounter in this independently isolated entry test.
        self.tournament_game(host,spectator,players,expected_instances=24)
        self.assertFalse(self.errors,self.errors)

    def test_25_target_after_previously_completed_check_in(self):
        self.mode='target-entry'
        def roster():
            self.hub=HubSession.objects.create(code='EVENING25',creator=self.user,is_active=True,started_at=timezone.now(),check_in_status='completed',locked_participant_count=25)
            self.steps=[HubGameStep.objects.create(session=self.hub,order=i,game_key=key,room_code=game.room_code,title=game.title)
                for i,(key,game) in enumerate((('quiz',self.quiz),('target',self.target),('estimation',self.final),('estimation',self.tournament)))]
            self.parties=[HubParticipant.objects.create(session=self.hub,nickname=name,is_active=True,scoring_eligible=True,checked_in_at=timezone.now()) for name in self.names]
            self.quiz.status='completed';self.quiz.started_at=timezone.now();self.quiz.ended_at=timezone.now();self.quiz.save()
        self.db(roster)
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True);self.host_page=host
        players=[self.page(reverse('games_hub:lobby',args=[self.hub.code]),player=p) for p in self.parties]
        self.player_pages=players;self.lobby(players)
        spectator=self.page(reverse('games_hub:spectate_session',args=[self.hub.code]),theme='vhs')
        self.target_game(host,spectator,players,'simple')
        self.assertFalse(self.errors,self.errors)
