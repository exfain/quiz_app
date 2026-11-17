"""Explicit pairings through the actual Multiplayer Hub HTTP boundary."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError
from games_hub.models import HubSession, HubGameStep, HubParticipant
from games_hub.views import get_leaderboard_data
from games_hub.lobby_join import issue_rejoin_token
from .test_hub_playable import make_material
from . import test_submissions_concurrency as concurrency
from .models import DuelGameInstance, DuelActionReceipt, DuelUnit, DuelScoreEntry
from .deadlines import reconcile_due_rounds


class EncounterTests(TransactionTestCase):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def setUp(self):
        make_material(self)
        self.host = Client(); self.host.force_login(self.user)
        self.hub = HubSession.objects.create(code='MIXED', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=3)
        self.parties = [HubParticipant.objects.create(session=self.hub, nickname=name,
            checked_in_at=timezone.now(), scoring_eligible=True) for name in ('Alpha', 'Beta', 'Gamma')]
        self.players = [Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(p)) for p in self.parties]
        self.step = HubGameStep.objects.create(session=self.hub, order=0, game_key='estimation', room_code=self.quiz.room_code)
        self.pair = [self.parties[0].pk, self.parties[2].pk]
        self.config = {'mode': 'target_score', 'target_score': 1}

    def start(self, client=None, **options):
        return (client or self.host).post(reverse('games_hub:activate_session_game', args=[self.hub.code]),
            {'action': 'start_duel', 'step_id': self.step.pk, 'game_key': 'estimation', 'room_code': self.quiz.room_code,
             'participant_ids': self.pair, 'match_config': self.config, **options}, content_type='application/json')

    def route(self, role, client=None):
        return (client or self.host).get(reverse('duel:encounter_routing', args=[self.hub.code, role]))

    def post_unit(self, action, *, client=None, role='host', **options):
        return (client or self.host).post(reverse('duel:estimation_'+role, args=[self.hub.code, self.instance.pk, self.unit.pk]),
            {**({'action': action} if role == 'host' else {}), 'client_action_id': str(uuid4()), **options}, content_type='application/json')

    def prepare(self):
        self.instance = DuelGameInstance.objects.get(hub_game_step=self.step)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=timezone.now()-timedelta(seconds=1))
        response = self.host.post(reverse('duel:match_api', args=[self.hub.code, self.instance.pk, 'host']),
            {'action': 'prepare', 'question_id': self.question.pk, 'previous_unit_id': None, 'client_action_id': str(uuid4())}, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.unit = self.instance.units.get()
        response = self.post_unit('start', duration_seconds=60)
        self.assertEqual(response.status_code, 200, response.content)

    def finish(self):
        self.prepare()
        for client, value in ((self.players[0], '100'), (self.players[2], '120')):
            response = self.post_unit('commit', client=client, role='participant', payload={'value': value})
            self.assertEqual(response.status_code, 200, response.content)
        for action in ('show_answers', 'show_solution', 'show_result', 'complete'):
            response = self.post_unit(action); self.assertEqual(response.status_code, 200, response.content)

    def test_explicit_start_freezes_pair_config_without_changing_session_or_source(self):
        before = type(self.quiz).objects.values().get(pk=self.quiz.pk)
        response = self.start(); self.assertEqual(response.status_code, 200, response.content)
        instance = DuelGameInstance.objects.get(); self.step.refresh_from_db(); self.hub.refresh_from_db()
        self.assertEqual([instance.participant_a_id, instance.participant_b_id], self.pair)
        self.assertEqual(instance.rule_config['match'], self.config)
        self.assertEqual(self.step.game_format, 'duel'); self.assertEqual(self.hub.session_type, 'multiplayer')
        self.assertEqual(self.hub.participants.count(), 3); self.assertFalse(DuelUnit.objects.exists())
        self.assertEqual(type(self.quiz).objects.values().get(pk=self.quiz.pk), before)

    def test_all_supported_game_transports_keep_pair_authorization_in_mixed_sessions(self):
        for key, model in HubSession._get_game_model_map().items():
            with self.subTest(game_key=key):
                # Zielansage has required explicit duration and is a regular
                # Multiplayer team game, not the separate duel bidding game.
                game = model.objects.create(creator=self.user, title='Mixed '+key,
                    **({'answer_seconds':60,'scoring_mode':'simple'} if key == 'target' else {}))
                self.step.game_key = key; self.step.room_code = game.room_code
                self.step.game_format = 'standard'; self.step.intro_started_at = self.step.intro_ends_at = None
                self.step.save()
                response = self.start(game_key=key, room_code=game.room_code)
                if key == 'target':
                    self.assertEqual(response.status_code,409,response.content)
                    self.assertIn('nicht für 2-Spieler-Sessions',response.json()['error'])
                    self.assertFalse(DuelGameInstance.objects.filter(hub_game_step=self.step).exists())
                    self.step.refresh_from_db()
                    self.assertEqual(self.step.game_format,'standard')
                    continue
                self.assertEqual(response.status_code, 200, response.content)
                instance = DuelGameInstance.objects.get(hub_game_step=self.step)
                api_name = 'match_api' if key == 'estimation' else key+'_match_api'
                for role, client in (('host', self.host), ('participant', self.players[0]), ('spectator', Client())):
                    url = reverse('duel:'+api_name, args=[self.hub.code, instance.pk, role])
                    self.assertEqual(client.get(url).status_code, 200, (key, role))
                self.assertEqual(self.players[1].get(reverse('duel:'+api_name, args=[self.hub.code, instance.pk, 'participant'])).status_code, 403)
                instance.action_receipts.all().delete(); instance.delete()

    def test_foreign_host_and_foreign_slot_are_rejected(self):
        from django.contrib.auth.models import User
        user = User.objects.create_user('foreign-host', password='pw', is_staff=True)
        client = Client(); client.force_login(user)
        self.assertEqual(self.start(client).status_code, 403)
        other = HubSession.objects.create(code='OTHER', creator=self.user)
        step = HubGameStep.objects.create(session=other, order=0, game_key='estimation', room_code=self.quiz.room_code)
        self.assertEqual(self.start(step_id=step.pk).status_code, 400)
        self.assertFalse(DuelGameInstance.objects.exists())

    def test_invalid_pairings_and_config_remain_retryable(self):
        foreign = HubParticipant.objects.create(session=HubSession.objects.create(code='FOREIGN'), nickname='Other')
        for pair in ([self.pair[0]]*2, [self.pair[0], foreign.pk], [True, self.pair[1]], [], None):
            self.assertEqual(self.start(participant_ids=pair).status_code, 409)
        for field, value in (('is_active', False), ('check_in_excluded_by_host', True), ('left_permanently_at', timezone.now())):
            party = self.parties[2]; old = getattr(party, field); setattr(party, field, value); party.save()
            self.assertEqual(self.start().status_code, 409); setattr(party, field, old); party.save()
        self.assertEqual(self.start(match_config={}).status_code, 409)
        self.assertFalse(DuelGameInstance.objects.exists()); self.step.refresh_from_db(); self.assertEqual(self.step.game_format, 'standard')
        self.assertEqual(self.start().status_code, 200)

    def test_repeated_and_parallel_starts_use_one_receipt_and_one_pair(self):
        clients = [Client(), Client()]
        for c in clients: c.force_login(self.user)
        results = self.parallel([lambda c=c: self.start(c) for c in clients])
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertEqual(results[0].json(), results[1].json()); self.assertEqual(self.start().json(), results[0].json())
        self.assertEqual(DuelGameInstance.objects.count(), 1); self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.assertEqual(self.start(participant_ids=self.pair[::-1]).status_code, 409)
        self.assertEqual(self.start(match_config={'mode':'best_of_n','best_of':7}).status_code, 409)

    def test_receipt_failure_rolls_back_pair_slot_config_and_intro(self):
        with patch.object(DuelActionReceipt, 'save', side_effect=IntegrityError('receipt failure')):
            self.assertEqual(self.start().status_code, 503)
        self.step.refresh_from_db(); self.assertEqual(self.step.game_format, 'standard')
        self.assertEqual(self.step.duel_match_config, {}); self.assertIsNone(self.step.intro_started_at)
        self.assertFalse(DuelGameInstance.objects.exists()); self.assertEqual(self.start().status_code, 200)

    def test_only_selected_token_identities_route_and_play(self):
        self.assertEqual(self.start(self.players[0]).status_code, 302)
        self.assertEqual(self.start().status_code, 200); instance = DuelGameInstance.objects.get()
        for i in (0, 2): self.assertIn(str(instance.pk), self.route('participant', self.players[i]).json()['url'])
        self.assertIsNone(self.route('participant', self.players[1]).json()['url'])
        self.assertTrue(self.route('participant', self.players[1]).json()['spectator_url'])
        self.assertIsNone(self.route('participant', Client()).json().get('url')); self.assertEqual(self.route('participant', Client()).status_code, 403)
        url = reverse('duel:match_api', args=[self.hub.code, instance.pk, 'participant'])
        self.assertEqual(self.players[1].get(url).status_code, 403)
        self.assertEqual(self.players[1].get(url.replace('/participant/', '/host/')).status_code, 403)
        self.assertEqual(Client().get(url.replace('/participant/', '/spectator/')).status_code, 200)

    def test_deadline_worker_also_closes_mixed_encounters_without_read_side_effects(self):
        self.start(); self.prepare(); state = self.unit.estimation_round_state
        with patch('django.utils.timezone.now', return_value=state.deadline):
            self.assertEqual(self.route('spectator', Client()).status_code, 200)
            state.refresh_from_db(); self.assertEqual(state.phase, 'answering')
            self.assertEqual(reconcile_due_rounds(), 1); self.assertEqual(reconcile_due_rounds(), 0)
        state.refresh_from_db(); self.assertEqual(state.phase, 'awaiting_reveal'); self.assertFalse(DuelScoreEntry.objects.exists())

    def test_result_stays_out_of_evening_ranking_and_next_normal_game_uses_everyone(self):
        self.parties[1].score_adjustment = 7; self.parties[1].save()
        before = get_leaderboard_data(self.hub)
        self.assertEqual(self.start().status_code, 200)
        blocked = self.host.post(reverse('games_hub:activate_session_game', args=[self.hub.code]), {'game_key':'estimation','room_code':self.quiz.room_code}, content_type='application/json')
        self.assertFalse(blocked.json()['success']); self.quiz.refresh_from_db(); self.assertEqual(self.quiz.status, 'waiting')
        self.finish(); self.instance.refresh_from_db(); self.assertEqual(self.instance.match_status, 'completed')
        data = self.route('host').json(); self.assertEqual(data['result']['winner'], 'Alpha'); self.assertIsNone(data['url'])
        after = get_leaderboard_data(self.hub)
        self.assertEqual([p['total_score'] for p in before['participants']], [p['total_score'] for p in after['participants']])
        self.assertEqual([(p['name'], p['rank'], p['total_with_adjustment']) for p in before['participants']],
                         [(p['name'], p['rank'], p['total_with_adjustment']) for p in after['participants']])
        self.assertEqual(after['participants'][0]['name'], 'Beta')  # Duel winner Alpha does not overwrite the evening leader.
        self.assertEqual(after['games'], [])
        next_step = HubGameStep.objects.create(session=self.hub, order=1, game_key='estimation', room_code=self.quiz.room_code)
        response = self.host.post(reverse('games_hub:activate_session_game', args=[self.hub.code]), {'game_key':'estimation','room_code':self.quiz.room_code}, content_type='application/json')
        self.assertTrue(response.json()['success'], response.content)
        self.hub.refresh_from_db(); self.assertEqual(self.hub.current_step_index, 1)
        self.assertEqual(set(next_step.participant_snapshots.values_list('participant_id', flat=True)), {p.pk for p in self.parties})
        self.assertEqual(DuelGameInstance.objects.count(), 1)

    def test_running_multiplayer_cannot_be_hijacked_and_normal_start_remains_default(self):
        response = self.host.post(reverse('games_hub:activate_session_game', args=[self.hub.code]), {'game_key':'estimation','room_code':self.quiz.room_code}, content_type='application/json')
        self.assertTrue(response.json()['success']); self.assertFalse(DuelGameInstance.objects.exists())
        self.assertEqual(self.start().status_code, 409)

    def test_reused_source_cannot_leak_its_old_multiplayer_state_into_duel_spectator(self):
        from games_hub.spectator import build_spectator_state
        self.quiz.status = 'completed'; self.quiz.started_at = timezone.now()
        self.quiz.save()
        self.assertEqual(self.start().status_code, 200)
        self.assertIsNone(build_spectator_state(self.hub)['game'])
        self.assertIsNotNone(self.route('spectator', Client()).json()['url'])

