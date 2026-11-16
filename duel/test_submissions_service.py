"""Isolated submission-service contracts; no transport or game handlers."""
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.db import IntegrityError, OperationalError, connection
from django.utils import timezone

from . import test_commands as commands_tests
from .commands import ActionConflict, ActorContext, CommandRejected
from .contracts import MatchStatus
from .models import (
    DuelActionReceipt, DuelDecision, DuelGameInstance, DuelMatchOutcome, DuelScoreEntry,
    DuelSubmission, DuelUnit, DuelUnitOutcome, ReceiptActorType, ReceiptStatus,
    SubmissionKind, SubmissionStatus, UnitStatus,
)
from .submissions import commit_submission, get_committed_submission, get_submission_status, save_draft


class SubmissionFixture(commands_tests.CommandFixture):
    def setUp(self):
        super().setUp()
        DuelGameInstance.objects.filter(pk__in=[self.instance.pk, self.other_instance.pk]).update(
            match_status=MatchStatus.IN_PROGRESS.value,
        )
        DuelUnit.objects.filter(pk=self.other_unit.pk).update(status=UnitStatus.ACTIVE.value)
        self.instance.refresh_from_db()
        self.other_instance.refresh_from_db()
        self.other_unit.refresh_from_db()
        self.actor_b = ActorContext(ReceiptActorType.PARTICIPANT, self.party_b.pk)

    def args(self, **overrides):
        args = dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.actor,
                    submission_kind=SubmissionKind.ANSWER)
        args.update(overrides)
        return args

    def commit(self, **overrides):
        args = {"client_action_id": str(uuid4()), "payload": {"value": 42}, **overrides}
        return commit_submission(**self.args(**args))

    def draft(self, **overrides):
        args = {"client_action_id": str(uuid4()), "payload": {"value": 42}, **overrides}
        return save_draft(**self.args(**args))

    def read(self, **overrides):
        return get_committed_submission(**self.args(**overrides))

    def status(self, **overrides):
        return get_submission_status(**self.args(**overrides))

    def revision(self):
        return DuelGameInstance.objects.get(pk=self.instance.pk).state_revision


