"""Quick Quiz duel acceptance at the real Hub/HTTP boundary and locked services."""
import json
import subprocess
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import Client, TransactionTestCase, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizQuestion, QuizAnswer, QuizParticipant
from games_hub.models import HubSession, HubGameStep, HubParticipant
from games_hub.lobby_join import issue_rejoin_token
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from .commands import ActorContext, ActionConflict
from .controllers.quiz import buzz, judge_quiz_buzz, evaluate_choices, reconcile_quiz_deadline, commit_quiz_answer
from .contracts import RoundOutcome
from .deadlines import reconcile_due_rounds
from .models import (
    DuelGameInstance, DuelQuizQuestionBinding, DuelQuizRoundState, DuelUnit,
    DuelScoreEntry, DuelUnitOutcome, DuelActionReceipt, DuelDecision, ReceiptActorType, ReceiptStatus,
)
from . import test_submissions_concurrency as concurrency
from .test_deadline_worker import worker_environment


def make_quiz_material(case):
    case.user = User.objects.create_superuser('quiz-duel-host', '', 'pw')
    case.quiz = Quiz.objects.create(creator=case.user, title='Quick Quiz Duell')
    case.short = QuizQuestion.objects.create(created_by=case.user, question_text='Welche Hauptstadt ist mit dieser sehr langen Frage gemeint?', question_type='short_answer', correct_answer='Berlin', time_limit=60)
    case.tf = QuizQuestion.objects.create(created_by=case.user, question_text='Erste Wahr/Falsch-Frage?', question_type='true_false', correct_answer='True', time_limit=60)
    case.tf2 = QuizQuestion.objects.create(created_by=case.user, question_text='Zweite Wahr/Falsch-Frage?', question_type='true_false', correct_answer='False', time_limit=60)
    case.mc = QuizQuestion.objects.create(created_by=case.user, question_text='Welche Antwort ist richtig?', question_type='multiple_choice', option_a='Richtig', option_b='Falsch', option_c='Auch falsch', option_d='Wieder falsch', correct_answer='A', time_limit=60)
    case.quiz.selected_questions.add(case.short, case.tf, case.tf2, case.mc)


