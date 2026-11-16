"""Persisted phases/clock, server publication gates and transaction integration."""
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import test_estimation_question_binding as fixtures
from .commands import ActorContext, ActionConflict, CommandRejected, execute_action, HandlerResult
from .controllers.estimation import (
    RevealContext, RevealPhase, commit_estimate, get_estimation_state, project_estimation,
)
from .estimation_questions import get_estimation_question_context
from .estimation_rounds import (
    prepare_estimation_round, start_estimation_round, reconcile_estimation_deadline,
    advance_estimation_phase, complete_estimation_round,
)
from .match_service import get_match_snapshot
from .models import (
    DuelEstimationRoundState as State, EstimationPhase as Phase, DuelUnit, DuelGameInstance,
    DuelActionReceipt, DuelScoreEntry, DuelUnitOutcome, DuelMatchOutcome, DuelSubmission,
    ReceiptStatus, ReceiptActorType,
)


class PhaseFixture(fixtures.BindingFixture):
    def setUp(self):
        super().setUp()
        self.bind()
        self.clock = timezone.now()
        self.actor_b = ActorContext(ReceiptActorType.PARTICIPANT, self.party_b.pk)

    def command_args(self, **kwargs):
        return dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.host,
                    client_action_id=str(uuid4())) | kwargs

    def start(self, seconds=60, **kwargs):
        with patch("duel.estimation_rounds.timezone.now", return_value=self.clock):
            return start_estimation_round(**self.command_args(duration_seconds=seconds, **kwargs))

    def answer(self, actor=None, value="95", question=None, at=10, **kwargs):
        with patch("duel.submissions.timezone.now", return_value=self.clock+timedelta(seconds=at)):
            return commit_estimate(**(dict(question=question or get_estimation_question_context(self.unit.pk),
                actor=actor or self.actor, client_action_id=str(uuid4()), payload={"value": value}, expected_revision=0) | kwargs))

    def both(self, a="95", b="120"):
        return self.answer(value=a), self.answer(actor=self.actor_b, value=b, at=11)

    def advance(self, target, at=20, **kwargs):
        with patch("duel.estimation_rounds.timezone.now", return_value=self.clock+timedelta(seconds=at)):
            return advance_estimation_phase(**self.command_args(target_phase=target, **kwargs))

    def reconcile(self, at=60, **kwargs):
        with patch("duel.estimation_rounds.timezone.now", return_value=self.clock+timedelta(seconds=at)):
            return reconcile_estimation_deadline(**self.command_args(**kwargs))

    def snapshot(self, at=35, actor=None, **kwargs):
        with patch("duel.controllers.estimation.timezone.now", return_value=self.clock+timedelta(seconds=at)):
            return get_estimation_state(unit_id=self.unit.pk, instance_id=self.instance.pk, actor=actor, **kwargs)

    def phase(self):
        return State.objects.get(unit=self.unit).phase

    def through_result(self, a="95", b="120"):
        self.start()
        self.both(a, b)
        for target in (Phase.SHOW_ANSWERS, Phase.SHOW_SOLUTION, Phase.SHOW_RESULT):
            self.assertEqual(self.advance(target).status, ReceiptStatus.APPLIED)


