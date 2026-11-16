"""File-backed SQLite concurrency of the complete Estimation result batch."""
from pathlib import Path
from uuid import uuid4

from django.db import connection

from . import test_submissions_concurrency as concurrency
from .commands import ActionConflict, CommandRejected
from .controllers.estimation import RevealContext, RevealPhase
from .match_service import get_match_snapshot
from .models import DuelActionReceipt, DuelMatchOutcome, DuelScoreEntry, DuelSubmission, DuelUnitOutcome, ReceiptStatus
from .test_estimation_controller import EstimationFixture


class EstimationConcurrencyTests(EstimationFixture):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def setUp(self):
        super().setUp()
        if connection.vendor != "sqlite":
            self.skipTest("SQLite IMMEDIATE concurrency coverage.")
        self.assertTrue(Path(connection.settings_dict["NAME"]).is_file())
        self.assertNotIn("mode=memory", str(connection.settings_dict["NAME"]))
        self.assertEqual(connection.transaction_mode, "IMMEDIATE")

    def test_nearly_simultaneous_submissions_are_independent_and_safely_evaluated(self):
        results = self.parallel([lambda: self.answer("95"), lambda: self.answer("120", actor=self.actor_b)])
        self.assertEqual(sorted(result.state_revision for result in results), [1, 2])
        self.assertEqual(DuelSubmission.objects.count(), 2)
        self.assertEqual(self.evaluate().data["points"], {"a": 1, "b": 0})
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 5)

    def test_identical_parallel_results_complete_match_once(self):
        self.configure(threshold=1)
        self.both()
        key = str(uuid4())
        results = self.parallel([lambda: self.evaluate(client_action_id=key)] * 2)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(results[0].data, results[1].data)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.assertEqual(DuelMatchOutcome.objects.count(), 1)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 5)

    def test_distinct_parallel_evaluations_cannot_double_book(self):
        self.both()
        results = self.parallel([lambda: self.evaluate(), lambda: self.evaluate()])
        self.assertEqual(sum(result.status == ReceiptStatus.APPLIED for result in results), 1)
        self.assertEqual(sum(result.status == ReceiptStatus.REJECTED for result in results), 1)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 5)

    def test_conflicting_parallel_requests_preserve_first_complete_result(self):
        self.both()
        from dataclasses import replace
        from decimal import Decimal
        other = RevealContext(replace(self.question, target=Decimal(200)), RevealPhase.RESULT)
        key = str(uuid4())
        def conflicting_material():
            try:
                return self.evaluate(client_action_id=key, reveal=other)
            except CommandRejected as exc:
                return exc
        results = self.parallel([lambda: self.evaluate(client_action_id=key),
                                 conflicting_material])
        rejected = [result for result in results if isinstance(result, CommandRejected)]
        self.assertEqual(len(rejected), 1)
        self.assertIn(rejected[0].code, ("action_id_conflict", "estimation_material_mismatch"))
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.assertEqual(DuelUnitOutcome.objects.get().winner_id, self.party_a.pk)
        self.assertEqual(DuelActionReceipt.objects.filter(action_type="duel.estimation.evaluate").count(), 1)

    def test_evaluation_racing_last_answer_never_scores_a_partial_pair(self):
        self.answer("95")
        submission, evaluation = self.parallel([lambda: self.answer("120", actor=self.actor_b), lambda: self.evaluate()])
        self.assertEqual(submission.status, ReceiptStatus.APPLIED)
        if evaluation.status == ReceiptStatus.REJECTED:
            self.assertIn(evaluation.data["code"], ("invalid_phase", "missing_answers_unresolved"))
            self.assertFalse(DuelScoreEntry.objects.exists())
            self.assertEqual(self.evaluate().status, ReceiptStatus.APPLIED)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.get().winner_id, self.party_a.pk)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 5)

    def test_simultaneous_tie_inputs_do_not_use_acceptance_sequence_as_tiebreak(self):
        self.parallel([lambda: self.answer("90"), lambda: self.answer("110", actor=self.actor_b)])
        result = self.evaluate()
        self.assertEqual(result.data["points"], {"a": 1, "b": 1})
        self.assertEqual(result.data["round_outcome"], "draw")
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual((snapshot["party_a"]["round_wins"], snapshot["party_b"]["round_wins"]), (0, 0))
