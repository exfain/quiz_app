"""Real file-backed SQLite/IMMEDIATE tests with independent connections."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import patch
from uuid import UUID, uuid4

from django.db import OperationalError, connection, connections

from .commands import ActionConflict
from .models import DuelActionReceipt, DuelSubmission, DuelUnit, ReceiptStatus, SubmissionKind
from .test_submissions_service import SubmissionFixture


class SubmissionConcurrencyTests(SubmissionFixture):
    def setUp(self):
        super().setUp()
        if connection.vendor != "sqlite":
            self.skipTest("These tests exercise SQLite IMMEDIATE.")
        self.assertTrue(Path(connection.settings_dict["NAME"]).is_file())
        self.assertNotIn("mode=memory", str(connection.settings_dict["NAME"]))
        self.assertEqual(connection.transaction_mode, "IMMEDIATE")

    def parallel(self, operations):
        barrier, guard = Barrier(len(operations)), Lock()
        identities, begins = [], []

        def worker(operation):
            db = connections["default"]
            try:
                db.ensure_connection()
                with guard:
                    identities.append(id(db.connection))
                def observe(execute, sql, params, many, context):
                    if sql.startswith("BEGIN"):
                        with guard:
                            begins.append(sql)
                    return execute(sql, params, many, context)
                barrier.wait(timeout=10)
                with db.execute_wrapper(observe):
                    try:
                        return operation()
                    except ActionConflict as exc:
                        return exc
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(worker, operation) for operation in operations]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(len(set(identities)), len(operations))
        self.assertTrue(begins)
        self.assertTrue(all(sql == "BEGIN IMMEDIATE" for sql in begins))
        return results

    def assert_consistent_commits(self, count):
        committed = DuelSubmission.objects.filter(status="committed")
        receipts = DuelActionReceipt.objects.filter(status="applied", action_type="duel.submission.commit")
        self.assertEqual(committed.count(), count)
        self.assertEqual(receipts.count(), count)
        for receipt in receipts:
            row = committed.get(pk=receipt.result["data"]["submission_id"])
            self.assertEqual(row.unit_id, receipt.unit_id)
            self.assertEqual(str(row.participant_id), receipt.actor_key)
            self.assertEqual(row.committed_at.isoformat(), receipt.result["data"]["committed_at"])
            self.assertEqual(row.acceptance_sequence, receipt.result["data"]["acceptance_sequence"])

    def test_concurrent_different_bids_have_unique_sequences_and_no_lost_revision(self):
        results = self.parallel([
            lambda: self.commit(submission_kind=SubmissionKind.BID, payload={"value": 2}),
            lambda: self.commit(actor=self.actor_b, submission_kind=SubmissionKind.BID, payload={"value": 100}),
        ])
        self.assertEqual(sorted(result.data["acceptance_sequence"] for result in results), [1, 2])
        self.assertEqual(sorted(result.state_revision for result in results), [1, 2])
        self.assertEqual(self.revision(), 2)
        self.assert_consistent_commits(2)

    def test_concurrent_equal_bids_are_serialized_without_comparing_browser_times(self):
        results = self.parallel([
            lambda: self.commit(submission_kind=SubmissionKind.BID, payload={"value": 8, "browser_time": 999}),
            lambda: self.commit(actor=self.actor_b, submission_kind=SubmissionKind.BID, payload={"value": 8, "browser_time": 1}),
        ])
        self.assertEqual(sorted(result.data["acceptance_sequence"] for result in results), [1, 2])
        self.assert_consistent_commits(2)

    def test_identical_requests_commit_once_with_original_acknowledgement(self):
        key = str(uuid4())
        results = self.parallel([lambda: self.commit(client_action_id=key)] * 2)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(results[0].data, results[1].data)
        self.assertEqual(results[0].receipt_id, results[1].receipt_id)
        self.assertEqual(self.revision(), 1)
        self.assert_consistent_commits(1)

    def test_conflicting_same_action_id_has_only_one_success_and_no_partial_input(self):
        key = str(uuid4())
        results = self.parallel([lambda: self.commit(client_action_id=key, payload={"value": 1}),
                                 lambda: self.commit(client_action_id=key, payload={"value": 2})])
        self.assertEqual(sum(isinstance(result, ActionConflict) for result in results), 1)
        self.assert_consistent_commits(1)
        self.assertEqual(self.revision(), 1)

    def test_different_action_ids_same_party_cannot_commit_twice(self):
        results = self.parallel([lambda: self.commit(payload={"value": 1}), lambda: self.commit(payload={"value": 2})])
        self.assertEqual(sum(result.status == ReceiptStatus.APPLIED for result in results), 1)
        rejected = next(result for result in results if result.status == ReceiptStatus.REJECTED)
        self.assertEqual(rejected.data["code"], "already_committed")
        self.assertEqual(DuelActionReceipt.objects.count(), 2)  # includes the controlled rejection
        self.assertEqual(self.revision(), 1)
        self.assert_consistent_commits(1)

    def test_two_parties_same_action_id_remain_independent(self):
        key = str(uuid4())
        results = self.parallel([lambda: self.commit(client_action_id=key, expected_revision=0),
                                 lambda: self.commit(client_action_id=key, actor=self.actor_b, expected_revision=0)])
        self.assertTrue(all(result.status == ReceiptStatus.APPLIED for result in results))
        self.assertEqual(self.revision(), 2)
        self.assert_consistent_commits(2)

    def test_concurrent_answer_and_bid_do_not_collide_in_unit_sequence(self):
        results = self.parallel([lambda: self.commit(), lambda: self.commit(submission_kind=SubmissionKind.BID)])
        self.assertEqual(sorted(result.data["acceptance_sequence"] for result in results), [1, 2])
        self.assert_consistent_commits(2)

    def test_parallel_draft_updates_create_only_one_service_draft(self):
        results = self.parallel([lambda: self.draft(payload={"value": 1}), lambda: self.draft(payload={"value": 2})])
        self.assertEqual(len({result.data["submission_id"] for result in results}), 1)
        self.assertEqual(DuelSubmission.objects.count(), 1)
        self.assertIsNone(DuelSubmission.objects.get().acceptance_sequence)
        self.assertEqual(self.revision(), 2)

    def test_draft_update_racing_commit_cannot_change_already_committed_data(self):
        draft = self.draft(payload={"value": 1})
        draft_id = UUID(draft.data["submission_id"])
        update, commit = self.parallel([lambda: self.draft(draft_id=draft_id, payload={"value": 2}),
                                       lambda: self.commit(draft_id=draft_id, payload=None)])
        self.assertEqual(commit.status, ReceiptStatus.APPLIED)
        expected = 2 if update.status == ReceiptStatus.APPLIED else 1
        self.assertEqual(self.read()["payload"], {"value": expected})
        self.assertEqual(self.revision(), 2 + (update.status == ReceiptStatus.APPLIED))
        self.assert_consistent_commits(1)

    def test_different_units_use_independent_sequences_under_same_parent_lock(self):
        other = DuelUnit.objects.create(game_instance=self.instance, number=2, status="active")
        results = self.parallel([lambda: self.commit(), lambda: self.commit(unit_id=other.pk)])
        self.assertTrue(all(result.data["acceptance_sequence"] == 1 for result in results))
        self.assertEqual(self.revision(), 2)
        self.assert_consistent_commits(2)

    def test_reader_cannot_observe_submission_before_receipt_transaction_commit(self):
        ready, release = Event(), Event()
        original_save = DuelActionReceipt.save
        def paused_receipt_save(receipt, *args, **kwargs):
            ready.set()  # submission has been inserted in the uncommitted transaction
            if not release.wait(timeout=10):
                raise RuntimeError("Receipt gate timed out.")
            return original_save(receipt, *args, **kwargs)
        def writer():
            try:
                return self.commit()
            finally:
                connections["default"].close()
        def reader():
            try:
                return DuelSubmission.objects.count(), DuelActionReceipt.objects.count(), self.revision()
            finally:
                connections["default"].close()
        with patch.object(DuelActionReceipt, "save", paused_receipt_save):
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(writer)
                try:
                    self.assertTrue(ready.wait(timeout=10))
                    self.assertEqual(pool.submit(reader).result(timeout=10), (0, 0, 0))
                finally:
                    release.set()
                future.result(timeout=20)
        self.assert_consistent_commits(1)
        self.assertEqual(self.revision(), 1)

    def test_real_sqlite_writer_lock_uses_existing_whole_transaction_retry(self):
        failed_begin, attempts = Event(), []
        locker = sqlite3.connect(str(connection.settings_dict["NAME"]), isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        def worker():
            db = connections["default"]
            try:
                db.ensure_connection()
                with db.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout = 1")
                def observe(execute, sql, params, many, context):
                    if sql.startswith("BEGIN"):
                        attempts.append(sql)
                    try:
                        return execute(sql, params, many, context)
                    except OperationalError:
                        failed_begin.set()
                        raise
                with db.execute_wrapper(observe):
                    return self.commit(submission_kind=SubmissionKind.BID)
            finally:
                db.close()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker)
                try:
                    self.assertTrue(failed_begin.wait(timeout=10))
                finally:
                    locker.rollback()
                result = future.result(timeout=20)
        finally:
            locker.close()
        self.assertGreaterEqual(len(attempts), 2)
        self.assertTrue(all(sql == "BEGIN IMMEDIATE" for sql in attempts))
        self.assertEqual(result.data["acceptance_sequence"], 1)
        self.assertEqual(self.revision(), 1)
        self.assert_consistent_commits(1)
