import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4

from django.db import OperationalError, connection, connections

from .commands import HandlerResult
from .models import DuelActionReceipt, ReceiptStatus
from .test_commands import CommandFixture


class DuelCommandConcurrencyTests(CommandFixture):
    def setUp(self):
        super().setUp()
        if connection.vendor != "sqlite":
            self.skipTest("These tests exercise the project's SQLite IMMEDIATE strategy.")
        name = str(connection.settings_dict["NAME"])
        self.assertNotIn("mode=memory", name)
        self.assertNotEqual(name, ":memory:")
        self.assertTrue(Path(name).is_file(), "Concurrency tests require a real isolated SQLite file.")
        self.assertEqual(connection.transaction_mode, "IMMEDIATE")

    def threaded(self, *, same_action):
        barrier = Barrier(2)
        guard = Lock()
        handler_calls = []
        connection_ids = []
        begin_statements = []

        def handle(context):
            with guard:
                handler_calls.append(context.client_action_id)
            count = context.instance.rule_config.get("test_count", 0) + 1
            context.instance.rule_config = {"test_count": count}
            context.instance.save(update_fields=["rule_config"])
            time.sleep(0.03)
            return HandlerResult(ReceiptStatus.APPLIED, {"count": count})

        def worker(index):
            db = connections["default"]
            db.close()
            try:
                db.ensure_connection()
                with guard:
                    connection_ids.append(id(db.connection))
                def capture(execute, sql, params, many, context):
                    if sql.startswith("BEGIN"):
                        with guard:
                            begin_statements.append(sql)
                    return execute(sql, params, many, context)
                barrier.wait(timeout=10)
                with db.execute_wrapper(capture):
                    return self.command(
                        client_action_id=self.action_id if same_action else f"{self.action_id}:{index}", handler=handle,
                    )
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, index) for index in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(len(set(connection_ids)), 2)
        self.assertTrue(begin_statements)
        self.assertTrue(all(sql == "BEGIN IMMEDIATE" for sql in begin_statements))
        return results, handler_calls

    def test_nearly_simultaneous_identical_requests_apply_once_on_separate_connections(self):
        results, calls = self.threaded(same_action=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual({result.receipt_id for result in results}, {DuelActionReceipt.objects.get().pk})
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(results[0].data, results[1].data)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, 1)
        self.assertEqual(self.instance.rule_config, {"test_count": 1})

    def test_nearly_simultaneous_different_requests_keep_both_revisions_and_receipts(self):
        results, calls = self.threaded(same_action=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(result.state_revision for result in results), [1, 2])
        self.assertEqual(DuelActionReceipt.objects.count(), 2)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.state_revision, 2)
        self.assertEqual(self.instance.rule_config, {"test_count": 2})

    def test_actual_sqlite_writer_lock_is_retried_after_rollback(self):
        failed_begin = Event()
        attempts = []
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
                    return self.command()
            finally:
                db.close()

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker)
                try:
                    self.assertTrue(failed_begin.wait(timeout=10), "Expected a real blocked BEGIN IMMEDIATE.")
                finally:
                    locker.rollback()
                result = future.result(timeout=15)
        finally:
            locker.close()
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result.state_revision, 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def processes(self, *, same_action):
        # Child interpreters override the DB name BEFORE django.setup(), using
        # only the test runner's file. No manage.py migrate or live DB is used.
        script = r'''
import json, os, pathlib, sys, time
from uuid import UUID
from django.conf import settings
settings.DATABASES["default"]["NAME"] = sys.argv[1]
import django
django.setup()
from django.db import connections
from duel.commands import ActorContext, HandlerResult, execute_action
from duel.models import ReceiptActorType, ReceiptStatus
gate = pathlib.Path(sys.argv[2])
(gate / ("ready-" + sys.argv[3])).touch()
deadline = time.monotonic() + 30
while not (gate / "start").exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("Timed out waiting for test start gate")
    time.sleep(0.01)
def handle(ctx):
    count = ctx.instance.rule_config.get("test_count", 0) + 1
    ctx.instance.rule_config = {"test_count": count}
    ctx.instance.save(update_fields=["rule_config"])
    time.sleep(0.03)
    return HandlerResult(ReceiptStatus.APPLIED, {"count": count})
result = execute_action(instance_id=UUID(sys.argv[4]), unit_id=int(sys.argv[5]),
    actor=ActorContext(ReceiptActorType.PARTICIPANT, int(sys.argv[6])),
    client_action_id=sys.argv[7], action_type="test.increment", payload={"increment": 1},
    expected_revision=0, handler=handle)
print(json.dumps({"pid": os.getpid(), "receipt_id": str(result.receipt_id),
    "revision": result.state_revision, "data": result.data, "replayed": result.replayed}))
connections.close_all()
'''
        with TemporaryDirectory(prefix="duel-command-gate-") as temp:
            gate = Path(temp)
            env = os.environ.copy()
            env["DJANGO_SETTINGS_MODULE"] = os.environ.get("DJANGO_SETTINGS_MODULE", "games_website.settings")
            workers = []
            try:
                for index in range(2):
                    action_id = self.action_id if same_action else str(uuid4())
                    workers.append(subprocess.Popen(
                        [sys.executable, "-B", "-c", script, str(connection.settings_dict["NAME"]), str(gate),
                         str(index), str(self.instance.pk), str(self.unit.pk), str(self.party_a.pk), action_id],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    ))
                deadline = time.monotonic() + 30
                while not all((gate / f"ready-{index}").exists() for index in range(2)):
                    if time.monotonic() >= deadline or any(worker.poll() is not None for worker in workers):
                        self.fail("Child process failed or timed out before the test start gate.")
                    time.sleep(0.01)
                (gate / "start").touch()
                results = []
                for worker in workers:
                    stdout, stderr = worker.communicate(timeout=30)
                    self.assertEqual(worker.returncode, 0, stderr)
                    results.append(json.loads(stdout.strip().splitlines()[-1]))
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                    worker.communicate(timeout=5)
        self.assertEqual(len({result["pid"] for result in results}), 2)
        return results

    def test_identical_requests_from_two_real_processes_apply_once(self):
        results = self.processes(same_action=True)
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(results[0]["receipt_id"], results[1]["receipt_id"])
        self.assertEqual(results[0]["data"], results[1]["data"])
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.rule_config, {"test_count": 1})
        self.assertEqual(self.instance.state_revision, 1)

    def test_distinct_requests_from_two_real_processes_do_not_lose_updates(self):
        results = self.processes(same_action=False)
        self.assertEqual(sorted(result["revision"] for result in results), [1, 2])
        self.assertEqual(DuelActionReceipt.objects.count(), 2)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.rule_config, {"test_count": 2})
        self.assertEqual(self.instance.state_revision, 2)
