from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F
from django.test import TransactionTestCase
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .commands import (
    ActionConflict, ActorContext, CommandRejected, HandlerResult, execute_action, request_fingerprint,
)
from .models import (
    DuelActionReceipt, DuelGameInstance, DuelScoreEntry, DuelUnit,
    ReceiptActorType, ReceiptStatus, UnitStatus,
)


class CommandFixture(TransactionTestCase):
    def setUp(self):
        self.hub = HubSession.objects.create(code="COMMAND-HUB")
        self.step = HubGameStep.objects.create(session=self.hub, order=0, game_key="estimation")
        self.party_a = HubParticipant.objects.create(session=self.hub, nickname="Party A")
        self.party_b = HubParticipant.objects.create(session=self.hub, nickname="Party B")
        self.third = HubParticipant.objects.create(session=self.hub, nickname="Third")
        values = dict(hub_session=self.hub, hub_game_step=self.step, game_key="estimation",
                      participant_a=self.party_a, participant_b=self.party_b)
        self.instance = DuelGameInstance.objects.create(**values)
        self.other_instance = DuelGameInstance.objects.create(**values)
        self.unit = DuelUnit.objects.create(game_instance=self.instance, number=1, status=UnitStatus.ACTIVE)
        self.other_unit = DuelUnit.objects.create(game_instance=self.other_instance, number=1)
        self.actor = ActorContext(ReceiptActorType.PARTICIPANT, self.party_a.pk)
        self.action_id = str(uuid4())
        self.calls = 0

    def increment(self, context):
        self.calls += 1
        self.assertTrue(connection.in_atomic_block)
        count = context.instance.rule_config.get("test_count", 0) + 1
        context.instance.rule_config = {"test_count": count}
        context.instance.save(using=context.using, update_fields=["rule_config"])
        return HandlerResult(ReceiptStatus.APPLIED, {"code": "accepted", "count": count})

    def point(self, context):
        result = self.increment(context)
        entry = DuelScoreEntry(
            game_instance=context.instance, unit=context.unit, participant=context.participant,
            delta=1, source_key=f"test:{context.client_action_id}",
        )
        entry.full_clean()
        entry.save(using=context.using)
        return result

    def command(self, **overrides):
        args = dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.actor,
                    client_action_id=self.action_id, action_type="test.increment", payload={"increment": 1},
                    expected_revision=0, handler=self.increment)
        args.update(overrides)
        return execute_action(**args)

    def assert_empty_state(self):
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, 0)
        self.assertEqual(self.instance.rule_config, {})
        self.assertFalse(DuelActionReceipt.objects.exists())
        self.assertFalse(DuelScoreEntry.objects.exists())


