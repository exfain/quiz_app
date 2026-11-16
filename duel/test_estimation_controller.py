"""Exact Estimation duel rules, authoritative commands and phased projections."""
import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import SimpleTestCase
from django.utils import timezone

from Estimation.models import EstimationQuestion, EstimationQuiz, EstimationAnswer, EstimationParticipant

from . import test_match_service as match_tests
from .commands import ActorContext, ActionConflict, CommandRejected
from .contracts import RoundOutcome
from .controllers.estimation import (
    AnswerAvailability, QuestionContext, RevealContext, RevealPhase,
    apply_estimation_result, commit_estimate, evaluate_estimates, get_estimation_state,
    load_question_context, parse_estimate, _project_estimation as project_estimation,
)
from .match_service import get_match_snapshot, MatchIntegrityError
from .matches import MatchMode
from .models import (
    DuelActionReceipt, DuelGameInstance, DuelMatchOutcome, DuelScoreEntry, DuelSubmission,
    DuelUnit, DuelUnitOutcome, DuelEstimationQuestionBinding, DuelEstimationRoundState,
    EstimationPhase, ReceiptActorType, ReceiptStatus, SubmissionKind,
)
from .submissions import save_draft
from .estimation_questions import canonical_target
from .estimation_rounds import show_estimation_answers, show_estimation_solution, reconcile_estimation_deadline


