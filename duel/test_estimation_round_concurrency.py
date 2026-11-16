"""Phase/clock races on separate connections to a file-backed SQLite DB."""
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.db import connection

from . import test_submissions_concurrency as concurrency
from . import test_estimation_rounds as fixtures
from .commands import ActionConflict
from .controllers.estimation import commit_estimate, get_estimation_state
from .estimation_questions import get_estimation_question_context
from .estimation_rounds import advance_estimation_phase, reconcile_estimation_deadline
from .models import (
    DuelEstimationRoundState as State, EstimationPhase as Phase,
    DuelActionReceipt, DuelSubmission, DuelScoreEntry, ReceiptStatus,
)


class EstimationRoundConcurrencyTests(fixtures.PhaseFixture):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def setUp(self):
        super().setUp()
        if connection.vendor != "sqlite":
            self.skipTest("SQLite IMMEDIATE concurrency coverage.")
        self.assertTrue(Path(connection.settings_dict["NAME"]).is_file())
        self.assertNotIn("mode=memory", str(connection.settings_dict["NAME"]))
        self.assertEqual(connection.transaction_mode, "IMMEDIATE")
        self.start()

    def submit(self, actor=None, key=None):
        return commit_estimate(question=get_estimation_question_context(self.unit.pk),
            actor=actor or self.actor, client_action_id=key or str(uuid4()),
            payload={"value": "95"}, expected_revision=0)

    def reveal(self, target=Phase.SHOW_ANSWERS, key=None):
        return advance_estimation_phase(**self.command_args(
            target_phase=target, client_action_id=key or str(uuid4())))

    def revision(self):
        self.instance.refresh_from_db()
        return self.instance.state_revision

    def test_simultaneous_answers_close_once_without_reveal_or_lost_revision(self):
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=10)):
            results = self.parallel([lambda: self.submit(), lambda: self.submit(self.actor_b)])
        self.assertEqual(sorted(r.state_revision for r in results), [3, 4])
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertEqual(DuelSubmission.objects.count(), 2)
        self.assertEqual(self.revision(), 4)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertEqual(DuelActionReceipt.objects.filter(action_type="duel.submission.commit").count(), 2)

    def test_duplicate_second_answer_closes_once(self):
        self.answer()
        key = str(uuid4())
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=11)):
            results = self.parallel([lambda: self.submit(self.actor_b, key)]*2)
        self.assertEqual(sorted(r.replayed for r in results), [False, True])
        self.assertEqual(results[0].data, results[1].data)
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertEqual(self.revision(), 4)

    def test_last_answer_racing_early_reconciliation_is_not_blocked(self):
        self.answer()
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=59)):
            answer, reconciliation = self.parallel([
                lambda: self.submit(self.actor_b),
                lambda: reconcile_estimation_deadline(**self.command_args()),
            ])
        self.assertEqual(answer.status, ReceiptStatus.APPLIED)
        self.assertEqual(reconciliation.status, ReceiptStatus.REJECTED)
        self.assertIn(reconciliation.data["code"], ("phase_not_answering", "deadline_not_reached"))
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertEqual(self.revision(), 4)

    def test_answer_at_deadline_racing_closure_is_always_rejected(self):
        self.answer()
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=60)):
            answer, reconciliation = self.parallel([
                lambda: self.submit(self.actor_b),
                lambda: reconcile_estimation_deadline(**self.command_args()),
            ])
        self.assertEqual(answer.status, ReceiptStatus.REJECTED)
        self.assertEqual(reconciliation.status, ReceiptStatus.APPLIED)
        self.assertEqual(DuelSubmission.objects.count(), 1)
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertEqual(self.revision(), 4)
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_identical_reveals_replay_one_transition(self):
        self.both()
        key = str(uuid4())
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=20)):
            results = self.parallel([lambda: self.reveal(key=key)]*2)
        self.assertEqual(sorted(r.replayed for r in results), [False, True])
        self.assertEqual(results[0].data, results[1].data)
        self.assertEqual(self.phase(), Phase.SHOW_ANSWERS)
        self.assertEqual(self.revision(), 5)

    def test_distinct_reveal_requests_cannot_repeat_same_transition(self):
        self.both()
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=20)):
            results = self.parallel([lambda: self.reveal(), lambda: self.reveal()])
        self.assertEqual(sum(r.status == ReceiptStatus.APPLIED for r in results), 1)
        self.assertEqual(sum(r.status == ReceiptStatus.REJECTED for r in results), 1)
        self.assertEqual(self.phase(), Phase.SHOW_ANSWERS)
        self.assertEqual(self.revision(), 5)

    def test_conflicting_reveal_action_id_cannot_change_original_request(self):
        self.both()
        key = str(uuid4())
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=20)):
            results = self.parallel([lambda: self.reveal(key=key),
                lambda: self.reveal(Phase.SHOW_SOLUTION, key)])
        self.assertEqual(sum(isinstance(r, ActionConflict) for r in results), 1)
        self.assertIn(self.phase(), (Phase.AWAITING_REVEAL, Phase.SHOW_ANSWERS))
        self.assertLessEqual(self.revision(), 5)
        self.assertEqual(DuelActionReceipt.objects.filter(client_action_id=key).count(), 1)
        self.assertNotIn("target", self.snapshot())

    def test_distinct_ordered_reveals_never_skip_required_phase(self):
        self.both()
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=20)):
            answers, solution = self.parallel([lambda: self.reveal(), lambda: self.reveal(Phase.SHOW_SOLUTION)])
        self.assertEqual(answers.status, ReceiptStatus.APPLIED)
        if solution.status == ReceiptStatus.REJECTED:
            self.assertEqual(self.phase(), Phase.SHOW_ANSWERS)
            self.assertNotIn("target", self.snapshot())
        else:
            self.assertEqual(self.phase(), Phase.SHOW_SOLUTION)
        self.assertEqual(self.revision(), 5 + (solution.status == ReceiptStatus.APPLIED))
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_reconnect_snapshot_racing_deadline_is_read_only_and_has_no_solution(self):
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=60)):
            snapshot, reconciliation = self.parallel([
                lambda: get_estimation_state(unit_id=self.unit.pk, actor=self.actor),
                lambda: reconcile_estimation_deadline(**self.command_args()),
            ])
        self.assertNotIn("target", snapshot)
        self.assertFalse(snapshot["can_answer"])
        self.assertIn(snapshot["phase"], (Phase.ANSWERING, Phase.AWAITING_REVEAL))
        self.assertEqual(reconciliation.status, ReceiptStatus.APPLIED)
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertEqual(self.revision(), 3)
        self.assertEqual(DuelActionReceipt.objects.count(), 3)

    def test_reveal_racing_deadline_closure_never_reveals_solution(self):
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=60)):
            reconciliation, reveal = self.parallel([
                lambda: reconcile_estimation_deadline(**self.command_args()), lambda: self.reveal(),
            ])
        self.assertEqual(reconciliation.status, ReceiptStatus.APPLIED)
        self.assertEqual(self.phase(), Phase.SHOW_ANSWERS if reveal.status == ReceiptStatus.APPLIED else Phase.AWAITING_REVEAL)
        self.assertNotIn("target", self.snapshot(at=61))
        self.assertFalse(DuelScoreEntry.objects.exists())
