"""Polling separate encounters must not compete for SQLite's writer lock."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from django.db import connections, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import test_estimation_rounds as fixtures
from .controllers.estimation import get_estimation_state
from .match_service import get_match_snapshot
from .read_transactions import snapshot_transaction


class SnapshotContentionTests(fixtures.PhaseFixture):
    def setUp(self):
        super().setUp()
        self.step.game_format = 'duel'
        self.step.save(update_fields=['game_format'])

    def test_estimation_browser_bounds_persisted_row_validation_queries(self):
        with CaptureQueriesContext(connections['default']) as queries:
            response=Client().get(reverse('duel:match_api', args=[self.hub.code,self.instance.pk,'spectator']))
        self.assertEqual(response.status_code,200,response.content)
        self.assertNotIn('target',response.json()['unit'])
        self.assertNotIn('BEGIN IMMEDIATE',[q['sql'] for q in queries],
            'The pre-snapshot permission/identity read must not reserve a writer either')
        # Before: 69 roundtrips; after: 47, retaining field/cross-table checks.
        self.assertLessEqual(len(queries),50,'Repeated persisted-row SQL validation amplified every device poll')

    def test_independent_readers_do_not_reserve_the_writer_lock(self):
        if connections['default'].vendor != 'sqlite':
            self.skipTest('SQLite reader reservation regression')
        for projection in ('round', 'match', 'browser'):
            with self.subTest(projection=projection):
                held, release = Event(), Event()
                def reader(pause=False):
                    connections.close_all()
                    db = connections['default']
                    def observe(execute, sql, params, many, context):
                        result = execute(sql, params, many, context)
                        if pause and not held.is_set() and sql.lstrip().upper().startswith('SELECT'):
                            held.set()
                            if not release.wait(10):
                                raise AssertionError('Reader was not released')
                        return result
                    try:
                        with db.execute_wrapper(observe):
                            if projection == 'round':
                                return get_estimation_state(instance_id=self.instance.pk, unit_id=self.unit.pk)
                            if projection == 'browser':
                                response = Client().get(reverse('duel:match_api', args=[self.hub.code, self.instance.pk, 'spectator']))
                                self.assertEqual(response.status_code, 200, response.content)
                                return response.json()
                            return get_match_snapshot(self.instance.pk)
                    finally:
                        connections.close_all()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(reader, True)
                    try:
                        self.assertTrue(held.wait(5))
                        second = pool.submit(reader)
                        try:
                            result = second.result(timeout=3)
                        except TimeoutError:
                            self.fail('Read-only polling reserved the writer lock')
                        self.assertIn('state_revision', result)
                    finally:
                        release.set()
                        first.result(timeout=5)

    def test_read_mode_is_local_restored_and_reuses_an_enclosing_write(self):
        db = connections['default']
        db.ensure_connection()
        if db.vendor != 'sqlite':
            self.skipTest('SQLite transaction mode regression')
        original = db.transaction_mode
        with self.assertRaisesRegex(RuntimeError, 'test read failure'):
            with snapshot_transaction():
                raise RuntimeError('test read failure')
        self.assertEqual(db.transaction_mode, original)
        readers = (lambda: get_estimation_state(instance_id=self.instance.pk, unit_id=self.unit.pk),
                   lambda: get_match_snapshot(self.instance.pk))
        for reader in readers:
            with CaptureQueriesContext(db) as queries:
                reader()
            self.assertIn('BEGIN DEFERRED', [q['sql'] for q in queries])
            self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
            self.assertEqual(db.transaction_mode, original)
            with transaction.atomic():
                reader()
                self.assertEqual(db.transaction_mode, original)
