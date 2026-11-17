import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import connections, IntegrityError, transaction
from django.test import Client, SimpleTestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from games_hub.lobby_join import issue_rejoin_token
from games_hub.models import HubSession,HubParticipant,HubGameStep
from .models import TargetGame,Task,Run,Enrollment,Round,TeamRound,Answer,Receipt
from .service import score_teams,perform,snapshot


class RankingTests(SimpleTestCase):
    def test_ten_teams_competition_ranks_and_fixed_failure_point(self):
        rows=score_teams([5,5,4,3,2,2,1,1,1,1],[5,6,4,0,2,0,0,0,0,0],'ranking')
        self.assertEqual([(r['rank'],r['points']) for r in rows[:5]],[(1,11),(1,11),(3,9),(10,1),(4,8)])
        self.assertTrue(all(r['rank']==10 and r['points']==1 for r in rows if not r['achieved']))

    def test_simple_does_not_pay_extra_answers_and_failures_get_zero(self):
        self.assertEqual([r['points'] for r in score_teams([2,2,1],[3,1,1],'simple')],[2,0,1])

    def test_all_failed_or_all_tied_have_explicit_results(self):
        self.assertEqual([r['points'] for r in score_teams([1,1,1],[0,0,0],'ranking')],[1,1,1])
        self.assertEqual([r['rank'] for r in score_teams([1,1,1],[1,1,2],'ranking')],[1,1,1])


class Fixture(TransactionTestCase):
    def setUp(self):
        self.user=User.objects.create_user(username='target-host',password='test',is_staff=True)
        self.client.force_login(self.user)
        self.game=TargetGame.objects.create(title='Städte nennen',creator=self.user,scoring_mode='simple',answer_seconds=10)
        self.task=self.game.tasks.create(question_text='Nennt deutsche Städte.',solutions=[
            {'text':'München','aliases':['Munich','Muenchen']},{'text':'Berlin','aliases':[]},{'text':'Hamburg','aliases':[]}])
        self.hub=HubSession.objects.create(code='TARGET',creator=self.user,is_active=True,started_at=timezone.now(),
            check_in_status='completed',locked_participant_count=3,overall_scoring_mode='simple')
        self.parties=[HubParticipant.objects.create(session=self.hub,nickname=n,scoring_eligible=True,is_active=True)
            for n in ['Team Alpha','Team Beta','Team Gamma']]
        self.players=[Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(p)) for p in self.parties]
        self.step=HubGameStep.objects.create(session=self.hub,game_key='target',room_code=self.game.room_code,order=0,title=self.game.title)
        self.run=None

    def start(self):
        response=self.client.post(reverse('games_hub:activate_session_game',args=[self.hub.code]),
            {'game_key':'target','room_code':self.game.room_code},content_type='application/json')
        self.assertEqual(response.status_code,200,response.content);self.assertTrue(response.json()['success'],response.content)
        self.run=Run.objects.get(step=self.step)
        for client,party in zip(self.players,self.parties):
            joined=client.post(reverse('target_game:join'),{'room_code':self.game.room_code,
                'hub_session':self.hub.code,'participant_name':party.nickname},content_type='application/json')
            self.assertEqual(joined.status_code,200)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=timezone.now()-timezone.timedelta(seconds=1))
        return response

    def url(self,role='host'):
        return reverse('target_game:api',args=[self.hub.code,self.game.room_code,role])

    def post(self,action,side=None,key=None,**data):
        current=self.run.rounds.order_by('-number').first() if self.run else None
        payload={'action':action,'client_action_id':key or str(uuid4()),**({'round_id':current.pk} if current else {}),**data}
        return (self.client if side is None else self.players[side]).post(self.url('host' if side is None else 'participant'),payload,content_type='application/json')

    def prepare(self,goals=(2,2,1)):
        self.start();response=self.post('prepare',task_id=self.task.pk,previous_round_id=None)
        self.assertEqual(response.status_code,200,response.content)
        for side,goal in enumerate(goals):
            self.assertEqual(self.post('ready',side).status_code,200)
            self.assertEqual(self.post('goal',side,target=goal).status_code,200)
        self.assertEqual(self.post('start').status_code,200)
        return self.run.rounds.get()

    def parallel(self,operations):
        barrier=Barrier(len(operations))
        def invoke(operation):
            connections.close_all()
            try:barrier.wait();return operation()
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:return list(pool.map(invoke,operations))


