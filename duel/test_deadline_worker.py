import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from datetime import timedelta
from unittest.mock import patch
from contextlib import contextmanager

from django.db import connection
from django.utils import timezone
from . import test_estimation_rounds as fixtures
from .deadlines import reconcile_due_rounds
from .models import DuelEstimationRoundState, DuelScoreEntry, DuelSubmission
from . import test_submissions_concurrency as concurrency
from .controllers.estimation import commit_estimate
from .estimation_questions import get_estimation_question_context


@contextmanager
def worker_environment():
    """A real child process pointed exclusively at this isolated test DB."""
    root=Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix='duel-worker-') as directory:
        settings_name=os.environ['DJANGO_SETTINGS_MODULE']
        text=f'from {settings_name} import *\nfrom copy import deepcopy\nDATABASES=deepcopy(DATABASES)\nDATABASES["default"]["NAME"]={str(connection.settings_dict["NAME"])!r}\n'
        Path(directory,'worker_test_settings.py').write_text(text,encoding='utf-8')
        env=os.environ.copy();env['PYTHONPATH']=directory+os.pathsep+env.get('PYTHONPATH','')
        yield [sys.executable,'-B','manage.py','duel_deadlines','--settings=worker_test_settings'], {'cwd':root,'env':env}


class DeadlineWorkerTests(fixtures.PhaseFixture):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def setUp(self):
        super().setUp()
        self.hub.session_type='duel';self.hub.save(update_fields=['session_type'])
        self.step.game_format='duel';self.step.save(update_fields=['game_format'])

    def test_explicit_duels_inside_multiplayer_sessions_are_scheduled(self):
        self.start(seconds=1)
        self.hub.session_type='multiplayer';self.hub.save(update_fields=['session_type'])
        with patch('django.utils.timezone.now',return_value=self.clock+timedelta(seconds=2)):
            self.assertEqual(reconcile_due_rounds(),1)
            self.assertEqual(reconcile_due_rounds(),0)
        self.assertEqual(DuelEstimationRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')

    def test_standard_slots_are_not_scheduled_as_duels(self):
        self.start(seconds=1)
        self.hub.session_type='multiplayer';self.hub.save(update_fields=['session_type'])
        self.step.game_format='standard';self.step.save(update_fields=['game_format'])
        with patch('django.utils.timezone.now',return_value=self.clock+timedelta(seconds=2)):
            self.assertEqual(reconcile_due_rounds(),0)
        self.assertEqual(DuelEstimationRoundState.objects.get(unit=self.unit).phase,'answering')

    def test_two_worker_connections_close_due_round_only_once(self):
        self.start(seconds=1)
        with patch('django.utils.timezone.now',return_value=self.clock+timedelta(seconds=2)):
            counts=self.parallel([reconcile_due_rounds,reconcile_due_rounds])
        self.assertEqual(sorted(counts),[0,1])
        self.assertEqual(DuelEstimationRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_second_answer_racing_actual_worker_at_deadline_cannot_be_accepted(self):
        self.start(seconds=10);self.answer(at=1)
        with patch('django.utils.timezone.now',return_value=self.clock+timedelta(seconds=10)):
            count,answer=self.parallel([reconcile_due_rounds,lambda:commit_estimate(
                question=get_estimation_question_context(self.unit.pk),actor=self.actor_b,
                client_action_id='racing-deadline',payload={'value':'120'})])
        self.assertEqual(count,1);self.assertEqual(answer.status.value,'rejected')
        self.assertEqual(DuelSubmission.objects.count(),1)
        self.assertEqual(DuelEstimationRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_valid_second_answer_racing_early_worker_closes_once(self):
        self.start(seconds=10);self.answer(at=1)
        with patch('django.utils.timezone.now',return_value=self.clock+timedelta(seconds=9)):
            count,answer=self.parallel([reconcile_due_rounds,lambda:commit_estimate(
                question=get_estimation_question_context(self.unit.pk),actor=self.actor_b,
                client_action_id='racing-before-deadline',payload={'value':'120'})])
        self.assertEqual(count,0);self.assertEqual(answer.status.value,'applied')
        self.assertEqual(DuelSubmission.objects.count(),2)
        self.assertEqual(DuelEstimationRoundState.objects.get(unit=self.unit).phase,'awaiting_reveal')
    def test_no_browser_needed_due_clock_closes_without_revealing_or_scoring(self):
        self.start(seconds=1)
        with patch('django.utils.timezone.now',return_value=self.clock+timedelta(seconds=2)):
            self.assertEqual(reconcile_due_rounds(),1)
            self.assertEqual(reconcile_due_rounds(),0)
        state=DuelEstimationRoundState.objects.get(unit=self.unit)
        self.assertEqual(state.phase,'awaiting_reveal')
        self.assertEqual(state.answering_closed_at,state.deadline)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertEqual(self.answer(at=2).status.value,'rejected')

    def test_fresh_worker_process_recovers_overdue_round_and_restart_is_idempotent(self):
        self.clock=timezone.now()-timedelta(seconds=4)
        self.start(seconds=1)
        root=Path(__file__).resolve().parents[1]
        with TemporaryDirectory(prefix='duel-worker-') as directory:
            settings_name=os.environ['DJANGO_SETTINGS_MODULE']
            text=f'from {settings_name} import *\nfrom copy import deepcopy\nDATABASES=deepcopy(DATABASES)\nDATABASES["default"]["NAME"]={str(connection.settings_dict["NAME"])!r}\n'
            Path(directory,'worker_test_settings.py').write_text(text,encoding='utf-8')
            env=os.environ.copy();env['PYTHONPATH']=directory+os.pathsep+env.get('PYTHONPATH','')
            command=[sys.executable,'-B','manage.py','duel_deadlines','--once','--settings=worker_test_settings']
            first=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=35)
            self.assertEqual(first.returncode,0,first.stderr)
            self.assertEqual(first.stdout.strip(),'1')
            before=DuelEstimationRoundState.objects.values().get(unit=self.unit)
            second=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=35)
            self.assertEqual(second.returncode,0,second.stderr)
            self.assertEqual(second.stdout.strip(),'0')
            self.assertEqual(DuelEstimationRoundState.objects.values().get(unit=self.unit),before)
            self.assertFalse(DuelScoreEntry.objects.exists())