class QuizFixture(TransactionTestCase):
    config = {'mode': 'best_of_n', 'best_of': 7}

    def setUp(self):
        make_quiz_material(self)
        self.host = Client(); self.host.force_login(self.user)
        self.hub = HubSession.objects.create(code='QUIZ-DUEL', creator=self.user, session_type='duel',
            is_active=True, check_in_status='completed', locked_participant_count=2)
        self.parties = [HubParticipant.objects.create(session=self.hub, nickname=name, scoring_eligible=True, checked_in_at=timezone.now()) for name in ('Alpha', 'Beta')]
        self.step = HubGameStep.objects.create(session=self.hub, order=0, game_key='quiz', room_code=self.quiz.room_code, duel_match_config=self.config)
        self.clients = [Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(p)) for p in self.parties]
        self.spectator = Client()
        response = self.start_slot()
        self.assertEqual(response.status_code, 200, response.content)
        self.instance = DuelGameInstance.objects.get(hub_game_step=self.step)
        # Only fixture setup shortens the intro; browser acceptance plays it.
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=timezone.now()-timedelta(seconds=1))
        self.unit = None

    def start_slot(self, client=None):
        return (client or self.host).post(reverse('games_hub:activate_session_game', args=[self.hub.code]),
            data=json.dumps({'step_id': self.step.pk, 'game_key': 'quiz', 'room_code': self.quiz.room_code}), content_type='application/json')

    def match_url(self, role='host'):
        return reverse('duel:quiz_match_api', args=[self.hub.code, self.instance.pk, role])

    def unit_url(self, role='host'):
        return reverse('duel:quiz_'+role, args=[self.hub.code, self.instance.pk, self.unit.pk])

    def post(self, data, client=None, role='host', key=None, match=False):
        return (client or self.host).post(self.match_url(role) if match else self.unit_url(role),
            data=json.dumps({**data, 'client_action_id': key or str(uuid4())}), content_type='application/json')

    def prepare(self, question=None):
        response = self.post({'action': 'prepare', 'question_id': (question or self.tf).pk,
                              'previous_unit_id': self.unit.pk if self.unit else None}, match=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.unit = self.instance.units.latest('number')
        return response

    def start(self, question=None):
        self.prepare(question)
        response = self.post({'action': 'start'})
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def answer(self, side=0, value='True', key=None):
        return self.post({'action': 'answer', 'payload': {'option_id': value}}, self.clients[side], 'participant', key=key)

    def do_buzz(self, side=0, key=None):
        return self.post({'action': 'buzz'}, self.clients[side], 'participant', key=key)

    def snapshot(self, role='spectator', client=None):
        return (client or self.spectator).get(self.match_url(role)).json()

    def reveal(self):
        for action in ('show_answers', 'show_solution', 'show_result'):
            response = self.post({'action': action})
            self.assertEqual(response.status_code, 200, response.content)
        return response

    def finish(self):
        response = self.post({'action': 'complete'})
        self.assertEqual(response.status_code, 200, response.content)

    def actor(self, side=0):
        return ActorContext(ReceiptActorType.PARTICIPANT, self.parties[side].pk)


class ChoiceRuleTests(SimpleTestCase):
    def test_independent_points_and_explicit_round_results(self):
        for a,b,expected in [('A','B',(1,0,RoundOutcome.PARTY_A_WINS)), ('B','A',(0,1,RoundOutcome.PARTY_B_WINS)),
            ('A','A',(1,1,RoundOutcome.DRAW)), ('B','B',(0,0,RoundOutcome.NO_WINNER)),
            ('A',None,(1,0,RoundOutcome.PARTY_A_WINS)), (None,'A',(0,1,RoundOutcome.PARTY_B_WINS)),
            (None,None,(0,0,RoundOutcome.NO_WINNER))]:
            with self.subTest(a=a,b=b):
                pa,pb,result = evaluate_choices('A',a,b)
                self.assertEqual((pa,pb,result.outcome),expected)


class QuickQuizDuelTests(QuizFixture):
    def test_hub_start_is_idempotent_and_preserves_multiplayer_material(self):
        source = Quiz.objects.values().get(pk=self.quiz.pk)
        response = self.start_slot()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['instance_id'],str(self.instance.pk))
        self.assertEqual(DuelGameInstance.objects.count(),1)
        self.assertFalse(self.instance.units.exists())
        self.assertEqual(Quiz.objects.values().get(pk=self.quiz.pk),source)

    def test_normal_session_with_two_people_still_starts_multiplayer(self):
        self.instance.action_receipts.all().delete(); self.instance.delete()
        self.step.game_format = 'standard'; self.step.intro_started_at = self.step.intro_ends_at = None
        self.step.save(update_fields=['game_format', 'intro_started_at', 'intro_ends_at'])
        self.hub.session_type='multiplayer'; self.hub.save()
        response=self.start_slot()
        self.assertEqual(response.status_code,200,response.content)
        self.assertNotIn('duel',response.json())
        self.quiz.refresh_from_db(); self.assertEqual(self.quiz.status,'active')
        self.assertEqual(DuelGameInstance.objects.count(),0)

    def test_planning_accepts_quiz_and_validates_match_configuration(self):
        data={'name':'Quiz plan','code':'QUIZ-PLAN','session_type':'duel','games_order':json.dumps([
            {'game_key':'quiz','room_code':self.quiz.room_code,'duel_match_config':{'mode':'best_of_n','best_of':7}}])}
        self.assertEqual(self.host.post(reverse('games_hub:create_session'),data).status_code,302)
        session=HubSession.objects.get(code='QUIZ-PLAN');step=session.steps.get()
        self.assertEqual(step.get_duel_match_config().required_wins,4)
        url=reverse('duel:session_api',args=[session.code])
        bad=self.host.post(url,data=json.dumps({'action':'save_plan','configs':{str(step.pk):{'mode':'best_of_n','best_of':2}}}),content_type='application/json')
        self.assertEqual(bad.status_code,400)
        step.refresh_from_db();self.assertEqual(step.duel_match_config,{'mode':'best_of_n','best_of':7})

    def test_prepared_state_hides_material_and_does_not_start_a_timer(self):
        self.prepare(self.short)
        state=DuelQuizRoundState.objects.get(unit=self.unit)
        self.assertEqual(state.phase,'prepared');self.assertIsNone(state.deadline)
        for role,client in [('participant',self.clients[0]),('spectator',self.spectator)]:
            public=self.snapshot(role,client)['unit']
            for secret in ('question','options','solution','host_solution','answers','points'):
                self.assertNotIn(secret,public)

    def test_snapshot_is_read_only_and_disallows_client_reveal_parameters(self):
        self.start()
        with CaptureQueriesContext(connection) as queries:self.snapshot()
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        response=self.spectator.get(self.match_url('spectator')+'?reveal=true')
        # Match orchestration must reject caller-selected projection parameters.
        self.assertEqual(response.status_code,400)

    def test_frozen_source_survives_edits_and_question_can_recur(self):
        self.start();original=self.unit.quiz_question_binding.question_text
        self.tf.question_text='Changed source';self.tf.correct_answer='False';self.tf.save()
        self.assertEqual(self.snapshot()['unit']['question'],original)
        self.answer(0,'True');self.answer(1,'False');self.reveal();self.finish()
        old=self.unit.pk;self.start(self.tf)
        self.assertNotEqual(self.unit.pk,old)
        self.assertEqual(self.snapshot()['unit']['question'],'Changed source')
        self.assertEqual(self.instance.units.count(),2)

    def test_two_independent_answers_reveal_in_order_and_book_once(self):
        self.start()
        key='first-answer'
        first=self.answer(0,'True',key);self.assertEqual(first.status_code,200,first.content)
        self.assertEqual(self.answer(1,'False').status_code,200)
        self.assertEqual(DuelQuizRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')
        before=self.instance.state_revision
        self.instance.refresh_from_db();before=self.instance.state_revision
        self.assertTrue(self.answer(0,'True',key).json()['action']['replayed'])
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,before)
        self.assertEqual(self.answer(0,'False',key).status_code,409)
        for forbidden in ('answers','solution','points'):self.assertNotIn(forbidden,self.snapshot()['unit'])
        self.assertEqual(self.post({'action':'show_solution'}).status_code,409)
        self.assertEqual(self.post({'action':'show_answers'}).status_code,200)
        state=self.snapshot()['unit'];self.assertIn('answers',state);self.assertNotIn('solution',state)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertEqual(self.post({'action':'show_solution'}).status_code,200)
        self.assertIn('solution',self.snapshot()['unit']);self.assertNotIn('points',self.snapshot()['unit'])
        key='result';self.assertEqual(self.post({'action':'show_result'},key=key).status_code,200)
        state=self.snapshot()['unit'];self.assertEqual(state['points'],{'a':1,'b':0})
        self.assertEqual(state['round_outcome'],'party_a_wins')
        self.assertTrue(self.post({'action':'show_result'},key=key).json()['action']['replayed'])
        self.assertEqual(DuelScoreEntry.objects.count(),2)
        self.assertEqual(DuelUnitOutcome.objects.count(),1)
        self.assertFalse(QuizAnswer.objects.exists());self.assertFalse(QuizParticipant.objects.exists())

    def test_both_correct_get_one_point_without_round_wins(self):
        self.start(self.mc);self.answer(0,'A');self.answer(1,'A');self.reveal()
        state=self.snapshot()['unit'];self.assertEqual(state['points'],{'a':1,'b':1})
        self.assertEqual(state['round_outcome'],'draw')
        self.assertEqual(state['match']['party_a']['round_wins'],0)
        self.assertEqual(state['match']['party_b']['round_wins'],0)

    def test_both_incorrect_get_zero_points_and_no_winner(self):
        self.start(self.tf);self.answer(0,'False');self.answer(1,'False');self.reveal()
        self.assertEqual(self.snapshot()['unit']['points'],{'a':0,'b':0})
        self.assertEqual(self.unit.outcome.outcome,'no_winner')

    def test_missing_answer_is_zero_after_deadline_and_no_get_reconciliation(self):
        self.start();self.answer(0,'True')
        deadline=DuelQuizRoundState.objects.get(unit=self.unit).deadline
        with patch('django.utils.timezone.now',return_value=deadline):
            self.assertEqual(self.answer(1,'True').status_code,409)
            self.snapshot()
            self.assertEqual(DuelQuizRoundState.objects.get(unit=self.unit).phase,'answering')
            self.assertEqual(reconcile_due_rounds(),1)
            self.assertEqual(reconcile_due_rounds(),0)
            self.reveal()
        self.assertEqual(self.snapshot()['unit']['points'],{'a':1,'b':0})

    def test_no_buzz_expires_without_points_or_winner(self):
        self.start(self.short);deadline=DuelQuizRoundState.objects.get(unit=self.unit).deadline
        with patch('django.utils.timezone.now',return_value=deadline):
            self.assertEqual(self.do_buzz().status_code,409)
            self.assertEqual(reconcile_due_rounds(),1)
            self.reveal()
        self.assertEqual(self.snapshot()['unit']['points'],{'a':0,'b':0})
        self.assertEqual(self.unit.outcome.outcome,'no_winner')

    def test_existing_worker_process_recovers_due_quiz_after_restart_without_clients(self):
        self.prepare(self.tf)
        started=timezone.now()-timedelta(seconds=70)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=started-timedelta(seconds=1))
        with patch('django.utils.timezone.now',return_value=started):
            self.assertEqual(self.post({'action':'start'}).status_code,200)
        with worker_environment() as (command,options):
            for expected in ('1','0'):
                result=subprocess.run([*command,'--once'],capture_output=True,text=True,timeout=35,**options)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout.strip(),expected)
        self.assertEqual(DuelQuizRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')
        self.assertNotIn('answers',self.snapshot()['unit']);self.assertFalse(DuelScoreEntry.objects.exists())

    def test_first_buzz_freezes_reveal_and_requires_explicit_host_verdict(self):
        self.start(self.short);state=DuelQuizRoundState.objects.get(unit=self.unit)
        at=state.started_at+timedelta(milliseconds=200)
        with patch('django.utils.timezone.now',return_value=at):
            first=self.do_buzz(1,'buzz')
        self.assertEqual(first.status_code,200,first.content)
        state.refresh_from_db();frozen=state.frozen_characters
        self.assertLess(frozen,len(self.short.question_text));self.assertEqual(state.buzz_participant_id,self.parties[1].pk)
        self.assertEqual(self.do_buzz(0).status_code,409)
        self.assertTrue(self.do_buzz(1,'buzz').json()['action']['replayed'])
        self.assertEqual(self.answer(0).status_code,409)
        public=self.snapshot()['unit'];self.assertNotIn('solution',public);self.assertNotIn('host_solution',public);self.assertNotIn('buzz_id',public)
        self.assertEqual(self.snapshot('host',self.host)['unit']['host_solution'],'Berlin')
        self.assertEqual(self.post({'action':'judge','buzz_id':str(uuid4()),'verdict':'correct'}).status_code,409)
        self.assertEqual(self.post({'action':'judge','buzz_id':str(state.buzz_id),'verdict':'correct'}).status_code,200)
        self.assertEqual(self.snapshot()['unit']['points'],{'a':0,'b':1})
        self.assertNotIn('answers',self.snapshot()['unit'])  # oral attempt, no invented device answers
        state.refresh_from_db();self.assertEqual(state.frozen_characters,frozen)
        self.assertEqual(DuelDecision.objects.get().subject_key,str(state.buzz_id))

    def test_incorrect_oral_answer_gives_opponent_one_point(self):
        self.start(self.short);self.do_buzz(0)
        state=DuelQuizRoundState.objects.get(unit=self.unit)
        self.assertEqual(self.post({'action':'judge','buzz_id':str(state.buzz_id),'verdict':'incorrect'}).status_code,200)
        self.assertEqual(self.snapshot()['unit']['points'],{'a':0,'b':1})

    def test_permissions_and_strict_payloads(self):
        self.start()
        self.assertEqual(self.post({'action':'show_answers'},self.clients[0],'participant').status_code,403)
        self.assertEqual(self.clients[0].post(self.unit_url('host'),data=json.dumps({'action':'show_answers','client_action_id':'host-spoof'}),content_type='application/json').status_code,403)
        third=HubParticipant.objects.create(session=self.hub,nickname='Third')
        stranger=Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(third))
        self.assertEqual(self.post({'action':'answer','payload':{'option_id':'True'}},stranger,'participant').status_code,403)
        for value in (None, {}, {'option_id':'evil'}, {'option_id':'True','participant_id':self.parties[1].pk}, {'option_id':True}):
            self.assertIn(self.post({'action':'answer','payload':value},self.clients[0],'participant').status_code,(400,409))
        self.assertEqual(self.spectator.post(self.unit_url('spectator'),data='{}',content_type='application/json').status_code,404)

    def test_foreign_match_and_cross_game_transport_are_rejected(self):
        other=HubSession.objects.create(code='OTHER',session_type='duel')
        url=reverse('duel:quiz_match_api',args=[other.code,self.instance.pk,'participant'])
        self.assertEqual(self.clients[0].get(url).status_code,404)
        url=reverse('duel:match_api',args=[self.hub.code,self.instance.pk,'host'])
        self.assertEqual(self.host.get(url).status_code,404)

    def test_wrong_quiz_binding_rolls_back_the_new_unit(self):
        foreign=QuizQuestion.objects.create(created_by=self.user,question_text='Foreign?',correct_answer='A')
        response=self.post({'action':'prepare','question_id':foreign.pk,'previous_unit_id':None},match=True)
        self.assertEqual(response.status_code,409)
        self.assertFalse(self.instance.units.exists())

    def test_invalid_true_false_material_is_not_silently_treated_as_false(self):
        self.tf.correct_answer='invalid';self.tf.save()
        response=self.post({'action':'prepare','question_id':self.tf.pk,'previous_unit_id':None},match=True)
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['action']['code'],'invalid_question')
        self.assertFalse(self.instance.units.exists())

    def test_quiz_transport_requires_explicit_duel_format_inside_multiplayer_sessions(self):
        self.hub.session_type='multiplayer';self.hub.save()
        self.assertEqual(self.clients[0].get(self.match_url('participant')).status_code,200)
        self.step.game_format='standard';self.step.save(update_fields=['game_format'])
        self.assertEqual(self.clients[0].get(self.match_url('participant')).status_code,404)

    def test_receipt_failure_rolls_back_submission_and_phase(self):
        self.start();self.answer(0,'True')
        before=DuelActionReceipt.objects.count()
        with patch.object(DuelActionReceipt,'save',side_effect=IntegrityError('receipt failed')):
            with self.assertLogs('duel.views',level='ERROR'):
                self.assertEqual(self.answer(1,'False').status_code,500)
        self.assertEqual(self.unit.submissions.filter(status='committed').count(),1)
        self.assertEqual(DuelQuizRoundState.objects.get(unit=self.unit).phase,'answering')
        self.assertEqual(DuelActionReceipt.objects.count(),before)

    def test_database_rejects_duplicate_round_state_and_invalid_phase(self):
        self.prepare()
        with self.assertRaises(IntegrityError),transaction.atomic():
            DuelQuizRoundState.objects.create(unit=self.unit)
        with self.assertRaises(IntegrityError),transaction.atomic():
            DuelQuizRoundState.objects.filter(unit=self.unit).update(phase='invented')

    def test_result_failure_rolls_back_decision_points_outcome_and_match(self):
        self.start(self.short);self.do_buzz()
        state=DuelQuizRoundState.objects.get(unit=self.unit)
        with patch('duel.controllers.quiz.complete_unit_in_action',side_effect=IntegrityError('outcome failed')):
            with self.assertLogs('duel.views',level='ERROR'):
                self.assertEqual(self.post({'action':'judge','buzz_id':str(state.buzz_id),'verdict':'correct'}).status_code,500)
        self.assertFalse(DuelDecision.objects.exists());self.assertFalse(DuelScoreEntry.objects.exists());self.assertFalse(DuelUnitOutcome.objects.exists())
        state.refresh_from_db();self.assertEqual(state.phase,'awaiting_decision')