class DuelCommandTests(CommandFixture):
    def test_first_action_applies_once_with_receipt_points_and_revision(self):
        result = self.command(handler=self.point)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.assertEqual(result.state_revision, 1)
        self.assertFalse(result.replayed)
        self.assertEqual(result.data, {"code": "accepted", "count": 1})
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, 1)
        self.assertEqual(DuelScoreEntry.objects.get().delta, 1)
        receipt = DuelActionReceipt.objects.get()
        self.assertEqual(receipt.pk, result.receipt_id)
        self.assertEqual(receipt.actor_key, str(self.party_a.pk))
        self.assertEqual(receipt.status, "applied")
        self.assertEqual(receipt.result, {"data": result.data, "state_revision": 1})

    def test_identical_retry_returns_original_without_processing_or_points(self):
        original = self.command(handler=self.point)
        before = DuelActionReceipt.objects.values().get()
        retry = self.command(handler=Mock(side_effect=AssertionError("must not run")))
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.receipt_id, original.receipt_id)
        self.assertEqual(retry.data, original.data)
        self.assertEqual(retry.state_revision, 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(DuelActionReceipt.objects.values().get(), before)
        self.assertEqual(DuelScoreEntry.objects.count(), 1)

    def test_different_payload_with_same_id_conflicts(self):
        self.command()
        before = DuelActionReceipt.objects.values().get()
        with self.assertRaises(ActionConflict) as caught:
            self.command(payload={"increment": 2})
        self.assertEqual(caught.exception.code, "action_id_conflict")
        self.assertEqual(self.calls, 1)
        self.assertEqual(DuelActionReceipt.objects.values().get(), before)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, 1)

    def test_different_action_type_with_same_id_conflicts(self):
        self.command()
        with self.assertRaises(ActionConflict):
            self.command(action_type="test.other")
        self.assertEqual(self.calls, 1)

    def test_different_unit_with_same_id_conflicts(self):
        self.command()
        unit = DuelUnit.objects.create(game_instance=self.instance, number=2)
        with self.assertRaises(ActionConflict):
            self.command(unit_id=unit.pk)
        self.assertEqual(self.calls, 1)

    def test_object_key_order_does_not_change_fingerprint_or_retry(self):
        first = self.command(payload={"b": {"d": 4, "c": 3}, "a": 1})
        retry = self.command(payload={"a": 1, "b": {"c": 3, "d": 4}})
        self.assertTrue(retry.replayed)
        self.assertEqual(first.receipt_id, retry.receipt_id)
        self.assertEqual(self.calls, 1)

    def test_distinct_action_ids_are_independent(self):
        self.command()
        second = self.command(client_action_id=str(uuid4()))
        self.assertEqual(second.state_revision, 2)
        self.assertEqual(self.calls, 2)
        self.assertEqual(DuelActionReceipt.objects.count(), 2)

    def test_same_action_id_for_both_participants_does_not_collide(self):
        self.command(handler=self.point)
        second = self.command(actor=ActorContext(ReceiptActorType.PARTICIPANT, self.party_b.pk), handler=self.point)
        self.assertEqual(second.state_revision, 2)
        self.assertEqual(DuelActionReceipt.objects.count(), 2)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)

    def test_same_action_id_for_host_and_participant_does_not_collide(self):
        self.command()
        # This is a trusted internal assertion, not a host authentication API.
        second = self.command(actor=ActorContext(ReceiptActorType.HOST, self.party_a.pk))
        self.assertEqual(second.state_revision, 2)
        self.assertEqual(DuelActionReceipt.objects.count(), 2)

    def test_same_action_id_in_different_instances_does_not_collide(self):
        first = self.command()
        second = self.command(instance_id=self.other_instance.pk, unit_id=self.other_unit.pk)
        self.assertNotEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(second.state_revision, 1)
        self.assertEqual(self.calls, 2)

    def test_third_session_participant_cannot_access_encounter_or_receipt(self):
        self.command()
        with self.assertRaises(CommandRejected) as caught:
            self.command(actor=ActorContext(ReceiptActorType.PARTICIPANT, self.third.pk))
        self.assertEqual(caught.exception.code, "actor_not_allowed")
        self.assertEqual(self.calls, 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_missing_participant_rejected(self):
        with self.assertRaises(CommandRejected) as caught:
            self.command(actor=ActorContext(ReceiptActorType.PARTICIPANT, 99999999))
        self.assertEqual(caught.exception.code, "actor_not_found")
        self.assert_empty_state()

    def test_same_nickname_from_other_session_is_not_identity(self):
        hub = HubSession.objects.create(code="FOREIGN-COMMAND")
        party = HubParticipant.objects.create(session=hub, nickname=self.party_a.nickname)
        with self.assertRaises(CommandRejected):
            self.command(actor=ActorContext(ReceiptActorType.PARTICIPANT, party.pk))
        self.assert_empty_state()

    def test_persisted_party_session_mismatch_is_detected(self):
        hub = HubSession.objects.create(code="MOVED-COMMAND")
        HubParticipant.objects.filter(pk=self.party_a.pk).update(session=hub)
        with self.assertRaises(CommandRejected) as caught:
            self.command()
        self.assertEqual(caught.exception.code, "invalid_instance")
        self.assert_empty_state()

    def test_persisted_step_session_mismatch_is_detected(self):
        hub = HubSession.objects.create(code="MOVED-STEP")
        HubGameStep.objects.filter(pk=self.step.pk).update(session=hub)
        with self.assertRaises(CommandRejected):
            self.command()
        self.assert_empty_state()

    def test_foreign_unit_rejected_even_with_same_unit_number(self):
        self.assertEqual(self.unit.number, self.other_unit.number)
        with self.assertRaises(CommandRejected) as caught:
            self.command(unit_id=self.other_unit.pk)
        self.assertEqual(caught.exception.code, "invalid_unit")
        self.assert_empty_state()

    def test_nonexistent_unit_and_instance_rejected(self):
        for overrides in ({"unit_id": 99999999}, {"instance_id": uuid4()}):
            with self.subTest(overrides=overrides), self.assertRaises(CommandRejected):
                self.command(**overrides)
        self.assert_empty_state()

    def test_instance_level_action_without_unit_allowed(self):
        result = self.command(unit_id=None)
        self.assertEqual(result.state_revision, 1)
        self.assertIsNone(DuelActionReceipt.objects.get().unit_id)

    def test_old_instance_with_new_unit_cannot_change_current_instance(self):
        before = DuelGameInstance.objects.filter(pk=self.other_instance.pk).values().get()
        with self.assertRaises(CommandRejected):
            self.command(instance_id=self.instance.pk, unit_id=self.other_unit.pk)
        self.assertEqual(DuelGameInstance.objects.filter(pk=self.other_instance.pk).values().get(), before)
        self.assert_empty_state()

    def test_old_instance_id_never_resolves_to_new_run_of_same_game_step(self):
        self.command(instance_id=self.instance.pk, unit_id=self.unit.pk)
        self.other_instance.refresh_from_db()
        self.assertEqual(self.other_instance.state_revision, 0)
        self.assertEqual(self.other_instance.rule_config, {})

    def test_controlled_rejection_rolls_back_handler_writes_but_keeps_receipt(self):
        callbacks = []

        def reject(context):
            self.point(context)
            transaction.on_commit(lambda: callbacks.append("must not run"))
            return HandlerResult(ReceiptStatus.REJECTED, {"code": "invalid_phase"})

        result = self.command(handler=reject)
        self.assertEqual(result.status, ReceiptStatus.REJECTED)
        self.assertEqual(result.data, {"code": "invalid_phase"})
        self.assertEqual(result.state_revision, 0)
        self.assertFalse(callbacks)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.rule_config, {})
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertEqual(DuelActionReceipt.objects.get().status, "rejected")
        replay = self.command(handler=Mock(side_effect=AssertionError("must not run")))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.data, result.data)

    def test_unexpected_exception_rolls_back_everything(self):
        def fail(context):
            self.point(context)
            raise RuntimeError("technical failure")

        with self.assertRaisesMessage(RuntimeError, "technical failure"):
            self.command(handler=fail)
        self.assert_empty_state()

    def test_receipt_database_error_rolls_back_points_and_revision(self):
        with patch.object(DuelActionReceipt, "save", side_effect=IntegrityError("receipt failure")):
            with self.assertRaises(IntegrityError):
                self.command(handler=self.point)
        self.assert_empty_state()

    def test_actual_handler_constraint_failure_rolls_back_all_changes(self):
        def fail(context):
            self.point(context)
            DuelScoreEntry.objects.create(
                game_instance=context.instance, participant=context.participant, delta=2,
                source_key=f"test:{context.client_action_id}",
            )

        with self.assertRaises(IntegrityError):
            self.command(handler=fail)
        self.assert_empty_state()

    def test_non_lock_operational_error_is_not_retried(self):
        def fail(context):
            self.increment(context)
            raise OperationalError("no such table: missing")

        with self.assertRaises(OperationalError):
            self.command(handler=fail)
        self.assertEqual(self.calls, 1)
        self.assert_empty_state()

    def test_sqlite_lock_error_retries_entire_rolled_back_operation(self):
        def retry(context):
            result = self.point(context)
            if self.calls == 1:
                raise OperationalError("database is locked")
            return result

        result = self.command(handler=retry)
        self.assertEqual(self.calls, 2)  # first attempt rolled back, not applied twice
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.state_revision, 1)
        self.assertEqual(DuelScoreEntry.objects.count(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_replay_retains_original_revision_after_later_actions(self):
        first = self.command()
        self.command(client_action_id=str(uuid4()), expected_revision=1)
        replay = self.command(handler=Mock(side_effect=AssertionError("must not run")))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.state_revision, first.state_revision)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, 2)

    def test_controller_can_reject_stale_new_action_but_replay_runs_first(self):
        def strict(context):
            if context.expected_revision != context.instance.state_revision:
                return HandlerResult(ReceiptStatus.REJECTED, {"code": "stale_context"})
            return self.increment(context)

        self.command(handler=strict)
        self.assertTrue(self.command(handler=strict).replayed)
        rejected = self.command(client_action_id=str(uuid4()), handler=strict)
        self.assertEqual(rejected.status, ReceiptStatus.REJECTED)
        self.assertEqual(rejected.state_revision, 1)
        self.assertEqual(self.calls, 1)

    def test_independent_second_party_action_is_not_blocked_by_first_revision(self):
        first = self.command(expected_revision=0)
        second = self.command(actor=ActorContext(ReceiptActorType.PARTICIPANT, self.party_b.pk), expected_revision=0)
        self.assertEqual((first.state_revision, second.state_revision), (1, 2))

    def test_replay_precedes_controller_phase_check(self):
        def phase(context):
            if context.unit.status != UnitStatus.ACTIVE:
                return HandlerResult(ReceiptStatus.REJECTED, {"code": "invalid_phase"})
            context.unit.status = UnitStatus.COMPLETED
            context.unit.save(update_fields=["status"])
            return self.increment(context)

        self.command(handler=phase)
        self.assertTrue(self.command(handler=phase).replayed)
        rejected = self.command(handler=phase, client_action_id=str(uuid4()))
        self.assertEqual(rejected.status, ReceiptStatus.REJECTED)
        self.assertEqual(rejected.state_revision, 1)

    def test_lost_response_and_new_actor_object_recover_original_result(self):
        first = self.command(handler=self.point)
        # Simulated connection loss after commit; no transport is implemented.
        actor = ActorContext(ReceiptActorType.PARTICIPANT, self.party_a.pk)
        replay = self.command(actor=actor, handler=Mock(side_effect=AssertionError("must not run")))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(replay.receipt_id, first.receipt_id)
        self.assertEqual(DuelScoreEntry.objects.count(), 1)

    def test_handler_cannot_advance_revision_itself(self):
        def fail(context):
            self.increment(context)
            DuelGameInstance.objects.filter(pk=context.instance.pk).update(state_revision=F("state_revision") + 1)
            return HandlerResult(ReceiptStatus.APPLIED, {"code": "bad"})

        with self.assertRaisesMessage(RuntimeError, "must not update"):
            self.command(handler=fail)
        self.assert_empty_state()

    def test_handler_cannot_retarget_instance_identity(self):
        def fail(context):
            self.increment(context)
            context.instance.pk = self.other_instance.pk
            return HandlerResult(ReceiptStatus.APPLIED, {})

        with self.assertRaisesMessage(RuntimeError, "replace command identities"):
            self.command(handler=fail)
        self.assert_empty_state()

    def test_handler_cannot_replace_parties(self):
        def fail(context):
            self.increment(context)
            DuelGameInstance.objects.filter(pk=context.instance.pk).update(participant_b=self.third)
            return HandlerResult(ReceiptStatus.APPLIED, {})

        with self.assertRaisesMessage(RuntimeError, "identity or parties"):
            self.command(handler=fail)
        self.assert_empty_state()

    def test_invalid_handler_result_or_non_json_data_roll_back(self):
        for outcome in (None, {"status": "applied"}, HandlerResult(ReceiptStatus.APPLIED, {"bad": object()})):
            def fail(context):
                self.point(context)
                return outcome
            with self.subTest(outcome=outcome):
                with self.assertRaises((TypeError, ValueError)):
                    self.command(handler=fail)
                self.assert_empty_state()

    def test_handler_payload_mutation_does_not_change_fingerprint_or_callers_data(self):
        payload = {"nested": {"value": "A"}}
        def mutate(context):
            context.payload["nested"]["value"] = "B"
            return self.increment(context)
        self.command(payload=payload, handler=mutate)
        self.assertEqual(payload, {"nested": {"value": "A"}})
        self.assertTrue(self.command(payload=payload).replayed)

    def test_returned_data_cannot_mutate_stored_receipt(self):
        first = self.command()
        first.data["count"] = 999
        self.assertEqual(self.command().data["count"], 1)

    def test_on_commit_runs_after_receipt_and_revision_are_visible(self):
        observations = []
        def handle(context):
            transaction.on_commit(lambda: observations.append((
                connection.in_atomic_block, DuelActionReceipt.objects.count(),
                DuelGameInstance.objects.get(pk=context.instance.pk).state_revision,
            )))
            self.assertFalse(observations)
            return self.increment(context)
        self.command(handler=handle)
        self.assertEqual(observations, [(False, 1, 1)])

    def test_rollback_discards_on_commit_callbacks(self):
        observations = []
        def fail(context):
            transaction.on_commit(lambda: observations.append("bad"))
            self.point(context)
            raise RuntimeError("rollback")
        with self.assertRaises(RuntimeError):
            self.command(handler=fail)
        self.assertFalse(observations)
        self.assert_empty_state()

    def test_post_commit_lock_callback_failure_is_not_retried_or_masked(self):
        def fail_callback():
            raise OperationalError("database is locked")
        def handle(context):
            transaction.on_commit(fail_callback)
            return self.increment(context)
        with self.assertRaisesMessage(RuntimeError, "Post-commit callback failed"):
            self.command(handler=handle)
        self.assertEqual(self.calls, 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.assertTrue(self.command().replayed)

    def test_nested_transaction_is_rejected(self):
        with transaction.atomic():
            with self.assertRaisesMessage(RuntimeError, "own outer transaction"):
                self.command()
        self.assert_empty_state()

    def test_sqlite_deferred_configuration_fails_closed(self):
        with patch.object(connection, "transaction_mode", "DEFERRED"):
            with self.assertRaisesMessage(RuntimeError, "IMMEDIATE"):
                self.command()
        self.assert_empty_state()

    def test_invalid_identifiers_or_payload_do_not_call_handler(self):
        for overrides in (
            {"actor": {"actor_type": "host", "actor_id": 1}}, {"client_action_id": ""},
            {"client_action_id": "a" * 129}, {"handler": "client.handler"}, {"instance_id": str(self.instance.pk)},
            {"unit_id": 0}, {"expected_revision": True}, {"expected_revision": -1}, {"action_type": ""},
            {"payload": {1: "bad"}}, {"payload": {"v": float("nan")}}, {"payload": {"v": float("inf")}},
            {"payload": {"v": (1, 2)}}, {"payload": {"v": object()}},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises((TypeError, ValueError)):
                    self.command(**overrides)
        self.assertEqual(self.calls, 0)
        self.assert_empty_state()

    def test_actor_context_requires_typed_role_and_positive_id(self):
        for role, identity in (("host", 1), (ReceiptActorType.PARTICIPANT, True),
                               (ReceiptActorType.PARTICIPANT, 0), (ReceiptActorType.HOST, "1")):
            with self.subTest(role=role, identity=identity), self.assertRaises((TypeError, ValueError)):
                ActorContext(role, identity)

    def test_fingerprints_cover_instance_unit_action_payload_and_revision_hint(self):
        args = dict(instance_id=self.instance.pk, unit_id=self.unit.pk,
                    action_type="test.increment", payload={"a": 1, "b": [2, 3]}, expected_revision=0)
        original = request_fingerprint(**args)
        self.assertEqual(len(original), 64)
        for overrides in ({"instance_id": self.other_instance.pk}, {"unit_id": None},
                          {"action_type": "test.other"}, {"payload": {"a": 2, "b": [2, 3]}},
                          {"expected_revision": 1}, {"payload": {"a": 1, "b": [3, 2]}}):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(request_fingerprint(**(args | overrides)), original)

    def test_existing_receipt_uniqueness_still_enforced_without_full_clean(self):
        self.command()
        receipt = DuelActionReceipt.objects.get()
        receipt.pk = uuid4()
        with self.assertRaises(IntegrityError), transaction.atomic():
            receipt.save(force_insert=True)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_only_new_applied_action_updates_instance_timestamp(self):
        stamp = timezone.now()
        with patch("duel.commands.timezone.now", return_value=stamp):
            self.command()
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.updated_at, stamp)
        self.command()
        self.command(client_action_id=str(uuid4()), handler=lambda context: HandlerResult(
            ReceiptStatus.REJECTED, {"code": "invalid_phase"},
        ))
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.updated_at, stamp)