class SubmissionServiceTests(SubmissionFixture):
    def test_draft_create_update_has_no_acceptance_and_is_not_an_answer(self):
        first = self.draft()
        self.assertEqual(first.status, ReceiptStatus.APPLIED)
        self.assertEqual(first.data["status"], "draft")
        self.assertIsNone(first.data["committed_at"])
        self.assertIsNone(first.data["acceptance_sequence"])
        self.assertIsNone(self.read())
        self.assertEqual(self.status(), SubmissionStatus.DRAFT)
        second = self.draft(payload={"value": 84})
        self.assertEqual(second.data["submission_id"], first.data["submission_id"])
        self.assertEqual(DuelSubmission.objects.get().payload, {"value": 84})
        self.assertEqual(self.revision(), 2)

    def test_explicit_draft_can_commit_its_persisted_data(self):
        draft = self.draft(payload={"latitude": 52.5, "longitude": 13.4})
        result = self.commit(draft_id=UUID(draft.data["submission_id"]), payload=None)
        self.assertEqual(result.data["submission_id"], draft.data["submission_id"])
        self.assertEqual(DuelSubmission.objects.count(), 1)
        self.assertEqual(self.read()["payload"], {"latitude": 52.5, "longitude": 13.4})
        self.assertEqual(self.status(), SubmissionStatus.COMMITTED)
        self.assertEqual(result.state_revision, 2)

    def test_explicit_draft_can_receive_final_payload_at_commit(self):
        draft = self.draft()
        self.commit(draft_id=UUID(draft.data["submission_id"]), payload={"option_id": "A"})
        self.assertEqual(self.read()["payload"], {"option_id": "A"})

    def test_direct_commit_preserves_json_and_server_acceptance_metadata(self):
        payload = {"value": 42, "text": "Straße", "nested": [None, True, {"x": 1.25}]}
        before = timezone.now()
        result = self.commit(payload=payload)
        after = timezone.now()
        stored = DuelSubmission.objects.get()
        self.assertEqual(stored.payload, payload)
        self.assertEqual(stored.status, SubmissionStatus.COMMITTED)
        self.assertTrue(before <= stored.committed_at <= after)
        self.assertEqual(stored.acceptance_sequence, 1)
        self.assertEqual(result.data, {
            "submission_id": str(stored.pk), "instance_id": str(self.instance.pk),
            "unit_id": self.unit.pk, "submission_kind": "answer", "status": "committed",
            "committed_at": stored.committed_at.isoformat(), "acceptance_sequence": 1,
        })
        self.assertEqual(result.state_revision, 1)
        self.assertEqual(DuelActionReceipt.objects.get().result, {"data": result.data, "state_revision": 1})

    def test_second_party_is_independent_of_global_revision(self):
        self.commit(expected_revision=0)
        second = self.commit(actor=self.actor_b, expected_revision=0)
        self.assertEqual(second.status, ReceiptStatus.APPLIED)
        self.assertEqual(second.data["acceptance_sequence"], 2)
        self.assertEqual(self.revision(), 2)

    def test_answer_and_bid_share_unit_sequence_but_not_input_uniqueness(self):
        first = self.commit()
        bid = self.commit(submission_kind=SubmissionKind.BID, payload={"value": 8})
        other = self.commit(actor=self.actor_b, submission_kind=SubmissionKind.BID, payload={"value": 8})
        self.assertEqual([r.data["acceptance_sequence"] for r in (first, bid, other)], [1, 2, 3])
        self.assertEqual(DuelSubmission.objects.count(), 3)

    def test_draft_does_not_block_new_committed_row_and_committed_status_takes_precedence(self):
        self.draft()
        result = self.commit(payload={"value": 9})
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.assertEqual(DuelSubmission.objects.count(), 2)
        self.assertEqual(self.status(), SubmissionStatus.COMMITTED)
        self.assertEqual(self.read()["payload"], {"value": 9})

    def test_historical_drafts_have_deterministic_selection_and_are_not_rewritten(self):
        old = DuelSubmission.objects.create(unit=self.unit, participant=self.party_a, submission_kind="answer", payload={"old": True})
        newer = DuelSubmission.objects.create(unit=self.unit, participant=self.party_a, submission_kind="answer", payload={"new": True})
        result = self.draft(payload={"chosen": True})
        self.assertEqual(result.data["submission_id"], str(newer.pk))
        old.refresh_from_db()
        self.assertEqual(old.payload, {"old": True})
        self.assertIsNone(self.read())
        self.assertEqual(DuelSubmission.objects.count(), 2)

    def test_explicit_older_draft_can_be_updated_without_changing_newer_draft(self):
        old = self.draft()
        newer = DuelSubmission.objects.create(unit=self.unit, participant=self.party_a, submission_kind="answer", payload={"new": True})
        self.draft(draft_id=UUID(old.data["submission_id"]), payload={"value": 7})
        newer.refresh_from_db()
        self.assertEqual(newer.payload, {"new": True})

    def test_equal_and_different_bids_retain_server_order_without_comparison(self):
        for values in ((8, 8), (2, 100)):
            with self.subTest(values=values):
                unit = DuelUnit.objects.create(game_instance=self.instance, number=values[1] + 1, status="active")
                first = self.commit(unit_id=unit.pk, submission_kind=SubmissionKind.BID, payload={"value": values[0]})
                second = self.commit(unit_id=unit.pk, actor=self.actor_b, submission_kind=SubmissionKind.BID, payload={"value": values[1]})
                self.assertEqual([first.data["acceptance_sequence"], second.data["acceptance_sequence"]], [1, 2])

    def test_new_unit_and_new_instance_have_independent_sequences(self):
        self.commit()
        unit = DuelUnit.objects.create(game_instance=self.instance, number=2, status="active")
        for args in ({"unit_id": unit.pk}, {"instance_id": self.other_instance.pk, "unit_id": self.other_unit.pk}):
            with self.subTest(args=args):
                self.assertEqual(self.commit(**args).data["acceptance_sequence"], 1)

    def test_existing_accepted_sequence_is_not_reused(self):
        DuelSubmission.objects.create(unit=self.unit, participant=self.party_b, submission_kind="bid", status="committed",
                                      committed_at=timezone.now(), acceptance_sequence=7, payload={"value": 8})
        self.assertEqual(self.commit().data["acceptance_sequence"], 8)

    def test_identical_retry_preserves_id_payload_timestamp_sequence_receipt_and_revision(self):
        key = str(uuid4())
        original = self.commit(client_action_id=key, payload={"b": 2, "a": 1})
        stored = DuelSubmission.objects.values().get()
        self.commit(actor=self.actor_b)
        retry = self.commit(client_action_id=key, payload={"a": 1, "b": 2})
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.data, original.data)
        self.assertEqual(retry.receipt_id, original.receipt_id)
        self.assertEqual(retry.state_revision, 1)
        self.assertEqual(self.revision(), 2)
        self.assertEqual(DuelSubmission.objects.values().get(pk=stored["id"]), stored)
        self.assertEqual(DuelActionReceipt.objects.count(), 2)

    def test_conflicting_retry_leaves_original_commit_unchanged(self):
        key = str(uuid4())
        self.commit(client_action_id=key)
        stored = DuelSubmission.objects.values().get()
        with self.assertRaises(ActionConflict):
            self.commit(client_action_id=key, payload={"value": 999})
        self.assertEqual(DuelSubmission.objects.values().get(), stored)
        self.assertEqual(self.revision(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_kind_draft_reference_and_operation_are_part_of_request_identity(self):
        key = str(uuid4())
        self.draft(client_action_id=key)
        for operation, overrides in ((self.commit, {}), (self.draft, {"submission_kind": SubmissionKind.BID}),
                                      (self.draft, {"draft_id": uuid4()})):
            with self.subTest(overrides=overrides), self.assertRaises(ActionConflict):
                operation(client_action_id=key, **overrides)
        self.assertEqual(self.revision(), 1)

    def test_new_action_id_cannot_rewrite_commit_or_update_any_leftover_draft(self):
        draft = self.draft()
        self.commit()
        before = list(DuelSubmission.objects.order_by("id").values())
        for operation, args in ((self.commit, {}), (self.draft, {}),
                                (self.commit, {"draft_id": UUID(draft.data["submission_id"]), "payload": None})):
            with self.subTest(operation=operation.__name__):
                result = operation(**args)
                self.assertEqual(result.status, ReceiptStatus.REJECTED)
                self.assertEqual(result.data["code"], "already_committed")
        self.assertEqual(list(DuelSubmission.objects.order_by("id").values()), before)
        self.assertEqual(self.revision(), 2)

    def test_retry_of_draft_returns_old_acknowledgement_after_update_and_commit(self):
        key = str(uuid4())
        draft = self.draft(client_action_id=key)
        self.draft(payload={"value": 2})
        self.commit(draft_id=UUID(draft.data["submission_id"]), payload=None)
        retry = self.draft(client_action_id=key)
        self.assertEqual(retry.data, draft.data)
        self.assertTrue(retry.replayed)
        self.assertEqual(self.read()["payload"], {"value": 2})
        self.assertEqual(self.revision(), 3)

    def test_identical_commit_retry_still_succeeds_after_unit_and_match_close(self):
        key = str(uuid4())
        first = self.commit(client_action_id=key)
        DuelUnit.objects.filter(pk=self.unit.pk).update(status="completed")
        DuelGameInstance.objects.filter(pk=self.instance.pk).update(match_status="completed")
        result = self.commit(client_action_id=key)
        self.assertTrue(result.replayed)
        self.assertEqual(result.data, first.data)
        self.assertEqual(self.revision(), 1)

    def test_new_inputs_rejected_for_not_started_closed_or_aborted_match(self):
        for state in (MatchStatus.NOT_STARTED, MatchStatus.COMPLETED, MatchStatus.ABORTED):
            with self.subTest(state=state):
                DuelGameInstance.objects.filter(pk=self.instance.pk).update(match_status=state.value)
                for operation in (self.commit, self.draft):
                    result = operation()
                    self.assertEqual(result.status, ReceiptStatus.REJECTED)
        self.assertEqual(self.revision(), 0)
        self.assertFalse(DuelSubmission.objects.exists())

    def test_prepared_completed_and_inconsistently_closed_units_reject_inputs(self):
        for status, completed_at in (("prepared", None), ("completed", None), ("active", timezone.now())):
            with self.subTest(status=status, completed_at=completed_at):
                DuelUnit.objects.filter(pk=self.unit.pk).update(status=status, completed_at=completed_at)
                for operation in (self.commit, self.draft):
                    result = operation()
                    self.assertEqual(result.data["code"], "unit_not_active")
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertEqual(self.revision(), 0)

    def test_third_participant_and_host_cannot_write_or_read_inputs(self):
        actors = [ActorContext(ReceiptActorType.PARTICIPANT, self.third.pk), ActorContext(ReceiptActorType.HOST, 123)]
        for actor in actors:
            for operation in (self.commit, self.draft, self.read, self.status):
                with self.subTest(actor=actor, operation=operation.__name__), self.assertRaises(CommandRejected):
                    operation(actor=actor)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_nonexistent_participant_cannot_submit(self):
        with self.assertRaises(CommandRejected):
            self.commit(actor=ActorContext(ReceiptActorType.PARTICIPANT, self.third.pk + 1000))
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_foreign_unit_missing_unit_and_missing_instance_rejected(self):
        for args in ({"unit_id": self.other_unit.pk}, {"unit_id": self.other_unit.pk + 1000}, {"instance_id": uuid4()}):
            for operation in (self.commit, self.draft, self.read, self.status):
                with self.subTest(args=args, operation=operation.__name__), self.assertRaises(CommandRejected):
                    operation(**args)
        self.assertEqual(self.revision(), 0)
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_foreign_instance_does_not_grant_membership_through_same_session(self):
        DuelGameInstance.objects.filter(pk=self.other_instance.pk).update(participant_a=self.third)
        with self.assertRaises(CommandRejected):
            self.commit(instance_id=self.other_instance.pk, unit_id=self.other_unit.pk)
        self.assertEqual(self.revision(), 0)

    def test_cross_session_party_relationship_is_rechecked(self):
        from games_hub.models import HubParticipant, HubSession
        foreign = HubParticipant.objects.create(session=HubSession.objects.create(code="FOREIGN-SUB"), nickname="Foreign")
        DuelGameInstance.objects.filter(pk=self.instance.pk).update(participant_a=foreign)
        with self.assertRaises(CommandRejected):
            self.commit(actor=ActorContext(ReceiptActorType.PARTICIPANT, foreign.pk))
        self.assertFalse(DuelSubmission.objects.exists())

    def test_foreign_draft_uuid_cannot_read_or_modify_other_partys_answer(self):
        draft = self.draft(actor=self.actor_b, payload={"secret": 8})
        for operation in (self.commit, self.draft):
            result = operation(draft_id=UUID(draft.data["submission_id"]), payload={"value": 999})
            self.assertEqual(result.data["code"], "invalid_draft")
        self.assertEqual(DuelSubmission.objects.get().payload, {"secret": 8})
        self.assertIsNone(self.read())

    def test_missing_other_kind_and_foreign_unit_draft_are_not_reused(self):
        bid = self.draft(submission_kind=SubmissionKind.BID)
        other = self.draft(instance_id=self.other_instance.pk, unit_id=self.other_unit.pk)
        for draft_id in (uuid4(), UUID(bid.data["submission_id"]), UUID(other.data["submission_id"])):
            result = self.commit(draft_id=draft_id, payload=None)
            self.assertEqual(result.data["code"], "invalid_draft")
        self.assertFalse(DuelSubmission.objects.filter(status="committed").exists())

    def test_invalid_arguments_fail_without_receipts_or_writes(self):
        for args in ({"submission_kind": "answer"}, {"submission_kind": "unknown"}, {"unit_id": None},
                     {"unit_id": True}, {"instance_id": str(self.instance.pk)}, {"draft_id": "not-uuid"},
                     {"actor": {"participant_id": self.party_a.pk}}, {"validate": False}, {"payload": None}):
            with self.subTest(args=args), self.assertRaises((TypeError, ValueError)):
                self.commit(**args)
        self.assertFalse(DuelActionReceipt.objects.exists())
        self.assertEqual(self.revision(), 0)

    def test_non_json_and_non_object_payloads_are_not_coerced(self):
        for payload in ([], "42", 42, {1: "wrong key"}, {"value": float("nan")}, {"value": float("inf")},
                        {"value": (1, 2)}, {"value": uuid4()}, {"value": timezone.now()}, {"value": {1, 2}}):
            with self.subTest(payload=payload), self.assertRaises((TypeError, ValueError)):
                self.commit(payload=payload)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_stored_draft_with_non_object_payload_cannot_bypass_validation(self):
        draft = DuelSubmission.objects.create(unit=self.unit, participant=self.party_a, submission_kind="answer", payload=[42])
        with self.assertRaises(TypeError):
            self.commit(payload=None, draft_id=draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "draft")
        self.assertEqual(self.revision(), 0)

    def test_payload_cannot_choose_authoritative_time_sequence_or_participant(self):
        payload = {"value": 8, "acceptance_sequence": 999, "committed_at": "1900-01-01", "participant_id": self.party_b.pk}
        self.commit(payload=payload)
        stored = DuelSubmission.objects.get()
        self.assertEqual(stored.acceptance_sequence, 1)
        self.assertEqual(stored.participant_id, self.party_a.pk)
        self.assertGreater(stored.committed_at.year, 1900)
        self.assertEqual(stored.payload, payload)  # uninterpreted data; never authority

    def test_reads_and_acknowledgements_never_expose_opponent_payload_or_mutable_models(self):
        self.commit(actor=self.actor_b, submission_kind=SubmissionKind.BID, payload={"secret": 999})
        own = self.commit(submission_kind=SubmissionKind.BID, payload={"value": 8})
        self.assertNotIn("payload", own.data)
        data = self.read(submission_kind=SubmissionKind.BID)
        self.assertEqual(data["payload"], {"value": 8})
        data["payload"]["value"] = 1000
        self.assertEqual(self.read(submission_kind=SubmissionKind.BID)["payload"], {"value": 8})
        self.assertEqual(self.revision(), 2)

    def test_status_is_none_for_no_input_and_independent_by_kind_and_party(self):
        self.assertIsNone(self.status())
        self.draft(submission_kind=SubmissionKind.BID)
        self.assertIsNone(self.status())
        self.assertIsNone(self.status(actor=self.actor_b, submission_kind=SubmissionKind.BID))
        self.assertEqual(self.status(submission_kind=SubmissionKind.BID), SubmissionStatus.DRAFT)

    def test_deadline_validation_runs_in_same_transaction_with_authoritative_acceptance_time(self):
        def validate(context, payload, now):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(context.participant.pk, self.party_a.pk)
            self.assertEqual(context.unit.pk, self.unit.pk)
            self.assertEqual(context.instance.pk, self.instance.pk)
            self.assertEqual(payload, {"value": 42})
            observed.append(now)
            return "deadline_passed" if now > deadline else None
        observed = []
        deadline = timezone.now() + timedelta(seconds=30)
        result = self.commit(validate=validate)
        self.assertEqual(DuelSubmission.objects.get().committed_at, observed[0])
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        deadline = timezone.now() - timedelta(seconds=1)
        failed = self.commit(actor=self.actor_b, validate=lambda ctx, payload, now: "deadline_passed" if now > deadline else None)
        self.assertEqual(failed.data["code"], "deadline_passed")
        self.assertEqual(self.revision(), 1)
        self.assertEqual(DuelSubmission.objects.count(), 1)

    def test_controlled_rejection_is_receipted_without_sequence_or_draft_update(self):
        draft = self.draft()
        stored = DuelSubmission.objects.values().get()
        key = str(uuid4())
        validator = Mock(return_value="answer_phase_closed")
        args = {"client_action_id": key, "draft_id": UUID(draft.data["submission_id"]), "validate": validator}
        result = self.commit(**args)
        retry = self.commit(**args)
        self.assertEqual(result.status, ReceiptStatus.REJECTED)
        self.assertTrue(retry.replayed)
        self.assertEqual(validator.call_count, 1)
        self.assertEqual(DuelSubmission.objects.values().get(), stored)
        self.assertEqual(self.revision(), 1)

    def test_validator_exception_rolls_back_and_invalid_return_is_a_technical_error(self):
        for validator, error in ((Mock(side_effect=RuntimeError("phase lookup failed")), RuntimeError),
                                 (Mock(return_value=True), TypeError)):
            with self.subTest(error=error), self.assertRaises(error):
                self.commit(validate=validator)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())
        self.assertEqual(self.revision(), 0)

    def test_receipt_database_failure_rolls_back_commit_and_can_be_retried(self):
        draft = self.draft()
        stored = DuelSubmission.objects.values().get()
        key = str(uuid4())
        args = {"draft_id": UUID(draft.data["submission_id"]), "payload": None, "client_action_id": key}
        with patch.object(DuelActionReceipt, "save", side_effect=IntegrityError("receipt failed")):
            with self.assertRaises(IntegrityError):
                self.commit(**args)
        self.assertEqual(DuelSubmission.objects.values().get(), stored)
        self.assertEqual(self.revision(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.assertEqual(self.commit(**args).data["acceptance_sequence"], 1)

    def test_submission_database_failure_leaves_no_receipt_sequence_or_revision(self):
        with patch.object(DuelSubmission, "save", side_effect=IntegrityError("submission failed")):
            with self.assertRaises(IntegrityError):
                self.commit()
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())
        self.assertEqual(self.revision(), 0)
        self.assertEqual(self.commit().data["acceptance_sequence"], 1)

    def test_real_receipt_check_constraint_failure_rolls_back_entire_commit(self):
        original_save = DuelActionReceipt.save
        def invalid_receipt(receipt, *args, **kwargs):
            receipt.status = "invalid"  # fail the actual DB CHECK after full_clean
            return original_save(receipt, *args, **kwargs)
        with patch.object(DuelActionReceipt, "save", invalid_receipt):
            with self.assertRaises(IntegrityError):
                self.commit()
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())
        self.assertEqual(self.revision(), 0)

    def test_lock_failure_after_insert_retries_whole_commit_without_leaking_sequence(self):
        original_save, attempts = DuelSubmission.save, []
        def locked_after_insert(submission, *args, **kwargs):
            original_save(submission, *args, **kwargs)
            attempts.append(submission.pk)
            if len(attempts) == 1:
                raise OperationalError("database is locked")
        with patch.object(DuelSubmission, "save", locked_after_insert):
            result = self.commit()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(DuelSubmission.objects.count(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.assertEqual(result.data["acceptance_sequence"], 1)
        self.assertEqual(self.revision(), 1)

    def test_controller_validation_receives_resolved_draft_payload_in_transaction(self):
        draft = self.draft(payload={"value": 5})
        def validate(context, payload, now):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(payload, {"value": 5})
            return "outside_game_schema"
        result = self.commit(draft_id=UUID(draft.data["submission_id"]), payload=None, validate=validate)
        self.assertEqual(result.data["code"], "outside_game_schema")
        self.assertIsNone(self.read())
        self.assertEqual(self.revision(), 1)

    def test_valid_commits_do_not_score_complete_units_or_change_match_state(self):
        before = DuelGameInstance.objects.values().get(pk=self.instance.pk)
        unit_before = DuelUnit.objects.values().get(pk=self.unit.pk)
        self.commit()
        self.commit(actor=self.actor_b)
        after = DuelGameInstance.objects.values().get(pk=self.instance.pk)
        for key in before.keys() - {"state_revision", "updated_at"}:
            self.assertEqual(after[key], before[key])
        self.assertEqual(DuelUnit.objects.values().get(pk=self.unit.pk), unit_before)
        for model in (DuelScoreEntry, DuelUnitOutcome, DuelMatchOutcome, DuelDecision):
            self.assertFalse(model.objects.exists())
