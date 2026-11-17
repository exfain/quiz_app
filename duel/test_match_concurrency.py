from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from uuid import uuid4

from django.db import connection, connections

from .commands import ActionConflict, HandlerResult, execute_action
from .contracts import RoundOutcome
from .matches import MatchMode
from .match_service import get_match_snapshot, reconcile_match_in_action
from .models import DuelActionReceipt, DuelUnitOutcome, DuelMatchOutcome, DuelScoreEntry, ReceiptStatus
from .test_match_service import MatchFixture


class MatchConcurrencyTests(MatchFixture):
    def setUp(self):
        super().setUp()
        if connection.vendor != "sqlite":
            self.skipTest("SQLite file/IMMEDIATE concurrency coverage.")
        self.assertTrue(Path(connection.settings_dict["NAME"]).is_file())
        self.assertNotIn("mode=memory", str(connection.settings_dict["NAME"]))
        self.assertEqual(connection.transaction_mode, "IMMEDIATE")

    def parallel(self, operations):
        barrier = Barrier(len(operations))
        ids, guard = [], Lock()
        def worker(operation):
            db = connections["default"]
            try:
                db.ensure_connection()
                with guard:
                    ids.append(id(db.connection))
                barrier.wait(timeout=10)
                try:
                    return operation()
                except ActionConflict as exc:
                    return exc
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(worker, operation) for operation in operations]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(len(set(ids)), len(operations))
        return results

    def test_identical_parallel_unit_completion_once_including_match_result(self):
        self.configure(threshold=1)
        key = str(uuid4())
        results = self.parallel([lambda: self.finish(client_action_id=key)] * 2)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.assertEqual(DuelMatchOutcome.objects.count(), 1)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 1)

    def test_conflicting_same_action_id_cannot_rewrite_unit(self):
        key = str(uuid4())
        results = self.parallel([lambda: self.finish(client_action_id=key),
            lambda: self.finish(RoundOutcome.PARTY_B_WINS, client_action_id=key)])
        self.assertEqual(sum(isinstance(result, ActionConflict) for result in results), 1)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 1)

    def test_different_ids_for_same_unit_count_one_round_win(self):
        results = self.parallel([lambda: self.finish(), lambda: self.finish(RoundOutcome.PARTY_B_WINS)])
        self.assertEqual(sum(result.status == ReceiptStatus.APPLIED for result in results), 1)
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["party_a"]["round_wins"] + snapshot["party_b"]["round_wins"], 1)
        self.assertEqual(snapshot["state_revision"], 1)

    def test_parallel_different_units_keep_both_wins_and_single_match_completion(self):
        other = self.next_unit()
        results = self.parallel([lambda: self.finish(), lambda: self.finish(unit_id=other.pk)])
        self.assertTrue(all(result.status == ReceiptStatus.APPLIED for result in results))
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["party_a"]["round_wins"], 2)
        self.assertEqual(snapshot["state_revision"], 2)
        self.assertEqual(DuelMatchOutcome.objects.count(), 1)

    def test_identical_and_distinct_parallel_finalizations_never_duplicate_completion(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score(self.party_a, 10)
        key = str(uuid4())
        results = self.parallel([lambda: self.finalize(client_action_id=key)] * 2)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        repeated = self.parallel([lambda: self.finalize(), lambda: self.finalize()])
        self.assertTrue(all(result.status == ReceiptStatus.REJECTED for result in repeated))
        self.assertEqual(DuelMatchOutcome.objects.count(), 1)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 1)

    def test_parallel_score_batches_do_not_lose_bookings_or_revisions(self):
        self.configure(MatchMode.TARGET_SCORE, 100)
        results = self.parallel([lambda: self.score_batch([(self.party_a, 3)]),
                                 lambda: self.score_batch([(self.party_a, 4)])])
        self.assertTrue(all(result.status == ReceiptStatus.APPLIED for result in results))
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["party_a"]["score"], 7)
        self.assertEqual(snapshot["state_revision"], 2)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)

    def test_other_instance_in_same_session_does_not_receive_outcomes(self):
        self.other_instance.match_status = self.instance.match_status
        self.other_instance.rule_config = self.instance.rule_config
        self.other_instance.save()
        self.other_unit.status = self.unit.status
        self.other_unit.save()
        self.parallel([lambda: self.finish(), lambda: self.finish(instance_id=self.other_instance.pk, unit_id=self.other_unit.pk)])
        for instance in (self.instance, self.other_instance):
            snapshot = get_match_snapshot(instance.pk)
            self.assertEqual(snapshot["completed_units"], 1)
            self.assertEqual(snapshot["state_revision"], 1)

    def test_snapshot_cannot_observe_half_of_score_batch(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score(self.party_a, 9)
        self.score(self.party_b, 9)
        first_written, release, reader_started = Event(), Event(), Event()
        def handler(context):
            DuelScoreEntry.objects.create(game_instance=context.instance, participant=self.party_a, delta=1, source_key="batch-a")
            first_written.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Test batch release timed out.")
            DuelScoreEntry.objects.create(game_instance=context.instance, participant=self.party_b, delta=1, source_key="batch-b")
            evaluation = reconcile_match_in_action(context)
            return HandlerResult(ReceiptStatus.APPLIED, {"evaluation": evaluation.state.value})
        def writer():
            try:
                return execute_action(instance_id=self.instance.pk, actor=self.actor, client_action_id="atomic-batch",
                    action_type="test.atomic_batch", payload={}, handler=handler)
            finally:
                connections["default"].close()
        def reader():
            try:
                reader_started.set()
                return get_match_snapshot(self.instance.pk)
            finally:
                connections["default"].close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            write_future = pool.submit(writer)
            try:
                self.assertTrue(first_written.wait(timeout=10))
                read_future = pool.submit(reader)
                self.assertTrue(reader_started.wait(timeout=10))
                # A SQLite reader sees the previous COMPLETE committed batch
                # without reserving a writer lock or seeing the first new row.
                before = read_future.result(timeout=3)
                self.assertEqual((before['party_a']['score'], before['party_b']['score']), (9, 9))
                self.assertEqual(before['evaluation'], 'running')
            finally:
                release.set()
            write_future.result(timeout=20)
            read_future.result(timeout=20)
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual((snapshot["party_a"]["score"], snapshot["party_b"]["score"]), (10, 10))
        self.assertEqual(snapshot["evaluation"], "sudden_death")
        self.assertIsNone(snapshot["winner_id"])
        self.assertFalse(DuelMatchOutcome.objects.exists())
