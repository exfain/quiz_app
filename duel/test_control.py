"""Actual planned Hub starts and role-checked manual/bid HTTP actions."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection, transaction, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, SimpleTestCase
from django.utils import timezone
from django.urls import reverse
from buzzer.models import BuzzerGame
from host_points.models import HostPointsGame
from games_hub.models import HubGameStep
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from . import test_oral as reference
from . import test_submissions_concurrency as concurrency
from .models import BidGame, BidTask, DuelControlRound, DuelSubmission, DuelScoreEntry, DuelUnitOutcome, DuelDecision, DuelBuzz, ReceiptActorType, ReceiptStatus
from .commands import ActorContext, ActionConflict
from .controllers import control
from .deadlines import reconcile_due_rounds
from .test_deadline_worker import worker_environment
import subprocess
from django.test import Client
from games_hub.models import HubParticipant
from games_hub.lobby_join import issue_rejoin_token


def make_material(case, key):
    if not hasattr(case, 'user'): case.user = User.objects.create_superuser('control-host', '', 'pw')
    model = {'bid': BidGame, 'buzzer': BuzzerGame, 'host_points': HostPointsGame}[key]
    case.game = model.objects.create(title=key+' test', creator=case.user)
    if key == 'bid': case.question = BidTask.objects.create(game=case.game, text='Nenne europäische Länder.', bid_seconds=120, answer_seconds=120)
    else: case.question = type('Selection', (), {'pk': 1})()


class ControlFixture(reference.OralFixture):
    key = 'bid'

    def setUp(self):
        with patch('duel.test_oral.make_material', make_material): super().setUp()

    def start(self):
        self.prepare(); response = self.post({'action': 'start'}); self.assertEqual(response.status_code, 200, response.content)
        return DuelControlRound.objects.get(unit=self.unit)

    def bid(self, side=0, value=2, id=None): return self.post({'action': 'bid', 'payload': {'value': value}}, side, id)

    def increment(self, count=None, id=None):
        if count is None: count = DuelControlRound.objects.get(unit=self.unit).counter
        return self.post({'action': 'increment', 'expected_counter': count}, key=id)


class BidRulesTests(ControlFixture):
    def test_no_bids_finish_without_points_or_winner(self):
        state = self.start()
        with patch('django.utils.timezone.now', return_value=state.bid_deadline): self.assertEqual(reconcile_due_rounds(), 1)
        self.assertEqual(self.snapshot()['bids'], {'a': None, 'b': None})
        self.assertEqual(self.snapshot()['result_reason'], 'no_bids'); self.assertEqual(list(DuelScoreEntry.objects.values_list('delta',flat=True)), [0,0]); self.assertEqual(self.post({'action':'complete'}).status_code,200)

    def test_registered_duel_only_type_and_authenticated_role_projections(self):
        self.assertEqual(HubGameStep.DUEL_ONLY_GAME_KEYS, {'bid'})
        self.start(); third = HubParticipant.objects.create(session=self.hub, nickname='Third')
        stranger = Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(third))
        self.assertEqual(stranger.get(self.url('participant')).status_code, 403)
        self.assertEqual(self.spectator.get(self.url('host')).status_code, 403)
        self.assertEqual(self.players[0].get(self.url('host')).status_code, 403)
        self.assertEqual(self.players[0].get(self.url('participant')+'?reveal=true').status_code, 400)
        self.assertEqual(self.spectator.post(self.url('spectator'), {'action': 'start', 'client_action_id': 'forged'}, content_type='application/json').status_code, 403)
        self.hub.session_type = 'multiplayer'; self.hub.save(update_fields=['session_type'])
        self.assertEqual(self.hub_start().status_code, 409)

    def test_admin_content_requires_ownership_and_explicit_positive_durations(self):
        other = User.objects.create_user('other-admin', password='pw', is_staff=True)
        client = Client(); client.force_login(other)
        url = reverse('admin_dashboard:bid_management')+f'?game={self.game.pk}'
        self.assertEqual(client.get(url).status_code, 404)
        url = reverse('admin_dashboard:bid_management')+f'?game={self.game.pk}'
        bad = self.host.post(url, {'action': 'save_task', 'text': 'Test', 'bid_seconds': 0, 'answer_seconds': 3, 'is_active': 'on'})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.game.tasks.count(), 1)
        ok = self.host.post(url, {'action': 'save_task', 'text': 'New task', 'bid_seconds': 3, 'answer_seconds': 5, 'is_active': 'on'})
        self.assertEqual(ok.status_code, 302); self.assertEqual(self.game.tasks.count(), 2)

    def test_fresh_worker_recovers_expired_answer_phase_and_restart_is_idempotent(self):
        self.prepare(); clock = timezone.now()-timedelta(minutes=3)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=clock-timedelta(seconds=1))
        with patch('django.utils.timezone.now', return_value=clock): self.assertEqual(self.post({'action': 'start'}).status_code, 200)
        with patch('django.utils.timezone.now', return_value=clock+timedelta(seconds=1)): self.bid(0, 2); self.bid(1, 1)
        with worker_environment() as (command, options):
            first = subprocess.run(command+['--once'], capture_output=True, text=True, timeout=35, **options)
            self.assertEqual(first.returncode, 0, first.stderr); self.assertEqual(first.stdout.strip(), '1')
            before = self.snapshot()
            second = subprocess.run(command+['--once'], capture_output=True, text=True, timeout=35, **options)
            self.assertEqual(second.returncode, 0, second.stderr); self.assertEqual(second.stdout.strip(), '0')
        self.assertEqual(self.snapshot()['winner_id'], self.parties[1].pk); self.assertEqual(self.snapshot()['state_revision'], before['state_revision'])

    def test_two_hidden_bids_choose_higher_and_start_server_clock(self):
        state = self.start(); self.bid(0, 5)
        for role in ('spectator', 'participant', 'host'):
            public = self.snapshot(role); self.assertNotIn('bids', public); self.assertNotIn('winning_bid', public)
        self.assertEqual(self.snapshot('participant')['own_bid'], 5)
        self.assertEqual(self.bid(1, 3).status_code, 200)
        state.refresh_from_db(); self.assertEqual(state.right_participant_id, self.parties[0].pk)
        self.assertEqual(state.answer_deadline-state.answer_started_at, timedelta(seconds=120))
        self.assertEqual(self.snapshot()['winning_bid'], 5)

    def test_equal_bids_use_server_sequence_even_with_identical_timestamps(self):
        state = self.start()
        with patch('django.utils.timezone.now', return_value=state.bid_started_at+timedelta(seconds=1)):
            self.bid(1, 2); self.bid(0, 2)
        self.assertEqual(self.snapshot()['right_participant_id'], self.parties[1].pk)
        self.assertEqual(list(DuelSubmission.objects.order_by('acceptance_sequence').values_list('acceptance_sequence', flat=True)), [1, 2])

    def test_counter_goal_immediately_books_one_round_and_point(self):
        self.start(); self.bid(0, 2); self.bid(1, 1)
        self.assertEqual(self.increment().status_code, 200); self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertEqual(self.increment().status_code, 200)
        self.assertEqual(self.snapshot()['winner_id'], self.parties[0].pk)
        self.assertEqual(self.unit.outcome.winner_id, self.parties[0].pk)
        self.assertEqual(sum(DuelScoreEntry.objects.values_list('delta', flat=True)), 1)
        self.assertEqual(self.increment().status_code, 409)

    def test_increment_retry_and_new_id_with_same_expected_counter_do_not_double_count(self):
        self.start(); self.bid(0, 4); self.bid(1, 1)
        self.assertEqual(self.increment(0, 'same').status_code, 200)
        self.assertTrue(self.increment(0, 'same').json()['action']['replayed'])
        self.assertEqual(self.increment(1, 'same').status_code, 409)
        self.assertEqual(self.increment(0).status_code, 409)
        self.assertEqual(self.snapshot()['counter'], 1)

    def test_timeout_awards_opponent_and_exact_deadline_cannot_increment_to_win(self):
        self.start(); self.bid(0, 1); self.bid(1, 2)
        state = DuelControlRound.objects.get(unit=self.unit)
        with patch('django.utils.timezone.now', return_value=state.answer_deadline):
            self.assertEqual(self.increment(0).status_code, 200)
            self.assertEqual(reconcile_due_rounds(), 0)
        self.assertEqual(self.snapshot()['counter'], 0); self.assertEqual(self.snapshot()['winner_id'], self.parties[0].pk)

    def test_worker_recovers_expired_answer_clock_without_clients(self):
        self.start(); self.bid(0, 2); self.bid(1, 1); state = DuelControlRound.objects.get(unit=self.unit)
        with patch('django.utils.timezone.now', return_value=state.answer_deadline):
            self.assertEqual(reconcile_due_rounds(), 1); self.assertEqual(reconcile_due_rounds(), 0)
        self.assertEqual(self.snapshot()['winner_id'], self.parties[1].pk)

    def test_single_bid_gets_answer_right_without_automatic_points(self):
        state = self.start(); self.bid(0, 2)
        with patch('django.utils.timezone.now', return_value=state.bid_deadline):
            self.assertEqual(self.bid(1, 3).status_code, 409); self.assertEqual(reconcile_due_rounds(), 1)
        result = self.snapshot(); self.assertEqual(result['phase'], 'answering')
        self.assertEqual(result['right_participant_id'], self.parties[0].pk); self.assertIsNotNone(result['deadline'])
        self.assertFalse(DuelScoreEntry.objects.exists()); self.assertEqual(self.post({'action': 'complete'}).status_code, 409)

    def test_invalid_bid_and_forged_fields_cannot_write(self):
        self.start()
        for value in (0, -1, 1.5, True, '2', None, {}, []): self.assertEqual(self.bid(0, value).status_code, 400)
        self.assertEqual(self.post({'action': 'bid', 'payload': {'value': 2}, 'acceptance_sequence': 1}, 0).status_code, 400)
        self.assertEqual(self.post({'action': 'increment', 'expected_counter': 0}, 0).status_code, 403)
        self.assertFalse(DuelSubmission.objects.exists())

    def test_bid_retry_conflict_and_new_id_never_replace_committed_value(self):
        self.start(); self.bid(0, 2, 'original')
        self.assertTrue(self.bid(0, 2, 'original').json()['action']['replayed'])
        self.assertEqual(self.bid(0, 3, 'original').status_code, 409); self.assertEqual(self.bid(0, 3).status_code, 409)
        self.assertEqual(DuelSubmission.objects.get().payload, {'value': 2})

    def test_task_snapshot_survives_content_edit(self):
        self.prepare(); self.question.text = 'Changed'; self.question.answer_seconds = 1; self.question.save()
        state = DuelControlRound.objects.get(unit=self.unit)
        self.assertEqual(state.material['text'], 'Nenne europäische Länder.'); self.assertEqual(state.material['answer_seconds'], 120)

    def test_result_failure_rolls_back_counter_points_and_outcome(self):
        self.start(); self.bid(0, 1); self.bid(1, 2); self.increment()
        with patch('duel.controllers.control.complete_unit_in_action', side_effect=IntegrityError('injected')):
            with self.assertRaises(IntegrityError):
                control.host_action(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id='fail', action='increment', expected_counter=1)
        self.assertEqual(self.snapshot()['counter'], 1); self.assertFalse(DuelScoreEntry.objects.exists()); self.assertFalse(DuelUnitOutcome.objects.exists())


class BuzzerAdapterTests(ControlFixture):
    key = 'buzzer'

    def test_wrong_blocks_only_buzzing_party_and_other_can_win(self):
        self.start(); self.assertEqual(self.snapshot()['phase'], 'ready'); self.assertEqual(self.buzz().status_code, 409)
        self.assertEqual(self.post({'action': 'open'}).status_code, 200); self.assertEqual(self.buzz().status_code, 200)
        self.assertTrue(self.snapshot('participant')['own_has_right']); self.assertFalse(self.snapshot('participant')['own_blocked'])
        buzz = self.snapshot()['buzz_id']; self.assertEqual(self.post({'action': 'judge', 'buzz_id': buzz, 'verdict': 'incorrect'}).status_code, 200)
        self.assertTrue(self.snapshot('participant')['own_blocked'])
        self.assertFalse(self.snapshot('participant')['own_has_right'])
        self.assertEqual(self.snapshot()['blocked'], [self.parties[0].pk])
        self.assertEqual(self.buzz().status_code, 409); self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertEqual(self.buzz(1).status_code, 200); buzz = self.snapshot()['buzz_id']
        self.assertEqual(self.post({'action': 'judge', 'buzz_id': buzz, 'verdict': 'correct'}).status_code, 200)
        self.assertEqual(self.snapshot()['winner_id'], self.parties[1].pk); self.assertEqual(DuelDecision.objects.count(), 2)

    def test_source_points_setting_and_best_of_round_win_stay_separate(self):
        self.game.points_per_correct = 3; self.game.save(); self.start(); self.post({'action': 'open'}); self.buzz()
        self.post({'action': 'judge', 'buzz_id': self.snapshot()['buzz_id'], 'verdict': 'correct'})
        tally = self.host.get(self.url('host', match=True)).json()['match']
        self.assertEqual(tally['party_a']['score'], 3); self.assertEqual(tally['party_a']['round_wins'], 1)

    def test_both_wrong_require_explicit_end_and_never_award_opponent_points(self):
        self.start(); self.post({'action': 'open'})
        for side in (0, 1):
            self.buzz(side); self.post({'action': 'judge', 'buzz_id': self.snapshot()['buzz_id'], 'verdict': 'incorrect'})
        self.assertEqual(self.snapshot()['phase'], 'blocked'); self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertEqual(self.post({'action': 'end_round'}).status_code, 200)
        self.assertEqual(self.unit.outcome.outcome, 'no_winner'); self.assertEqual(sum(DuelScoreEntry.objects.values_list('delta', flat=True)), 0)

    def test_normal_two_party_multiplayer_session_uses_original_buzzer(self):
        # Start with a planned normal slot, not a running duel whose session
        # type was changed behind the lifecycle guards.
        self.instance.action_receipts.all().delete(); self.instance.delete()
        self.step.game_format = 'standard'; self.step.intro_started_at = self.step.intro_ends_at = None
        self.step.save(update_fields=['game_format', 'intro_started_at', 'intro_ends_at'])
        self.hub.session_type = 'multiplayer'; self.hub.started_at = timezone.now(); self.hub.save()
        self.assertEqual(self.hub_start().status_code, 200)
        self.game.refresh_from_db(); self.assertEqual(self.game.status, 'active')


class HostPointsAdapterTests(ControlFixture):
    key = 'host_points'
    config = {'mode': 'target_score', 'target_score': 3}

    def adjust(self, delta, side=0, count=None, id=None):
        if count is None: count = DuelControlRound.objects.get(unit=self.unit).counter
        return self.post({'action': 'adjust', 'participant_id': self.parties[side].pk, 'delta': delta, 'expected_counter': count}, key=id)

    def test_positive_negative_adjustments_use_journal_and_target_closes_without_round_winner(self):
        self.start(); self.assertEqual(self.adjust(2).status_code, 200); self.assertEqual(self.adjust(-1).status_code, 200)
        self.assertEqual(self.adjust(2).status_code, 200)
        self.instance.refresh_from_db(); self.assertEqual(self.instance.match_status, 'completed')
        self.assertEqual(self.instance.match_outcome.winner_id, self.parties[0].pk)
        self.assertEqual(self.unit.outcome.outcome, 'no_winner'); self.assertEqual(self.post({'action': 'complete'}).status_code, 200)
        self.assertEqual(list(DuelScoreEntry.objects.order_by('created_at').values_list('delta', flat=True)), [2, -1, 2])

    def test_repeated_adjustment_and_invalid_values_do_not_change_points(self):
        self.start(); self.adjust(1, count=0, id='same')
        self.assertTrue(self.adjust(1, count=0, id='same').json()['action']['replayed'])
        self.assertEqual(self.adjust(1, count=0).status_code, 409)
        self.assertEqual(self.adjust(0).status_code, 409); self.assertEqual(self.adjust(True).status_code, 409)
        self.assertEqual(DuelScoreEntry.objects.count(), 1)

    def test_best_of_is_not_silently_enabled(self):
        self.step.duel_match_config = {'mode': 'best_of_n', 'best_of': 7}; self.step.save()
        response = self.host.post(reverse('duel:session_api', args=[self.hub.code]), {'action': 'save_plan', 'configs': {str(self.step.pk): self.step.duel_match_config}}, content_type='application/json')
        self.assertIn(response.status_code, (400, 409))

    def test_two_participant_multiplayer_still_starts_the_original_game(self):
        self.instance.action_receipts.all().delete(); self.instance.delete()
        self.step.game_format = 'standard'; self.step.intro_started_at = self.step.intro_ends_at = None
        self.step.save(update_fields=['game_format', 'intro_started_at', 'intro_ends_at'])
        self.hub.session_type = 'multiplayer'; self.hub.started_at = timezone.now(); self.hub.save()
        self.assertEqual(self.hub_start().status_code, 200)
        self.game.refresh_from_db(); self.assertEqual(self.game.status, 'active')


class BuzzerConcurrencyTests(ControlFixture):
    key = 'buzzer'
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def test_two_simultaneous_buzzes_assign_exactly_one_right(self):
        self.start(); self.post({'action': 'open'})
        results = self.parallel([lambda side=side: control.buzz(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.actor(side), client_action_id=f'buzz-{side}') for side in (0, 1)])
        self.assertEqual(sum(r.status == ReceiptStatus.APPLIED for r in results), 1); self.assertEqual(DuelBuzz.objects.count(), 1)


class ControlConcurrencyTests(ControlFixture):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def test_parallel_equal_bids_and_duplicate_counters_have_one_authoritative_order(self):
        self.start()
        results = self.parallel([lambda side=side: control.commit_bid(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.actor(side), client_action_id=f'bid-{side}', payload={'value': 2}) for side in (0, 1)])
        self.assertTrue(all(r.status == ReceiptStatus.APPLIED for r in results))
        first = DuelSubmission.objects.order_by('acceptance_sequence').first(); self.assertEqual(self.snapshot()['right_participant_id'], first.participant_id)
        results = self.parallel([lambda i=i: control.host_action(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id=f'count-{i}', action='increment', expected_counter=0) for i in (0, 1)])
        self.assertEqual(sum(r.status == ReceiptStatus.APPLIED for r in results), 1); self.assertEqual(self.snapshot()['counter'], 1)


class ControlMigrationTests(TransactionTestCase):
    def test_round_trip_only_adds_local_control_content_and_state(self):
        executor = MigrationExecutor(connection); latest = executor.loader.graph.leaf_nodes(); others = [n for n in latest if n[0] != 'duel']
        before = others+[('duel', '0014_device_input_rounds')]
        executor.migrate(before)
        try:
            tables = set(connection.introspection.table_names())
            MigrationExecutor(connection).migrate(others+[('duel', '0015_bidding_and_manual_rounds')])
            self.assertEqual(set(connection.introspection.table_names())-tables, {'duel_bidgame', 'duel_bidtask', 'duel_duelcontrolround'})
            MigrationExecutor(connection).migrate(before); self.assertEqual(set(connection.introspection.table_names()), tables)
        finally: MigrationExecutor(connection).migrate(latest)


class ControlSyncTests(SimpleTestCase):
    def test_content_and_state_are_skipped_before_sync_discovery(self):
        config = apps.get_app_config('duel')
        self.assertTrue({BidGame, BidTask, DuelControlRound}.issubset(set(config.get_models())))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with patch('games_website.services.apps.get_app_configs', return_value=[config]), patch.object(config, 'get_models', side_effect=AssertionError('must not discover duel')):
                self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))
