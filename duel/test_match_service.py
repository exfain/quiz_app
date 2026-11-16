from unittest.mock import Mock, patch
from uuid import uuid4

from django.db import IntegrityError

from .commands import ActorContext, ActionConflict, CommandRejected, HandlerResult, execute_action
from .contracts import MatchStatus, RoundOutcome, RoundResult
from .matches import MatchConfig, MatchMode
from .match_service import start_match, complete_unit, complete_unit_in_action, finalize_match, reconcile_match_in_action, MatchIntegrityError
from .models import DuelGameInstance, DuelUnit, DuelUnitOutcome, DuelMatchOutcome, DuelScoreEntry, DuelActionReceipt, ReceiptActorType, ReceiptStatus, UnitStatus
from .test_commands import CommandFixture


class MatchFixture(CommandFixture):
    def setUp(self):
        super().setUp()
        self.host = ActorContext(ReceiptActorType.HOST, 1)  # internal confirmed identity, no public auth adapter
        self.configure()

    def configure(self, mode=MatchMode.BEST_OF_N, threshold=3):
        config = MatchConfig(mode, **{"best_of" if mode == MatchMode.BEST_OF_N else "target_score": threshold})
        self.instance.match_status = MatchStatus.IN_PROGRESS.value
        self.instance.rule_config = {"match": config.to_dict(), "game_option": "unchanged"}
        self.instance.save()
        return config

    def next_unit(self, instance=None):
        instance = instance or self.instance
        return DuelUnit.objects.create(game_instance=instance, number=instance.units.count() + 1, status=UnitStatus.ACTIVE)

    def finish(self, outcome=RoundOutcome.PARTY_A_WINS, **kwargs):
        return complete_unit(**(dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.host,
            client_action_id=str(uuid4()), result=RoundResult(outcome), expected_revision=0) | kwargs))

    def score(self, party, delta, key=None, instance=None):
        return DuelScoreEntry.objects.create(game_instance=instance or self.instance, participant=party,
                                             delta=delta, source_key=key or str(uuid4()))

    def finalize(self, **kwargs):
        return finalize_match(**(dict(instance_id=self.instance.pk, actor=self.host, client_action_id=str(uuid4())) | kwargs))

    def score_batch(self, entries, **overrides):
        def handler(context):
            for party, delta in entries:
                DuelScoreEntry.objects.create(game_instance=context.instance, participant_id=party.pk,
                    delta=delta, source_key=f"test:{context.client_action_id}:{party.pk}")
            evaluation = reconcile_match_in_action(context)
            return HandlerResult(ReceiptStatus.APPLIED, {"evaluation": evaluation.state.value, "reason": evaluation.reason})
        return execute_action(**(dict(instance_id=self.instance.pk, actor=self.actor, client_action_id=str(uuid4()),
            action_type="test.complete_score_batch", payload={"entries": [[p.pk, d] for p, d in entries]},
            handler=handler) | overrides))


