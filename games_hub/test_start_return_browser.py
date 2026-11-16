"""Real Hub/game transports, with network fault injection only at browser edges."""
import json
from playwright.sync_api import TimeoutError as BrowserTimeoutError

from django.urls import reverse

from QuizGame.models import Quiz, QuizQuestion
from where_is_this.test_intro import make_entry_material
from .models import HubGameStep, HubSession
from .test_session_repairs_browser import SessionBrowserBase


class HostStartAndManualReturnBrowserTests(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        self.db(lambda: make_entry_material(self))
        self.client.force_login(self.user)
        self.sent, self.received, self.documents = [], [], []

    def context(self, **options):
        context = super().context(**options)
        context.add_init_script("""(() => {
          // CDN Bootstrap is replaced by the project's local test shim. Give
          // its modals their normal visible/hidden lifecycle for real clicks.
          const instances = new WeakMap();
          class Modal {
            constructor(el) { this.el=typeof el==='string'?document.querySelector(el):el; }
            show() {this.el.style.display='block';this.el.classList.add('show');}
            hide() {this.el.style.display='none';this.el.classList.remove('show');this.el.dispatchEvent(new Event('hidden.bs.modal'));}
            static getOrCreateInstance(el) {if(!instances.has(el))instances.set(el,new Modal(el));return instances.get(el);}
            static getInstance(el) {return instances.get(el);}
          }
          window.bootstrap=window.bootstrap||{};
          window.bootstrap.Modal=Modal;
          document.addEventListener('DOMContentLoaded',()=>{
            document.querySelectorAll('.modal').forEach(el=>el.style.display='none');
            const style=document.createElement('style');
            style.textContent='.modal.show{position:fixed;inset:0;z-index:2000;overflow:auto;background:#0008}.modal.show .modal-dialog{background:white;color:black;margin:8vh auto;padding:2rem;max-width:700px}';
            document.head.append(style);
          },{once:true});
          const Native=window.WebSocket;
          window.__startIntents=[];
          window.__startAcks=[];
          window.__wherePending=false;
          window.WebSocket=class extends Native {
            constructor(...args) {
              super(...args);
              this.addEventListener('message',event=>{
                const data=JSON.parse(event.data);
                if(data.type==='host_start_confirmed')window.__startAcks.push(data.start_request_id);
                if(data.type==='where_state')window.__wherePending=!!data.pending_input;
                if(['lobby_join_success','join_error','nickname_status'].includes(data.type))window.__joinResult={type:data.type,code:data.code,message:data.message};
              });
            }
            send(raw) {
              const data=JSON.parse(raw);
              if(data.type==='admin_start_quiz') {
                window.__startIntents.push(data.start_request_id);
                if(window.__dropStart)return;
                if(window.__holdStart){window.__heldStart={socket:this,raw};return;}
              }
              return super.send(raw);
            }
          };
        })();""")
        return context

    def attach(self, page):
        def socket(ws):
            ws.on('framesent', lambda raw: self.sent.append(json.loads(raw)))
            ws.on('framereceived', lambda raw: self.received.append(json.loads(raw)))
        page.on('websocket', socket)
        page.on('request', lambda req: self.documents.append(req.url) if req.resource_type=='document' else None)

    def prepare(self, kind):
        if kind == 'quiz':
            def create():
                self.hub.steps.all().delete()
                quiz=Quiz.objects.create(creator=self.user,title='Start test',status='waiting')
                question=QuizQuestion.objects.create(created_by=self.user,question_text='Was ist 2+2?',
                    question_type='multiple_choice',option_a='4',option_b='5',correct_answer='A')
                quiz.selected_questions.add(question)
                HubGameStep.objects.create(session=self.hub,game_key='quiz',room_code=quiz.room_code,order=0)
                return quiz
            game=self.db(create)
        else:
            game=self.games[0]
        lobby=reverse('games_hub:lobby',args=[self.hub.code])
        players=[self.page(lobby,player=p,theme='vhs') for p in self.parties]
        for page in players:
            page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True)
        self.attach(host)
        host.click(f'.launch-btn[data-room-code="{game.room_code}"]')
        host.wait_for_url('**/monitor/**')
        host.wait_for_selector('#startQuizBtn:not([disabled])')
        if kind=='quiz':
            host.wait_for_function('window.adminGameMonitor.websocket?.readyState===1')
        return game,host,players

    def wait_started(self, game, host, kind):
        if kind=='quiz':
            host.wait_for_selector('#endQuizBtn:not([disabled])')
        else:
            host.wait_for_selector('#sendQuestionBtn:not([disabled])')
            host.wait_for_function('window.__startAcks.includes(window.__startIntents.at(-1))')
        game.refresh_from_db()
        self.assertEqual(game.status,'active')
        self.assertIsNotNone(game.started_at)
        starts=[p for p in self.sent if p.get('type')=='admin_start_quiz']
        self.assertEqual(len(starts),1)
        ack='quiz_started' if kind=='quiz' else 'host_start_confirmed'
        self.assertTrue(any(p.get('type')==ack and p.get('start_request_id')==starts[0]['start_request_id'] for p in self.received))

    def confirmed_double_click(self, kind):
        game,host,_=self.prepare(kind)
        before=len(self.documents)
        host.evaluate("""() => {
          const real=window.fetch;
          window.fetch=async (...args)=>{if(String(args[0]).includes('/activate-game/'))await new Promise(r=>setTimeout(r,2500));return real(...args);};
          const b=document.getElementById('startQuizBtn');
          b.dispatchEvent(new MouseEvent('click',{bubbles:true}));
          b.dispatchEvent(new MouseEvent('click',{bubbles:true}));
        }""")
        host.wait_for_timeout(2100)
        self.assertTrue(host.locator('#startQuizBtn').is_disabled())
        self.assertFalse(any(p.get('type')=='admin_start_quiz' for p in self.sent))
        self.wait_started(game,host,kind)
        self.assertEqual(len(self.documents)-before,1 if kind=='quiz' else 0)
        self.assertFalse(self.errors)

    def rejected_then_retry(self, kind):
        game,host,_=self.prepare(kind)
        before=len(self.documents)
        # The live server runs in a separate process: hold the real WS request
        # after successful preflight, then make its session invalid on the DB.
        host.evaluate('window.__holdStart=true')
        host.click('#startQuizBtn')
        host.wait_for_function('!!window.__heldStart')
        self.db(lambda:HubSession.objects.filter(pk=self.hub.pk).update(is_active=False))
        host.evaluate('window.__holdStart=false;window.__heldStart.socket.send(window.__heldStart.raw)')
        host.wait_for_selector('#startQuizBtn:not([disabled])')
        error='#quizHostError' if kind=='quiz' else '#whereError'
        host.wait_for_function('(id)=>!!document.querySelector(id)?.textContent.trim()',arg=error)
        game.refresh_from_db()
        self.assertIsNone(game.started_at)
        self.assertEqual(len(self.documents),before)
        self.db(lambda:HubSession.objects.filter(pk=self.hub.pk).update(is_active=True))
        self.sent.clear()
        host.click('#startQuizBtn')
        self.wait_started(game,host,kind)
        self.assertFalse(self.errors)

    def timeout_then_retry(self, kind):
        game,host,_=self.prepare(kind)
        host.evaluate('window.__dropStart=true')
        before=len(self.documents)
        host.click('#startQuizBtn')
        host.wait_for_function('window.__startIntents.length===1')
        host.wait_for_selector('#startQuizBtn:not([disabled])',timeout=20000)
        error='#quizHostError' if kind=='quiz' else '#whereError'
        self.assertIn('Keine Startbestätigung',host.inner_text(error))
        game.refresh_from_db()
        self.assertIsNone(game.started_at)
        self.assertEqual(len(self.documents),before)
        host.evaluate('window.__dropStart=false')
        host.click('#startQuizBtn')
        self.wait_started(game,host,kind)
        self.assertFalse(self.errors)

    def test_quick_quiz_confirmed_double_click(self): self.confirmed_double_click('quiz')
    def test_where_confirmed_double_click(self): self.confirmed_double_click('where')
    def test_quick_quiz_rejected_start_can_retry(self): self.rejected_then_retry('quiz')
    def test_where_rejected_start_can_retry(self): self.rejected_then_retry('where')
    def test_quick_quiz_no_confirmation_can_retry(self): self.timeout_then_retry('quiz')
    def test_where_no_confirmation_can_retry(self): self.timeout_then_retry('where')

    def preflight_failure_then_retry(self, kind, hang=False):
        game,host,_=self.prepare(kind)
        before=len(self.documents)
        host.route('**/activate-game/',lambda route:None if hang else route.abort())
        host.click('#startQuizBtn')
        host.wait_for_selector('#startQuizBtn:not([disabled])',timeout=20000)
        self.assertFalse(any(p.get('type')=='admin_start_quiz' for p in self.sent))
        self.assertEqual(len(self.documents),before)
        game.refresh_from_db();self.assertIsNone(game.started_at)
        host.unroute('**/activate-game/')
        host.click('#startQuizBtn')
        self.wait_started(game,host,kind)
        self.assertFalse(self.errors)

    def test_quick_quiz_failed_http_preflight_can_retry(self): self.preflight_failure_then_retry('quiz')
    def test_where_missing_http_preflight_response_can_retry(self): self.preflight_failure_then_retry('where',hang=True)

    def test_where_manual_return_is_only_enabled_after_end_and_next_game_is_playable(self):
        game,host,players=self.prepare('where')
        host.click('#startQuizBtn');self.wait_started(game,host,'where')
        for page in players:
            page.wait_for_url('**/where/play/**')
            page.wait_for_function("!document.documentElement.classList.contains('is-session-game-intro-active') && !document.documentElement.classList.contains('qa-vhs-transition--active')",timeout=20000)
            self.assertTrue(page.locator('#returnToLobbyBtn').is_disabled())
        host.click('#sendQuestionBtn')
        for page in players:page.wait_for_selector('#whereQuestion:not([hidden])')
        host.click('#endQuestionBtn')
        for page in players:
            page.wait_for_function("document.getElementById('whereMessage').textContent==='Gemeinsame Auflösung'")
            self.assertTrue(page.locator('#returnToLobbyBtn').is_disabled())
            page.reload()
            page.wait_for_selector('#whereQuestion:not([hidden])')
            self.assertTrue(page.locator('#returnToLobbyBtn').is_disabled())
        host.click('#endQuizBtn')
        for page in players:
            page.wait_for_selector('#returnToLobbyBtn:not([disabled])')
            page.reload();page.wait_for_selector('#returnToLobbyBtn:not([disabled])')
            page.wait_for_timeout(700)
            self.assertIn('/where/play/',page.url)
        players[0].close()
        players[0]=self.page(reverse('where_is_this:result',args=[game.room_code,'Alpha'])+'?hub_session='+self.hub.code,
            player=self.parties[0],theme='vhs')
        players[0].wait_for_selector('#returnToLobbyBtn:not([disabled])')
        for page,party in zip(players,self.parties):
            self.assertTrue(self.db(lambda:game.participants.get(name=party.nickname).is_active))
            page.click('#returnToLobbyBtn')
            page.wait_for_url('**/hub/lobby/**')
            self.assertIn(self.hub.code,page.url)
            try:
                page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
            except BrowserTimeoutError:
                self.fail(json.dumps({'url':page.url,'join':page.evaluate('window.__joinResult'),
                    'nickname':page.locator('#nicknameStatus').inner_text(),'errors':self.errors}))
            self.assertFalse(self.db(lambda:game.participants.get(name=party.nickname).is_active))
        second=self.games[1]
        host.goto(self.live_server_url+reverse('games_hub:monitor',args=[self.hub.code]))
        host.click(f'.launch-btn[data-room-code="{second.room_code}"]')
        host.wait_for_selector('#startQuizBtn:not([disabled])');host.click('#startQuizBtn')
        host.wait_for_selector('#sendQuestionBtn:not([disabled])')
        for page in players:
            page.wait_for_url(f'**/where/play/{second.room_code}/**')
            page.wait_for_function("!document.documentElement.classList.contains('is-session-game-intro-active') && !document.documentElement.classList.contains('qa-vhs-transition--active')",timeout=20000)
        host.click('#sendQuestionBtn')
        page=players[0];page.wait_for_selector('#whereMap[data-loaded="true"]')
        box=page.locator('#whereMap').bounding_box()
        page.mouse.click(box['x']+box['width']*.55,box['y']+box['height']*.5)
        page.wait_for_function('window.__wherePending')
        page.wait_for_selector('#submitAnswerBtn:not([disabled])');page.click('#submitAnswerBtn')
        page.wait_for_function("document.getElementById('whereMessage').textContent.includes('eingeloggt')")
        self.assertEqual(self.db(lambda:second.where_answers.count()),1)
        self.assertFalse(self.errors)

    def test_where_leave_overview_pause_resume_and_end_follow_confirmed_state(self):
        game,host,_=self.prepare('where')
        host.click('#startQuizBtn');self.wait_started(game,host,'where')
        host.click('#sendQuestionBtn');host.wait_for_selector('#endQuestionBtn:not([disabled])')
        question_id=self.db(lambda:type(game).objects.get(pk=game.pk).current_question_id)
        for action,status in [('Overview','active'),('Inactive','inactive'),('End','completed')]:
            host.click('[data-host-game-leave]')
            host.wait_for_selector('#activeGameLeaveModal.show')
            host.click('#activeGameLeave'+action+'Btn')
            host.wait_for_url('**/hub/monitor/**')
            game.refresh_from_db();self.assertEqual(game.status,status)
            if action=='End':break
            host.click(f'.launch-btn[data-room-code="{game.room_code}"]')
            host.wait_for_url('**/where/monitor/**')
            if action=='Inactive':
                host.wait_for_selector('#startQuizBtn:not([disabled])');host.click('#startQuizBtn')
                host.wait_for_selector('#endQuestionBtn:not([disabled])')
            else:host.wait_for_selector('#endQuestionBtn:not([disabled])')
            game.refresh_from_db();self.assertEqual(game.current_question_id,question_id)
        self.assertFalse(self.errors)
