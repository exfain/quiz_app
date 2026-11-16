"""Real SQLite file/independent connection coverage of binding idempotency."""
from pathlib import Path
from uuid import uuid4

from django.db import connection

from . import test_submissions_concurrency as concurrent
from .commands import ActionConflict
from .models import DuelEstimationQuestionBinding as Binding, DuelActionReceipt, ReceiptStatus
from .test_estimation_question_binding import BindingFixture


class EstimationBindingConcurrencyTests(BindingFixture):
    parallel = concurrent.SubmissionConcurrencyTests.parallel

    def setUp(self):
        super().setUp()
        if connection.vendor != "sqlite":
            self.skipTest("SQLite IMMEDIATE concurrency coverage.")
        self.assertTrue(Path(connection.settings_dict["NAME"]).is_file())
        self.assertEqual(connection.transaction_mode, "IMMEDIATE")

    def test_same_action_same_question_is_applied_once(self):
        key = str(uuid4())
        results = self.parallel([lambda: self.bind(client_action_id=key)] * 2)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(results[0].data, results[1].data)
        self.assertEqual(Binding.objects.count(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)
        self.assertEqual(results[0].state_revision, 1)

    def test_distinct_actions_same_question_do_not_duplicate_binding_or_revision(self):
        results = self.parallel([lambda: self.bind(), lambda: self.bind()])
        self.assertEqual(sum(result.status == ReceiptStatus.APPLIED for result in results), 1)
        self.assertEqual(sum(result.data.get("code") == "already_bound" for result in results), 1)
        self.assertEqual(Binding.objects.count(), 1)
        self.assertTrue(all(result.state_revision == 1 for result in results))

    def test_two_questions_cannot_replace_first_accepted_binding(self):
        results = self.parallel([lambda: self.bind(), lambda: self.bind(question_id=self.alternative.pk)])
        accepted = next(result for result in results if result.status == ReceiptStatus.APPLIED)
        self.assertEqual(sum(result.data.get("code") == "question_binding_conflict" for result in results), 1)
        self.assertEqual(Binding.objects.get().question_id, accepted.data["question_id"])
        self.assertEqual(Binding.objects.count(), 1)

    def test_reused_action_id_for_other_question_conflicts(self):
        key = str(uuid4())
        results = self.parallel([lambda: self.bind(client_action_id=key),
                                 lambda: self.bind(client_action_id=key, question_id=self.alternative.pk)])
        self.assertEqual(sum(isinstance(result, ActionConflict) for result in results), 1)
        self.assertEqual(Binding.objects.count(), 1)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_same_material_in_two_units_has_two_bindings_without_lost_revision(self):
        unit = self.prepared_unit()
        results = self.parallel([lambda: self.bind(), lambda: self.bind(unit_id=unit.pk)])
        self.assertTrue(all(result.status == ReceiptStatus.APPLIED for result in results))
        self.assertEqual(sorted(result.state_revision for result in results), [1, 2])
        self.assertEqual(Binding.objects.count(), 2)
