"""Read-only polling must not reserve SQLite's sole writer lock."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from django.db import connections, transaction
from django.test.utils import CaptureQueriesContext

from . import tests as reference
from .models import Run
from .service import snapshot
from games_hub.models import HubParticipant


class SnapshotContentionTests(reference.Fixture):
    def test_25_team_snapshots_have_bounded_queries_and_keep_role_projections(self):
        for n in range(3,25):
            self.parties.append(HubParticipant.objects.create(session=self.hub,nickname=f'Team {n}',scoring_eligible=True,is_active=True))
        self.hub.locked_participant_count=25;self.hub.save(update_fields=['locked_participant_count'])
        self.start();self.assertEqual(self.post('prepare',task_id=self.task.pk,previous_round_id=None).status_code,200)
        db=connections['default']
        for role,budget in [('host',8),('participant',7),('spectator',5)]:
            with self.subTest(role=role):
                with CaptureQueriesContext(db) as queries:
                    data=snapshot(self.run.pk,role,self.parties[0] if role=='participant' else None)
                self.assertLessEqual(len(queries),budget,'Polling must not query each team separately')
                current=data['round'];self.assertEqual(current['team_count'],25)
                self.assertEqual((current['ready_count'],current['target_count']),(0,0))
                if role=='host':self.assertEqual(len(current['teams']),25)
                else:
                    self.assertNotIn('teams',current);self.assertNotIn('solutions',current)
                if role=='participant':self.assertEqual(current['own']['name'],self.parties[0].nickname)

    def test_independent_snapshot_can_finish_while_another_reader_is_open(self):
        self.start()
        if connections['default'].vendor!='sqlite':self.skipTest('SQLite reservation regression')
        held=Event();release=Event()
        def reader(pause=False):
            connections.close_all()
            db=connections['default']
            def hold(execute,sql,params,many,context):
                result=execute(sql,params,many,context)
                if pause and not held.is_set() and sql.lstrip().upper().startswith('SELECT') and '"target_game_run"' in sql:
                    held.set()
                    if not release.wait(10):raise AssertionError('Reader release was not signalled')
                return result
            try:
                with db.execute_wrapper(hold):return snapshot(self.run.pk,'spectator')
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(reader,True)
            try:
                self.assertTrue(held.wait(5),'First reader never entered its snapshot')
                second=pool.submit(reader)
                try:result=second.result(timeout=3)
                except TimeoutError:self.fail('A read-only snapshot reserved the writer lock and blocked another reader')
                self.assertEqual(result['run_id'],str(self.run.pk))
            finally:
                release.set();first.result(timeout=5)

    def test_snapshot_restores_write_mode_after_success_failure_and_nesting(self):
        self.start();db=connections['default'];db.ensure_connection()
        if db.vendor!='sqlite':self.skipTest('SQLite transaction-mode regression')
        original=db.transaction_mode
        with CaptureQueriesContext(db) as queries:snapshot(self.run.pk,'host')
        self.assertEqual(db.transaction_mode,original)
        self.assertIn('BEGIN DEFERRED',[q['sql'] for q in queries])
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        from uuid import uuid4
        with self.assertRaises(Run.DoesNotExist):snapshot(uuid4(),'host')
        self.assertEqual(db.transaction_mode,original)
        with CaptureQueriesContext(db) as queries:
            with transaction.atomic():
                snapshot(self.run.pk,'host')
                self.assertEqual(db.transaction_mode,original)
        self.assertIn('BEGIN '+original if original else 'BEGIN',[q['sql'] for q in queries])
