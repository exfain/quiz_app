import json
import subprocess
from datetime import timedelta
from io import BytesIO, StringIO
from unittest.mock import patch
from uuid import uuid4

from PIL import Image, PngImagePlugin
from django.apps import apps
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from clue_rush.models import ClueRushGame, ClueQuestion, Clue
from who_is_lying.models import WhoQuiz, WhoQuestion
from who_is_that.models import WhoThatQuiz, WhoThatQuestion
from wann_war_das.models import WannWarDasGame, WannWarDasQuestion
from games_hub.models import HubSession, HubGameStep, HubParticipant
from games_hub.lobby_join import issue_rejoin_token
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from .models import DuelGameInstance, DuelOralRound, DuelBuzz, DuelDecision, DuelScoreEntry, DuelUnitOutcome, DuelActionReceipt, ReceiptActorType, ReceiptStatus
from .commands import ActorContext, ActionConflict
from .controllers import oral
from .deadlines import reconcile_due_rounds
from . import test_submissions_concurrency as concurrency
from .test_deadline_worker import worker_environment


def make_material(case, key):
    if not hasattr(case,'user'):case.user=User.objects.create_superuser('oral-host','','pw')
    if key=='clue_rush':
        case.game=ClueRushGame.objects.create(creator=case.user,title='Clue Rush Duel')
        case.question=ClueQuestion.objects.create(created_by=case.user,question_text='Gesuchte Stadt?',answer='Berlin')
        for i in range(3):Clue.objects.create(clue_question=case.question,clue_text=f'Geheimer Hinweis {i+1}',order=i+1,duration=2)
    elif key=='who':
        case.game=WhoQuiz.objects.create(creator=case.user,title='Who Duel')
        case.question=WhoQuestion.objects.create(created_by=case.user,statement='Wer sagt die Wahrheit?',time_limit=2,
            people=[{'name':'Name Alpha','is_lying':False},{'name':'Name Beta','is_lying':True},{'name':'Name Gamma','is_lying':True}])
    elif key=='who_that':
        case.game=WhoThatQuiz.objects.create(creator=case.user,title='Picture Duel')
        out=BytesIO();info=PngImagePlugin.PngInfo();info.add_text('Description','SecretPerson')
        Image.new('RGB',(96,96),'blue').save(out,format='PNG',pnginfo=info)
        case.question=WhoThatQuestion.objects.create(created_by=case.user,question_text='Wer ist das?',correct_answer='SecretPerson',time_limit=15,
            image=SimpleUploadedFile('SecretPerson.png',out.getvalue(),content_type='image/png'))
    else:
        case.game=WannWarDasGame.objects.create(creator=case.user,title='Year Duel')
        case.question=WannWarDasQuestion.objects.create(created_by=case.user,question_text='In welchem Jahr?',correct_answer=1989,
            start_tolerance=1,tolerance_increment=2,max_tolerance=5,seconds_per_step=1,time_limit=15)
    case.game.selected_questions.add(case.question)


