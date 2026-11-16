"""Persisted Estimation phase/clock commands; internal, no scheduler or transport.

All writes compose inside execute_action. Reads never reconcile or publish a
future phase. Reconciliation is an explicit HOST/server operation; transports
or a future worker may call it, but no global scheduling infrastructure is added.
Submissions check the deadline even before a late reconciliation has run.

Missing inputs remain absent in the answers/solution phases. The confirmed
one/zero-submission rules are applied only on Host result release. No automatic
reveal, timeout scoring, pause/restart or next-unit logic.
"""
from datetime import timedelta
from uuid import UUID

from django.db import connection
from django.utils import timezone

from .commands import ActorContext, ActionContext, ActionResult, CommandRejected, HandlerResult, execute_action
from .contracts import MatchStatus
from .estimation_questions import get_estimation_question_context
from .models import (
    DuelEstimationRoundState, DuelSubmission, EstimationPhase, ReceiptActorType, ReceiptStatus,
    SubmissionKind, SubmissionStatus, UnitStatus,
)


def _host(actor):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "An internally authorized host is required.")


def _reject(code, **data):
    return HandlerResult(ReceiptStatus.REJECTED, {"code": code, **data})


def _state(context: ActionContext):
    if not connection.in_atomic_block or context.unit is None:
        raise RuntimeError("Round mutations require the existing command transaction and unit.")
    question = get_estimation_question_context(context.unit.pk)
    if question.instance_id != context.instance.pk:
        raise CommandRejected("invalid_unit", "Question belongs to another instance.")
    state = DuelEstimationRoundState.objects.using(context.using).select_for_update().filter(unit=context.unit).first()
    if state is None:
        raise CommandRejected("round_not_initialized", "Initialize the prepared round first.")
    state.full_clean()
    return state


def _metadata(state):
    return {"unit_id": state.unit_id, "phase": state.phase,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "deadline": state.deadline.isoformat() if state.deadline else None,
        "answering_closed_at": state.answering_closed_at.isoformat() if state.answering_closed_at else None,
        "phase_changed_at": state.phase_changed_at.isoformat()}


def _change(state, phase, *, closed_at=None, using="default"):
    state.phase = phase.value
    state.phase_changed_at = timezone.now()
    if closed_at is not None:
        state.answering_closed_at = closed_at
    state.full_clean()
    state.save(using=using, update_fields=["phase", "phase_changed_at", "answering_closed_at"])


def prepare_estimation_round(*, instance_id: UUID, unit_id: int, actor: ActorContext,
                             client_action_id: str) -> ActionResult:
    """Initialize only legacy PREPARED bindings, never infer an old active clock.

    New bindings already create PREPARED in their original binding transaction.
    This is not a backfill of started rounds and never activates a unit.
    """
    _host(actor)
    def handler(context):
        get_estimation_question_context(context.unit.pk)  # binding required
        if context.unit.status != UnitStatus.PREPARED or context.unit.started_at is not None or context.unit.completed_at is not None:
            return _reject("unit_not_prepared")
        if DuelEstimationRoundState.objects.using(context.using).filter(unit=context.unit).exists():
            return _reject("round_already_prepared")
        state = DuelEstimationRoundState(unit=context.unit)
        state.full_clean()
        state.save(using=context.using, force_insert=True)
        return HandlerResult(ReceiptStatus.APPLIED, _metadata(state))
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
                          action_type="duel.estimation.prepare_round", payload={}, handler=handler)


def start_estimation_round(*, instance_id: UUID, unit_id: int, actor: ActorContext, client_action_id: str,
                           duration_seconds: int, expected_revision: int | None = None) -> ActionResult:
    """An explicit server-configured duration; no reliable duel default exists."""
    _host(actor)
    if type(duration_seconds) is not int or duration_seconds <= 0:
        raise ValueError("duration_seconds must be a positive integer.")
    try:
        duration = timedelta(seconds=duration_seconds)
    except OverflowError as exc:
        raise ValueError("Unsupported timer duration.") from exc
    def handler(context):
        state = _state(context)
        if context.instance.match_status != MatchStatus.IN_PROGRESS.value:
            return _reject("match_not_running")
        if state.phase != EstimationPhase.PREPARED or context.unit.status != UnitStatus.PREPARED:
            return _reject("round_already_started")
        if context.unit.started_at is not None or context.unit.completed_at is not None:
            return _reject("unit_not_prepared")
        if DuelSubmission.objects.using(context.using).filter(unit=context.unit).exists():
            return _reject("unit_has_input_history")
        now = timezone.now()
        step = context.instance.hub_game_step
        if (step.game_format in {'duel', 'tournament'} and step.intro_ends_at is not None
                and now < step.intro_ends_at):
            return _reject('intro_not_finished')
        try:
            deadline = now + duration
        except OverflowError:
            return _reject("invalid_duration")
        context.unit.status = UnitStatus.ACTIVE.value
        context.unit.started_at = now
        context.unit.full_clean()
        context.unit.save(using=context.using, update_fields=["status", "started_at", "updated_at"])
        state.phase = EstimationPhase.ANSWERING.value
        state.started_at, state.deadline, state.phase_changed_at = now, deadline, now
        state.full_clean()
        state.save(using=context.using, update_fields=["phase", "started_at", "deadline", "phase_changed_at"])
        return HandlerResult(ReceiptStatus.APPLIED, _metadata(state))
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.estimation.start_round", payload={"duration_seconds": duration_seconds},
        expected_revision=expected_revision, handler=handler)


