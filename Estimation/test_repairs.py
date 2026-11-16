import json,uuid,math
from datetime import timedelta
from unittest.mock import AsyncMock,patch
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from Estimation import models as m
from Estimation.consumers import EstimationConsumer
from Estimation.views import _build_question_scoreboard
from games_hub.models import HubSession,HubGameStep,HubParticipant
from games_hub.authoritative_state import current_snapshot,reset_question_flow
from games_hub.spectator import _serialize_estimation
from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime,start_unit_tutorial_if_needed

class EstimationRepairTests(TransactionTestCase):
 def setUp(self):
  self.user=User.objects.create_user(username='audit',is_staff=True)
  self.hub=HubSession.objects.create(code='ESTAUDIT',is_active=True,started_at=timezone.now()-timedelta(minutes=1))
  self.quiz=m.EstimationQuiz.objects.create(creator=self.user,status='active',started_at=timezone.now(),scoring_mode='zones')
  self.questions=[m.EstimationQuestion.objects.create(created_by=self.user,question_text='Estimate this value '+str(i),correct_answer=100,zone_count=5,tolerance_percentage=10) for i in range(5)]
  self.question=self.questions[0];self.quiz.selected_questions.set(self.questions)
  self.quiz.question_order=[q.pk for q in self.questions];self.quiz.save()
  self.session=m.EstimationSession.objects.create(quiz=self.quiz)
  HubGameStep.objects.create(session=self.hub,game_key='estimation',room_code=self.quiz.room_code,order=0)
  self.alice=m.EstimationParticipant.objects.create(quiz=self.quiz,name='Alice',hub_session_code=self.hub.code)
  self.bob=m.EstimationParticipant.objects.create(quiz=self.quiz,name='Bob',hub_session_code=self.hub.code)
  self.consumer=EstimationConsumer();self.consumer.room_code=self.quiz.room_code;self.consumer.room_group_name='estimation_'+self.quiz.room_code
  self.consumer.scope={'user':self.user};self.consumer.send=AsyncMock();self.consumer.channel_layer=type('Layer',(),{'group_send':AsyncMock()})()
  reset_question_flow(game_key='estimation',room_code=self.quiz.room_code,session_code=self.hub.code,mode='manual_three_phase')
 def action(self,q=None):
  s=current_snapshot('estimation',self.quiz.room_code,self.hub.code)
  return {'question_id':(q or self.question).pk,'game_id':str(self.quiz.pk),'state_revision':s['state_revision'],'client_action_id':str(uuid.uuid4()),'hub_session':self.hub.code}
 def start(self,q=None,age=3,duration=30):
  q=q or self.question
  r=async_to_sync(self.consumer.present_estimation_question)(self.quiz.pk,q.pk,self.hub.code,self.action(q),duration,timezone.now()-timedelta(seconds=max(20,age+10)))
  self.assertTrue(r.accepted,r.message)
  r=async_to_sync(self.consumer.open_estimation_answering)(self.quiz.pk,q.pk,self.hub.code,self.action(q),timezone.now()-timedelta(seconds=age))
  self.assertTrue(r.accepted,r.message);self.quiz.refresh_from_db();self.session.refresh_from_db()
 def end(self):
  async_to_sync(self.consumer.handle_admin_end_question)({'hub_session':self.hub.code})
  self.quiz.refresh_from_db();self.session.refresh_from_db()
  return self.consumer.channel_layer.group_send.call_args.args[1]
 def save(self,value=100,name='Alice'):
  return async_to_sync(self.consumer.save_participant_answer)(name,self.hub.code,value,0,self.quiz.current_question_id)
 def pending(self,value=100,name='Alice'):
  return async_to_sync(self.consumer.save_pending_answer)(name,self.hub.code,value,self.quiz.current_question_id)

 def test_repeated_question_has_independent_answers_points_and_total(self):
  self.start();self.assertIsNotNone(self.save());self.end();self.start();self.assertIsNotNone(self.save())
  self.alice.refresh_from_db();self.assertEqual(self.alice.total_score,10)
  self.assertEqual(list(m.EstimationAnswer.objects.order_by('round_number').values_list('round_number',flat=True)),[1,2])
  board,history,number=_build_question_scoreboard(self.quiz,self.alice,self.hub.code)
  self.assertEqual((number,len(board)),(2,6));self.assertEqual(history[0]['points'],5)

 def test_unanswered_rounds_keep_player_wire_and_spectator_in_sync(self):
  for q in [self.questions[2],self.questions[0]]:
   self.start(q);self.end()
  self.start(self.questions[4]);board,history,number=_build_question_scoreboard(self.quiz,self.alice,self.hub.code)
  self.assertEqual(number,3);self.assertEqual(len(history),2)
  state=async_to_sync(self.consumer.get_current_question_data)('Alice',self.hub.code)
  self.assertEqual(state['question_number'],3)
  self.assertEqual(_serialize_estimation(self.quiz,self.hub)['progress'],{'kind':'question','current':3,'total':5})

 def test_tutorial_draft_and_confirmed_answers_are_zero_with_consistent_counters(self):
  self.quiz.tutorial_question=self.question;self.quiz.save()
  prepare_unit_tutorial_runtime('estimation',self.quiz.room_code,self.hub.code,True)
  start_unit_tutorial_if_needed('estimation',self.quiz.room_code,self.hub.code)
  self.start();self.save();self.pending(100,'Bob');self.end()
  self.assertEqual(list(m.EstimationAnswer.objects.values_list('points_earned',flat=True)),[0,0])
  self.start(self.questions[1]);board,history,number=_build_question_scoreboard(self.quiz,self.alice,self.hub.code)
  self.assertEqual((number,len(board)),(1,4))
  state=async_to_sync(self.consumer.get_current_question_data)('Alice',self.hub.code)
  self.assertEqual(state['question_number'],1)
  self.assertFalse(state['is_tutorial_round'])
  self.assertEqual(_serialize_estimation(self.quiz,self.hub)['progress'],{'kind':'question','current':1,'total':4})

 def test_manual_correction_is_additive(self):
  self.start();self.save();self.client.force_login(self.user)
  r=self.client.post(reverse('admin_dashboard:set_estimation_participant_score'),json.dumps({'participant_id':self.alice.pk,'score':7}),content_type='application/json')
  self.assertEqual(r.status_code,200,r.content)
  self.end();self.start(self.questions[1]);self.save();self.alice.refresh_from_db()
  self.assertEqual(self.alice.total_score,12)

 def test_pause_freezes_state_and_shifts_draft_and_deadline_on_resume(self):
  from games_hub.quiz_pause import pause,resume
  self.start();self.pending();before=self.session.question_end_time;now=timezone.now()
  self.assertTrue(pause(self.quiz,'estimation',self.hub.code,at=now))
  self.assertFalse(current_snapshot('estimation',self.quiz.room_code,self.hub.code)['answering_allowed'])
  self.assertIsNone(self.save())
  self.assertTrue(resume(self.quiz,'estimation',self.hub.code,at=now+timedelta(seconds=60)))
  self.session.refresh_from_db();self.assertEqual(self.session.question_end_time,before+timedelta(seconds=60))
  state=current_snapshot('estimation',self.quiz.room_code,self.hub.code)
  self.assertEqual(state['question_phase'],'answering_open')
  with patch('Estimation.runtime.timezone.now',return_value=now+timedelta(seconds=61)):
   self.end()
  self.assertEqual(m.EstimationAnswer.objects.get().points_earned,5)

 def test_duplicate_start_preserves_current_round_and_deadline(self):
  self.start();deadline=self.session.question_end_time
  async_to_sync(self.consumer.handle_admin_start_quiz)({'hub_session':self.hub.code})
  self.quiz.refresh_from_db();self.session.refresh_from_db()
  self.assertEqual(self.quiz.current_question_id,self.question.pk)
  self.assertEqual(self.session.question_end_time,deadline)
  self.assertEqual(current_snapshot('estimation',self.quiz.room_code,self.hub.code)['question_phase'],'answering_open')

 def test_expired_host_reload_finalizes_draft_once_and_returns_to_overview(self):
  self.start(duration=2,age=1);self.pending()
  self.client.force_login(self.user)
  with patch('Estimation.runtime.timezone.now',return_value=timezone.now()+timedelta(seconds=4)):
   r=self.client.get(reverse('admin_dashboard:estimation_monitor',args=[self.quiz.room_code]),{'hub_session':self.hub.code})
  self.assertContains(r,'id="questionSelection"');self.quiz.refresh_from_db()
  self.assertIsNone(self.quiz.current_question_id)
  self.assertEqual(m.EstimationAnswer.objects.get().points_earned,5)

 def test_old_end_cannot_finish_another_question_or_repeat(self):
  self.start();self.end();self.start(self.questions[1])
  from .runtime import end_question
  self.assertIsNone(end_question(self.quiz.pk,self.hub.code,question_id=self.question.pk,round_number=1))
  self.quiz.refresh_from_db();self.assertEqual(self.quiz.current_question_id,self.questions[1].pk)
  self.end();self.start()
  self.assertIsNone(end_question(self.quiz.pk,self.hub.code,question_id=self.question.pk,round_number=1))

 def test_invalid_numbers_rejected_and_legacy_bad_draft_cannot_block_end(self):
  self.start()
  for value in ['NaN','Infinity','-Infinity','1e309',{},True]:
   self.assertIsNone(self.save(value));self.assertFalse(self.pending(value))
  self.session.pending_answers={str(self.alice.pk):{'question_id':self.question.pk,'user_answer':'Infinity','hub_session_code':self.hub.code,'updated_at':timezone.now().isoformat()}}
  self.session.save();self.end();self.assertFalse(m.EstimationAnswer.objects.exists())
  self.quiz.refresh_from_db();self.assertIsNone(self.quiz.current_question_id)

 def test_decimal_boundary_and_precise_zone_labels(self):
  self.question.correct_answer=.5
  self.assertEqual(self.question.calculate_score(.55),5)
  self.assertEqual(self.question.calculate_score(.6),4)
  self.assertEqual(self.question.get_zone_reveal_data()['zones'][0]['absolute_range_display'],'0.45-0.55')
  self.question.correct_answer=-.5
  self.assertEqual(self.question.calculate_score(-.55),5)

 def test_large_finite_answer_has_valid_json_payload(self):
  self.question.correct_answer=.5;self.question.save();self.start()
  result=self.save(1e308);self.assertIsNotNone(result)
  json.dumps(result,allow_nan=False)
  json.dumps(self.end(),allow_nan=False)

 def test_rank_ties_use_speed_and_all_round_participants(self):
  self.quiz.scoring_mode='rank';self.quiz.save();self.start()
  self.save(90);self.save(110,'Bob')
  # The point limit includes every participant in this round, even without an answer.
  m.EstimationParticipant.objects.create(quiz=self.quiz,name='Late',hub_session_code=self.hub.code)
  event=self.end()
  self.assertEqual([(r['participant_name'],r['points_earned']) for r in event['rank_results']],[('Alice',3),('Bob',2)])

 def test_direct_game_end_scores_confirmed_rank_answers_and_discards_drafts(self):
  self.quiz.scoring_mode='rank';self.quiz.save();self.start();self.save();self.pending(125,'Bob')
  async_to_sync(self.consumer.handle_admin_end_quiz)({})
  self.alice.refresh_from_db();self.session.refresh_from_db()
  self.assertEqual(self.alice.total_score,2);self.assertEqual(m.EstimationAnswer.objects.count(),1)
  self.assertFalse(self.session.is_question_active);self.assertIsNone(self.session.question_end_time)
  self.assertEqual(self.session.pending_answers,{})

 def test_confirmation_and_current_snapshot_do_not_disclose_evaluation(self):
  self.start()
  async_to_sync(self.consumer.handle_participant_submit_answer)({'participant_name':'Alice','hub_session':self.hub.code,'question_id':self.question.pk,'user_answer':100})
  confirmation=json.loads(self.consumer.send.call_args.kwargs['text_data'])
  self.assertEqual(confirmation['type'],'answer_submitted')
  for field in ['points_earned','accuracy_percentage','percentage_difference']:
   self.assertNotIn(field,confirmation)
  state=async_to_sync(self.consumer.get_current_question_data)('Alice',self.hub.code)
  self.assertNotIn('points_earned',state['existing_answer'])
  self.consumer.scope={};self.consumer.send.reset_mock()
  async_to_sync(self.consumer.participant_answered)({'answer':{'user_answer':100}})
  self.consumer.send.assert_not_called()

 def test_rejected_answers_and_pending_updates_get_explicit_reply(self):
  self.start(age=40)
  data={'participant_name':'Alice','hub_session':self.hub.code,'question_id':self.question.pk,'user_answer':100}
  async_to_sync(self.consumer.handle_participant_submit_answer)(data)
  self.assertEqual(json.loads(self.consumer.send.call_args.kwargs['text_data'])['type'],'answer_rejected')
  async_to_sync(self.consumer.handle_participant_update_pending_answer)(data)
  self.assertEqual(json.loads(self.consumer.send.call_args.kwargs['text_data'])['type'],'pending_answer_rejected')

 def test_http_end_uses_same_draft_finalization_and_clears_phase(self):
  self.start();self.pending();self.client.force_login(self.user)
  r=self.client.post(reverse('admin_dashboard:end_estimation_question',args=[self.quiz.room_code]),json.dumps({'hub_session':self.hub.code,'question_id':self.question.pk,'round_number':1}),content_type='application/json')
  self.assertEqual(r.status_code,200,r.content)
  self.assertEqual(m.EstimationAnswer.objects.get().points_earned,5)
  self.assertIsNone(current_snapshot('estimation',self.quiz.room_code,self.hub.code)['question_phase'])

 def test_rank_and_public_lists_are_session_scoped(self):
  m.EstimationParticipant.objects.create(quiz=self.quiz,name='Alice',hub_session_code='OTHER',total_score=100)
  self.assertEqual(self.alice.get_rank(),1)
  for endpoint,key in [('api_participants','participants'),('api_leaderboard','leaderboard')]:
   r=self.client.get(reverse('estimation:'+endpoint,args=[self.quiz.room_code]),{'hub_session':self.hub.code})
   self.assertEqual(len(r.json()[key]),2)

 def test_host_stats_and_responses_are_scoped_to_session_and_round(self):
  self.start();self.save();self.end();self.start()
  outsider=m.EstimationParticipant.objects.create(quiz=self.quiz,name='Other',hub_session_code='OTHER',total_score=100)
  m.EstimationAnswer.objects.create(quiz=self.quiz,participant=outsider,question=self.question,user_answer=100)
  self.client.force_login(self.user)
  r=self.client.get(reverse('admin_dashboard:api_estimation_quiz_stats',args=[self.quiz.room_code]),{'hub_session':self.hub.code})
  self.assertEqual(r.status_code,200,r.content)
  self.assertEqual(r.json()['stats']['participant_count'],2)
  self.assertEqual(r.json()['stats']['current_question_responses'],0)
  r=self.client.get(reverse('admin_dashboard:api_estimation_live_responses',args=[self.quiz.room_code]),{'hub_session':self.hub.code})
  self.assertEqual(r.json()['responses'],[])

 def test_legacy_round_history_retains_answers_without_hub_step(self):
  self.hub.steps.all().delete()
  reset_question_flow(game_key='estimation',room_code=self.quiz.room_code,session_code=self.hub.code,mode='manual_three_phase')
  m.EstimationAnswer.objects.create(quiz=self.quiz,participant=self.alice,question=self.question,user_answer=100)
  self.session.current_question_number=1;self.session.total_questions_sent=1;self.session.save()
  self.start(self.questions[1])
  self.session.refresh_from_db()
  self.assertEqual(self.session.question_rounds,[self.question.pk,self.questions[1].pk])
  board,history,number=_build_question_scoreboard(self.quiz,self.alice,self.hub.code)
  self.assertEqual(number,2);self.assertEqual(history[0]['points'],5)

 def test_rank_reveal_restores_ranking_after_reload(self):
  self.quiz.scoring_mode='rank';self.quiz.save();self.start();self.save();self.save(125,'Bob');self.end()
  r=self.client.get(reverse('estimation:play',args=[self.quiz.room_code,'Alice']),{'hub_session':self.hub.code})
  state=r.context['estimation_initial_state']
  self.assertEqual(state['phase'],'reveal')
  self.assertEqual([(row['participant_name'],row['points_earned']) for row in state['rank_results']],[('Alice',2),('Bob',1)])

 def test_unanswered_rank_round_remembers_all_participants_as_maximum(self):
  self.quiz.scoring_mode='rank';self.quiz.save();self.start()
  m.EstimationParticipant.objects.create(quiz=self.quiz,name='Late',hub_session_code=self.hub.code)
  event=self.end();self.assertEqual(event['max_points'],3)
  board,history,number=_build_question_scoreboard(self.quiz,self.alice,self.hub.code)
  self.assertEqual(history[0]['max_points'],3)
