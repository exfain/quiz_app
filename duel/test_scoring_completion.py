"""Confirmed missing/tie/solved rules through real authenticated Duel transports."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from django.test import SimpleTestCase, TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.db import connection, transaction, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from . import test_tournaments as tournament
from . import test_inputs as inputs
from . import test_turns as turns
from . import test_control as controls
from . import test_match_service as matches
from . import test_encounters as encounters
from .commands import ActorContext, HandlerResult, execute_action, ActionConflict
from .contracts import RoundResult, RoundOutcome
from .controllers.estimation import evaluate_estimates
from .matches import MatchMode
from .match_service import complete_unit_in_action, get_match_snapshot
from .models import DuelUnit, DuelScoreEntry, DuelMatchOutcome, DuelActionReceipt, ReceiptStatus


class ExactEstimationRules(SimpleTestCase):
    def test_all_missing_and_equal_cases_have_explicit_final_results(self):
        for a,b,points,outcome in [(95,None,(1,0),'party_a_wins'), (None,95,(0,1),'party_b_wins'),
                (None,None,(0,0),'no_winner'), (90,110,(1,1),'draw')]:
            with self.subTest(a=a,b=b):
                result = evaluate_estimates(100,a,b)
                self.assertTrue(result.evaluable)
                self.assertEqual((result.points_a,result.points_b),points)
                self.assertEqual(result.round_result.outcome.value,outcome)


class TargetTiebreakTests(matches.MatchFixture):
    def setUp(self):
        super().setUp(); self.configure(MatchMode.TARGET_SCORE, 1)

    def batch(self, unit, a, b, outcome, key=None):
        def handler(context):
            for party,delta in ((self.party_a,a),(self.party_b,b)):
                DuelScoreEntry.objects.create(game_instance=context.instance, unit=context.unit,
                    participant=party,delta=delta,source_key=f'round:{unit.pk}')
            evaluation=complete_unit_in_action(context,RoundResult(outcome))
            return HandlerResult(ReceiptStatus.APPLIED, {'evaluation':evaluation.state.value})
        return execute_action(instance_id=self.instance.pk,unit_id=unit.pk,actor=self.host,
            client_action_id=key or str(uuid4()),action_type='test.scoring',payload={'a':a,'b':b,'outcome':outcome.value},handler=handler)

    def test_both_goals_preserve_points_and_reload_until_first_solo_round(self):
        first=self.batch(self.unit,1,1,RoundOutcome.DRAW,key='first')
        self.assertEqual(first.data['evaluation'],'sudden_death')
        self.instance.refresh_from_db();self.assertEqual(self.instance.tiebreak_unit_id,self.unit.pk)
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assertTrue(self.batch(self.unit,1,1,RoundOutcome.DRAW,key='first').replayed)
        self.batch(self.next_unit(),1,1,RoundOutcome.DRAW)
        result=self.batch(self.next_unit(),0,1,RoundOutcome.PARTY_B_WINS)
        self.assertEqual(result.data['evaluation'],'party_b_wins')
        snap=get_match_snapshot(self.instance.pk)
        self.assertEqual((snap['party_a']['score'],snap['party_b']['score']),(2,3))
        self.assertEqual(snap['winner_id'],self.party_b.pk)
        self.assertEqual(DuelMatchOutcome.objects.count(),1)

    def test_first_simultaneous_crossing_does_not_use_its_solo_round_as_tiebreak(self):
        self.batch(self.unit,1,2,RoundOutcome.PARTY_B_WINS)
        self.assertEqual(get_match_snapshot(self.instance.pk)['evaluation'],'sudden_death')
        self.batch(self.next_unit(),1,0,RoundOutcome.PARTY_A_WINS)
        self.assertEqual(get_match_snapshot(self.instance.pk)['winner_id'],self.party_a.pk)

    def test_transaction_failure_rolls_back_points_boundary_and_result(self):
        with patch('duel.models.DuelGameInstance.save',side_effect=IntegrityError('boundary failed')):
            with self.assertRaises(IntegrityError): self.batch(self.unit,1,1,RoundOutcome.DRAW)
        self.instance.refresh_from_db();self.assertIsNone(self.instance.tiebreak_unit_id)
        self.assertFalse(DuelScoreEntry.objects.exists());self.assertFalse(self.unit.__class__.objects.get(pk=self.unit.pk).status=='completed')
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_a_boundary_before_the_target_scores_is_rejected(self):
        from .models import DuelGameInstance
        from .match_service import MatchIntegrityError
        self.batch(self.unit,0,0,RoundOutcome.NO_WINNER)
        DuelGameInstance.objects.filter(pk=self.instance.pk).update(tiebreak_unit_id=self.unit.pk)
        with self.assertRaises(MatchIntegrityError):get_match_snapshot(self.instance.pk)

    def test_command_cannot_commit_an_unproven_boundary_or_replace_one(self):
        from .match_service import MatchIntegrityError
        self.batch(self.unit,0,0,RoundOutcome.NO_WINNER)
        def latch(context):
            context.instance.tiebreak_unit_id=self.unit.pk
            context.instance.save(update_fields=['tiebreak_unit'])
            return HandlerResult(ReceiptStatus.APPLIED,{})
        before=get_match_snapshot(self.instance.pk)['state_revision']
        count=DuelActionReceipt.objects.count()
        with self.assertRaises(MatchIntegrityError):
            execute_action(instance_id=self.instance.pk,actor=self.host,client_action_id='unproven',
                action_type='test.boundary',payload={},handler=latch)
        self.instance.refresh_from_db();self.assertIsNone(self.instance.tiebreak_unit_id)
        self.assertEqual(self.instance.state_revision,before);self.assertEqual(DuelActionReceipt.objects.count(),count)
        trigger=self.next_unit();self.batch(trigger,1,1,RoundOutcome.DRAW)
        def replace(context):
            context.instance.tiebreak_unit_id=self.unit.pk
            context.instance.save(update_fields=['tiebreak_unit'])
            return HandlerResult(ReceiptStatus.APPLIED,{})
        with self.assertRaisesMessage(RuntimeError,'cannot be replaced'):
            execute_action(instance_id=self.instance.pk,actor=self.host,client_action_id='replace',
                action_type='test.boundary',payload={},handler=replace)
        self.instance.refresh_from_db();self.assertEqual(self.instance.tiebreak_unit_id,trigger.pk)


class WhereFinalRules(inputs.InputFixture):
    def test_one_no_and_equal_markers_finish_and_remain_idempotent(self):
        for submitted, expected, outcome in [((),(0,0),'no_winner'),((0,),(1,0),'party_a_wins'),
                ((1,),(0,1),'party_b_wins'),((0,1),(1,1),'draw')]:
            with self.subTest(submitted=submitted):
                state=self.start()
                for side in submitted:self.assertEqual(self.answer(side,{'latitude':52.5,'longitude':13.4}).status_code,200)
                from .deadlines import reconcile_due_rounds
                with patch('django.utils.timezone.now',return_value=state.deadline):
                    reconcile_due_rounds(); snap=self.reveal()
                    self.assertEqual(snap['points'],dict(zip(('a','b'),expected)))
                    self.assertEqual(snap['round_outcome'],outcome)
                    self.assertIn('result_reason',snap)
                    self.assertEqual(self.command('complete').status_code,200)


class BlackJackFinalRules(inputs.InputFixture):
    key='blackjack'
    def test_both_bust_and_equal_stars_are_zero_and_close_the_set(self):
        for both_bust in (True,False):
            self.start()
            for index in range(1 if both_bust else 2):
                if index:self.command('next');self.command('start')
                for side in (0,1):self.answer(side,123 if both_bust else 101)
                snap=self.reveal()
            self.assertEqual(snap['points'],{'a':0,'b':0})
            self.assertEqual(snap['round_outcome'],'no_winner')
            self.assertEqual(snap['result_reason'],'both_bust' if both_bust else 'equal_stars')
            self.assertEqual(self.command('complete').status_code,200)


class FullySolvedRules(turns.TurnFixture):
    def test_all_correct_task_gets_zero_and_completes(self):
        state=self.start()
        from .models import DuelTurnRound
        while not turns.exhausted(self.key,state.material,state.actions):
            self.assertEqual(self.record().status_code,200);state=DuelTurnRound.objects.get(unit=self.unit)
        self.assertEqual(self.post({'action':'show_result'}).status_code,200)
        snap=self.snapshot();self.assertEqual(snap['points'],{'a':0,'b':0})
        self.assertEqual(snap['round_outcome'],'no_winner');self.assertEqual(snap['result_reason'],'task_fully_solved')
        self.assertEqual(self.post({'action':'complete'}).status_code,200)


class FullySortedRules(FullySolvedRules): key='sorting_ladder'
class FullyKnownRules(FullySolvedRules): key='wer_weiss_mehr'


class BidMissingRules(controls.ControlFixture):
    def test_single_bid_receives_right_and_must_fulfil_it(self):
        state=self.start();self.bid(1,2)
        from .deadlines import reconcile_due_rounds
        with patch('django.utils.timezone.now',return_value=state.bid_deadline):
            self.assertEqual(reconcile_due_rounds(),1)
            snap=self.snapshot();self.assertEqual(snap['right_participant_id'],self.parties[1].pk)
            self.assertEqual(snap['winning_bid'],2)
            self.assertFalse(DuelScoreEntry.objects.exists())
            self.assertEqual(self.increment().status_code,200);self.assertEqual(self.increment().status_code,200)
            self.assertEqual(self.snapshot()['winner_id'],self.parties[1].pk)
            self.assertEqual(self.post({'action':'complete'}).status_code,200)

    def test_no_bids_close_zero_without_answer_timer(self):
        state=self.start()
        from .deadlines import reconcile_due_rounds
        with patch('django.utils.timezone.now',return_value=state.bid_deadline):reconcile_due_rounds()
        self.assertEqual(self.snapshot()['round_outcome'],'no_winner')
        self.assertEqual(self.snapshot()['result_reason'],'no_bids')
        self.assertEqual(list(DuelScoreEntry.objects.values_list('delta',flat=True)),[0,0])
        state.refresh_from_db();self.assertIsNone(state.answer_deadline)
        self.assertEqual(self.post({'action':'complete'}).status_code,200)


class DrawMigrationTests(TransactionTestCase):
    def test_additive_migration_roundtrip_preserves_previous_tables_and_values(self):
        latest=MigrationExecutor(connection).loader.graph.leaf_nodes()
        before=[n for n in latest if n[0]!='duel']+[('duel','0016_tournament_sets')]
        MigrationExecutor(connection).migrate(before)
        try:
            old=MigrationExecutor(connection).loader.project_state(before).apps
            Hub=old.get_model('games_hub','HubSession');Step=old.get_model('games_hub','HubGameStep')
            Party=old.get_model('games_hub','HubParticipant');Instance=old.get_model('duel','DuelGameInstance')
            Unit=old.get_model('duel','DuelUnit');Score=old.get_model('duel','DuelScoreEntry')
            Round=old.get_model('duel','DuelUnitOutcome');Match=old.get_model('duel','DuelMatchOutcome')
            hub=Hub.objects.create(code='OLD-SCORING');step=Step.objects.create(session=hub,order=1,game_key='estimation')
            a=Party.objects.create(session=hub,nickname='A',score_adjustment=9);b=Party.objects.create(session=hub,nickname='B')
            instance=Instance.objects.create(hub_session=hub,hub_game_step=step,participant_a=a,participant_b=b,
                game_key='estimation',match_status='completed',rule_config={'match':{'mode':'best_of_n','best_of':1}})
            unit=Unit.objects.create(game_instance=instance,number=1,status='completed',completed_at=timezone.now())
            Score.objects.create(game_instance=instance,unit=unit,participant=a,delta=1,source_key='old-result')
            Round.objects.create(unit=unit,outcome='party_a_wins',winner=a)
            Match.objects.create(game_instance=instance,winner=a,mode='best_of_n',threshold=1)
            models=(Hub,Step,Party,Instance,Unit,Score,Round,Match)
            rows={m._meta.label_lower:list(m.objects.order_by('pk').values()) for m in models}
            tables=set(connection.introspection.table_names());MigrationExecutor(connection).migrate(latest)
            self.assertEqual(set(connection.introspection.table_names()),tables)
            new=MigrationExecutor(connection).loader.project_state(latest).apps
            for name,original in rows.items():
                current=list(new.get_model(name).objects.order_by('pk').values())
                for row in current:
                    if name=='duel.duelgameinstance':self.assertIsNone(row.pop('tiebreak_unit_id'))
                    if name=='duel.duelunitoutcome':self.assertEqual(row.pop('reason'),'')
                    if name=='duel.duelmatchoutcome':self.assertEqual(row.pop('reason'),'win')
                self.assertEqual(current,original)
            MigrationExecutor(connection).migrate(before)
            for model in models:self.assertEqual(list(model.objects.order_by('pk').values()),rows[model._meta.label_lower])
        finally:MigrationExecutor(connection).migrate(latest)


class CompletionConstraintTests(matches.MatchFixture):
    def test_database_requires_a_winner_for_wins_and_only_explicit_draw_reasons_without_one(self):
        for reason,winner in [('win',None),('material_exhausted',self.party_a),
                ('tournament_no_winner',self.party_b),('unknown',None)]:
            with self.subTest(reason=reason,winner=winner),self.assertRaises(IntegrityError),transaction.atomic():
                DuelMatchOutcome.objects.create(game_instance=self.instance,winner=winner,mode='best_of_n',threshold=3,reason=reason)
        DuelMatchOutcome.objects.create(game_instance=self.instance,mode='best_of_n',threshold=3,reason='material_exhausted')


class HostDrawCompletionTests(TransactionTestCase):
    start=encounters.EncounterTests.start
    prepare=encounters.EncounterTests.prepare
    post_unit=encounters.EncounterTests.post_unit
    parallel=encounters.EncounterTests.parallel

    def setUp(self):
        encounters.EncounterTests.setUp(self)
        self.assertEqual(self.start().status_code,200);self.prepare()
        for client,value in ((self.players[0],'90'),(self.players[2],'110')):
            self.assertEqual(self.post_unit(None,client=client,role='participant',payload={'value':value}).status_code,200)
        for action in ('show_answers','show_solution','show_result','complete'):
            self.assertEqual(self.post_unit(action).status_code,200)

    def draw_url(self,role='host'):
        return reverse('duel:match_api',args=[self.hub.code,self.instance.pk,role])

    def test_exhausted_draw_is_persisted_idempotently_and_role_scoped(self):
        snapshot=self.host.get(self.draw_url()).json();self.assertTrue(snapshot['can_finish_draw'])
        for client,role in ((self.players[0],'participant'),(Client(),'spectator')):
            self.assertEqual(client.post(self.draw_url(role),{'action':'finish_draw','client_action_id':'draw'},content_type='application/json').status_code,403)
        before=DuelActionReceipt.objects.count()
        for n in range(2):
            response=self.host.post(self.draw_url(),{'action':'finish_draw','client_action_id':'draw'},content_type='application/json')
            self.assertEqual(response.status_code,200,response.content)
            self.assertEqual(response.json()['action']['replayed'],bool(n))
        snap=get_match_snapshot(self.instance.pk)
        self.assertEqual(snap['match_status'],'completed');self.assertTrue(snap['draw']);self.assertIsNone(snap['winner_id'])
        self.assertEqual((snap['party_a']['score'],snap['party_b']['score']),(1,1))
        self.assertEqual(DuelActionReceipt.objects.count(),before+1)
        self.assertEqual(DuelMatchOutcome.objects.get(game_instance=self.instance).reason,'material_exhausted')
        from .models import DuelGameInstance
        revision=snap['state_revision'];self.assertEqual(self.post_unit('start',duration_seconds=60).status_code,409)
        self.assertEqual(get_match_snapshot(self.instance.pk)['state_revision'],revision)

    def test_unused_material_prevents_premature_host_draw(self):
        from Estimation.models import EstimationQuestion
        q=EstimationQuestion.objects.create(created_by=self.user,question_text='Noch eine Frage?',correct_answer=200)
        self.quiz.selected_questions.add(q)
        self.assertFalse(self.host.get(self.draw_url()).json()['can_finish_draw'])
        response=self.host.post(self.draw_url(),{'action':'finish_draw','client_action_id':'premature'},content_type='application/json')
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['action']['code'],'material_available')
        self.assertFalse(DuelMatchOutcome.objects.exists())

    def test_parallel_host_draws_close_once_and_revision_is_not_lost(self):
        from .match_service import finish_exhausted_match
        from .models import ReceiptActorType
        before=get_match_snapshot(self.instance.pk)['state_revision']
        results=self.parallel([lambda i=i:finish_exhausted_match(instance_id=self.instance.pk,
            actor=ActorContext(ReceiptActorType.HOST,self.user.pk),client_action_id=f'end-{i}') for i in (0,1)])
        self.assertEqual(sum(r.status==ReceiptStatus.APPLIED for r in results),1)
        self.assertEqual(DuelMatchOutcome.objects.count(),1)
        self.assertEqual(get_match_snapshot(self.instance.pk)['state_revision'],before+1)

    def test_draw_schema_rollback_refuses_to_discard_the_record(self):
        self.host.post(self.draw_url(),{'action':'finish_draw','client_action_id':'draw'},content_type='application/json')
        with self.assertRaises(IrreversibleError):
            executor=MigrationExecutor(connection);latest=executor.loader.graph.leaf_nodes()
            executor.migrate([n for n in latest if n[0]!='duel']+[('duel','0016_tournament_sets')])
        self.assertTrue(get_match_snapshot(self.instance.pk)['draw'])