class OralFixture(TransactionTestCase):
    key='clue_rush'
    config={'mode':'best_of_n','best_of':7}

    def setUp(self):
        make_material(self,self.key)
        self.host=Client();self.host.force_login(self.user)
        self.hub=HubSession.objects.create(code='ORAL',creator=self.user,session_type='duel',is_active=True,
            check_in_status='completed',locked_participant_count=2)
        self.parties=[HubParticipant.objects.create(session=self.hub,nickname=n,scoring_eligible=True,checked_in_at=timezone.now()) for n in ('Alpha','Beta')]
        self.players=[Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(p)) for p in self.parties];self.spectator=Client()
        self.step=HubGameStep.objects.create(session=self.hub,game_key=self.key,room_code=self.game.room_code,order=0,duel_match_config=self.config)
        response=self.hub_start();self.assertEqual(response.status_code,200,response.content)
        self.instance=DuelGameInstance.objects.get(hub_game_step=self.step);self.unit=None
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=timezone.now()-timedelta(seconds=1))

    def hub_start(self):
        return self.host.post(reverse('games_hub:activate_session_game',args=[self.hub.code]),data=json.dumps({'step_id':self.step.pk,'game_key':self.key,'room_code':self.game.room_code}),content_type='application/json')

    def url(self,role='host',match=False):
        return reverse('duel:'+self.key+('_match_api' if match else '_'+role),args=[self.hub.code,self.instance.pk,role] if match else [self.hub.code,self.instance.pk,self.unit.pk])

    def post(self,data,side=None,key=None,match=False):
        role='host' if side is None else 'participant';client=self.host if side is None else self.players[side]
        return client.post(self.url(role,match),data=json.dumps({**data,'client_action_id':key or str(uuid4())}),content_type='application/json')

    def prepare(self):
        response=self.post({'action':'prepare','question_id':self.question.pk,'previous_unit_id':self.unit.pk if self.unit else None},match=True)
        self.assertEqual(response.status_code,200,response.content);self.unit=self.instance.units.latest('number')

    def start(self):
        self.prepare();response=self.post({'action':'start'});self.assertEqual(response.status_code,200,response.content)
        return DuelOralRound.objects.get(unit=self.unit)

    def buzz(self,side=0,key=None):return self.post({'action':'buzz'},side,key)

    def judge(self,verdict='correct',key=None):
        return self.post({'action':'judge','buzz_id':str(self.unit.buzzes.latest('sequence').pk),'verdict':verdict},key=key)

    def snapshot(self,role='spectator',side=0):
        client=self.spectator if role=='spectator' else self.host if role=='host' else self.players[side]
        return client.get(self.url(role,match=True)).json()['unit']

    def actor(self,side=0):return ActorContext(ReceiptActorType.PARTICIPANT,self.parties[side].pk)


