import json,os,time,uuid
from datetime import timedelta
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from asgiref.sync import async_to_sync
from channels.testing import ChannelsLiveServerTestCase
from django.conf import settings
from django.db import connections
from django.urls import reverse
from django.utils import timezone
from games_hub.playwright_e2e import start_chromium_browser,install_browser_test_stubs
from games_hub.authoritative_state import reset_question_flow,current_snapshot
from games_hub.models import HubParticipant
from Estimation import models as m
from . import test_repairs as fixtures


def report(*args,**kwargs):
 pass

class EstimationBrowserRepairTests(ChannelsLiveServerTestCase):
 action=fixtures.EstimationRepairTests.action
 def operate(self,fn):
  def work():
   try:return fn()
   finally:connections.close_all()
  with ThreadPoolExecutor(max_workers=1) as pool:return pool.submit(work).result()
 def start(self,**kwargs):return self.operate(lambda:fixtures.EstimationRepairTests.start(self,**kwargs))
 @classmethod
 def setUpClass(cls):
  super().setUpClass();cls.env=patch.dict(os.environ,{'DJANGO_ALLOW_ASYNC_UNSAFE':'1'});cls.env.start()
  cls.pw,cls.browser=start_chromium_browser(headless=True)
 @classmethod
 def tearDownClass(cls):
  cls.browser.close();cls.pw.stop();cls.env.stop();super().tearDownClass()
 def setUp(self):
  fixtures.EstimationRepairTests.setUp(self);self.contexts=[];self.errors=[]
  HubParticipant.objects.create(session=self.hub,nickname='Alice');HubParticipant.objects.create(session=self.hub,nickname='Bob')
 def tearDown(self):
  for context in self.contexts:context.close()
  time.sleep(.2)
 def page(self,label,theme='standard',host=False):
  context=self.browser.new_context();self.contexts.append(context);install_browser_test_stubs(context)
  context.add_init_script('localStorage.setItem("participant_interface_theme",'+json.dumps(theme)+');')
  if host:
   self.client.force_login(self.user);context.add_cookies([{'name':settings.SESSION_COOKIE_NAME,'value':self.client.cookies[settings.SESSION_COOKIE_NAME].value,'url':self.live_server_url}])
  page=context.new_page();page.on('pageerror',lambda error:self.errors.append(label+': '+str(error)));return page
 def player_url(self,name,hub=True):return self.live_server_url+reverse('estimation:play',args=[self.quiz.room_code,name])+('?hub_session='+self.hub.code if hub else '')
 def monitor_url(self):return self.live_server_url+reverse('admin_dashboard:estimation_monitor',args=[self.quiz.room_code])+'?hub_session='+self.hub.code
 def test_normal_flow_and_privacy(self):
  self.start(duration=90);host=self.page('host',host=True);alice=self.page('alice');bob=self.page('bob','vhs');frames=[]
  bob.on('websocket',lambda socket:socket.on('framereceived',lambda payload:frames.append(json.loads(payload))))
  host.goto(self.monitor_url());alice.goto(self.player_url('Alice'));bob.goto(self.player_url('Bob'))
  alice.wait_for_selector('#estimateInput:not([disabled])');bob.wait_for_selector('#estimateInput:not([disabled])')
  alice.fill('#estimateInput','100');alice.click('#submitAnswerBtn');alice.wait_for_timeout(600)
  leaked=[f for f in frames if f.get('type')=='participant_answered']
  self.assertEqual(leaked,[])
  bob.fill('#estimateInput','125');bob.wait_for_timeout(300);host.click('#endQuestionBtn')
  alice.wait_for_selector('#correctAnswerState:not(.d-none)',timeout=15000);bob.wait_for_selector('#correctAnswerState:not(.d-none)',timeout=15000)
  self.alice.refresh_from_db();self.bob.refresh_from_db()
  report('browser_normal',scores=[self.alice.total_score,self.bob.total_score],rows=m.EstimationAnswer.objects.count(),alice_answer_leaked_to_bob_before_end=leaked,overview_after_end=host.locator('#questionSelection').count(),errors=self.errors)
  self.assertEqual([self.alice.total_score,self.bob.total_score],[5,3])
  self.assertFalse(self.errors)
 def test_special_names(self):
  self.start(duration=90);facts=[]
  for name,theme in [('Team <West','standard'),('Trailing '+chr(92),'vhs')]:
   m.EstimationParticipant.objects.create(quiz=self.quiz,name=name,hub_session_code=self.hub.code);HubParticipant.objects.create(session=self.hub,nickname=name)
   page=self.page(name,theme);before=len(self.errors);frames=[];sent=[]
   def observe(socket):
    socket.on('framereceived',lambda payload:frames.append(json.loads(payload)));socket.on('framesent',lambda payload:sent.append(json.loads(payload)))
   page.on('websocket',observe);page.goto(self.player_url(name));page.wait_for_timeout(300)
   if len(self.errors)==before:
    page.fill('#estimateInput','100');page.click('#submitAnswerBtn');page.wait_for_timeout(700)
   facts.append({'name':name,'errors':self.errors[before:],'saved':m.EstimationAnswer.objects.filter(participant__name=name).count(),'sent_names':[f.get('participant_name') for f in sent if f.get('type')=='participant_submit_answer'],'rejections':[f.get('code') for f in frames if f.get('type')=='action_rejected'],'submitted_hint_visible':page.locator('#zoneSubmitFeedback:not(.d-none)').count()})
   page.close()
  for item in facts:
   self.assertFalse(item['errors']);self.assertEqual(item['saved'],1);self.assertEqual(item['sent_names'],[item['name']]);self.assertTrue(all(code == 'stale_action' for code in item['rejections']))
  report('browser_names',cases=facts)
 def test_expired_monitor_timer(self):
  self.start(age=60,duration=30);host=self.page('expired-host',host=True);host.goto(self.monitor_url())
  host.wait_for_selector('#questionSelection');self.quiz.refresh_from_db()
  self.assertIsNone(self.quiz.current_question_id)
  self.assertEqual(host.locator('#questionTimeLeft').count(),0)
  self.assertFalse(self.errors)

 def test_score_modal(self):
  self.start(duration=90);host=self.page('modal-host',host=True);host.goto(self.monitor_url());host.locator('.edit-score-btn').first.click();host.wait_for_timeout(400)
  data=host.evaluate("""() => {const m=document.getElementById('editScoreModal'), i=document.getElementById('editScoreInput'), r=i.getBoundingClientRect(), hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return {visible:m.classList.contains('show'),in_main:!!m.closest('main'),clickable:m.contains(hit),top:hit?.className,bootstrap:window.bootstrap?.Modal?.VERSION};}""")
  self.assertTrue(data['visible']);self.assertTrue(data['clickable']);self.assertFalse(data['in_main']);self.assertFalse(self.errors)
 def test_pause_player_controls(self):
  self.start(duration=90);host=self.page('pause-host',host=True);alice=self.page('pause-player','vhs');frames=[];alice.on('websocket',lambda socket:socket.on('framereceived',lambda payload:frames.append(json.loads(payload))));host.on('dialog',lambda d:d.accept());host.goto(self.monitor_url());alice.goto(self.player_url('Alice'));alice.wait_for_selector('#estimateInput:not([disabled])')
  alice.fill('#estimateInput','100');host.click('#setInactiveBtn');host.wait_for_selector('#startQuizBtn');alice.wait_for_timeout(300)
  try:alice.wait_for_function('() => document.getElementById("estimateInput").disabled',timeout=10000)
  except Exception:self.fail(json.dumps({'frames':frames,'errors':self.errors},default=str))
  self.assertEqual(alice.input_value('#estimateInput'),'100')
  host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
  host.click('#startQuizBtn');alice.wait_for_selector('#estimateInput:not([disabled])')
  self.assertEqual(alice.input_value('#estimateInput'),'100')
  alice.click('#submitAnswerBtn');alice.wait_for_selector('#zoneSubmitFeedback:not(.d-none)')
  host.wait_for_selector('#endQuestionBtn');host.click('#endQuestionBtn')
  alice.wait_for_selector('#correctAnswerState:not(.d-none)');self.alice.refresh_from_db()
  self.assertEqual(self.alice.total_score,5);self.assertFalse(self.errors)

 def test_standalone(self):
  self.quiz=m.EstimationQuiz.objects.create(creator=self.user,status='active',started_at=timezone.now(),scoring_mode='zones');self.quiz.selected_questions.add(self.question);m.EstimationSession.objects.create(quiz=self.quiz);self.consumer.room_code=self.quiz.room_code
  m.EstimationParticipant.objects.create(quiz=self.quiz,name='Solo')
  def open_game():
   reset_question_flow(game_key='estimation',room_code=self.quiz.room_code,session_code=None,mode='manual_three_phase')
   s=current_snapshot('estimation',self.quiz.room_code,None);action={'question_id':self.question.pk,'game_id':str(self.quiz.pk),'state_revision':s['state_revision'],'client_action_id':str(uuid.uuid4())}
   r=async_to_sync(self.consumer.present_estimation_question)(self.quiz.pk,self.question.pk,None,action,90,timezone.now()-timedelta(seconds=20));self.assertTrue(r.accepted,r.message)
   action['state_revision']=r.snapshot['state_revision'];action['client_action_id']=str(uuid.uuid4())
   r=async_to_sync(self.consumer.open_estimation_answering)(self.quiz.pk,self.question.pk,None,action,timezone.now()-timedelta(seconds=2));self.assertTrue(r.accepted,r.message)
  self.operate(open_game);page=self.page('solo');sent=[];page.on('websocket',lambda socket:socket.on('framesent',lambda value:sent.append(json.loads(value))));page.goto(self.player_url('Solo',hub=False));page.wait_for_selector('#estimateInput:not([disabled])');page.fill('#estimateInput','100');page.click('#submitAnswerBtn');page.wait_for_timeout(700)
  self.assertEqual(m.EstimationAnswer.objects.filter(quiz=self.quiz).count(),1);self.assertFalse(self.errors)
  self.assertEqual([f.get('hub_session') for f in sent if f.get('type')=='participant_submit_answer'],[None])
  report('browser_standalone',sent_hub=[f.get('hub_session') for f in sent if f.get('type')=='participant_submit_answer'],saved=m.EstimationAnswer.objects.filter(quiz=self.quiz).count(),looks_submitted=page.locator('#zoneSubmitFeedback:not(.d-none)').count(),errors=self.errors)

 def test_full_host_rank_flow(self):
  self.quiz.scoring_mode='rank';self.quiz.save()
  host=self.page('full-host',host=True);alice=self.page('full-alice');bob=self.page('full-bob','vhs')
  host.goto(self.monitor_url());alice.goto(self.player_url('Alice'));bob.goto(self.player_url('Bob'))
  host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
  host.click('.send-question-btn[data-question-id="'+str(self.question.pk)+'"]')
  host.wait_for_selector('#sendPreparedQuestionBtn');prepared=alice.locator('#estimateInput').is_disabled()
  host.click('#sendPreparedQuestionBtn');host.wait_for_selector('#openAnsweringBtn:not([disabled])',timeout=15000)
  self.session.refresh_from_db();before=self.session.is_question_active
  host.click('#openAnsweringBtn');alice.wait_for_selector('#estimateInput:not([disabled])');bob.wait_for_selector('#estimateInput:not([disabled])')
  alice.fill('#estimateInput','100');alice.click('#submitAnswerBtn');bob.fill('#estimateInput','125');bob.click('#submitAnswerBtn');bob.wait_for_timeout(600)
  host.wait_for_selector('#endQuestionBtn');host.click('#endQuestionBtn')
  alice.wait_for_selector('#correctAnswerState:not(.d-none)');bob.wait_for_selector('#correctAnswerState:not(.d-none)')
  self.alice.refresh_from_db();self.bob.refresh_from_db()
  self.assertTrue(prepared);self.assertFalse(before);self.assertEqual([self.alice.total_score,self.bob.total_score],[2,1]);self.assertFalse(self.errors)
  report('browser_full_rank_flow',input_locked_when_prepared=prepared,deadline_active_before_release=before,scores=[self.alice.total_score,self.bob.total_score],errors=self.errors)
 def test_tutorial_with_already_connected_player(self):
  from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime
  self.quiz.tutorial_question=self.question;self.quiz.save()
  prepare_unit_tutorial_runtime('estimation',self.quiz.room_code,self.hub.code,True)
  host=self.page('tutorial-host',host=True);alice=self.page('tutorial-player','vhs');sent=[];received=[]
  def watch(socket):
   socket.on('framesent',lambda payload:sent.append(json.loads(payload)));socket.on('framereceived',lambda payload:received.append(json.loads(payload)))
  alice.on('websocket',watch)
  host.goto(self.monitor_url());alice.goto(self.player_url('Alice'))
  host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
  host.click('.send-question-btn[data-question-id="'+str(self.question.pk)+'"]')
  host.wait_for_selector('#sendPreparedQuestionBtn');host.click('#sendPreparedQuestionBtn')
  host.wait_for_selector('#openAnsweringBtn:not([disabled])',timeout=15000);host.click('#openAnsweringBtn')
  alice.wait_for_selector('#estimateInput:not([disabled])');alice.fill('#estimateInput','100');alice.click('#submitAnswerBtn');alice.wait_for_timeout(700)
  self.assertEqual(m.EstimationAnswer.objects.get().points_earned,0);self.assertFalse(self.errors)
  report('browser_tutorial_player',submitted_question_ids=[f.get('question_id') for f in sent if f.get('type')=='participant_submit_answer'],saved=m.EstimationAnswer.objects.count(),rejections=[f.get('code') for f in received if f.get('type')=='action_rejected'],looks_submitted=alice.locator('#zoneSubmitFeedback:not(.d-none)').count(),errors=self.errors)

 def test_repeat_resets_input_and_counts_own_round_on_vhs(self):
  self.start(duration=90)
  host=self.page('repeat-host',host=True);alice=self.page('repeat-player','vhs')
  host.goto(self.monitor_url());alice.goto(self.player_url('Alice'))
  alice.wait_for_selector('#estimateInput:not([disabled])');alice.fill('#estimateInput','100');alice.click('#submitAnswerBtn')
  alice.wait_for_selector('#zoneSubmitFeedback:not(.d-none)');host.click('#endQuestionBtn')
  host.wait_for_selector('#questionSelection');host.wait_for_function('() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN')
  host.click(f'.send-question-btn[data-question-id="{self.question.pk}"]')
  host.wait_for_selector('#sendPreparedQuestionBtn');host.click('#sendPreparedQuestionBtn')
  host.wait_for_selector('#openAnsweringBtn:not([disabled])')
  alice.wait_for_function('() => document.getElementById("vhsEstimationProgress").textContent === "2/6"')
  host.click('#openAnsweringBtn');alice.wait_for_selector('#estimateInput:not([disabled])')
  self.assertEqual(alice.input_value('#estimateInput'),'')
  alice.fill('#estimateInput','100');alice.click('#submitAnswerBtn');alice.wait_for_selector('#zoneSubmitFeedback:not(.d-none)')
  host.wait_for_selector('#endQuestionBtn');host.click('#endQuestionBtn');alice.wait_for_selector('#correctAnswerState:not(.d-none)')
  self.alice.refresh_from_db();self.assertEqual(self.alice.total_score,10)
  self.assertEqual(m.EstimationAnswer.objects.filter(participant=self.alice).count(),2);self.assertFalse(self.errors)

 def test_private_draft_restores_after_reload_and_is_scored(self):
  self.start(duration=90);alice=self.page('draft-player');alice.goto(self.player_url('Alice'))
  alice.wait_for_selector('#estimateInput:not([disabled])');alice.fill('#estimateInput','100');alice.wait_for_timeout(400)
  alice.reload();alice.wait_for_function('() => document.getElementById("estimateInput").value === "100"')
  alice.evaluate('''() => {const i=document.getElementById('estimateInput');i.value='';i.dispatchEvent(new Event('input',{bubbles:true}));i.value='100';i.dispatchEvent(new Event('input',{bubbles:true}));}''')
  alice.wait_for_timeout(300)
  host=self.page('draft-host',host=True);host.goto(self.monitor_url());host.click('#endQuestionBtn')
  alice.wait_for_selector('#correctAnswerState:not(.d-none)');self.alice.refresh_from_db()
  self.assertEqual(self.alice.total_score,5);self.assertEqual(m.EstimationAnswer.objects.get().user_answer,100);self.assertFalse(self.errors)