class MatchServiceTests(MatchFixture):
    def test_start_requires_explicit_config_and_preserves_game_options(self):
        self.instance.match_status = MatchStatus.NOT_STARTED.value
        self.instance.rule_config = {"game_option": "unchanged"}
        self.instance.save()
        config = MatchConfig(MatchMode.BEST_OF_N, best_of=7)
        key = str(uuid4())
        result = start_match(instance_id=self.instance.pk, actor=self.host, client_action_id=key, config=config)
        replay = start_match(instance_id=self.instance.pk, actor=self.host, client_action_id=key, config=config)
        self.assertTrue(replay.replayed)
        self.assertEqual(result.state_revision, 1)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.rule_config, {"game_option": "unchanged", "match": config.to_dict()})
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertEqual(self.instance.units.count(), 1)  # start creates no units

    def test_running_config_cannot_be_changed_by_normal_handler(self):
        def handler(context):
            context.instance.rule_config["match"]["best_of"] = 7
            context.instance.save()
            return HandlerResult(ReceiptStatus.APPLIED, {})
        with self.assertRaisesMessage(RuntimeError, "configuration is immutable"):
            self.command(handler=handler)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.rule_config["match"]["best_of"], 3)
        self.assertEqual(self.instance.state_revision, 0)
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_round_win_updates_unit_once_without_awarding_points(self):
        result = self.finish()
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, UnitStatus.COMPLETED)
        self.assertEqual(self.unit.outcome.winner_id, self.party_a.pk)
        self.assertEqual(self.unit.completed_at, self.unit.outcome.completed_at)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelMatchOutcome.objects.exists())

    def test_best_of_three_completes_at_two_wins(self):
        self.finish()
        self.finish(unit_id=self.next_unit().pk)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.COMPLETED.value)
        self.assertEqual(self.instance.state_revision, 2)
        self.assertEqual(DuelMatchOutcome.objects.get().winner_id, self.party_a.pk)
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_best_of_five_and_seven_thresholds(self):
        for threshold in (5, 7):
            # Each subcase needs an independent instance/history.
            if threshold == 7:
                self.instance = self.other_instance
                self.unit = self.other_unit
                self.unit.status = UnitStatus.ACTIVE
                self.unit.save()
            self.configure(threshold=threshold)
            for index in range((threshold + 1) // 2):
                unit = self.unit if index == 0 else self.next_unit()
                self.finish(RoundOutcome.PARTY_B_WINS, unit_id=unit.pk)
                self.instance.refresh_from_db()
                self.assertEqual(self.instance.match_status == MatchStatus.COMPLETED.value, index == (threshold + 1) // 2 - 1)
            self.assertEqual(DuelMatchOutcome.objects.get(game_instance=self.instance).winner_id, self.party_b.pk)

    def test_identical_retry_and_conflicting_retry_do_not_recount(self):
        key = str(uuid4())
        first = self.finish(client_action_id=key)
        replay = self.finish(client_action_id=key)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.receipt_id, replay.receipt_id)
        with self.assertRaises(ActionConflict):
            self.finish(RoundOutcome.PARTY_B_WINS, client_action_id=key)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, 1)

    def test_same_unit_new_action_id_is_controlled_rejection(self):
        self.finish()
        result = self.finish()
        self.assertEqual(result.status, ReceiptStatus.REJECTED)
        self.assertEqual(result.state_revision, 1)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)

    def test_prepared_or_undecided_unit_remains_open(self):
        result = self.finish(RoundOutcome.UNDECIDED)
        self.assertEqual(result.status, ReceiptStatus.REJECTED)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, UnitStatus.ACTIVE)
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.unit.status = UnitStatus.PREPARED
        self.unit.save()
        self.assertEqual(self.finish().data["code"], "unit_not_active")

    def test_draw_and_no_winner_persist_without_choosing_match_rules(self):
        for outcome in (RoundOutcome.DRAW, RoundOutcome.NO_WINNER):
            unit = self.unit if outcome == RoundOutcome.DRAW else self.next_unit()
            result = self.finish(outcome, unit_id=unit.pk)
            self.assertEqual(result.data["evaluation"], "running")
            self.assertIsNone(DuelUnitOutcome.objects.get(unit=unit).winner_id)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_points_never_substitute_for_round_wins(self):
        self.score(self.party_a, 100)
        self.finish(RoundOutcome.PARTY_B_WINS)
        self.assertEqual(self.finalize().data["code"], "match_not_decided")
        self.assertFalse(DuelMatchOutcome.objects.exists())

    def test_target_reads_signed_and_zero_bookings(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        for delta in (6, 6, -2, 0):
            self.score(self.party_a, delta)
        self.score(self.party_b, -4)
        result = self.finalize()
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.assertEqual(DuelMatchOutcome.objects.get().winner_id, self.party_a.pk)
        self.assertEqual(DuelScoreEntry.objects.count(), 5)

    def test_target_batch_both_above_target_is_unresolved_even_with_different_totals(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        result = self.score_batch([(self.party_a, 11), (self.party_b, 12)])
        self.assertEqual(result.data["evaluation"], "sudden_death")
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertEqual(self.finalize().data["code"], "match_not_decided")

    def test_target_batch_evaluates_after_both_bookings_not_after_first(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score(self.party_a, 9)
        self.score(self.party_b, 9)
        result = self.score_batch([(self.party_a, 1), (self.party_b, 1)])
        self.assertEqual(result.data["evaluation"], "sudden_death")
        self.assertFalse(DuelMatchOutcome.objects.exists())

    def test_unique_target_winner_completes_in_same_score_transaction(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score_batch([(self.party_a, 12), (self.party_b, 7)])
        self.assertEqual(DuelMatchOutcome.objects.get().winner_id, self.party_a.pk)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.COMPLETED.value)
        self.assertEqual(self.instance.state_revision, 1)

    def test_finalization_retry_and_winner_conflict(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score(self.party_a, 10)
        rejected = self.finalize(expected_winner_id=self.party_b.pk)
        self.assertEqual(rejected.data["code"], "winner_conflict")
        self.assertFalse(DuelMatchOutcome.objects.exists())
        key = str(uuid4())
        first = self.finalize(client_action_id=key, expected_winner_id=self.party_a.pk)
        replay = self.finalize(client_action_id=key, expected_winner_id=self.party_a.pk)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.receipt_id, replay.receipt_id)
        with self.assertRaises(ActionConflict):
            self.finalize(client_action_id=key, expected_winner_id=self.party_b.pk)
        self.assertEqual(self.finalize().data["code"], "match_closed")
        self.assertEqual(DuelMatchOutcome.objects.count(), 1)

    def test_no_normal_action_can_change_completed_or_aborted_match(self):
        for status in (MatchStatus.COMPLETED.value, MatchStatus.ABORTED.value):
            DuelGameInstance.objects.filter(pk=self.instance.pk).update(match_status=status)
            handler = Mock(side_effect=AssertionError("Closed handler must not run"))
            result = self.command(client_action_id=str(uuid4()), handler=handler)
            self.assertEqual(result.status, ReceiptStatus.REJECTED)
            self.assertEqual(result.data["code"], "match_closed")
            handler.assert_not_called()
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_database_failure_cannot_leave_half_unit_or_match_result(self):
        self.configure(threshold=1)
        with patch.object(DuelMatchOutcome, "save", side_effect=IntegrityError("failure")):
            with self.assertRaises(IntegrityError):
                self.finish()
        self.unit.refresh_from_db()
        self.instance.refresh_from_db()
        self.assertEqual(self.unit.status, UnitStatus.ACTIVE)
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertEqual(self.instance.state_revision, 0)
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_receipt_failure_rolls_back_winner_parent_and_unit(self):
        self.configure(threshold=1)
        with patch.object(DuelActionReceipt, "save", side_effect=IntegrityError("failure")):
            with self.assertRaises(IntegrityError):
                self.finish()
        self.unit.refresh_from_db()
        self.instance.refresh_from_db()
        self.assertEqual(self.unit.status, UnitStatus.ACTIVE)
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())

    def test_different_instances_and_old_units_remain_isolated(self):
        with self.assertRaises(CommandRejected):
            self.finish(unit_id=self.other_unit.pk)
        self.finish()
        self.assertFalse(DuelUnitOutcome.objects.filter(unit__game_instance=self.other_instance).exists())
        self.other_instance.refresh_from_db()
        self.assertEqual(self.other_instance.state_revision, 0)

    def test_invalid_config_does_not_close_unit(self):
        self.instance.rule_config = {}
        self.instance.save()
        self.assertEqual(self.finish().data["code"], "invalid_configuration")
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, UnitStatus.ACTIVE)

    def test_inconsistent_completed_unit_history_is_not_guessed(self):
        self.unit.status = UnitStatus.COMPLETED
        self.unit.save()
        with self.assertRaises(MatchIntegrityError):
            self.finalize()
        self.assertFalse(DuelMatchOutcome.objects.exists())

    def test_failure_after_winner_insert_rolls_back_the_entire_completion(self):
        self.configure(threshold=1)
        original_save = DuelGameInstance.save
        def fail_status(instance, *args, **kwargs):
            if instance.match_status == MatchStatus.COMPLETED.value:
                raise IntegrityError("status write failed after winner insert")
            return original_save(instance, *args, **kwargs)
        with patch.object(DuelGameInstance, "save", fail_status):
            with self.assertRaises(IntegrityError):
                self.finish()
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.unit.refresh_from_db()
        self.instance.refresh_from_db()
        self.assertEqual(self.unit.status, UnitStatus.ACTIVE)
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertEqual(self.instance.state_revision, 0)

    def test_controlled_rejection_after_match_composition_rolls_back_all_results(self):
        self.configure(threshold=1)
        def handler(context):
            complete_unit_in_action(context, RoundResult(RoundOutcome.PARTY_A_WINS))
            return HandlerResult(ReceiptStatus.REJECTED, {"code": "later_controller_check_rejected"})
        result = execute_action(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.host,
            client_action_id="controlled-composition", action_type="test.reject", payload={}, handler=handler)
        self.assertEqual(result.status, ReceiptStatus.REJECTED)
        self.assertEqual(result.state_revision, 0)
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, UnitStatus.ACTIVE)

    def test_technical_failure_after_score_and_winner_composition_rolls_back_everything(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        def handler(context):
            DuelScoreEntry.objects.create(game_instance=context.instance, participant=self.party_a, delta=10, source_key="test-fail")
            reconcile_match_in_action(context)
            raise RuntimeError("technical failure after winner")
        with self.assertRaises(RuntimeError):
            execute_action(instance_id=self.instance.pk, actor=self.host, client_action_id="fail-composition",
                action_type="test.fail", payload={}, handler=handler)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertEqual(self.instance.state_revision, 0)