class QuizTargetScoreTests(QuizFixture):
    config={'mode':'target_score','target_score':1}

    def test_decisive_result_closes_match_then_allows_only_presentation_ack(self):
        self.start();self.answer(0,'True');self.answer(1,'False');self.reveal()
        self.instance.refresh_from_db();self.assertEqual(self.instance.match_status,'completed')
        self.assertEqual(self.instance.match_outcome.winner_id,self.parties[0].pk)
        self.assertEqual(self.post({'action':'start'}).status_code,409)
        self.assertEqual(self.answer(0,'False').status_code,409)
        key='final-ack';self.assertEqual(self.post({'action':'complete'},key=key).status_code,200)
        self.assertTrue(self.post({'action':'complete'},key=key).json()['action']['replayed'])
        self.assertEqual(DuelQuizRoundState.objects.get(unit=self.unit).phase,'completed')
        self.assertEqual(DuelScoreEntry.objects.count(),2)

    def test_simultaneous_target_reached_has_no_invented_winner(self):
        self.start();self.answer(0,'True');self.answer(1,'True');self.reveal();self.finish()
        state=self.snapshot()['unit'];self.assertEqual(state['points'],{'a':1,'b':1})
        self.assertEqual(state['match']['resolution_reason'],'sudden_death')
        self.instance.refresh_from_db();self.assertEqual(self.instance.match_status,'in_progress')
        self.assertFalse(hasattr(self.instance,'match_outcome'))
        # The simultaneous match ball is followed by a fresh, independent unit.
        self.assertEqual(self.post({'action':'prepare','question_id':self.tf.pk,'previous_unit_id':self.unit.pk},match=True).status_code,200)
        self.assertEqual(self.instance.units.count(),2)