class ClueDuelTests(OralFixture):
    def test_long_host_assessment_preserves_every_remaining_hint_and_time(self):
        state=self.start();first=state.started_at+timedelta(seconds=1)
        with patch('django.utils.timezone.now',return_value=first):self.assertEqual(self.buzz().status_code,200)
        state.refresh_from_db();self.assertIsNone(state.deadline);self.assertEqual(state.elapsed_us,1_000_000)
        later=first+timedelta(hours=1)
        with patch('django.utils.timezone.now',return_value=later):
            public=self.snapshot();self.assertEqual(public['clues'],['Geheimer Hinweis 1']);self.assertNotIn('solution',public)
            self.assertEqual(reconcile_due_rounds(),0)
            self.assertEqual(self.judge('incorrect').status_code,200)
            state.refresh_from_db();self.assertEqual(state.deadline,later+timedelta(seconds=5))
            self.assertEqual(self.buzz(0).status_code,409)
            self.assertFalse(DuelScoreEntry.objects.exists())
        with patch('django.utils.timezone.now',return_value=later+timedelta(seconds=2)):
            self.assertEqual(self.snapshot()['clues'],['Geheimer Hinweis 1','Geheimer Hinweis 2'])
            self.assertEqual(self.buzz(1).status_code,200)
            self.assertEqual(self.judge().status_code,200)
        self.assertEqual(self.snapshot()['points'],{'a':0,'b':1})
        self.assertEqual(DuelDecision.objects.count(),2);self.assertEqual(DuelBuzz.objects.count(),2)

    def test_two_incorrect_attempts_end_with_zero_points_and_no_round_winner(self):
        self.start();self.buzz();self.judge('incorrect');self.buzz(1);self.judge('incorrect')
        self.assertEqual(self.snapshot()['points'],{'a':0,'b':0})
        self.assertEqual(self.unit.outcome.outcome,'no_winner')

    def test_identical_and_conflicting_retries_do_not_reuse_attempts(self):
        self.start();key='buzz';first=self.buzz(0,key)
        self.assertEqual(first.status_code,200)
        self.assertTrue(self.buzz(0,key).json()['action']['replayed'])
        self.assertEqual(DuelBuzz.objects.count(),1)
        self.judge('incorrect',key='judge')
        self.assertTrue(self.judge('incorrect',key='judge').json()['action']['replayed'])
        self.assertEqual(self.judge('correct',key='judge').status_code,409)
        self.assertEqual(DuelDecision.objects.count(),1);self.assertFalse(DuelScoreEntry.objects.exists())

    def test_phase_and_actor_context_cannot_be_selected_by_payload(self):
        self.start()
        for data in ({'action':'judge','buzz_id':str(uuid4()),'verdict':'correct'}, {'action':'start'}):
            self.assertEqual(self.post(data,0).status_code,403)
        self.assertEqual(self.post({'action':'buzz','participant_id':self.parties[1].pk},0).status_code,400)
        outsider=HubParticipant.objects.create(session=self.hub,nickname='Outsider')
        client=Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(outsider))
        self.assertEqual(client.get(self.url('participant',match=True)).status_code,403)
        self.assertEqual(self.spectator.post(self.url('spectator'),data='{}',content_type='application/json').status_code,404)

    def test_read_is_write_free_and_never_reconciles_deadline(self):
        state=self.start()
        with patch('django.utils.timezone.now',return_value=state.deadline):
            with CaptureQueriesContext(connection) as queries:public=self.snapshot()
            self.assertFalse(public['can_buzz'])
            self.assertFalse(any(q['sql'].lstrip().upper().startswith(('UPDATE','INSERT','DELETE')) for q in queries))
            self.assertEqual(DuelOralRound.objects.get(unit=self.unit).phase,'answering')
            self.assertEqual(self.buzz().status_code,409)

    def test_failed_receipt_rolls_back_attempt_clock_and_revision(self):
        self.start();self.instance.refresh_from_db();before=self.instance.state_revision
        with patch.object(DuelActionReceipt,'save',side_effect=IntegrityError('receipt failed')):
            with self.assertLogs('duel.views',level='ERROR'):self.assertEqual(self.buzz().status_code,500)
        self.assertFalse(DuelBuzz.objects.exists());self.assertEqual(DuelOralRound.objects.get(unit=self.unit).phase,'answering')
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,before)

    def test_result_failure_rolls_back_decision_scores_and_outcome(self):
        self.start();self.buzz()
        with patch('duel.controllers.oral.complete_unit_in_action',side_effect=IntegrityError('outcome failed')):
            with self.assertLogs('duel.views',level='ERROR'):self.assertEqual(self.judge().status_code,500)
        self.assertFalse(DuelDecision.objects.exists());self.assertFalse(DuelScoreEntry.objects.exists());self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertEqual(DuelOralRound.objects.get(unit=self.unit).phase,'awaiting_decision')

    def test_question_snapshot_and_repeated_occurrences_are_independent(self):
        self.start();original=self.snapshot()['question'];self.question.question_text='Edited';self.question.answer='Other';self.question.save()
        self.assertEqual(self.snapshot()['question'],original)
        self.buzz();self.judge();self.assertEqual(self.snapshot()['solution'],'Berlin')
        self.post({'action':'complete'});previous=self.unit.pk;self.start()
        self.assertNotEqual(self.unit.pk,previous);self.assertEqual(self.snapshot()['question'],'Edited')
        self.assertEqual(self.unit.buzzes.count(),0)

    def test_multiplayer_session_with_two_people_keeps_original_start(self):
        # Test a normal planned slot, rather than relabeling a running Duel.
        self.instance.action_receipts.all().delete(); self.instance.delete()
        self.step.game_format = 'standard'; self.step.intro_started_at = self.step.intro_ends_at = None
        self.step.save(update_fields=['game_format', 'intro_started_at', 'intro_ends_at'])
        self.hub.session_type='multiplayer';self.hub.save()
        response=self.hub_start();self.assertEqual(response.status_code,200,response.content)
        self.game.refresh_from_db();self.assertEqual(self.game.status,'active')
        self.assertNotIn('duel',response.json());self.assertEqual(DuelGameInstance.objects.count(),0)

    def test_no_buzz_timeout_scores_zero_and_no_round_winner(self):
        state=self.start()
        with patch('django.utils.timezone.now',return_value=state.deadline):
            reconcile_due_rounds()
            response=self.post({'action':'show_result'})
            self.assertEqual(response.status_code,200,response.content)
            self.assertNotIn('resolution_reason',self.snapshot())
        self.assertEqual(self.snapshot()['points'],{'a':0,'b':0})
        self.assertEqual(self.unit.outcome.outcome,'no_winner')
        self.assertEqual(DuelScoreEntry.objects.count(),2)

    def test_database_guards_attempt_uniqueness_and_phase_values(self):
        self.start();self.buzz();attempt=DuelBuzz.objects.get()
        with self.assertRaises(IntegrityError),transaction.atomic():
            DuelBuzz.objects.create(unit=self.unit,participant=attempt.participant,sequence=2,elapsed_us=0)
        with self.assertRaises(IntegrityError),transaction.atomic():
            DuelOralRound.objects.filter(unit=self.unit).update(phase='invented')

    def test_worker_restart_during_assessment_never_consumes_remaining_hint_time(self):
        self.question.clues.update(duration=60)
        past=timezone.now()-timedelta(seconds=600)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=past-timedelta(seconds=1))
        with patch('django.utils.timezone.now',return_value=past):self.start()
        with patch('django.utils.timezone.now',return_value=past+timedelta(seconds=1)):self.buzz()
        with worker_environment() as (command,options):
            for _ in range(2):
                result=subprocess.run([*command,'--once'],capture_output=True,text=True,timeout=35,**options)
                self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'0')
        self.assertEqual(DuelOralRound.objects.get(unit=self.unit).phase,'awaiting_decision')
        self.assertEqual(self.judge('incorrect').status_code,200)
        state=DuelOralRound.objects.get(unit=self.unit)
        self.assertEqual((state.deadline-state.resumed_at)//timedelta(microseconds=1),179_000_000)

    def test_result_projection_rejects_a_corrupted_score_without_repair_writes(self):
        self.start();self.buzz();self.judge()
        DuelScoreEntry.objects.filter(unit=self.unit,participant=self.parties[0]).update(delta=99)
        self.assertEqual(self.spectator.get(self.url('spectator',match=True)).status_code,409)

    def test_first_wrong_then_timeout_never_awards_an_opponent_point(self):
        self.start();self.buzz();self.judge('incorrect')
        state=DuelOralRound.objects.get(unit=self.unit)
        with patch('django.utils.timezone.now',return_value=state.deadline):
            reconcile_due_rounds();response=self.post({'action':'show_result'})
            self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(self.snapshot()['points'],{'a':0,'b':0});self.assertEqual(self.unit.outcome.outcome,'no_winner')


class WhoDuelTests(OralFixture):
    key='who'
    test_multiplayer_session_with_two_people_keeps_original_start=ClueDuelTests.test_multiplayer_session_with_two_people_keeps_original_start

    def test_exact_name_boundary_uses_server_active_person_not_previous_display(self):
        state=self.start();duration=state.material['person_us'];base=state.started_at
        with patch('django.utils.timezone.now',return_value=base+timedelta(microseconds=duration-1)):
            self.assertEqual(self.snapshot()['person']['person_index'],0)
        with patch('django.utils.timezone.now',return_value=base+timedelta(microseconds=duration)):
            response=self.buzz();self.assertEqual(response.status_code,200,response.content)
        attempt=DuelBuzz.objects.get();self.assertEqual(attempt.captured['person_index'],1)
        self.assertEqual(attempt.captured['name'],state.material['people'][1]['name'])
        lying=state.material['people'][1]['is_lying']
        self.assertEqual(self.snapshot()['points'],{'a':int(lying),'b':int(not lying)})
        self.assertFalse(DuelDecision.objects.exists())

    def test_truthful_name_awards_opponent_and_liar_awards_buzzing_party(self):
        for wanted in (False,True):
            state=self.start();index=next(i for i,p in enumerate(state.material['people']) if p['is_lying']==wanted)
            at=state.started_at+timedelta(microseconds=index*state.material['person_us'])
            with patch('django.utils.timezone.now',return_value=at):
                self.assertEqual(self.buzz().status_code,200)
                self.assertEqual(self.snapshot()['points'],{'a':int(wanted),'b':int(not wanted)})
                self.post({'action':'complete'})

    def test_timeout_without_buzz_reveals_only_after_host_release(self):
        state=self.start()
        with patch('django.utils.timezone.now',return_value=state.deadline):
            self.assertEqual(reconcile_due_rounds(),1);self.assertEqual(reconcile_due_rounds(),0)
            self.assertNotIn('solution',self.snapshot());self.assertNotIn('people',self.snapshot())
            response=self.post({'action':'show_result'});self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(self.snapshot()['points'],{'a':0,'b':0});self.assertEqual(self.unit.outcome.outcome,'no_winner')


class ToleranceDuelTests(OralFixture):
    key='wann_war_das'
    test_multiplayer_session_with_two_people_keeps_original_start=ClueDuelTests.test_multiplayer_session_with_two_people_keeps_original_start
    test_no_buzz_timeout_scores_zero_and_no_round_winner=ClueDuelTests.test_no_buzz_timeout_scores_zero_and_no_round_winner

    def test_frozen_tolerance_survives_long_assessment_and_rejoin(self):
        state=self.start();at=state.started_at+timedelta(seconds=1)
        with patch('django.utils.timezone.now',return_value=at):self.assertEqual(self.buzz().status_code,200)
        with patch('django.utils.timezone.now',return_value=at+timedelta(hours=1)):
            public=self.snapshot();private=self.snapshot('host')
            self.assertEqual(public['tolerance'],'3');self.assertNotIn('solution',public);self.assertNotIn('accepted_range',public)
            self.assertEqual(private['accepted_range'],{'min':'1986','max':'1992'})
            self.assertEqual(DuelBuzz.objects.get().captured['tolerance'],'3')
            self.assertEqual(self.judge('incorrect').status_code,200)
        self.assertEqual(self.snapshot()['points'],{'a':0,'b':1})

    def test_client_cannot_claim_tolerance_or_target(self):
        self.start()
        self.assertEqual(self.post({'action':'buzz','tolerance':999,'target':1989},0).status_code,400)
        self.assertFalse(DuelBuzz.objects.exists())


class PictureDuelTests(OralFixture):
    key='who_that'
    config={'mode':'target_score','target_score':1}
    test_multiplayer_session_with_two_people_keeps_original_start=ClueDuelTests.test_multiplayer_session_with_two_people_keeps_original_start
    test_no_buzz_timeout_scores_zero_and_no_round_winner=ClueDuelTests.test_no_buzz_timeout_scores_zero_and_no_round_winner

    def test_private_image_snapshot_strips_solution_metadata_and_source_name(self):
        self.prepare();url=reverse('duel:oral_image',args=[self.hub.code,self.instance.pk,self.unit.pk,'participant'])
        self.assertEqual(self.players[0].get(url).status_code,404)
        self.post({'action':'start'});state=self.snapshot('participant')
        self.assertNotIn('solution',state);self.assertNotIn('SecretPerson',json.dumps(state))
        image=self.players[0].get(url);self.assertEqual(image.status_code,200)
        self.assertEqual(image['Content-Type'],'image/png');self.assertNotIn(b'SecretPerson',image.content)
        self.assertEqual(self.spectator.get(url).status_code,403)
        original=image.content;self.question.correct_answer='New person';self.question.save()
        self.assertEqual(self.players[0].get(url).content,original)
        self.buzz();self.assertNotIn('solution',self.snapshot());self.judge()
        self.assertEqual(self.snapshot()['solution'],'SecretPerson')
        self.instance.refresh_from_db();self.assertEqual(self.instance.match_status,'completed')
        self.assertEqual(self.post({'action':'complete'}).status_code,200)
        self.assertEqual(self.post({'action':'start'}).status_code,409)


class OralConcurrencyTests(OralFixture):
    parallel=concurrency.SubmissionConcurrencyTests.parallel

    def test_parallel_buzzes_one_right_one_revision(self):
        self.start();self.instance.refresh_from_db();before=self.instance.state_revision
        results=self.parallel([lambda i=i:oral.buzz(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=self.actor(i),client_action_id=f'buzz-{i}') for i in range(2)])
        self.assertEqual(sum(r.status==ReceiptStatus.APPLIED for r in results),1);self.assertEqual(DuelBuzz.objects.count(),1)
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,before+1)

    def test_duplicate_and_conflicting_judges_cannot_book_twice(self):
        self.start();self.buzz();attempt=DuelBuzz.objects.get()
        args=dict(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=ActorContext(ReceiptActorType.HOST,self.user.pk),client_action_id='judge',buzz_id=str(attempt.pk))
        results=self.parallel([lambda v=v:oral.judge(**args,verdict=v) for v in ('correct','incorrect')])
        self.assertEqual(sum(isinstance(r,ActionConflict) for r in results),1);self.assertEqual(DuelDecision.objects.count(),1)
        self.assertLessEqual(DuelScoreEntry.objects.count(),2)

    def test_buzz_at_deadline_racing_worker_is_not_accepted(self):
        state=self.start()
        with patch('django.utils.timezone.now',return_value=state.deadline):
            result,count=self.parallel([lambda:oral.buzz(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=self.actor(),client_action_id='late'),reconcile_due_rounds])
        self.assertEqual(result.status,ReceiptStatus.REJECTED);self.assertEqual(count,1);self.assertFalse(DuelBuzz.objects.exists())