def close_after_submission(context: ActionContext, submission) -> None:
    """Post-save composition: a second valid answer closes, never reveals/scores."""
    from .controllers.estimation import _answers
    state = _state(context)
    if state.phase != EstimationPhase.ANSWERING or submission.submission_kind != SubmissionKind.ANSWER:
        raise RuntimeError("Only an answering-phase estimate can close the answer phase.")
    a, b = _answers(get_estimation_question_context(context.unit.pk), context.instance, state)
    if a is not None and b is not None:
        _change(state, EstimationPhase.AWAITING_REVEAL, closed_at=submission.committed_at, using=context.using)


def reconcile_estimation_deadline(*, instance_id: UUID, unit_id: int, actor: ActorContext,
                                 client_action_id: str) -> ActionResult:
    _host(actor)
    def handler(context):
        state = _state(context)
        if state.phase != EstimationPhase.ANSWERING:
            return _reject("phase_not_answering")
        from .controllers.estimation import _answers
        a, b = _answers(get_estimation_question_context(context.unit.pk), context.instance, state)
        if a is not None and b is not None:
            closed = DuelSubmission.objects.using(context.using).filter(unit=context.unit,
                submission_kind=SubmissionKind.ANSWER, status=SubmissionStatus.COMMITTED).latest("committed_at").committed_at
        elif timezone.now() >= state.deadline:
            closed = state.deadline
        else:
            return _reject("deadline_not_reached")
        _change(state, EstimationPhase.AWAITING_REVEAL, closed_at=closed, using=context.using)
        return HandlerResult(ReceiptStatus.APPLIED, _metadata(state))
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.estimation.reconcile_deadline", payload={}, handler=handler)


def advance_estimation_phase(*, instance_id: UUID, unit_id: int, actor: ActorContext, client_action_id: str,
                             target_phase: EstimationPhase, expected_revision: int | None = None,
                             expected_question=None) -> ActionResult:
    """A HOST intent, never a projection parameter; exact source phase required."""
    _host(actor)
    sources = {EstimationPhase.SHOW_ANSWERS: EstimationPhase.AWAITING_REVEAL,
        EstimationPhase.SHOW_SOLUTION: EstimationPhase.SHOW_ANSWERS,
        EstimationPhase.SHOW_RESULT: EstimationPhase.SHOW_SOLUTION,
        EstimationPhase.COMPLETED: EstimationPhase.SHOW_RESULT}
    if not isinstance(target_phase, EstimationPhase) or target_phase not in sources:
        raise ValueError("Only the four ordered host reveal/completion transitions are accepted.")
    payload = {"target_phase": target_phase.value}
    if expected_question is not None:
        from .controllers.estimation import QuestionContext
        if not isinstance(expected_question, QuestionContext):
            raise TypeError("expected_question must be an internal typed assertion.")
        payload["expected_material"] = {"question_id": expected_question.question_id,
            "question_text": expected_question.question_text, "target": str(expected_question.target), "unit": expected_question.unit}
    def handler(context):
        state = _state(context)
        if expected_question is not None:
            from .controllers.estimation import _identity
            _identity(context.instance, context.unit, expected_question)
        if state.phase != sources[target_phase]:
            return _reject("invalid_phase", phase=state.phase)
        result = {}
        if target_phase == EstimationPhase.SHOW_RESULT:
            from .controllers.estimation import _apply_estimation_result_in_action
            scored = _apply_estimation_result_in_action(context)
            if scored.status == ReceiptStatus.REJECTED:
                return scored
            result = scored.data
        if target_phase == EstimationPhase.COMPLETED:
            from .controllers.estimation import get_estimation_state
            get_estimation_state(instance_id=instance_id, unit_id=unit_id, actor=actor)  # verified final journal/outcome
        _change(state, target_phase, using=context.using)
        return HandlerResult(ReceiptStatus.APPLIED, {**result, **_metadata(state)})
    action = ("duel.estimation.evaluate" if target_phase == EstimationPhase.SHOW_RESULT else
              "duel.estimation.complete_round" if target_phase == EstimationPhase.COMPLETED else
              f"duel.estimation.{target_phase.value}")
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type=action, payload=payload, expected_revision=expected_revision, handler=handler,
        _completed_presentation=target_phase == EstimationPhase.COMPLETED)


def show_estimation_answers(**kwargs):
    return advance_estimation_phase(target_phase=EstimationPhase.SHOW_ANSWERS, **kwargs)


def show_estimation_solution(**kwargs):
    return advance_estimation_phase(target_phase=EstimationPhase.SHOW_SOLUTION, **kwargs)


def show_estimation_result(**kwargs):
    return advance_estimation_phase(target_phase=EstimationPhase.SHOW_RESULT, **kwargs)


def complete_estimation_round(**kwargs):
    return advance_estimation_phase(target_phase=EstimationPhase.COMPLETED, **kwargs)