class EstimationRoundTests(PhaseFixture):
    def test_binding_creates_persisted_prepared_state_without_a_clock(self):
        row = State.objects.get(unit=self.unit)
        self.assertEqual(row.phase, Phase.PREPARED)
        self.assertIsNone(row.started_at)
        self.assertIsNone(row.deadline)
        self.assertIsNone(row.answering_closed_at)
        row.full_clean()
        state = self.snapshot(actor=self.actor)
        self.assertEqual(state["phase"], "prepared")
        self.assertIsNone(state["remaining_seconds"])
        self.assertFalse(state["can_answer"])
        self.assertNotIn("target", state)

    def test_start_persists_server_start_deadline_and_unit_lifecycle(self):
        result = self.start()
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.assertEqual(result.state_revision, 2)  # binding + start
        row = State.objects.get(unit=self.unit)
        self.unit.refresh_from_db()
        self.assertEqual(row.started_at, self.clock)
        self.assertEqual(row.deadline, self.clock+timedelta(seconds=60))
        self.assertEqual(row.phase_changed_at, self.clock)
        self.assertEqual(self.unit.status, "active")
        self.assertEqual(self.unit.started_at, row.started_at)
        row.full_clean()

    def test_reload_uses_original_deadline_instead_of_restarting_duration(self):
        self.start()
        state = self.snapshot(at=35, actor=self.actor)
        self.assertEqual(state["remaining_seconds"], 25)
        self.assertTrue(state["can_answer"])
        self.assertEqual(state["deadline"], (self.clock+timedelta(seconds=60)).isoformat())
        self.assertEqual(self.snapshot(at=50)["remaining_seconds"], 10)

    def test_invalid_duration_is_rejected_without_state_change(self):
        for value in (0, -1, True, "60", None, 0.5, float("inf"), 10**100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.start(value)
        self.assertEqual(self.phase(), Phase.PREPARED)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_double_start_replays_original_clock_but_new_id_cannot_restart_it(self):
        key = str(uuid4())
        first, retry = self.start(client_action_id=key), self.start(client_action_id=key)
        self.assertTrue(retry.replayed)
        self.assertEqual(first.data, retry.data)
        rejected = self.start(client_action_id=str(uuid4()))
        self.assertEqual(rejected.data["code"], "round_already_started")
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 2)
        with self.assertRaises(ActionConflict):
            self.start(120, client_action_id=key)

    def test_first_answer_keeps_answering_second_closes_without_reveal_or_scores(self):
        self.start()
        first = self.answer()
        self.assertEqual(self.phase(), Phase.ANSWERING)
        second = self.answer(actor=self.actor_b, value="120", at=11)
        self.assertEqual((first.state_revision, second.state_revision), (3, 4))
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        row = State.objects.get(unit=self.unit)
        self.assertEqual(row.answering_closed_at, self.clock+timedelta(seconds=11))
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())
        for actor in (None, self.actor, self.actor_b):
            snapshot = self.snapshot(actor=actor)
            self.assertNotIn("answers", snapshot)
            self.assertNotIn("target", snapshot)
            self.assertFalse(snapshot["answering_allowed"])

    def test_before_start_and_after_closure_do_not_accept_new_answers(self):
        self.assertEqual(self.answer().status, ReceiptStatus.REJECTED)
        self.start()
        self.reconcile()
        result = self.answer(at=61)
        self.assertEqual(result.data["code"], "answering_not_open")
        self.assertFalse(DuelSubmission.objects.exists())

    def test_exact_deadline_and_late_answer_are_rejected_even_without_reconciliation(self):
        self.start()
        for at in (60, 61):
            with self.subTest(at=at):
                result = self.answer(at=at)
                self.assertEqual(result.data["code"], "answer_deadline_passed")
        self.assertEqual(self.phase(), Phase.ANSWERING)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 2)

    def test_forged_context_deadline_cannot_extend_saved_answer_window(self):
        self.start()
        context = get_estimation_question_context(self.unit.pk)
        from dataclasses import replace
        forged = replace(context, deadline=self.clock+timedelta(days=1))
        result = self.answer(at=61, question=forged)
        self.assertEqual(result.data["code"], "answer_deadline_passed")

    def test_expired_snapshot_is_read_only_and_never_performs_reconciliation(self):
        self.start()
        before = State.objects.values().get()
        with CaptureQueriesContext(connection) as queries:
            state = self.snapshot(at=61)
        self.assertEqual(state["phase"], "answering")
        self.assertEqual(state["remaining_seconds"], 0)
        self.assertFalse(state["answering_allowed"])
        self.assertTrue(state["reconciliation_required"])
        self.assertEqual(State.objects.values().get(), before)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))

    def test_explicit_deadline_reconciliation_closes_only_and_is_idempotent(self):
        self.start()
        self.assertEqual(self.reconcile(at=59).data["code"], "deadline_not_reached")
        key = str(uuid4())
        closed = self.reconcile(at=70, client_action_id=key)
        self.assertEqual(closed.status, ReceiptStatus.APPLIED)
        self.assertEqual(self.phase(), Phase.AWAITING_REVEAL)
        self.assertEqual(State.objects.get().answering_closed_at, self.clock+timedelta(seconds=60))
        replay = self.reconcile(at=90, client_action_id=key)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.data, closed.data)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertNotIn("target", self.snapshot(at=90))
        self.assertNotIn("answers", self.snapshot(at=90))

    def test_only_ordered_host_transitions_are_allowed(self):
        self.start()
        for target in (Phase.SHOW_ANSWERS, Phase.SHOW_SOLUTION, Phase.SHOW_RESULT, Phase.COMPLETED):
            self.assertEqual(self.advance(target).data["code"], "invalid_phase")
        self.both()
        self.assertEqual(self.advance(Phase.SHOW_SOLUTION).data["code"], "invalid_phase")
        self.assertEqual(self.advance(Phase.SHOW_RESULT).data["code"], "invalid_phase")
        self.assertEqual(self.advance(Phase.SHOW_ANSWERS).status, ReceiptStatus.APPLIED)
        self.assertEqual(self.advance(Phase.SHOW_RESULT).data["code"], "invalid_phase")
        self.assertEqual(self.advance(Phase.SHOW_SOLUTION).status, ReceiptStatus.APPLIED)
        self.assertEqual(self.advance(Phase.SHOW_ANSWERS).data["code"], "invalid_phase")

    def test_host_permissions_reject_participants_for_all_phase_commands(self):
        for operation in (lambda: self.start(actor=self.actor), lambda: self.reconcile(actor=self.actor),
                          lambda: self.advance(Phase.SHOW_ANSWERS, actor=self.actor),
                          lambda: prepare_estimation_round(**self.command_args(actor=self.actor))):
            with self.assertRaises(CommandRejected):
                operation()
        self.assertEqual(self.phase(), Phase.PREPARED)

    def test_foreign_actor_and_foreign_unit_cannot_affect_this_round(self):
        self.start()
        with self.assertRaises(CommandRejected):
            self.answer(actor=ActorContext(ReceiptActorType.PARTICIPANT, self.third.pk))
        with self.assertRaises(CommandRejected):
            self.start(unit_id=self.other_unit.pk)
        self.assertEqual(self.phase(), Phase.ANSWERING)

    def test_read_projection_ignores_requested_future_reveal_context(self):
        self.start()
        self.answer()
        context = get_estimation_question_context(self.unit.pk)
        for requested in RevealPhase:
            with self.subTest(requested=requested):
                state = self.snapshot(actor=self.actor, reveal=RevealContext(context, requested))
                self.assertEqual(state["phase"], "answering")
                self.assertNotIn("target", state)
                self.assertNotIn("answers", state)
                self.assertEqual(state["own_answer"]["value"], "95")
                public = project_estimation(reveal=RevealContext(context, requested), actor=self.actor_b)
                self.assertNotIn("target", public)
                self.assertNotIn("answers", public)
                self.assertIsNone(public["own_answer"])

    def test_reveal_publication_and_score_timing_come_only_from_saved_phase(self):
        self.start()
        self.both()
        self.advance(Phase.SHOW_ANSWERS)
        state = self.snapshot()
        self.assertEqual(state["answers"]["a"]["value"], "95")
        self.assertNotIn("target", state)
        self.assertNotIn("points", state)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.advance(Phase.SHOW_SOLUTION)
        state = self.snapshot()
        self.assertEqual(state["target"]["value"], "100")
        self.assertEqual(state["deviations"], {"a": "5", "b": "20"})
        self.assertNotIn("points", state)
        self.advance(Phase.SHOW_RESULT)
        state = self.snapshot()
        self.assertEqual(state["points"], {"a": 1, "b": 0})
        self.assertEqual(state["round_outcome"], "party_a_wins")
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        self.advance(Phase.COMPLETED)
        self.assertEqual(self.snapshot()["phase"], "completed")

    def test_tie_books_one_each_but_no_round_win(self):
        self.through_result("90", "110")
        state = self.snapshot()
        self.assertEqual(state["points"], {"a": 1, "b": 1})
        self.assertEqual(state["round_outcome"], "draw")
        self.assertEqual(state["match"]["party_a"]["round_wins"], 0)
        self.assertEqual(state["match"]["party_b"]["round_wins"], 0)

    def test_new_unit_reuses_material_but_never_previous_timer_phase_or_answer(self):
        self.through_result()
        self.advance(Phase.COMPLETED)
        old_state = State.objects.values().get(unit=self.unit)
        old_scores = list(DuelScoreEntry.objects.order_by("pk").values())
        new_unit = self.prepared_unit()
        self.bind(unit_id=new_unit.pk)
        new_state = get_estimation_state(unit_id=new_unit.pk, actor=self.actor)
        self.assertEqual(new_state["phase"], "prepared")
        self.assertIsNone(new_state["deadline"])
        self.assertIsNone(new_state["own_answer"])
        self.assertNotIn("target", new_state)
        self.assertNotIn("answers", new_state)
        rejected = self.answer(client_action_id=str(uuid4()), at=40)
        self.assertEqual(rejected.status, ReceiptStatus.REJECTED)
        self.assertEqual(State.objects.values().get(unit=self.unit), old_state)
        self.assertEqual(list(DuelScoreEntry.objects.order_by("pk").values()), old_scores)
        self.assertEqual(State.objects.get(unit=new_unit).phase, Phase.PREPARED)

    def test_new_instance_does_not_inherit_clock_or_action_identity(self):
        key = str(uuid4())
        self.start(client_action_id=key)
        self.answer()
        fresh = DuelGameInstance.objects.create(hub_session=self.instance.hub_session,
            hub_game_step=self.instance.hub_game_step, participant_a=self.party_a,
            participant_b=self.party_b, game_key="estimation", match_status="in_progress",
            rule_config=self.instance.rule_config)
        unit = self.prepared_unit(fresh)
        self.bind(instance_id=fresh.pk, unit_id=unit.pk)
        question = get_estimation_question_context(unit.pk)
        self.assertIsNone(question.deadline)
        self.assertEqual(get_estimation_state(unit_id=unit.pk, actor=self.actor)["phase"], "prepared")
        with patch("django.utils.timezone.now", return_value=self.clock+timedelta(seconds=90)):
            result = start_estimation_round(**self.command_args(instance_id=fresh.pk, unit_id=unit.pk,
                client_action_id=key, duration_seconds=60))
        self.assertFalse(result.replayed)
        self.assertEqual(State.objects.get(unit=unit).deadline, self.clock+timedelta(seconds=150))
        self.assertEqual(State.objects.get(unit=self.unit).deadline, self.clock+timedelta(seconds=60))
        self.assertFalse(DuelSubmission.objects.filter(unit=unit).exists())
        fresh.refresh_from_db()
        revision = fresh.state_revision
        with self.assertRaises(CommandRejected):
            self.start(instance_id=fresh.pk, unit_id=self.unit.pk)
        fresh.refresh_from_db()
        self.assertEqual(fresh.state_revision, revision)

    def test_only_valid_answer_wins_after_the_ordered_reveal(self):
        self.start()
        self.answer()
        self.reconcile()
        self.advance(Phase.SHOW_ANSWERS, at=61)
        self.assertIsNone(self.snapshot(at=61)["answers"]["b"])
        self.advance(Phase.SHOW_SOLUTION, at=62)
        result = self.advance(Phase.SHOW_RESULT, at=63)
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.assertEqual(result.data['points'], {'a':1,'b':0})
        self.assertEqual(self.phase(), Phase.SHOW_RESULT)
        self.assertEqual(self.advance(Phase.COMPLETED, at=64).status, ReceiptStatus.APPLIED)
        self.assertEqual(self.snapshot(at=64)['result_reason'], 'only_valid_answer')
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.get(unit=self.unit).winner_id, self.party_a.pk)

    def test_host_replay_after_phase_change_retains_original_acknowledgement(self):
        self.start()
        self.both()
        key = str(uuid4())
        first = self.advance(Phase.SHOW_ANSWERS, client_action_id=key)
        self.advance(Phase.SHOW_SOLUTION)
        replay = self.advance(Phase.SHOW_ANSWERS, client_action_id=key)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(replay.state_revision, first.state_revision)
        with self.assertRaises(ActionConflict):
            self.advance(Phase.SHOW_SOLUTION, client_action_id=key)

    def test_duplicate_second_answer_does_not_close_twice_or_increment_again(self):
        self.start()
        self.answer()
        key = str(uuid4())
        first = self.answer(actor=self.actor_b, client_action_id=key, at=11)
        row = State.objects.values().get()
        retry = self.answer(actor=self.actor_b, client_action_id=key, at=12)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.state_revision, first.state_revision)
        self.assertEqual(State.objects.values().get(), row)

    def test_reload_in_every_phase_keeps_persisted_state_and_never_mutates_it(self):
        def stable():
            before = State.objects.values().get()
            one, two = self.snapshot(at=40), self.snapshot(at=41)
            one.pop("server_now"); two.pop("server_now")
            one.pop("remaining_seconds"); two.pop("remaining_seconds")
            self.assertEqual(one, two)
            self.assertEqual(State.objects.values().get(), before)
        stable()
        self.start()
        stable()
        self.answer()
        stable()
        self.answer(actor=self.actor_b, value="120", at=11)
        stable()
        for target in (Phase.SHOW_ANSWERS, Phase.SHOW_SOLUTION, Phase.SHOW_RESULT, Phase.COMPLETED):
            self.advance(target)
            stable()

    def test_final_presentation_completes_even_when_result_already_won_match(self):
        self.configure(threshold=1)
        self.through_result()
        self.assertEqual(get_match_snapshot(self.instance.pk)["match_status"], "completed")
        before = list(DuelScoreEntry.objects.values())
        key = str(uuid4())
        closed = self.advance(Phase.COMPLETED, client_action_id=key)
        self.assertEqual(closed.status, ReceiptStatus.APPLIED)
        self.assertEqual(self.phase(), Phase.COMPLETED)
        self.assertEqual(self.advance(Phase.COMPLETED, client_action_id=key).data, closed.data)
        self.assertEqual(list(DuelScoreEntry.objects.values()), before)
        self.assertEqual(DuelMatchOutcome.objects.count(), 1)
        self.assertEqual(self.advance(Phase.SHOW_ANSWERS).data["code"], "match_closed")

    def test_normal_actions_cannot_bypass_completed_match_guard(self):
        self.configure(threshold=1)
        self.through_result()
        handler = Mock(return_value=HandlerResult(ReceiptStatus.APPLIED, {}))
        result = execute_action(**self.command_args(), action_type="test.after_close", payload={}, handler=handler)
        self.assertEqual(result.data["code"], "match_closed")
        handler.assert_not_called()
        with self.assertRaises(ValueError):
            execute_action(**self.command_args(), action_type="test.after_close", payload={}, handler=handler,
                           _completed_presentation=True)

    def test_terminal_maintenance_cannot_reopen_completed_unit(self):
        self.configure(threshold=1)
        self.through_result()
        def bad_handler(context):
            DuelUnit.objects.filter(pk=context.unit.pk).update(status="active")
            return HandlerResult(ReceiptStatus.APPLIED, {})
        with self.assertRaises(RuntimeError):
            execute_action(**self.command_args(), action_type="duel.estimation.complete_round", payload={},
                           handler=bad_handler, _completed_presentation=True)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, "completed")
        self.assertEqual(self.phase(), Phase.SHOW_RESULT)

    def test_phase_receipt_failure_rolls_back_phase_and_revision(self):
        self.start()
        self.both()
        before, revision = State.objects.values().get(), get_match_snapshot(self.instance.pk)["state_revision"]
        with patch.object(DuelActionReceipt, "save", side_effect=IntegrityError("receipt failed")):
            with self.assertRaises(IntegrityError):
                self.advance(Phase.SHOW_ANSWERS)
        self.assertEqual(State.objects.values().get(), before)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], revision)

    def test_second_answer_callback_failure_rolls_back_input_phase_receipt_and_revision(self):
        self.start()
        self.answer()
        before, revision = State.objects.values().get(), get_match_snapshot(self.instance.pk)["state_revision"]
        with patch.object(State, "save", side_effect=IntegrityError("phase failed")):
            with self.assertRaises(IntegrityError):
                self.answer(actor=self.actor_b, at=11)
        self.assertEqual(DuelSubmission.objects.count(), 1)
        self.assertEqual(State.objects.values().get(), before)
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], revision)

    def test_result_phase_failure_rolls_back_scores_outcome_unit_and_match(self):
        self.configure(threshold=1)
        self.start(); self.both()
        self.advance(Phase.SHOW_ANSWERS); self.advance(Phase.SHOW_SOLUTION)
        with patch.object(State, "save", side_effect=IntegrityError("phase failed")):
            with self.assertRaises(IntegrityError):
                self.advance(Phase.SHOW_RESULT)
        self.assertEqual(self.phase(), Phase.SHOW_SOLUTION)
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertFalse(DuelMatchOutcome.objects.exists())
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, "active")

    def test_legacy_prepared_binding_can_be_initialized_but_active_clock_is_never_inferred(self):
        State.objects.filter(unit=self.unit).delete()
        result = prepare_estimation_round(**self.command_args())
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        self.assertEqual(self.phase(), Phase.PREPARED)
        self.start()
        State.objects.filter(unit=self.unit).delete()
        result = prepare_estimation_round(**self.command_args())
        self.assertEqual(result.data["code"], "unit_not_prepared")
        with self.assertRaises(CommandRejected):
            self.snapshot()