class ServiceTests(Fixture):
    def test_database_constraints_protect_clock_and_action_identity(self):
        current=self.prepare((1,1,1))
        for changes in ({'deadline':None},{'deadline':current.started_at},{'phase':'unknown'}):
            with self.subTest(changes=changes),self.assertRaises(IntegrityError),transaction.atomic():
                Round.objects.filter(pk=current.pk).update(**changes)
        self.post('answer',0,key='unique',text='Munich')
        receipt=Receipt.objects.get(actor_key=f'participant:{self.parties[0].pk}',action_id='unique')
        with self.assertRaises(IntegrityError),transaction.atomic():
            Receipt.objects.create(run=self.run,actor_key=receipt.actor_key,action_id=receipt.action_id,
                fingerprint=receipt.fingerprint,result=receipt.result)

    def test_parallel_hub_starts_and_duplicate_answer_have_one_effect(self):
        url=reverse('games_hub:activate_session_game',args=[self.hub.code])
        responses=self.parallel([lambda:self.client.post(url,{'game_key':'target','room_code':self.game.room_code},content_type='application/json') for _ in range(2)])
        self.assertTrue(all(r.json()['success'] for r in responses));self.assertEqual(Run.objects.count(),1)
        self.prepare((1,1,1))
        responses=self.parallel([lambda:self.post('answer',0,key='same-answer',text='Berlin') for _ in range(2)])
        self.assertEqual(Answer.objects.count(),1);self.assertEqual(sum(r.json()['action']['replayed'] for r in responses),1)

    def test_actual_hub_start_retry_does_not_duplicate_the_run_or_roster(self):
        self.start();first=self.run.pk;self.start()
        self.assertEqual(self.run.pk,first);self.assertEqual(Run.objects.count(),1);self.assertEqual(Enrollment.objects.count(),3)

    def test_empty_material_start_rolls_back_intro_and_remains_startable(self):
        self.task.is_active=False;self.task.save()
        response=self.client.post(reverse('games_hub:activate_session_game',args=[self.hub.code]),{'game_key':'target','room_code':self.game.room_code},content_type='application/json')
        self.assertFalse(response.json()['success']);self.assertFalse(Run.objects.exists());self.step.refresh_from_db();self.assertIsNone(self.step.intro_started_at)
        self.task.is_active=True;self.task.save();self.start()

    def test_ready_goal_visibility_boundaries_and_locked_goal(self):
        self.start();self.post('prepare',task_id=self.task.pk,previous_round_id=None)
        self.assertNotIn('question',self.players[0].get(self.url('participant')).json()['round'])
        self.assertEqual(self.post('goal',0,target=1).status_code,409)
        self.post('ready',0)
        self.assertIn('question',self.players[0].get(self.url('participant')).json()['round'])
        for goal in [0,4,1.5,True]:self.assertEqual(self.post('goal',0,target=goal).status_code,409)
        self.assertEqual(self.post('goal',0,target=1).status_code,200)
        self.assertEqual(self.post('goal',0,target=2).status_code,409)
        self.assertEqual(self.post('start').status_code,409)
        self.assertNotIn('teams',self.players[0].get(self.url('participant')).json()['round'])
        self.assertNotIn('solutions',Client().get(self.url('spectator')).json()['round'])

    def test_aliases_duplicates_wrong_answers_and_continued_submissions(self):
        self.prepare((1,1,1))
        for answer in ['Munich','Muenchen','quatsch','Berlin']:
            self.assertEqual(self.post('answer',0,text=answer).status_code,200)
        state=self.client.get(self.url()).json()['round']['teams'][0]
        self.assertEqual(state['correct_count'],2);self.assertEqual(len(state['answers']),4)
        own=self.players[0].get(self.url('participant')).json()['round']
        self.assertEqual(len(own['own']['answers']),4);self.assertNotIn('teams',own);self.assertNotIn('solutions',own)
        self.assertNotIn('own',Client().get(self.url('spectator')).json()['round'])

    def test_deadline_is_server_authoritative_get_is_read_only_and_rejoin_preserves_state(self):
        current=self.prepare();self.post('answer',0,text='Munich')
        self.run.refresh_from_db();revision=self.run.revision;receipts=Receipt.objects.count()
        with patch('django.utils.timezone.now',return_value=current.deadline):
            state=self.players[0].get(self.url('participant')).json()
            self.assertEqual(state['round']['phase'],'review');self.assertFalse(state['round']['own']['can_answer'])
            self.assertEqual(state['round']['own']['target'],2);self.assertEqual(state['round']['own']['answers'][0]['text'],'Munich')
            self.assertEqual(Receipt.objects.count(),receipts)
            self.assertEqual(self.post('answer',0,text='Berlin').status_code,409)
        self.run.refresh_from_db();self.assertEqual(self.run.revision,revision);self.assertEqual(Answer.objects.count(),1)

    def test_correction_recomputes_success_and_duplicate_credit_before_publication(self):
        current=self.prepare();self.post('answer',0,text='Muenchen');self.post('answer',0,text='Bärlin')
        answer=Answer.objects.get(text='Bärlin')
        self.assertEqual(self.post('correct',answer_id=answer.pk,solution_index=1).status_code,200)
        self.post('answer',0,text='Berlin')
        with patch('django.utils.timezone.now',return_value=current.deadline):
            self.assertEqual(self.post('publish',key='publish').status_code,200)
            self.assertEqual(self.post('publish',key='publish').json()['action']['replayed'],True)
            self.assertEqual(self.post('correct',answer_id=answer.pk,solution_index=2).status_code,409)
        first=self.client.get(self.url()).json()['round']['teams'][0]
        self.assertEqual((first['correct_count'],first['points'],first['achieved']),(2,2,True))
        self.assertEqual(self.run.teams.get(participant=self.parties[0]).total_score,2)
        answer.refresh_from_db();self.assertEqual(answer.corrected_by_id,self.user.pk)
        self.assertEqual(self.post('finish').status_code,200)
        self.assertEqual(self.post('return',0).status_code,200)
        from games_hub.views import get_leaderboard_data
        data=get_leaderboard_data(self.hub)
        self.assertTrue(any(g['game_key']=='target' for g in data['games']))

    def test_same_action_retry_conflict_and_technical_failure_rollback(self):
        self.prepare();self.post('answer',0,key='a',text='Munich');self.run.refresh_from_db();rev=self.run.revision
        self.assertTrue(self.post('answer',0,key='a',text='Munich').json()['action']['replayed'])
        self.assertEqual(self.post('answer',0,key='a',text='Berlin').status_code,409)
        self.run.refresh_from_db();self.assertEqual(self.run.revision,rev);self.assertEqual(Answer.objects.count(),1)
        with patch('target_game.service.Receipt.objects.create',side_effect=IntegrityError('receipt failed')):
            self.assertEqual(self.post('answer',1,text='Berlin').status_code,500)
        self.assertEqual(Answer.objects.count(),1);self.run.refresh_from_db();self.assertEqual(self.run.revision,rev)

    def test_roles_cannot_impersonate_and_foreign_answers_cannot_be_corrected(self):
        self.prepare()
        self.assertEqual(self.post('start',0).status_code,403)
        self.assertEqual(Client().post(self.url('spectator'),{'action':'start'},content_type='application/json').status_code,403)
        other=HubSession.objects.create(code='OTHER')
        outsider=HubParticipant.objects.create(session=other,nickname='Outside')
        client=Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(outsider))
        self.assertEqual(client.get(self.url('participant')).status_code,403)
        thief=Client();thief.force_login(User.objects.create_user(username='other-host',is_staff=True))
        self.assertEqual(thief.get(self.url()).status_code,403)
        foreign_step=HubGameStep.objects.create(session=other,game_key='target',room_code=self.game.room_code,order=0)
        foreign_run=Run.objects.create(step=foreign_step,game=self.game,scoring_mode='simple',answer_seconds=10,text_match_threshold=100)
        foreign_round=Round.objects.create(run=foreign_run,number=1,task=self.task,material={'question':'Andere Aufgabe','solutions':self.task.solutions})
        foreign_team=TeamRound.objects.create(round=foreign_round,enrollment=Enrollment.objects.create(run=foreign_run,participant=outsider))
        answer=Answer.objects.create(team=foreign_team,text='Berlin')
        self.run.refresh_from_db();revision=self.run.revision
        self.assertEqual(self.post('correct',answer_id=answer.pk,solution_index=1).status_code,409)
        answer.refresh_from_db();self.run.refresh_from_db()
        self.assertIsNone(answer.override_index);self.assertIsNone(answer.corrected_by_id)
        self.assertEqual(self.run.revision,revision)

    def test_shared_return_requires_the_actual_team_credential_and_game_end(self):
        self.prepare()
        url=reverse('games_hub:participant_return_to_lobby',args=[self.hub.code])
        data={'game_key':'target','room_code':self.game.room_code,'participant_name':self.parties[0].nickname}
        self.assertEqual(Client().post(url,data,content_type='application/json').status_code,403)
        self.assertEqual(self.players[1].post(url,data,content_type='application/json').status_code,403)
        self.assertEqual(self.players[0].post(url,data,content_type='application/json').status_code,409)
        self.assertEqual(self.post('abort').status_code,200)
        self.assertEqual(self.players[0].post(url,data,content_type='application/json').status_code,200)

    def test_host_recall_invalidates_actions_from_an_old_open_tab(self):
        self.prepare()
        from games_hub.lobby_return_flow import mark_session_game_participants_inactive
        mark_session_game_participants_inactive(self.hub.code)
        self.assertEqual(self.post('answer',0,text='Berlin').status_code,409)
        self.assertFalse(self.players[0].get(self.url('participant')).json()['round']['own']['can_answer'])
        self.assertFalse(Answer.objects.exists())

    def test_material_is_frozen_and_next_round_is_independent(self):
        current=self.prepare((1,1,1));self.task.question_text='Geändert';self.task.solutions=[{'text':'Andere Lösung','aliases':[]}];self.task.save()
        self.assertEqual(self.post('answer',0,text='Munich').status_code,200)
        self.assertEqual(Answer.objects.get().matched_index,0)
        with patch('django.utils.timezone.now',return_value=current.deadline):self.post('publish')
        self.post('prepare',task_id=self.task.pk,previous_round_id=current.pk)
        new=self.run.rounds.order_by('-number').first()
        self.assertNotEqual(new.pk,current.pk);self.assertEqual(new.material['question'],'Geändert')
        self.assertFalse(new.teams.filter(target__isnull=False).exists());self.assertIsNone(new.deadline)
        self.assertEqual(Answer.objects.filter(team__round=new).count(),0)
        self.assertEqual(self.post('answer',0,round_id=current.pk,text='Berlin').status_code,409)
        self.assertEqual(self.post('finish',round_id=current.pk).status_code,409)

    def test_host_monitor_before_start_and_public_embedding_are_role_scoped(self):
        response=self.client.get(reverse('target_game:monitor',args=[self.game.room_code]),{'hub_session':self.hub.code})
        self.assertContains(response,'data-host-game-start');self.assertContains(response,'data-host-game-leave')
        self.assertFalse(Run.objects.exists());self.start()
        response=Client().get(reverse('target_game:spectate',args=[self.game.room_code]),{'hub_session':self.hub.code})
        self.assertEqual(response['X-Frame-Options'],'SAMEORIGIN')
        self.assertNotContains(response,'Berlin');self.assertNotContains(response,'München')

    def test_supabase_excludes_runtime_before_model_discovery(self):
        from django.apps import apps
        from games_website.services import sync_all_models_to_supabase,restore_all_models_from_supabase
        config=apps.get_app_config('target_game')
        for service in (sync_all_models_to_supabase,restore_all_models_from_supabase):
            with patch('games_website.services.apps.get_app_configs',return_value=[config]),patch.object(config,'get_models',side_effect=AssertionError('excluded before discovery')):
                service()

    def test_parallel_answers_and_duplicate_publish_are_serialized(self):
        current=self.prepare((1,1,1))
        responses=self.parallel([lambda i=i:self.post('answer',i,key='same',text='Berlin') for i in (0,1)])
        self.assertTrue(all(r.status_code==200 for r in responses));self.assertEqual(Answer.objects.count(),2)
        with patch('django.utils.timezone.now',return_value=current.deadline):
            responses=self.parallel([lambda:self.post('publish',key='one-publication') for _ in range(2)])
        self.assertTrue(all(r.status_code==200 for r in responses));self.assertEqual(sum(r.json()['action']['replayed'] for r in responses),1)
        self.assertEqual(list(self.run.teams.order_by('pk').values_list('total_score',flat=True)),[1,1,0])

    def test_answer_at_deadline_racing_publication_cannot_change_the_result(self):
        current=self.prepare((1,1,1))
        with patch('django.utils.timezone.now',return_value=current.deadline):
            answer,publish=self.parallel([lambda:self.post('answer',0,text='Berlin'),lambda:self.post('publish')])
        self.assertEqual(answer.status_code,409);self.assertEqual(publish.status_code,200)
        self.assertFalse(Answer.objects.exists());self.assertTrue(all(t.points==0 for t in current.teams.all()))

    def test_a_new_session_uses_a_fresh_run_and_start_time(self):
        self.prepare((1,1,1));self.post('abort')
        original=self.run.pk
        from games_hub.session_results import freeze_previous_results
        self.game.refresh_from_db();freeze_previous_results('target',self.game,'FRESH')
        next_hub=HubSession.objects.create(code='FRESH',creator=self.user,is_active=True,started_at=timezone.now(),check_in_status='completed',locked_participant_count=1)
        HubParticipant.objects.create(session=next_hub,nickname='New Team',scoring_eligible=True,is_active=True)
        next_step=HubGameStep.objects.create(session=next_hub,game_key='target',room_code=self.game.room_code,order=0)
        self.game.status='waiting';self.game.save(update_fields=['status'])
        response=self.client.post(reverse('games_hub:activate_session_game',args=[next_hub.code]),{'game_key':'target','room_code':self.game.room_code},content_type='application/json')
        self.assertTrue(response.json()['success'],response.content)
        self.game.refresh_from_db();self.assertGreaterEqual(self.game.started_at,next_hub.started_at)
        run=Run.objects.get(step=next_step);self.assertNotEqual(run.pk,original);self.assertFalse(run.rounds.exists())