class EstimationRuleTests(SimpleTestCase):
    def test_a_closer_gets_one_b_zero(self):
        result = evaluate_estimates(100, 95, 120)
        self.assertEqual((result.deviation_a, result.deviation_b), (Decimal(5), Decimal(20)))
        self.assertEqual((result.points_a, result.points_b), (1, 0))
        self.assertEqual(result.round_result.outcome, RoundOutcome.PARTY_A_WINS)

    def test_b_closer_gets_one_a_zero(self):
        result = evaluate_estimates(100, 120, 95)
        self.assertEqual((result.points_a, result.points_b), (0, 1))
        self.assertEqual(result.round_result.outcome, RoundOutcome.PARTY_B_WINS)

    def test_tie_gives_two_points_but_no_round_winner(self):
        result = evaluate_estimates(100, 90, 110)
        self.assertEqual((result.points_a, result.points_b), (1, 1))
        self.assertEqual(result.round_result.outcome, RoundOutcome.DRAW)

    def test_negative_values_use_absolute_not_percentage_deviation(self):
        result = evaluate_estimates(-20, -15, -40)
        self.assertEqual((result.deviation_a, result.deviation_b), (Decimal(5), Decimal(20)))

    def test_zero_target_supports_closest_and_tied_answers(self):
        result = evaluate_estimates(0, -1, 2)
        self.assertEqual(result.round_result.outcome, RoundOutcome.PARTY_A_WINS)
        self.assertEqual(evaluate_estimates(0, -2, 2).round_result.outcome, RoundOutcome.DRAW)

    def test_decimal_strings_have_no_float_rounding_tiebreak(self):
        result = evaluate_estimates("0.3", "0.2", "0.4")
        self.assertEqual(result.deviation_a, Decimal("0.1"))
        self.assertEqual(result.deviation_b, Decimal("0.1"))
        self.assertEqual(result.round_result.outcome, RoundOutcome.DRAW)

    def test_long_integer_coefficients_remain_distinguishable(self):
        a, b = "100000000000000000000000000000000000001", "100000000000000000000000000000000000002"
        result = evaluate_estimates(0, a, b)
        self.assertEqual(result.deviation_a, Decimal(a))
        self.assertEqual(result.deviation_b, Decimal(b))
        self.assertEqual(result.round_result.outcome, RoundOutcome.PARTY_A_WINS)

    def test_large_opposite_signed_numbers_produce_exact_finite_decimal_difference(self):
        result = evaluate_estimates("-1e308", "1e308", "-1e308")
        self.assertEqual(result.deviation_a, Decimal("2e308"))
        self.assertEqual(result.deviation_b, Decimal(0))
        self.assertEqual(result.round_result.outcome, RoundOutcome.PARTY_B_WINS)

    def test_very_small_values_survive_float_underflow(self):
        result = evaluate_estimates("1e-400", "0", "2e-400")
        self.assertEqual(result.deviation_a, Decimal("1e-400"))
        self.assertEqual(result.deviation_b, Decimal("1e-400"))
        self.assertEqual(result.round_result.outcome, RoundOutcome.DRAW)

    def test_calculation_does_not_depend_on_ambient_decimal_precision(self):
        with localcontext() as context:
            context.prec = 2
            context.Emax = 2
            context.Emin = -2
            result = evaluate_estimates("123456789.123456789", "123456789.123456788", "123456789.123456791")
        self.assertEqual(result.deviation_a, Decimal("0.000000001"))
        self.assertEqual(result.deviation_b, Decimal("0.000000002"))

    def test_valid_decimal_conventions(self):
        for raw, expected in ((" +12.5 ", "12.5"), (".5", "0.5"), ("1.", "1"),
                              ("-2e-3", "-0.002"), (0, "0"), (1.25, "1.25")):
            with self.subTest(raw=raw):
                self.assertEqual(parse_estimate(raw), Decimal(expected))

    def test_invalid_formats_nonfinite_and_json_types_rejected(self):
        for raw in ("", " ", "NaN", "Infinity", "-Infinity", "1e309", "1,5", "1 000", "1_000", "2 m", "0x10",
                    True, False, None, {}, [], float("nan"), float("inf"), Decimal("NaN"), "1e-100000000",
                    "1e-999999999999999999999999999"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_estimate(raw)

    def test_missing_answers_have_explicit_final_scores(self):
        for a, b, state in ((None, 95, AnswerAvailability.ONLY_B), (95, None, AnswerAvailability.ONLY_A),
                             (None, None, AnswerAvailability.NONE)):
            with self.subTest(state=state):
                result = evaluate_estimates(100, a, b)
                self.assertTrue(result.evaluable)
                self.assertEqual(result.availability, state)
                self.assertEqual((result.points_a, result.points_b), (int(a is not None), int(b is not None)))
                self.assertEqual(result.round_result.outcome, RoundOutcome.PARTY_A_WINS if a is not None else RoundOutcome.PARTY_B_WINS if b is not None else RoundOutcome.NO_WINNER)

    def context(self, unit="meters"):
        return QuestionContext(uuid4(), 1, 2, "Wie hoch?", Decimal("100"), unit)

    def test_pure_projections_reveal_only_authorized_fields_in_sequence(self):
        question = self.context()
        for phase in RevealPhase:
            with self.subTest(phase=phase):
                state = project_estimation(reveal=RevealContext(question, phase), answer_a=Decimal(95), answer_b=Decimal(120))
                self.assertEqual("answers" in state, phase != RevealPhase.QUESTION)
                self.assertEqual("target" in state, phase in (RevealPhase.SOLUTION, RevealPhase.RESULT))
                self.assertEqual("deviations" in state, phase in (RevealPhase.SOLUTION, RevealPhase.RESULT))
                self.assertEqual("points" in state, phase == RevealPhase.RESULT)
                self.assertEqual("round_outcome" in state, phase == RevealPhase.RESULT)
                json.dumps(state, allow_nan=False)

    def test_display_uses_existing_units_and_keeps_decimal_digits(self):
        for unit, expected in (("meters", "0.123456789123456789 m"), ("EUR", "€0.123456789123456789"),
                               ("year", "0.123456789123456789 Jahr")):
            with self.subTest(unit=unit):
                state = project_estimation(reveal=RevealContext(self.context(unit), RevealPhase.ANSWERS),
                    answer_a=Decimal("0.123456789123456789"), answer_b=None)
                self.assertEqual(state["answers"]["a"]["formatted"], expected)

    def test_contexts_are_immutable_and_not_untyped_client_phase_parameters(self):
        question = self.context()
        with self.assertRaises(FrozenInstanceError):
            question.target = Decimal(999)
        with self.assertRaises(TypeError):
            RevealContext(question, "result")
        with self.assertRaises(TypeError):
            project_estimation(reveal={"phase": "result"}, answer_a=Decimal(1), answer_b=Decimal(2))
        with self.assertRaises(ValueError):
            replace(question, deadline=timezone.now().replace(tzinfo=None))


class EstimationFixture(match_tests.MatchFixture):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("estimation-duel-material")
        self.material = EstimationQuestion.objects.create(question_text="Wie hoch?", correct_answer=100,
                                                         unit="meters", created_by=self.user)
        self.quiz = EstimationQuiz.objects.create(creator=self.user)
        self.quiz.selected_questions.add(self.material)
        self.step.room_code = self.quiz.room_code
        self.step.save(update_fields=["room_code"])
        DuelGameInstance.objects.filter(pk=self.other_instance.pk).update(match_status="in_progress", rule_config=self.instance.rule_config)
        # Prebuilt fixture state, not actions: each controller test still starts
        # at revision zero with no receipts. Binding command behavior is covered
        # separately by test_estimation_question_binding.
        self.prepare_fixture_binding(self.unit)
        self.prepare_fixture_binding(self.other_unit)
        self.question = self.load()
        self.actor_b = ActorContext(ReceiptActorType.PARTICIPANT, self.party_b.pk)
        DuelGameInstance.objects.filter(pk=self.other_instance.pk).update(match_status="in_progress", rule_config=self.instance.rule_config)
        DuelUnit.objects.filter(pk=self.other_unit.pk).update(status="active")

    def prepare_fixture_binding(self, unit):
        unit.status = "prepared"
        unit.save(update_fields=["status"])
        binding = DuelEstimationQuestionBinding(unit=unit, quiz=self.quiz, question=self.material,
            question_text=self.material.question_text, target_value=canonical_target(self.material.correct_answer),
            unit_key=self.material.unit)
        binding.full_clean()
        binding.save()
        unit.status = "active"
        unit.started_at = timezone.now() - timedelta(seconds=1)
        unit.save(update_fields=["status", "started_at"])
        # Prebuilt initial state; real start/phase commands are tested separately.
        DuelEstimationRoundState.objects.create(unit=unit, phase="answering", started_at=unit.started_at,
            deadline=unit.started_at+timedelta(hours=1), phase_changed_at=unit.started_at)

    def next_unit(self, instance=None):
        unit = super().next_unit(instance)
        self.prepare_fixture_binding(unit)
        return unit

    def load(self, **kwargs):
        return load_question_context(**(dict(instance_id=self.instance.pk, unit_id=self.unit.pk,
                                           question_id=self.material.pk) | kwargs))

    def answer(self, value="95", **kwargs):
        return commit_estimate(**(dict(question=self.question, actor=self.actor, payload={"value": value},
                                      client_action_id=str(uuid4()), expected_revision=0) | kwargs))

    def both(self, a="95", b="120", **kwargs):
        return self.answer(a, **kwargs), self.answer(b, actor=self.actor_b, **kwargs)

    def evaluate(self, **kwargs):
        question = kwargs.get("reveal", RevealContext(self.question, RevealPhase.RESULT)).question
        self.release_solution(question)
        return apply_estimation_result(**(dict(reveal=RevealContext(self.question, RevealPhase.RESULT),
            actor=self.host, client_action_id=str(uuid4())) | kwargs))

    def release_solution(self, question):
        args = dict(instance_id=question.instance_id, unit_id=question.unit_id, actor=self.host)
        for source, operation in ((EstimationPhase.AWAITING_REVEAL, show_estimation_answers),
                                  (EstimationPhase.SHOW_ANSWERS, show_estimation_solution)):
            if DuelEstimationRoundState.objects.get(unit_id=question.unit_id).phase == source:
                operation(**args, client_action_id=str(uuid4()))

    def state(self, phase=RevealPhase.QUESTION, **kwargs):
        args = dict(instance_id=self.question.instance_id, unit_id=self.question.unit_id,
                    actor=self.host, client_action_id=str(uuid4()))
        if phase == RevealPhase.ANSWERS:
            state = DuelEstimationRoundState.objects.get(unit_id=self.question.unit_id)
            if state.phase == EstimationPhase.ANSWERING:
                raise CommandRejected("answering_not_finished", "No host reveal before answer closure.")
            if state.phase == EstimationPhase.AWAITING_REVEAL:
                show_estimation_answers(**args)
        elif phase == RevealPhase.SOLUTION:
            self.release_solution(self.question)
        return get_estimation_state(**(dict(reveal=RevealContext(self.question, phase)) | kwargs))


class EstimationControllerTests(EstimationFixture):
    def test_context_loads_target_from_server_question_without_editing_material(self):
        self.assertEqual(self.question.target, Decimal(100))
        self.assertEqual(self.question.question_id, self.material.pk)
        self.assertEqual(self.question.unit, "meters")
        self.material.refresh_from_db()
        self.assertEqual(self.material.correct_answer, 100)

    def test_invalid_answer_rejected_before_committed_storage(self):
        for value in ("", "wrong", "NaN", "Infinity", "1e309", "1,5", {}, [], True, None):
            with self.subTest(value=value):
                result = self.answer(value)
                self.assertEqual(result.status, ReceiptStatus.REJECTED)
                self.assertEqual(result.data["code"], "invalid_estimate")
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 0)

    def test_non_json_numbers_fail_without_commits(self):
        for value in (float("nan"), float("inf"), Decimal("1.5")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.answer(value)
        self.assertFalse(DuelSubmission.objects.exists())

    def test_participant_payload_cannot_supply_target_question_or_unit(self):
        for key in ("target", "correct_answer", "question_id", "unit_id"):
            with self.subTest(key=key):
                result = self.answer(payload={"value": 95, key: 1000})
                self.assertEqual(result.data["code"], "invalid_estimate")
        self.assertFalse(DuelSubmission.objects.exists())

    def test_two_parties_commit_independently_despite_revision_change(self):
        a, b = self.both()
        self.assertEqual((a.state_revision, b.state_revision), (1, 2))
        self.assertEqual(DuelSubmission.objects.filter(status="committed").count(), 2)
        self.assertTrue(self.state()["answer_phase_complete"])
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_commit_retries_and_immutable_answers(self):
        key = str(uuid4())
        first = self.answer(client_action_id=key)
        retry = self.answer(client_action_id=key)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.data, first.data)
        with self.assertRaises(ActionConflict):
            self.answer("96", client_action_id=key)
        self.assertEqual(self.answer("96").data["code"], "already_committed")
        self.assertEqual(DuelSubmission.objects.get().payload, {"value": "95"})

    def test_existing_draft_receives_numeric_validation_on_commit(self):
        draft = save_draft(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.actor,
                           submission_kind=SubmissionKind.ANSWER, payload={"value": "bad"}, client_action_id="draft")
        from uuid import UUID
        draft_id = UUID(draft.data["submission_id"])
        self.assertEqual(self.answer(payload=None, draft_id=draft_id).data["code"], "invalid_estimate")
        self.assertEqual(self.answer(payload={"value": "95.5"}, draft_id=draft_id).status, ReceiptStatus.APPLIED)
        self.assertEqual(DuelSubmission.objects.get().payload, {"value": "95.5"})

    def test_third_participant_cannot_write_read_or_evaluate(self):
        actor = ActorContext(ReceiptActorType.PARTICIPANT, self.third.pk)
        for operation in (lambda: self.answer(actor=actor), lambda: self.state(actor=actor), lambda: self.evaluate(actor=actor)):
            with self.assertRaises(CommandRejected):
                operation()
        self.assertFalse(DuelSubmission.objects.exists())

    def test_foreign_unit_and_non_estimation_game_are_rejected(self):
        question = replace(self.question, unit_id=self.other_unit.pk)
        with self.assertRaises(CommandRejected):
            self.answer(question=question)
        self.instance.game_key = "buzzer"
        self.instance.save(update_fields=["game_key"])
        with self.assertRaises(CommandRejected):
            self.answer()
        self.assertFalse(DuelSubmission.objects.exists())

    def test_server_deadline_checked_during_acceptance_including_exact_boundary(self):
        deadline = timezone.now() + timedelta(seconds=10)
        DuelEstimationRoundState.objects.filter(unit_id=self.unit.pk).update(deadline=deadline)
        question = replace(self.question, deadline=deadline)
        with patch("duel.submissions.timezone.now", return_value=deadline - timedelta(microseconds=1)):
            self.assertEqual(self.answer(question=question).status, ReceiptStatus.APPLIED)
        with patch("duel.submissions.timezone.now", return_value=deadline):
            result = self.answer(actor=self.actor_b, question=question)
        self.assertEqual(result.data["code"], "answer_deadline_passed")
        with patch("duel.estimation_rounds.timezone.now", return_value=deadline):
            reconcile_estimation_deadline(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.host, client_action_id=str(uuid4()))
            show_estimation_answers(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.host, client_action_id=str(uuid4()))
            state = get_estimation_state(reveal=RevealContext(question, RevealPhase.ANSWERS))
        self.assertIsNone(state["answers"]["b"])
        self.assertNotIn("target", state)

    def test_reveal_not_allowed_before_both_answers_or_server_deadline(self):
        self.answer()
        with self.assertRaises(CommandRejected):
            self.state(RevealPhase.ANSWERS)

    def test_phase_a_contains_only_own_answer_and_never_target_or_opponent_value(self):
        self.both("95", "123")
        spectator = self.state()
        a = self.state(actor=self.actor)
        b = self.state(actor=self.actor_b)
        for state in (spectator, a, b):
            self.assertNotIn("target", state)
            self.assertNotIn("answers", state)
            self.assertNotIn("points", state)
        self.assertNotIn("own_answer", spectator)
        self.assertEqual(a["own_answer"]["value"], "95")
        self.assertEqual(b["own_answer"]["value"], "123")

    def test_b_shows_answers_c_adds_solution_without_bookings_or_points(self):
        self.both()
        b, c = self.state(RevealPhase.ANSWERS), self.state(RevealPhase.SOLUTION)
        self.assertEqual(b["answers"]["a"]["value"], "95")
        self.assertNotIn("target", b)
        self.assertEqual(c["target"]["value"], "100")
        self.assertEqual(c["deviations"], {"a": "5", "b": "20"})
        self.assertNotIn("points", c)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())

    def test_result_phase_cannot_preview_unpersisted_final_points(self):
        self.both()
        state = get_estimation_state(reveal=RevealContext(self.question, RevealPhase.RESULT))
        self.assertEqual(state["phase"], "awaiting_reveal")
        self.assertNotIn("points", state)
        self.assertNotIn("target", state)

    def test_a_wins_exactly_one_round_and_both_delta_rows_are_booked(self):
        self.both()
        result = self.evaluate()
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.assertEqual(result.data["points"], {"a": 1, "b": 0})
        self.assertEqual(list(DuelScoreEntry.objects.order_by("participant_id").values_list("delta", flat=True)), [1, 0])
        self.assertEqual(DuelUnitOutcome.objects.get().winner_id, self.party_a.pk)
        self.assertEqual(get_match_snapshot(self.instance.pk)["party_a"]["round_wins"], 1)
        self.assertEqual(self.state(RevealPhase.RESULT)["points"], {"a": 1, "b": 0})

    def test_b_wins_with_negative_values(self):
        # A new occurrence snapshots edited source material; a live context may
        # no longer substitute a target for an already bound unit.
        self.material.correct_answer = -20
        self.material.save(update_fields=["correct_answer"])
        self.question = self.load(unit_id=self.next_unit().pk)
        self.both("-50", "-15")
        self.evaluate()
        self.assertEqual(DuelUnitOutcome.objects.get().winner_id, self.party_b.pk)

    def test_tie_books_one_each_and_no_round_winner(self):
        self.both("90", "110")
        self.evaluate()
        self.assertEqual(list(DuelScoreEntry.objects.values_list("delta", flat=True)), [1, 1])
        self.assertIsNone(DuelUnitOutcome.objects.get().winner_id)
        self.assertEqual(DuelUnitOutcome.objects.get().outcome, "draw")
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual((snapshot["party_a"]["round_wins"], snapshot["party_b"]["round_wins"]), (0, 0))
        self.assertEqual(snapshot["evaluation"], "running")

    def test_speed_and_acceptance_order_never_break_a_tie(self):
        self.answer("110", actor=self.actor_b)
        self.answer("90")
        result = self.evaluate()
        self.assertEqual(result.data["points"], {"a": 1, "b": 1})
        self.assertEqual(result.data["round_outcome"], "draw")

    def test_missing_answers_produce_only_the_confirmed_final_scores(self):
        for a, b, availability in ((None, "95", "only_b"), ("95", None, "only_a"), (None, None, "none")):
            unit = self.next_unit()
            question = replace(self.question, unit_id=unit.pk)
            if a is not None:
                self.answer(a, question=question)
            if b is not None:
                self.answer(b, actor=self.actor_b, question=question)
            state = DuelEstimationRoundState.objects.get(unit=unit)
            with patch("duel.estimation_rounds.timezone.now", return_value=state.deadline):
                reconcile_estimation_deadline(instance_id=self.instance.pk, unit_id=unit.pk, actor=self.host, client_action_id=str(uuid4()))
                result = self.evaluate(reveal=RevealContext(question, RevealPhase.RESULT))
            self.assertEqual(result.status, ReceiptStatus.APPLIED)
            self.assertEqual(result.data['points'], {'a': int(a is not None), 'b': int(b is not None)})
            unit.refresh_from_db()
            self.assertEqual(unit.status, "completed")
            self.assertEqual(unit.outcome.winner_id, self.party_a.pk if a else self.party_b.pk if b else None)
        self.assertEqual(DuelScoreEntry.objects.count(), 6)
        self.assertEqual(DuelUnitOutcome.objects.count(), 3)

    def test_best_of_uses_round_wins_and_allows_same_material_in_new_units(self):
        for number in range(2):
            if number:
                self.question = replace(self.question, unit_id=self.next_unit().pk)
            self.both()
            self.evaluate()
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["party_a"]["round_wins"], 2)
        self.assertEqual(snapshot["party_a"]["score"], 2)
        self.assertEqual(snapshot["match_status"], "completed")
        self.assertEqual(DuelUnitOutcome.objects.count(), 2)
        self.assertEqual(DuelSubmission.objects.count(), 4)

    def test_target_score_reads_complete_points_batch(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score(self.party_a, 9)
        self.score(self.party_b, 8)
        self.both()
        self.evaluate()
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual(snapshot["winner_id"], self.party_a.pk)
        self.assertEqual(snapshot["party_a"]["score"], 10)
        self.assertEqual(snapshot["party_b"]["score"], 8)

    def test_both_parties_reaching_target_is_unresolved_after_complete_batch(self):
        self.configure(MatchMode.TARGET_SCORE, 10)
        self.score(self.party_a, 9)
        self.score(self.party_b, 9)
        self.both("90", "110")
        self.evaluate()
        snapshot = get_match_snapshot(self.instance.pk)
        self.assertEqual((snapshot["party_a"]["score"], snapshot["party_b"]["score"]), (10, 10))
        self.assertEqual(snapshot["evaluation"], "sudden_death")
        self.assertEqual(snapshot['tiebreak_unit_id'], self.unit.pk)
        self.assertEqual(snapshot["match_status"], "in_progress")
        self.assertFalse(DuelMatchOutcome.objects.exists())

    def test_applied_retry_is_idempotent_and_new_action_cannot_double_evaluate(self):
        self.both()
        key = str(uuid4())
        first, retry = self.evaluate(client_action_id=key), self.evaluate(client_action_id=key)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.data, first.data)
        self.assertEqual(retry.state_revision, 5)
        self.assertEqual(self.evaluate().status, ReceiptStatus.REJECTED)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 5)

    def test_contradictory_material_target_retry_is_a_conflict(self):
        self.both()
        key = str(uuid4())
        self.evaluate(client_action_id=key)
        other = RevealContext(replace(self.question, target=Decimal(200)), RevealPhase.RESULT)
        with self.assertRaises(ActionConflict):
            self.evaluate(client_action_id=key, reveal=other)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)

    def test_evaluation_requires_host_and_trusted_result_phase(self):
        self.both()
        with self.assertRaises(CommandRejected):
            self.evaluate(actor=self.actor)
        with self.assertRaises(ValueError):
            self.evaluate(reveal=RevealContext(self.question, RevealPhase.SOLUTION))
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_unit_completed_match_completed_and_original_answer_retry(self):
        self.configure(threshold=1)
        key = str(uuid4())
        first = self.answer(client_action_id=key)
        self.answer("120", actor=self.actor_b)
        self.evaluate()
        self.assertEqual(self.answer().status, ReceiptStatus.REJECTED)
        self.assertEqual(self.answer(client_action_id=key).data, first.data)
        self.assertEqual(self.evaluate().status, ReceiptStatus.REJECTED)

    def test_reload_reconstructs_answers_outcome_and_scores_with_same_trusted_context(self):
        self.both()
        self.evaluate()
        state = self.state(RevealPhase.RESULT)
        fresh_question = self.load()
        reloaded = get_estimation_state(reveal=RevealContext(fresh_question, RevealPhase.RESULT))
        state.pop("server_now")
        reloaded.pop("server_now")
        self.assertEqual(reloaded, state)

    def test_new_instance_has_no_old_answers_or_return_state(self):
        self.both()
        self.evaluate()
        question = self.load(instance_id=self.other_instance.pk, unit_id=self.other_unit.pk)
        state = get_estimation_state(reveal=RevealContext(question, RevealPhase.QUESTION), actor=self.actor)
        self.assertFalse(state["committed"])
        self.assertFalse(state["answer_phase_complete"])
        self.assertIsNone(state["own_answer"])
        self.assertEqual(state["state_revision"], 0)

    def test_invalid_persisted_generic_submission_cannot_be_scored(self):
        self.both()
        DuelSubmission.objects.filter(participant=self.party_b).update(payload={"value": "not numeric"})
        with self.assertRaises(MatchIntegrityError):
            self.evaluate()
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())

    def test_database_error_during_second_booking_rolls_back_both(self):
        self.both()
        original_save, calls = DuelScoreEntry.save, []
        def fail_second(entry, *args, **kwargs):
            original_save(entry, *args, **kwargs)
            calls.append(entry.pk)
            if len(calls) == 2:
                raise IntegrityError("second score failed")
        with patch.object(DuelScoreEntry, "save", fail_second), self.assertRaises(IntegrityError):
            self.evaluate()
        self.assert_no_partial_result()

    def test_error_after_unit_outcome_creation_rolls_back_entire_result(self):
        self.both()
        with patch("duel.match_service.reconcile_match_in_action", side_effect=RuntimeError("evaluation failed")):
            with self.assertRaises(RuntimeError):
                self.evaluate()
        self.assert_no_partial_result()

    def test_receipt_failure_rolls_back_points_unit_and_match_completion(self):
        self.configure(threshold=1)
        self.both()
        self.release_solution(self.question)
        with patch.object(DuelActionReceipt, "save", side_effect=IntegrityError("receipt failed")):
            with self.assertRaises(IntegrityError):
                self.evaluate()
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.assert_no_partial_result()

    def assert_no_partial_result(self):
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, "active")
        self.assertIsNone(self.unit.completed_at)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertEqual(DuelActionReceipt.objects.count(), 4)  # two answers and two explicit host reveals
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 4)
        self.assertEqual(self.evaluate().status, ReceiptStatus.APPLIED)

    def test_no_multiplayer_answer_participant_or_question_mutation(self):
        original = EstimationQuestion.objects.values().get(pk=self.material.pk)
        self.both()
        self.evaluate()
        self.assertEqual(EstimationQuestion.objects.values().get(pk=self.material.pk), original)
        self.assertFalse(EstimationAnswer.objects.exists())
        self.assertFalse(EstimationParticipant.objects.exists())