class EstimationRoundModelTests(PhaseFixture):
    def test_one_to_one_is_enforced_in_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                State.objects.create(unit=self.unit)

    def test_database_rejects_invalid_phase_and_timestamp_combinations(self):
        for fields in ({"phase": "unknown"}, {"phase": "answering"}, {"deadline": self.clock},
                       {"phase": "answering", "started_at": self.clock, "deadline": self.clock},
                       {"phase": "awaiting_reveal", "started_at": self.clock, "deadline": self.clock+timedelta(seconds=60),
                        "answering_closed_at": self.clock-timedelta(seconds=1)}):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    State.objects.filter(unit=self.unit).update(**fields)

    def test_full_clean_rejects_timer_edit_and_phase_skip(self):
        self.start()
        row = State.objects.get(unit=self.unit)
        row.deadline += timedelta(seconds=60)
        with self.assertRaises(ValidationError):
            row.full_clean()
        row.refresh_from_db()
        row.phase = Phase.SHOW_RESULT
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_full_clean_rejects_naive_server_timestamps_and_missing_material(self):
        self.start()
        row = State.objects.get(unit=self.unit)
        row.started_at = row.started_at.replace(tzinfo=None)
        with self.assertRaises(ValidationError):
            row.full_clean()
        unit = self.prepared_unit()
        with self.assertRaises(ValidationError):
            State(unit=unit).full_clean()