class ContentAndPlanTests(Fixture):
    def test_content_editor_stores_aliases_and_updates_game_configuration(self):
        url=reverse('target_game:manage')+f'?game={self.game.pk}'
        response=self.client.post(url,{'action':'save_task','question_text':'Hauptstädte','is_active':'on','answer_lines':'London | Londinium\nParis'})
        self.assertEqual(response.status_code,302)
        task=self.game.tasks.get(question_text='Hauptstädte');self.assertEqual(task.solutions[0]['aliases'],['Londinium'])
        self.assertEqual(self.client.post(url,{'action':'save_game','title':'Zielrunde','scoring_mode':'ranking','answer_seconds':'30'}).status_code,302)
        self.game.refresh_from_db();self.assertEqual((self.game.scoring_mode,self.game.answer_seconds),('ranking',30))

    def test_regular_planning_lists_and_persists_the_game_and_rejects_duel_session(self):
        url=reverse('games_hub:create_session');self.assertContains(self.client.get(url),'Zielansage')
        payload={'name':'Geplant','code':'PLAN','session_type':'multiplayer','games_order':json.dumps([{'game_key':'target','room_code':self.game.room_code,'title':self.game.title}])}
        self.assertEqual(self.client.post(url,payload).status_code,302)
        self.assertEqual(HubGameStep.objects.get(session__code='PLAN').game_key,'target')
        payload.update(code='DUELPLAN',session_type='duel')
        self.assertEqual(self.client.post(url,payload).status_code,400);self.assertFalse(HubSession.objects.filter(code='DUELPLAN').exists())
