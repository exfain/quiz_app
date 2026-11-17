"""Tournament persistence, actual HTTP, barriers and separate SQLite writers."""
from io import StringIO
from unittest.mock import patch
from uuid import uuid4
from django.apps import apps
from django.db import connection, transaction, IntegrityError, OperationalError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, SimpleTestCase, Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from games_hub.models import HubSession, HubGameStep, HubParticipant
from games_hub.lobby_join import issue_rejoin_token
from games_hub.views import get_leaderboard_data
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from . import test_encounters as fixture
from . import test_submissions_concurrency as concurrency
from .models import (DuelTournament, DuelTournamentSet, DuelTournamentPair, DuelTournamentSeat,
    DuelGameInstance, DuelActionReceipt, DuelScoreEntry, DuelSubmission)
from .commands import CommandRejected
from . import tournaments


class TournamentTests(TransactionTestCase):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def setUp(self):
        fixture.EncounterTests.setUp(self)
        self.parties.append(HubParticipant.objects.create(session=self.hub, nickname='Delta', scoring_eligible=True, checked_in_at=timezone.now()))
        self.hub.locked_participant_count = 4; self.hub.save()
        self.clients = {p.pk: Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(p)) for p in self.parties}

    def start(self, client=None):
        response = (client or self.host).post(reverse('games_hub:activate_session_game', args=[self.hub.code]),
            {'action':'start_tournament','game_key':self.step.game_key,'room_code':self.step.room_code,'step_id':self.step.pk}, content_type='application/json')
        if response.status_code == 200: self.run = DuelTournament.objects.get(step=self.step)
        return response

    def api(self, role='host', client=None):
        return (client or self.host).get(reverse('duel:tournament_api', args=[self.hub.code,self.run.pk,role]))

    def draw(self, previous=None, selection=None):
        response = self.host.post(reverse('duel:tournament_api',args=[self.hub.code,self.run.pk,'host']),
            {'action':'draw','previous_set_id':str(previous) if previous else None,
             'selection':selection or {'id':self.question.pk,'duration_seconds':60}},content_type='application/json')
        if response.status_code == 200: self.run.refresh_from_db()
        return response

    def post(self, pair, action, *, party=None, **options):
        role = 'participant' if party else 'host'
        name = 'duel:estimation_'+role
        unit = pair.game_instance.units.get()
        data = {'client_action_id':str(uuid4()), **options}
        if not party: data['action'] = action
        return (self.clients[party] if party else self.host).post(reverse(name,args=[self.hub.code,pair.game_instance_id,unit.pk]),data,content_type='application/json')

    def pairs(self): return list(self.run.sets.get(number=self.run.current_set).pairs.select_related('game_instance').order_by('position'))

    def open_pairs(self):
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=timezone.now())
        for pair in self.pairs(): self.assertEqual(self.post(pair,'start',duration_seconds=60).status_code,200)

    def answer_pair(self,pair,tie=False):
        for pk,value in ((pair.game_instance.participant_a_id,'90' if tie else '100'),(pair.game_instance.participant_b_id,'110' if tie else '120')):
            response=self.post(pair,None,party=pk,payload={'value':value});self.assertEqual(response.status_code,200,response.content)

    def finish_pair(self,pair):
        for action in ('show_answers','show_solution','show_result','complete'):
            response=self.post(pair,action);self.assertEqual(response.status_code,200,response.content)

    def test_start_draw_persist_reload_retry_and_frozen_material(self):
        first=self.start();self.assertEqual(first.status_code,200,first.content);self.assertEqual(first.json(),self.start().json())
        self.assertFalse(DuelGameInstance.objects.exists());self.assertEqual(self.draw().status_code,200)
        ids=list(DuelTournamentSeat.objects.order_by('participant_id').values_list('participant_id','pair_id'))
        self.assertEqual(len(ids),4);self.assertEqual(len(self.pairs()),2)
        revision=self.run.coordinator.state_revision;response=self.draw();self.assertTrue(response.json()['action']['replayed'])
        self.assertEqual(ids,list(DuelTournamentSeat.objects.order_by('participant_id').values_list('participant_id','pair_id')))
        self.run.coordinator.refresh_from_db();self.assertEqual(self.run.coordinator.state_revision,revision)
        self.question.correct_answer=900;self.question.save()
        self.assertEqual({p.game_instance.units.get().estimation_question_binding.target_value for p in self.pairs()},{'100'})
        self.assertEqual(self.api().json()['set_number'],1)

    def test_estimation_overview_reads_without_reserving_sqlite_writer(self):
        self.start(); self.assertEqual(self.draw().status_code, 200)
        original = connection.transaction_mode
        with CaptureQueriesContext(connection) as queries:
            response = self.api('spectator', Client())
        self.assertEqual(response.status_code, 200)
        if connection.vendor == 'sqlite':
            self.assertIn('BEGIN DEFERRED', [q['sql'] for q in queries])
        self.assertEqual(connection.transaction_mode, original)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))

    def test_25_estimation_parties_have_a_bounded_shared_reveal_check(self):
        for n in range(21):
            party=HubParticipant.objects.create(session=self.hub, nickname=f'Extra {n}', scoring_eligible=True,
                checked_in_at=timezone.now())
            self.clients[party.pk]=Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(party))
        self.hub.locked_participant_count=25;self.hub.save()
        self.start();self.assertEqual(self.draw().status_code,200);self.open_pairs()
        pairs=self.pairs();self.assertEqual(len(pairs),12)
        for pair in pairs[:-1]:self.answer_pair(pair)
        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(tournaments.reveal_ready(pairs[0]))
        self.assertLessEqual(len(queries),3,'Every participant poll must not load all twelve encounters separately')
        self.answer_pair(pairs[-1])
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(tournaments.reveal_ready(pairs[0]))
        self.assertLessEqual(len(queries),3)
        with CaptureQueriesContext(connection) as queries:
            overview=self.api('spectator',Client())
        self.assertEqual(overview.status_code,200,overview.content)
        self.assertEqual(len(overview.json()['pairs']),12)
        self.assertTrue(all('points' not in row for row in overview.json()['pairs']))
        self.assertLessEqual(len(queries),80,'Overview polling must not reload every pairing inside each row')
        self.assertLessEqual(sum('FROM "games_hub_hubsession"' in q['sql'] for q in queries),2,
            'Encounter links must not reload the same session once per pairing')
        for pair in pairs:self.finish_pair(pair)
        with CaptureQueriesContext(connection) as queries:
            published=self.api('spectator',Client())
        self.assertEqual(published.status_code,200,published.content)
        data=published.json()
        self.assertTrue(data['complete'])
        self.assertEqual(len(data['results']),13)  # includes the zero-point bye
        self.assertTrue(all(row['points']==[1,0] for row in data['pairs']))
        self.assertLessEqual(len(queries),80,'Completed routers must not multiply scoring/history queries per device')
        last_state=tournaments.state_for(pairs[-1].game_instance)
        last_state.delete()
        self.assertFalse(tournaments.reveal_ready(pairs[0]),'Missing phase state must block the shared reveal')

    def test_odd_eligible_pool_gets_one_persisted_random_bye_without_points(self):
        extra=HubParticipant.objects.create(session=self.hub,nickname='Fifth',scoring_eligible=True,checked_in_at=timezone.now())
        for opts in ({'is_active':False},{'scoring_eligible':False},{'check_in_excluded_by_host':True},{'left_permanently_at':timezone.now()}):
            HubParticipant.objects.create(session=self.hub,nickname=str(opts),scoring_eligible=opts.pop('scoring_eligible',True),**opts)
        self.start();self.assertEqual(self.draw().status_code,200)
        seats=self.run.sets.get().seats;self.assertEqual(seats.count(),5);self.assertEqual(seats.filter(pair__isnull=True).count(),1)
        bye=seats.get(pair__isnull=True);self.assertFalse(DuelScoreEntry.objects.filter(participant=bye.participant).exists())
        self.assertEqual(self.draw().status_code,200);self.assertEqual(seats.get(pair__isnull=True).pk,bye.pk)
        self.assertIn(extra.pk,seats.values_list('participant_id',flat=True))

    def test_database_one_seat_across_both_pair_roles_and_model_foreign_consistency(self):
        self.start();self.draw();pair=self.pairs()[0];seat=pair.seats.first()
        with self.assertRaises(IntegrityError),transaction.atomic():
            DuelTournamentSeat.objects.create(tournament_set=seat.tournament_set,participant=seat.participant,pair=self.pairs()[1])
        self.assertEqual(DuelTournamentSeat.objects.count(),4)

    def test_early_pair_cannot_release_or_leak_any_solution_over_actual_transports(self):
        self.start();self.draw();self.open_pairs();a,b=self.pairs();self.answer_pair(a)
        blocked=self.post(a,'show_answers');self.assertEqual(blocked.status_code,409);self.assertEqual(blocked.json()['error']['code'],'tournament_reveal_locked')
        url=reverse('duel:match_api',args=[self.hub.code,a.game_instance_id,'spectator'])
        data=Client().get(url).json();self.assertTrue(data['unit']['tournament_waiting']);self.assertIsNone(data['match'])
        for key in ('answers','target','solution','points','round_outcome'):self.assertNotIn(key,data['unit'])
        self.assertFalse(self.api('spectator',Client()).json()['pairs'][0].get('points'))
        self.answer_pair(b);self.finish_pair(a);self.finish_pair(b);self.assertTrue(self.api().json()['complete'])

    def test_new_set_uses_new_encounters_current_roster_and_preserves_draw_without_match_win(self):
        self.start();self.draw();self.open_pairs();first=self.run.sets.get();old_ids={p.game_instance_id for p in self.pairs()}
        for pair in self.pairs():self.answer_pair(pair,tie=True)
        for pair in self.pairs():self.finish_pair(pair)
        self.assertEqual({r['outcome'] for r in self.api().json()['results']},{'draw'})
        self.parties[0].is_active=False;self.parties[0].save()
        self.assertEqual(self.draw(first.pk).status_code,200);self.assertEqual(self.run.current_set,2)
        self.assertTrue(old_ids.isdisjoint({p.game_instance_id for p in self.pairs()}))
        self.assertNotIn(self.parties[0].pk,self.run.sets.get(number=2).seats.values_list('participant_id',flat=True))
        response=self.post(first.pairs.first(),'show_answers');self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['error']['code'],'stale_tournament_set')

    def test_no_answers_finish_zero_and_next_set_waits_for_all_presentations(self):
        self.start();self.draw();self.open_pairs();a,b=self.pairs()
        from .deadlines import reconcile_due_rounds
        deadline=a.game_instance.units.get().estimation_round_state.deadline
        from datetime import timedelta
        with patch('django.utils.timezone.now',return_value=deadline+timedelta(seconds=1)):
            reconcile_due_rounds()
            response=self.post(a,'show_answers');self.assertEqual(response.status_code,200,response.content)
            self.assertEqual(self.post(a,'show_solution').status_code,200)
            self.assertEqual(self.post(a,'show_result').status_code,200)
            self.assertEqual(self.post(a,'complete').status_code,200)
            self.assertEqual(self.draw(self.run.sets.get().pk).status_code,409)
            self.finish_pair(b)
            self.assertEqual(list(DuelScoreEntry.objects.values_list('delta',flat=True)), [0,0,0,0])
            self.assertTrue(all(p.game_instance.match_status == 'completed' for p in self.pairs()))
            self.assertEqual(self.draw(self.run.sets.get().pk).status_code,200)

    def test_wrong_identity_and_host_actions_do_not_cross_pairs(self):
        self.start();self.draw();self.open_pairs();a,b=self.pairs()
        outsider=b.game_instance.participant_a_id
        self.assertEqual(self.post(a,None,party=outsider,payload={'value':'100'}).status_code,403)
        url=reverse('duel:tournament_api',args=[self.hub.code,self.run.pk,'participant'])
        self.assertEqual(self.clients[outsider].post(url,{'action':'end'},content_type='application/json').status_code,403)
        self.assertEqual(Client().get(url).status_code,403)
        self.hub.is_active = False; self.hub.save()
        response = self.post(a, None, party=a.game_instance.participant_a_id, payload={'value':'100'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'session_closed')
        self.assertFalse(DuelSubmission.objects.exists())

    def test_end_only_after_all_pairs_return_and_evening_rank_is_unchanged(self):
        self.parties[1].score_adjustment=7;self.parties[1].save();before=get_leaderboard_data(self.hub)['participants']
        self.start();self.draw();self.open_pairs()
        url=reverse('duel:tournament_api',args=[self.hub.code,self.run.pk,'host'])
        self.assertEqual(self.host.post(url,{'action':'end'},content_type='application/json').status_code,409)
        for pair in self.pairs():self.answer_pair(pair)
        for pair in self.pairs():self.finish_pair(pair)
        self.assertEqual(self.host.post(url,{'action':'end'},content_type='application/json').json()['status'],'completed')
        self.assertTrue(self.host.post(url,{'action':'end'},content_type='application/json').json()['action']['replayed'])
        self.assertEqual([(p['name'],p['rank'],p['total_with_adjustment']) for p in before],[(p['name'],p['rank'],p['total_with_adjustment']) for p in get_leaderboard_data(self.hub)['participants']])
        self.assertFalse(get_leaderboard_data(self.hub)['games'])
        routing = self.host.get(reverse('duel:encounter_routing', args=[self.hub.code, 'host'])).json()
        self.assertNotIn('result', routing)  # A pair winner is not a tournament champion.

    def test_material_failure_rolls_back_complete_draw_and_can_be_retried(self):
        self.start()
        with patch('duel.tournaments._bind',side_effect=ValueError('bad material')):self.assertEqual(self.draw().status_code,400)
        self.assertFalse(DuelTournamentSet.objects.exists());self.assertFalse(DuelGameInstance.objects.exists())
        self.run.refresh_from_db();self.assertEqual(self.run.current_set,0);self.assertIsNone(self.run.coordinator_id)
        self.assertEqual(self.draw().status_code,200)

    def test_concurrent_draws_and_conflicting_repeat_keep_one_draw(self):
        self.start();results=self.parallel([lambda:tournaments.draw_set(user=self.user,tournament_id=self.run.pk,previous_set_id=None,selection={'id':self.question.pk,'duration_seconds':60})]*2)
        self.assertEqual(sum(r.replayed for r in results),1);self.assertEqual(DuelTournamentSet.objects.count(),1)
        self.assertEqual(DuelTournamentPair.objects.count(),2);self.assertEqual(DuelTournamentSeat.objects.count(),4)
        self.assertEqual(self.draw(selection={'id':self.question.pk,'duration_seconds':61}).status_code,409)
        self.assertEqual(self.draw(previous=uuid4()).status_code, 409)
        self.assertEqual(DuelTournamentSet.objects.count(), 1)

    def test_quiz_filters_and_rejects_actual_buzzer_content(self):
        from QuizGame.models import Quiz,QuizQuestion
        quiz=Quiz.objects.create(creator=self.user,title='Mixed content')
        short=QuizQuestion.objects.create(created_by=self.user,question_text='Nenne die Stadt',question_type='short_answer',correct_answer='Berlin')
        valid=QuizQuestion.objects.create(created_by=self.user,question_text='Ist das wahr?',question_type='true_false',correct_answer='True')
        quiz.selected_questions.add(short,valid);self.step.game_key='quiz';self.step.room_code=quiz.room_code;self.step.save()
        self.assertEqual(self.start().status_code,200)
        self.assertEqual([q['id'] for q in self.api().json()['choices']],[valid.pk])
        self.assertEqual(self.draw(selection={'id':short.pk}).status_code,409);self.assertFalse(DuelTournamentSet.objects.exists())
        self.assertEqual(self.draw(selection={'id':valid.pk}).status_code,200)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=timezone.now())
        for pair in self.pairs(): self.assertEqual(self.game_post(pair, 'start').status_code, 200)
        for pair in self.pairs():
            for party in (pair.game_instance.participant_a_id, pair.game_instance.participant_b_id):
                response = self.game_post(pair, 'answer', participant=party, payload={'option_id':'True'})
                self.assertEqual(response.status_code, 200, response.content)
        for pair in self.pairs():
            for action in ('show_answers','show_solution','show_result','complete'):
                response = self.game_post(pair, action)
                self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.api().json()['complete'])

    def material_for(self, key):
        from .test_inputs import make_material as inputs
        from .test_turns import make_material as turns
        from .test_control import make_material as control
        (inputs if key in {'where', 'blackjack'} else control if key == 'bid' else turns)(self, key)
        self.step.game_key, self.step.room_code = key, self.game.room_code
        self.step.save()
        self.assertEqual(self.start().status_code, 200)
        selection = tournaments.choices(self.step)[0]['id']
        response = self.draw(selection={'id': selection})
        self.assertEqual(response.status_code, 200, response.content)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=timezone.now())
        for pair in self.pairs():
            self.assertEqual(tournaments.state_for(pair.game_instance).phase, 'prepared')
            self.assertEqual(pair.game_instance.rule_config['match'], {'mode': 'best_of_n', 'best_of': 1})

    def game_post(self, pair, action, *, participant=None, **options):
        role = 'participant' if participant else 'host'
        url = reverse('duel:'+self.step.game_key+'_'+role,
            args=[self.hub.code, pair.game_instance_id, pair.game_instance.units.get().pk])
        response = (self.clients[participant] if participant else self.host).post(url,
            {'client_action_id': str(uuid4()), 'action': action, **options}, content_type='application/json')
        return response

    def finish_turn_material(self, key):
        self.material_for(key)
        for row in self.api('spectator', Client()).json()['pairs']:
            self.assertNotIn('submitted', row)
            self.assertEqual(row['confirmed_count'], 1 if key == 'sorting_ladder' else 0)
        for pair in self.pairs():
            response = self.game_post(pair, 'start', starting_participant_id=pair.game_instance.participant_a_id)
            self.assertEqual(response.status_code, 200, response.content)
        a, b = self.pairs()
        self.assertEqual(self.game_post(a, 'record', expected_turn=1, selection={'kind':'wrong'}).status_code, 200)
        self.assertEqual(self.game_post(a, 'show_result').status_code, 409)
        self.assertEqual(self.game_post(b, 'record', expected_turn=1, selection={'kind':'wrong'}).status_code, 200)
        for pair in (a, b):
            self.assertEqual(self.game_post(pair, 'show_result').status_code, 200)
            self.assertEqual(self.game_post(pair, 'complete').status_code, 200)
        self.assertTrue(self.api().json()['complete'])

    def test_assign_existing_turn_rules_and_common_reveal_barrier(self):
        self.finish_turn_material('assign')

    def test_sorting_existing_turn_rules_and_common_reveal_barrier(self):
        self.finish_turn_material('sorting_ladder')

    def test_knowledge_existing_turn_rules_and_common_reveal_barrier(self):
        self.finish_turn_material('wer_weiss_mehr')

    def input_answers(self, pair, values, index=0):
        for party, value in zip((pair.game_instance.participant_a_id, pair.game_instance.participant_b_id), values):
            payload = {'value': value} if self.step.game_key == 'blackjack' else value
            response = self.game_post(pair, 'commit', participant=party, question_index=index, payload=payload)
            self.assertEqual(response.status_code, 200, response.content)

    def test_where_existing_map_inputs_and_manual_reveal_for_all_pairs(self):
        self.material_for('where')
        a, b = self.pairs()
        for pair in (a, b): self.assertEqual(self.game_post(pair, 'start', question_index=0).status_code, 200)
        points = [{'latitude':52.5,'longitude':13.4}, {'latitude':40.0,'longitude':10.0}]
        self.input_answers(a, points)
        self.assertEqual(self.game_post(a, 'reveal', question_index=0).status_code, 409)
        self.input_answers(b, points)
        for pair in (a, b):
            for action in ('reveal', 'evaluate', 'complete'):
                response = self.game_post(pair, action, question_index=0)
                self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.api().json()['complete'])

    def test_blackjack_barrier_tracks_question_inside_common_set(self):
        self.material_for('blackjack')
        a, b = self.pairs()
        for pair in (a, b): self.assertEqual(self.game_post(pair, 'start', question_index=0).status_code, 200)
        self.input_answers(a, [101,102]); self.assertEqual(self.game_post(a, 'show_answers', question_index=0).status_code, 409)
        self.input_answers(b, [103,104])
        for pair in (a, b):
            for action in ('show_answers','show_solution','evaluate'):
                self.assertEqual(self.game_post(pair, action, question_index=0).status_code, 200)
        for action in ('next','start'): self.assertEqual(self.game_post(a, action, question_index=0 if action=='next' else 1).status_code, 200)
        self.input_answers(a, [110,122], index=1)
        self.assertEqual(self.game_post(a, 'show_answers', question_index=1).status_code, 409)
        for action in ('next','start'): self.assertEqual(self.game_post(b, action, question_index=0 if action=='next' else 1).status_code, 200)
        self.input_answers(b, [122,110], index=1)
        for pair in (a, b):
            for action in ('show_answers','show_solution','evaluate','complete'):
                response = self.game_post(pair, action, question_index=1)
                self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.api().json()['complete'])
        self.assertEqual({r['outcome'] for r in self.api().json()['results']}, {'party_a_wins','party_b_wins'})

    def test_bid_existing_server_sequence_and_counter_complete_without_buzzer(self):
        self.material_for('bid')
        for pair in self.pairs(): self.assertEqual(self.game_post(pair, 'start').status_code, 200)
        for pair in self.pairs():
            for party in (pair.game_instance.participant_a_id, pair.game_instance.participant_b_id):
                self.assertEqual(self.game_post(pair, 'bid', participant=party, payload={'value':1}).status_code, 200)
            self.assertEqual(self.game_post(pair, 'increment', expected_counter=0).status_code, 200)
        for pair in self.pairs():
            response = self.game_post(pair, 'complete')
            self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.api().json()['complete'])

    def test_simultaneous_hub_start_and_different_current_formats_are_isolated(self):
        results = self.parallel([lambda:tournaments.start_tournament(user=self.user, session_code=self.hub.code, step_id=self.step.pk)]*2)
        self.assertEqual(results[0], results[1]); self.assertEqual(DuelTournament.objects.count(), 1)
        self.assertFalse(DuelGameInstance.objects.exists())
        self.step.refresh_from_db(); self.assertEqual(self.step.game_format, 'tournament')

    def test_snapshots_are_read_only_and_a_participant_cannot_start_a_tournament(self):
        from django.conf import settings
        from urllib.parse import urlparse, parse_qs
        denied = self.start(self.clients[self.parties[0].pk])
        self.assertEqual(denied.status_code, 302)  # Existing login_required Host boundary.
        self.assertEqual(urlparse(denied.url).path, settings.LOGIN_URL)
        self.assertEqual(parse_qs(urlparse(denied.url).query)['next'],
            [reverse('games_hub:activate_session_game', args=[self.hub.code])])
        self.assertFalse(DuelTournament.objects.exists())
        self.start(); self.draw()
        before = DuelActionReceipt.objects.count()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.api('spectator', Client()).status_code, 200)
            self.assertEqual(self.api().status_code, 200)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        self.assertEqual(DuelActionReceipt.objects.count(), before)

    def test_readiness_check_and_pure_duel_session_do_not_accept_tournament_start(self):
        from games_hub.models import HubReadinessCheck
        check = HubReadinessCheck.objects.create(session=self.hub, active=True)
        self.assertEqual(self.start().status_code, 409)
        self.assertFalse(DuelTournament.objects.exists())
        check.active = False; check.ended_at = timezone.now(); check.save()
        self.hub.session_type = 'duel'; self.hub.save()
        self.assertEqual(self.start().status_code, 409)
        self.assertFalse(DuelTournament.objects.exists())

    def test_locked_post_snapshot_retries_only_the_read_not_the_draw(self):
        from . import tournament_transport, browser
        self.start()
        original = tournament_transport.snapshot
        calls = []
        def locked_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1: raise OperationalError('database is locked')
            return original(*args, **kwargs)
        with patch('duel.tournament_transport.snapshot', side_effect=locked_once):
            response = self.draw()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(calls), 2)
        self.assertFalse(response.json()['action']['replayed'])
        self.assertEqual(DuelTournamentSet.objects.count(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        original = browser._snapshot; calls.clear()
        pair = self.pairs()[0]
        with patch('duel.browser._snapshot', side_effect=locked_once):
            response = Client().get(reverse('duel:match_api', args=[self.hub.code, pair.game_instance_id, 'spectator']))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(calls), 2)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_organization_cannot_reopen_completed_pair_or_use_a_non_coordinator(self):
        self.start(); self.draw(); self.open_pairs()
        for pair in self.pairs(): self.answer_pair(pair)
        for pair in self.pairs(): self.finish_pair(pair)
        self.run.refresh_from_db(); before = self.run.coordinator
        identity = (before.match_status, before.rule_config, list(before.units.values()))
        self.assertEqual(self.draw(self.run.sets.get().pk).status_code, 200)
        before.refresh_from_db()
        self.assertEqual((before.match_status, before.rule_config, list(before.units.values())), identity)
        from .commands import execute_action, ActorContext, HandlerResult
        from .models import ReceiptActorType, ReceiptStatus
        other = self.pairs()[1].game_instance
        with self.assertRaises(CommandRejected):
            execute_action(instance_id=other.pk, actor=ActorContext(ReceiptActorType.HOST, self.user.pk),
                client_action_id='not-coordinator', action_type='duel.tournament.end', payload={},
                handler=lambda context: HandlerResult(ReceiptStatus.APPLIED, {}), _tournament_organization=True)


class TournamentMigrationTests(TransactionTestCase):
    def test_additive_roundtrip_only_adds_four_organization_tables(self):
        ex=MigrationExecutor(connection);latest=ex.loader.graph.leaf_nodes();before=[n for n in latest if n[0]!='duel']+[('duel','0015_bidding_and_manual_rounds')]
        ex.migrate(before)
        try:
            tables=set(connection.introspection.table_names());MigrationExecutor(connection).migrate(latest)
            self.assertEqual(set(connection.introspection.table_names())-tables,{'duel_dueltournament','duel_dueltournamentset','duel_dueltournamentpair','duel_dueltournamentseat'})
            MigrationExecutor(connection).migrate(before);self.assertEqual(set(connection.introspection.table_names()),tables)
        finally:MigrationExecutor(connection).migrate(latest)


class TournamentSupabaseTests(SimpleTestCase):
    def test_app_wide_exclusion_includes_all_organization_models(self):
        config=apps.get_app_config('duel');self.assertTrue({DuelTournament,DuelTournamentSet,DuelTournamentPair,DuelTournamentSeat}.issubset(set(config.get_models())))
        for service in (sync_all_models_to_supabase,restore_all_models_from_supabase):
            with patch('games_website.services.apps.get_app_configs',return_value=[config]),patch.object(config,'get_models',side_effect=AssertionError('excluded before discovery')):
                self.assertEqual(service(stdout=StringIO(),stderr=StringIO()),(0,[]))
