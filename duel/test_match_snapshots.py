import json

from .contracts import MatchStatus, RoundOutcome
from .matches import MatchConfig, MatchMode, MatchEvaluationState
from .match_service import get_match_snapshot, get_match_result, evaluate_current_match, start_match, MatchIntegrityError
from .models import DuelGameInstance, DuelScoreEntry, DuelSubmission, DuelActionReceipt, DuelUnitOutcome
from .test_match_service import MatchFixture


class MatchSnapshotTests(MatchFixture):
    def test_best_of_progress_reads_round_results_not_points(self):
        self.configure(threshold=7)
        self.score(self.party_b, 800)
        for index, outcome in enumerate((RoundOutcome.PARTY_A_WINS, RoundOutcome.PARTY_B_WINS, RoundOutcome.PARTY_A_WINS)):
            self.finish(outcome, unit_id=self.unit.pk if index == 0 else self.next_unit().pk)
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["required_wins"], 4)
        self.assertEqual(snapshot["best_of"], 7)
        self.assertEqual(snapshot["completed_units"], 3)
        self.assertEqual(snapshot["party_a"]["round_wins"], 2)
        self.assertEqual(snapshot["party_b"], {"participant_id": self.party_b.pk, "score": 800, "round_wins": 1})
        self.assertEqual(snapshot["evaluation"], "running")
        self.assertIsNone(get_match_result(self.instance.pk))

    def test_target_progress_keeps_negative_and_over_target_totals(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score(self.party_a, -2)
        self.score(self.party_b, 14)
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["target_score"], 10)
        self.assertEqual((snapshot["party_a"]["score"], snapshot["party_b"]["score"]), (-2, 14))
        self.assertIsNone(snapshot["winner_id"])  # candidate, not a persisted completion
        self.assertEqual(evaluate_current_match(self.instance.pk).state, MatchEvaluationState.PARTY_B_WINS)

    def test_draw_no_winner_and_zero_scores_are_explicit(self):
        self.finish(RoundOutcome.DRAW)
        self.finish(RoundOutcome.NO_WINNER, unit_id=self.next_unit().pk)
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["completed_units"], 2)
        self.assertEqual(snapshot["evaluation"], "running")
        self.assertIsNone(snapshot["resolution_reason"])
        self.assertEqual(snapshot["party_a"]["round_wins"], 0)
        self.assertIsNone(snapshot["winner_id"])

    def test_completed_match_reloads_identically(self):
        self.configure(threshold=1)
        self.finish()
        first = get_match_result(self.instance.pk)
        self.instance = DuelGameInstance.objects.get(pk=self.instance.pk)
        self.assertEqual(first, get_match_result(self.instance.pk))
        self.assertEqual(first["winner_id"], self.party_a.pk)
        self.assertEqual(first["match_status"], "completed")
        self.assertIsNotNone(first["completed_at"])

    def test_rule_snapshot_is_preserved_for_historical_completion(self):
        self.configure(threshold=1)
        self.finish()
        before = get_match_result(self.instance.pk)
        # Raw ORM bypass is not an authorized change; historical rule stays readable.
        DuelGameInstance.objects.filter(pk=self.instance.pk).update(rule_config={"match": {"mode": "target_score", "target_score": 99}})
        self.assertEqual(get_match_result(self.instance.pk), before)

    def test_new_instance_in_same_session_has_no_old_wins_or_points(self):
        self.configure(threshold=1)
        self.score(self.party_a, 7)
        self.finish()
        start_match(instance_id=self.other_instance.pk, actor=self.host, client_action_id="new-match",
                    config=MatchConfig(MatchMode.BEST_OF_N, best_of=3))
        snapshot = get_match_snapshot(self.other_instance.pk)
        self.assertEqual(snapshot["completed_units"], 0)
        self.assertEqual(snapshot["party_a"]["score"], 0)
        self.assertEqual(snapshot["party_a"]["round_wins"], 0)
        self.assertIsNone(snapshot["winner_id"])

    def test_snapshot_does_not_expose_answers_receipts_or_game_secrets(self):
        self.instance.rule_config["answer"] = "SECRET_SOLUTION"
        self.instance.rule_config["host_token"] = "SECRET_TOKEN"
        self.instance.save()
        DuelSubmission.objects.create(unit=self.unit, participant=self.party_b, submission_kind="answer", payload={"answer": "SECRET_DRAFT"})
        DuelActionReceipt.objects.create(game_instance=self.instance, actor_type="host", actor_key=self.host.key,
            client_action_id="SECRET_ACTION_ID", action_type="test.private", request_fingerprint="f" * 64,
            status="applied", result={"private": "SECRET_RESULT"})
        data = json.dumps(get_match_snapshot(self.instance.pk))
        for secret in ("SECRET_SOLUTION", "SECRET_TOKEN", "SECRET_DRAFT", "SECRET_ACTION_ID", "SECRET_RESULT"):
            self.assertNotIn(secret, data)

    def test_invalid_config_is_explicit_without_a_default(self):
        self.instance.rule_config = {}
        self.instance.save()
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["evaluation"], "invalid_configuration")
        self.assertIsNone(snapshot["mode"])
        self.assertIsNone(snapshot["winner_id"])

    def test_snapshot_is_read_only(self):
        before = DuelGameInstance.objects.filter(pk=self.instance.pk).values().get()
        get_match_snapshot(self.instance.pk)
        self.assertEqual(DuelGameInstance.objects.filter(pk=self.instance.pk).values().get(), before)
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_closed_without_winner_record_is_integrity_error(self):
        self.instance.match_status = MatchStatus.COMPLETED.value
        self.instance.save()
        with self.assertRaises(MatchIntegrityError):
            get_match_snapshot(self.instance.pk)

    def test_raw_foreign_score_or_unit_winner_cannot_decide_match(self):
        self.score(self.third, 1000)
        with self.assertRaises(MatchIntegrityError):
            get_match_snapshot(self.instance.pk)
        DuelScoreEntry.objects.filter(participant=self.third).delete()
        self.finish()
        DuelUnitOutcome.objects.filter(unit=self.unit).update(winner=self.third)
        with self.assertRaises(MatchIntegrityError):
            get_match_snapshot(self.instance.pk)

    def test_raw_correction_of_closed_scores_does_not_reopen_match(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score_batch([(self.party_a, 10), (self.party_b, 7)])
        self.score(self.party_a, -5)
        with self.assertRaises(MatchIntegrityError):
            get_match_result(self.instance.pk)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.COMPLETED.value)
