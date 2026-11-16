import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from Estimation.models import EstimationQuiz, EstimationQuestion
from games_hub.models import HubSession, HubGameStep, HubParticipant
from .models import DuelGameInstance, DuelUnit, DuelActionReceipt, DuelEstimationRoundState
from games_hub.lobby_join import issue_rejoin_token
from . import test_submissions_concurrency as concurrency


def make_material(case):
    case.user = User.objects.create_superuser('duel-browser-host', '', 'pw')
    case.quiz = EstimationQuiz.objects.create(creator=case.user, title='Estimation Duell')
    case.question = EstimationQuestion.objects.create(created_by=case.user, question_text='Wie hoch ist der Turm?', correct_answer=100, unit='meters')
    case.quiz.selected_questions.add(case.question)


class HubDuelStartTests(TransactionTestCase):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def setUp(self):
        make_material(self)
        self.client.force_login(self.user)
        self.hub = HubSession.objects.create(code='HUB-DUEL', creator=self.user, session_type='duel',
            is_active=True, check_in_status='completed', locked_participant_count=2)
        self.parties = [HubParticipant.objects.create(session=self.hub, nickname=name,
            scoring_eligible=True, checked_in_at=timezone.now()) for name in ('Party A', 'Party B')]
        self.step = HubGameStep.objects.create(session=self.hub, order=0, game_key='estimation', room_code=self.quiz.room_code,
            duel_match_config={'mode':'best_of_n','best_of':7})

    def start(self, client=None):
        return (client or self.client).post(reverse('games_hub:activate_session_game', args=[self.hub.code]),
            data=json.dumps({'step_id':self.step.pk,'game_key':'estimation','room_code':self.quiz.room_code}), content_type='application/json')

    def test_regular_hub_start_and_retry_create_one_atomic_match_without_round_or_source_mutation(self):
        source = EstimationQuiz.objects.values().get(pk=self.quiz.pk)
        first, retry = self.start(), self.start()
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json(), retry.json())
        instance = DuelGameInstance.objects.get()
        self.assertEqual((instance.hub_session_id,instance.hub_game_step_id),(self.hub.pk,self.step.pk))
        self.assertEqual((instance.participant_a_id,instance.participant_b_id),tuple(p.pk for p in self.parties))
        self.assertEqual(instance.match_status,'in_progress')
        self.assertEqual(instance.rule_config,{'match':{'mode':'best_of_n','best_of':7}})
        self.assertEqual(instance.state_revision,1)
        self.assertEqual(DuelActionReceipt.objects.count(),1)
        self.assertFalse(DuelUnit.objects.exists())
        self.assertFalse(DuelEstimationRoundState.objects.exists())
        self.assertEqual(EstimationQuiz.objects.values().get(pk=self.quiz.pk),source)
        self.step.refresh_from_db()
        self.assertEqual(self.step.game_format,'duel')
        self.assertIsNotNone(self.step.intro_started_at)

    def test_parallel_real_hub_http_starts_serialize_to_same_match(self):
        clients = [Client(),Client()]
        for client in clients:client.force_login(self.user)
        results = self.parallel([lambda:self.start(clients[0]),lambda:self.start(clients[1])])
        self.assertEqual([r.status_code for r in results],[200,200])
        self.assertEqual(results[0].json()['instance_id'],results[1].json()['instance_id'])
        self.assertEqual(DuelGameInstance.objects.count(),1)
        self.assertEqual(DuelActionReceipt.objects.count(),1)

    def test_invalid_parties_or_config_keep_slot_retryable(self):
        self.parties[-1].delete()
        result = self.start()
        self.assertEqual(result.status_code,409)
        self.assertIn('zwei',result.json()['error'])
        self.assertFalse(DuelGameInstance.objects.exists())
        HubParticipant.objects.create(session=self.hub,nickname='Replacement',scoring_eligible=True,checked_in_at=timezone.now())
        self.step.duel_match_config={};self.step.save()
        self.assertEqual(self.start().status_code,409)
        self.assertFalse(DuelGameInstance.objects.exists())
        self.step.duel_match_config={'mode':'target_score','target_score':10};self.step.save()
        self.assertEqual(self.start().status_code,200)

    def test_receipt_failure_rolls_back_creation_intro_and_hub_assignment(self):
        with patch.object(DuelActionReceipt,'save',side_effect=IntegrityError('failed receipt')):
            with self.assertLogs('games_hub.views',level='ERROR'):
                self.assertEqual(self.start().status_code,503)
        self.assertFalse(DuelGameInstance.objects.exists())
        self.step.refresh_from_db()
        self.assertIsNone(self.step.intro_started_at)
        self.assertEqual(self.step.game_format,'standard')
        self.assertEqual(self.start().status_code,200)

    def test_normal_multiplayer_session_with_two_parties_keeps_old_start(self):
        self.hub.session_type='multiplayer';self.hub.save()
        response=self.start()
        self.assertEqual(response.status_code,200,response.content)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status,'active')
        self.assertFalse(DuelGameInstance.objects.exists())

    def test_creation_and_plan_edit_reuse_contract_and_do_not_reset_material(self):
        self.quiz.status='active';self.quiz.save()
        response=self.client.post(reverse('games_hub:create_session'),{'name':'Planned duel','code':'NEW-DUEL','session_type':'duel',
            'games_order':json.dumps([{'game_key':'estimation','room_code':self.quiz.room_code,'duel_match_config':{'mode':'best_of_n','best_of':7}}])})
        self.assertEqual(response.status_code,302,response.content)
        new=HubSession.objects.get(code='NEW-DUEL');step=new.steps.get()
        self.assertEqual((new.session_type,step.game_format),('duel','duel'))
        self.quiz.refresh_from_db();self.assertEqual(self.quiz.status,'active')
        api=reverse('duel:session_api',args=[new.code])
        result=self.client.post(api,data=json.dumps({'action':'save_plan','configs':{str(step.pk):{'mode':'target_score','target_score':3}}}),content_type='application/json')
        self.assertEqual(result.status_code,200,result.content)
        self.assertEqual(self.client.get(api).json()['steps'][0]['config'],{'mode':'target_score','target_score':3})
        bad=self.client.post(api,data=json.dumps({'action':'save_plan','configs':{str(step.pk):{'mode':'best_of_n','best_of':2}}}),content_type='application/json')
        self.assertEqual(bad.status_code,400)
        step.refresh_from_db();self.assertEqual(step.duel_match_config,{'mode':'target_score','target_score':3})

    def test_preparation_uses_binding_and_waits_for_intro_and_explicit_timer_start(self):
        self.start();instance=DuelGameInstance.objects.get()
        api=reverse('duel:match_api',args=[self.hub.code,instance.pk,'host'])
        body={'action':'prepare','question_id':self.question.pk,'previous_unit_id':None,'client_action_id':'first-question'}
        first=self.client.post(api,data=json.dumps(body),content_type='application/json')
        self.assertEqual(first.status_code,200,first.content)
        self.assertEqual(self.client.post(api,data=json.dumps(body),content_type='application/json').status_code,200)
        self.assertEqual(instance.units.count(),1)
        unit=instance.units.get()
        self.assertEqual(unit.estimation_round_state.phase,'prepared')
        self.assertIsNone(unit.estimation_round_state.deadline)
        player=Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(self.parties[0]))
        public=player.get(reverse('duel:match_api',args=[self.hub.code,instance.pk,'participant'])).json()
        self.assertNotIn('target',public['unit']);self.assertNotIn('question',public['unit'])
        unit_api=reverse('duel:estimation_host',args=[self.hub.code,instance.pk,unit.pk])
        early=self.client.post(unit_api,data=json.dumps({'action':'start','duration_seconds':30,'client_action_id':'early-start'}),content_type='application/json')
        self.assertEqual(early.status_code,409)
        self.assertEqual(early.json()['action']['code'],'intro_not_finished')
        self.assertIsNone(DuelEstimationRoundState.objects.get(unit=unit).deadline)
        bad_edit=self.client.post(reverse('duel:session_api',args=[self.hub.code]),data=json.dumps({
            'action':'save_plan','configs':{str(self.step.pk):{'mode':'target_score','target_score':1}}}),content_type='application/json')
        self.assertEqual(bad_edit.status_code,400)

    def test_prepare_binding_failure_leaves_no_half_round(self):
        self.start();instance=DuelGameInstance.objects.get()
        api=reverse('duel:match_api',args=[self.hub.code,instance.pk,'host'])
        with patch.object(DuelEstimationRoundState,'save',side_effect=IntegrityError('failed binding state')):
            with self.assertLogs('duel.views',level='ERROR'):
                response=self.client.post(api,data=json.dumps({'action':'prepare','question_id':self.question.pk,
                    'previous_unit_id':None,'client_action_id':'failing-prepare'}),content_type='application/json')
        self.assertEqual(response.status_code,500)
        self.assertFalse(instance.units.exists())
        instance.refresh_from_db();self.assertEqual(instance.state_revision,1)