class OralMigrationTests(TransactionTestCase):
    def test_additive_round_trip_has_no_multiplayer_schema_dependency(self):
        executor=MigrationExecutor(connection);latest=executor.loader.graph.leaf_nodes()
        before=[n for n in latest if n[0]!='duel']+[('duel','0011_quick_quiz_duel')]
        after=[n for n in latest if n[0]!='duel']+[('duel','0012_oral_buzzer_rounds')]
        migration=executor.loader.get_migration('duel','0012_oral_buzzer_rounds')
        self.assertEqual(migration.dependencies,[('duel','0011_quick_quiz_duel')])
        self.assertEqual(len(migration.operations),2)
        executor.migrate(before)
        try:
            tables=set(connection.introspection.table_names());MigrationExecutor(connection).migrate(after)
            self.assertEqual(set(connection.introspection.table_names())-tables,{'duel_duelbuzz','duel_dueloralround'})
            MigrationExecutor(connection).migrate(before)
            self.assertEqual(set(connection.introspection.table_names()),tables)
        finally:MigrationExecutor(connection).migrate(latest)


class WhoConcurrencyTests(OralFixture):
    key='who'
    parallel=concurrency.SubmissionConcurrencyTests.parallel

    def test_name_boundary_and_competing_buzzes_have_one_atomic_result(self):
        state=self.start();self.instance.refresh_from_db();revision=self.instance.state_revision
        at=state.started_at+timedelta(microseconds=state.material['person_us'])
        with patch('django.utils.timezone.now',return_value=at):
            results=self.parallel([lambda i=i:oral.buzz(instance_id=self.instance.pk,unit_id=self.unit.pk,actor=self.actor(i),client_action_id=f'boundary-{i}') for i in range(2)])
        self.assertEqual(sum(r.status==ReceiptStatus.APPLIED for r in results),1)
        self.assertEqual(DuelBuzz.objects.get().captured['person_index'],1)
        self.assertEqual(DuelScoreEntry.objects.count(),2);self.assertEqual(DuelUnitOutcome.objects.count(),1)
        self.assertFalse(DuelDecision.objects.exists())
        self.instance.refresh_from_db();self.assertEqual(self.instance.state_revision,revision+1)


class OralSupabaseTests(SimpleTestCase):
    def test_new_models_are_excluded_before_sync_or_restore_discovery(self):
        config=apps.get_app_config('duel');self.assertTrue({DuelBuzz,DuelOralRound}.issubset(set(config.get_models())))
        for service in (sync_all_models_to_supabase,restore_all_models_from_supabase):
            with patch('games_website.services.apps.get_app_configs',return_value=[config]),patch.object(config,'get_models',side_effect=AssertionError('must not inspect duel')):
                self.assertEqual(service(stdout=StringIO(),stderr=StringIO()),(0,[]))