class QuizConcurrencyTests(QuizFixture):
    parallel=concurrency.SubmissionConcurrencyTests.parallel

    def test_concurrent_buzzes_accept_exactly_one_and_one_revision(self):
        self.start(self.short);self.instance.refresh_from_db();before=self.instance.state_revision
        results=self.parallel([lambda i=i:buzz(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=self.actor(i),client_action_id=f'buzz-{i}') for i in range(2)])
        self.assertEqual(sum(r.status==ReceiptStatus.APPLIED for r in results),1)
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,before+1)
        self.assertIsNotNone(DuelQuizRoundState.objects.get(unit=self.unit).buzz_id)

    def test_concurrent_independent_answers_close_once(self):
        self.start();self.instance.refresh_from_db();before=self.instance.state_revision
        results=self.parallel([lambda i=i:commit_quiz_answer(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=self.actor(i),client_action_id=f'answer-{i}',payload={'option_id':'True'}) for i in range(2)])
        self.assertTrue(all(r.status==ReceiptStatus.APPLIED for r in results))
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,before+2)
        self.assertEqual(DuelQuizRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')

    def test_answer_at_deadline_racing_worker_is_rejected_without_partial_commit(self):
        self.start();self.answer(0,'True');self.instance.refresh_from_db();before=self.instance.state_revision
        deadline=DuelQuizRoundState.objects.get(unit=self.unit).deadline
        with patch('django.utils.timezone.now',return_value=deadline):
            answer,count=self.parallel([lambda:commit_quiz_answer(instance_id=self.instance.pk,unit_id=self.unit.pk,
                actor=self.actor(1),client_action_id='deadline-answer',payload={'option_id':'True'}),reconcile_due_rounds])
        self.assertEqual(answer.status,ReceiptStatus.REJECTED);self.assertEqual(count,1)
        self.assertEqual(self.unit.submissions.count(),1)
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,before+1)
        self.assertEqual(DuelQuizRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')

    def test_concurrent_host_verdict_and_retry_book_one_result(self):
        self.start(self.short);self.do_buzz();state=DuelQuizRoundState.objects.get(unit=self.unit)
        args=dict(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=ActorContext(ReceiptActorType.HOST,self.user.pk),client_action_id='judge',buzz_id=str(state.buzz_id),verdict='correct')
        results=self.parallel([lambda:judge_quiz_buzz(**args)]*2)
        self.assertEqual(sorted(r.replayed for r in results),[False,True])
        self.assertEqual(DuelDecision.objects.count(),1);self.assertEqual(DuelScoreEntry.objects.count(),2)

    def test_concurrent_http_starts_keep_one_instance(self):
        clients=[Client(),Client()]
        for client in clients:client.force_login(self.user)
        results=self.parallel([lambda client=client:self.start_slot(client) for client in clients])
        self.assertTrue(all(r.status_code==200 for r in results))
        self.assertEqual(len({r.json()['instance_id'] for r in results}),1)
        self.assertEqual(DuelGameInstance.objects.count(),1)

    def test_first_parallel_start_of_new_slot_has_one_receipt_intro_and_instance(self):
        self.instance.match_status='aborted';self.instance.save()
        self.step=HubGameStep.objects.create(session=self.hub,order=1,game_key='quiz',room_code=self.quiz.room_code,duel_match_config=self.config)
        clients=[Client(),Client()]
        for client in clients:client.force_login(self.user)
        results=self.parallel([lambda client=client:self.start_slot(client) for client in clients])
        self.assertEqual([r.status_code for r in results],[200,200])
        self.assertEqual(len({r.json()['instance_id'] for r in results}),1)
        instance=DuelGameInstance.objects.get(hub_game_step=self.step)
        self.assertEqual(instance.state_revision,1)
        self.assertEqual(DuelActionReceipt.objects.filter(game_instance=instance).count(),1)
        self.step.refresh_from_db();self.assertEqual(self.step.intro_state_revision,1)

    def test_identical_concurrent_buzz_replays_original_frozen_right(self):
        self.start(self.short);self.instance.refresh_from_db();before=self.instance.state_revision
        args=dict(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=self.actor(),client_action_id='identical-buzz')
        results=self.parallel([lambda:buzz(**args)]*2)
        self.assertEqual(sorted(r.replayed for r in results),[False,True])
        self.assertEqual(results[0].data,results[1].data)
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,before+1)

    def test_conflicting_concurrent_host_verdicts_do_not_overwrite_first_decision(self):
        self.start(self.short);self.do_buzz();state=DuelQuizRoundState.objects.get(unit=self.unit)
        args=dict(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=ActorContext(ReceiptActorType.HOST,self.user.pk),client_action_id='conflicting-judge',buzz_id=str(state.buzz_id))
        results=self.parallel([lambda verdict=verdict:judge_quiz_buzz(**args,verdict=verdict) for verdict in ('correct','incorrect')])
        self.assertEqual(sum(isinstance(r,ActionConflict) for r in results),1)
        self.assertEqual(DuelDecision.objects.count(),1);self.assertEqual(DuelUnitOutcome.objects.count(),1)
        self.assertEqual(DuelScoreEntry.objects.count(),2)


class QuizSupabaseTests(SimpleTestCase):
    def test_new_quiz_models_are_excluded_before_sync_and_restore_discovery(self):
        config=apps.get_app_config('duel')
        self.assertTrue({DuelQuizQuestionBinding,DuelQuizRoundState}.issubset(set(config.get_models())))
        for service in (sync_all_models_to_supabase,restore_all_models_from_supabase):
            with patch('games_website.services.apps.get_app_configs',return_value=[config]):
                with patch.object(config,'get_models',side_effect=AssertionError('must not discover duel models')):
                    self.assertEqual(service(stdout=StringIO(),stderr=StringIO()),(0,[]))
