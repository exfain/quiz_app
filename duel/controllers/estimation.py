"""Internal Estimation duel rules, commands and explicit reveal projections.

QuestionContext is an internal material assertion, not an authentication
capability. load_question_context resolves the persisted unit binding. Public
projections load the stored round phase/clock; a caller's RevealContext cannot
grant a future reveal. Only the separate host commands advance that phase.

The source question uses FloatField. Decimal(str(stored_value)) preserves that
stored value; it cannot recover precision lost when material was imported.
Participant decimal strings retain precision. Existing finite-number and unit
conventions are reused, but float-backed decimal_number/display_number and the
percentage deviation/rank_answers are unsuitable for exact duel calculations.

Missing answers use the confirmed one/zero-submission rules. All result bookings use
execute_action and complete_unit_in_action AFTER both party bookings. The round
service composes scoring and phase changes in that same transaction. No next
unit, endpoint or Multiplayer state is changed here.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, Inexact, InvalidOperation, localcontext
from enum import Enum
from typing import Any
from uuid import UUID
from sys import float_info
from math import ceil

from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.utils import timezone

from Estimation.models import EstimationQuestion
from Estimation.numbers import finite_number
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubParticipant

from ..commands import ActorContext, ActionContext, ActionResult, CommandRejected, HandlerResult, execute_action
from ..contracts import MatchStatus, RoundOutcome, RoundResult
from ..match_service import complete_unit_in_action, get_match_snapshot, MatchIntegrityError
from ..models import (
    DuelGameInstance, DuelUnit, DuelSubmission, DuelScoreEntry, DuelUnitOutcome,
    ReceiptActorType, ReceiptStatus, SubmissionKind, SubmissionStatus, UnitStatus,
    DuelEstimationRoundState, EstimationPhase,
)
from ..submissions import commit_submission


# Resource guard for exact subtraction/output, not a scoring or rounding rule.
# Supports the full finite float magnitude range and very small decimal strings.
MAX_DECIMAL_PRECISION = 10000
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
Numeric = str | int | float | Decimal


def parse_estimate(value: Numeric) -> Decimal:
    """Dot decimals/scientific notation, no units, grouping, commas or bools.

    finite_number supplies the existing finite magnitude convention (1e309 is
    rejected). Its float result is NEVER used for scoring or normalization.
    Tiny nonzero decimal strings remain nonzero, despite float underflow.
    """
    if type(value) not in (str, int, float, Decimal):
        raise ValueError("An estimate must be a finite number.")
    text = str(value).strip()
    if len(text) > MAX_DECIMAL_PRECISION or not NUMBER.fullmatch(text):
        raise ValueError("Invalid decimal number format.")
    try:
        if type(value) is not Decimal:
            finite_number(text)
        result = Decimal(text)
    except (ValueError, TypeError, OverflowError, InvalidOperation) as exc:
        raise ValueError("An estimate must be finite.") from exc
    if (not result.is_finite() or abs(result.as_tuple().exponent) > MAX_DECIMAL_PRECISION
            or len(result.as_tuple().digits) > MAX_DECIMAL_PRECISION):
        raise ValueError("Unsupported decimal precision.")
    if result.copy_abs() > Decimal(str(float_info.max)):
        raise ValueError("An estimate must be within the finite source magnitude range.")
    return result


def _difference(answer: Decimal, target: Decimal) -> Decimal:
    precision = max(answer.adjusted(), target.adjusted(), 0) - min(
        answer.as_tuple().exponent, target.as_tuple().exponent, 0,
    ) + 2
    if precision > MAX_DECIMAL_PRECISION:
        raise ValueError("Unsupported exact subtraction precision.")
    with localcontext(Context(prec=precision)) as context:
        context.traps[Inexact] = True
        return (answer - target).copy_abs()


def _text(value: Decimal) -> str:
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class AnswerAvailability(str, Enum):
    BOTH = "both"
    ONLY_A = "only_a"
    ONLY_B = "only_b"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class EstimationEvaluation:
    availability: AnswerAvailability
    deviation_a: Decimal | None
    deviation_b: Decimal | None
    points_a: int | None
    points_b: int | None
    round_result: RoundResult

    @property
    def evaluable(self) -> bool:
        return self.round_result.outcome != RoundOutcome.UNDECIDED

    @property
    def reason(self) -> str:
        if self.availability != AnswerAvailability.BOTH: return 'no_answers' if self.availability == AnswerAvailability.NONE else 'only_valid_answer'
        return 'equal_deviation' if self.round_result.outcome == RoundOutcome.DRAW else 'closer_answer'


def evaluate_estimates(target: Numeric, answer_a: Numeric | None, answer_b: Numeric | None) -> EstimationEvaluation:
    """Pure exact comparison; None means missing, never infinite deviation."""
    target = parse_estimate(target)
    a = parse_estimate(answer_a) if answer_a is not None else None
    b = parse_estimate(answer_b) if answer_b is not None else None
    da = _difference(a, target) if a is not None else None
    db = _difference(b, target) if b is not None else None
    availability = (AnswerAvailability.BOTH if a is not None and b is not None else
                    AnswerAvailability.ONLY_A if a is not None else
                    AnswerAvailability.ONLY_B if b is not None else AnswerAvailability.NONE)
    if availability != AnswerAvailability.BOTH:
        outcome = RoundOutcome.PARTY_A_WINS if a is not None else RoundOutcome.PARTY_B_WINS if b is not None else RoundOutcome.NO_WINNER
        return EstimationEvaluation(availability, da, db, int(a is not None), int(b is not None), RoundResult(outcome))
    if da == db:
        return EstimationEvaluation(availability, da, db, 1, 1, RoundResult(RoundOutcome.DRAW))
    a_wins = da < db
    return EstimationEvaluation(availability, da, db, int(a_wins), int(not a_wins),
                                RoundResult(RoundOutcome.PARTY_A_WINS if a_wins else RoundOutcome.PARTY_B_WINS))


@dataclass(frozen=True, slots=True)
class QuestionContext:
    """Internal material snapshot; the resolver supplies the stored deadline."""
    instance_id: UUID
    unit_id: int
    question_id: int
    question_text: str
    target: Decimal
    unit: str
    deadline: datetime | None = None

    def __post_init__(self):
        if not isinstance(self.instance_id, UUID):
            raise TypeError("instance_id must be a UUID.")
        if any(type(value) is not int or value <= 0 for value in (self.unit_id, self.question_id)):
            raise ValueError("Concrete positive unit/question IDs are required.")
        if type(self.target) is not Decimal or parse_estimate(self.target) != self.target:
            raise TypeError("target must be a finite Decimal from server material.")
        if type(self.question_text) is not str or type(self.unit) is not str:
            raise TypeError("Question text and unit must be strings.")
        if self.deadline is not None and (not isinstance(self.deadline, datetime) or timezone.is_naive(self.deadline)):
            raise ValueError("deadline must be an aware server datetime or None.")


def load_question_context(
    *, instance_id: UUID, unit_id: int, question_id: int, deadline: datetime | None = None,
) -> QuestionContext:
    """Compatibility adapter: IDs are assertions, not material selection.

    Binding must already exist. No fallback to a live/source question is allowed.
    A supplied deadline is only an assertion against the persisted clock.
    """
    from ..estimation_questions import get_estimation_question_context
    context = get_estimation_question_context(unit_id)
    if context.instance_id != instance_id or context.question_id != question_id:
        raise CommandRejected("invalid_estimation_context", "IDs do not match the persisted material binding.")
    if deadline is not None and deadline != context.deadline:
        raise CommandRejected("estimation_deadline_mismatch", "Only the persisted deadline is authoritative.")
    return context


class RevealPhase(str, Enum):
    QUESTION = "question"
    ANSWERS = "answers"
    SOLUTION = "solution"
    RESULT = "result"


@dataclass(frozen=True, slots=True)
class RevealContext:
    """Internal formatting/legacy command intent, never reveal permission."""
    question: QuestionContext
    phase: RevealPhase

    def __post_init__(self):
        if not isinstance(self.question, QuestionContext) or not isinstance(self.phase, RevealPhase):
            raise TypeError("A trusted QuestionContext and RevealPhase are required.")


def _identity(instance: DuelGameInstance, unit: DuelUnit, question: QuestionContext):
    if (instance.pk != question.instance_id or unit.pk != question.unit_id
            or unit.game_instance_id != instance.pk or instance.game_key != "estimation"):
        raise CommandRejected("invalid_estimation_context", "Question/unit does not belong to this Estimation duel.")
    from ..estimation_questions import get_estimation_question_context
    bound = get_estimation_question_context(unit.pk)
    if (bound.instance_id, bound.question_id, bound.question_text, bound.target, bound.unit) != (
            question.instance_id, question.question_id, question.question_text, question.target, question.unit):
        raise CommandRejected("estimation_material_mismatch", "Supplied material differs from the persisted snapshot.")


def commit_estimate(
    *, question: QuestionContext, actor: ActorContext, client_action_id: str,
    payload: dict[str, Any] | None = None, draft_id: UUID | None = None,
    expected_revision: int | None = None,
) -> ActionResult:
    if not isinstance(question, QuestionContext):
        raise TypeError("question must be a trusted server QuestionContext.")
    def validate(context, resolved_payload, now):
        _identity(context.instance, context.unit, question)
        state = DuelEstimationRoundState.objects.using(context.using).filter(unit=context.unit).first()
        if state is None or state.phase != EstimationPhase.ANSWERING:
            return "answering_not_open"
        if now >= state.deadline:
            return "answer_deadline_passed"
        if set(resolved_payload) != {"value"}:
            return "invalid_estimate"
        try:
            _difference(parse_estimate(resolved_payload["value"]), question.target)
        except (ValueError, TypeError):
            return "invalid_estimate"
        return None
    from ..estimation_rounds import close_after_submission
    return commit_submission(instance_id=question.instance_id, unit_id=question.unit_id, actor=actor,
        submission_kind=SubmissionKind.ANSWER, client_action_id=client_action_id, payload=payload,
        draft_id=draft_id, expected_revision=expected_revision, validate=validate,
        on_committed=close_after_submission)


def _answers(question: QuestionContext, instance: DuelGameInstance, state=None) -> tuple[Decimal | None, Decimal | None]:
    rows = DuelSubmission.objects.using(DEFAULT_DB_ALIAS).filter(
        unit_id=question.unit_id, submission_kind=SubmissionKind.ANSWER, status=SubmissionStatus.COMMITTED,
    )
    values = {}
    for row in rows:
        if state is not None and (state.started_at is None or row.committed_at is None
                or not state.started_at <= row.committed_at < state.deadline):
            raise MatchIntegrityError("Committed Estimation answer lies outside the persisted window.")
        if row.participant_id not in (instance.participant_a_id, instance.participant_b_id):
            raise MatchIntegrityError("Persisted Estimation answer belongs to a non-party.")
        if type(row.payload) is not dict or set(row.payload) != {"value"}:
            raise MatchIntegrityError("Invalid persisted Estimation answer schema.")
        try:
            values[row.participant_id] = parse_estimate(row.payload["value"])
        except (ValueError, TypeError) as exc:
            raise MatchIntegrityError("Invalid persisted numerical answer.") from exc
    return values.get(instance.participant_a_id), values.get(instance.participant_b_id)


def _project_estimation(
    *, reveal: RevealContext, answer_a: Decimal | None, answer_b: Decimal | None,
    own_side: str | None = None,
) -> dict[str, Any]:
    """Pure projection, not a reveal authorization or phase transition.

    Only trusted server code supplies reveal and own_side. The DB-backed reader
    below derives own_side from ActorContext and requires persisted results for D.
    """
    if not isinstance(reveal, RevealContext):
        raise TypeError("An explicitly trusted RevealContext is required.")
    if own_side not in (None, "a", "b"):
        raise ValueError("Invalid internal party side.")
    question = reveal.question
    label, symbol = EstimationQuestion.get_unit_presentation(question.unit)
    def number(value):
        if value is None:
            return None
        text = _text(parse_estimate(value))
        formatted = f"{symbol}{text}" if question.unit in ("dollars", "euros", "EUR") else f"{text} {symbol}".strip()
        return {"value": text, "formatted": formatted}
    data = {
        "instance_id": str(question.instance_id), "unit_id": question.unit_id,
        "question_id": question.question_id, "phase": reveal.phase.value,
        "question": question.question_text, "unit": {"key": question.unit, "label": label, "symbol": symbol},
        "deadline": question.deadline.isoformat() if question.deadline else None,
    }
    if reveal.phase == RevealPhase.QUESTION:
        if own_side is not None:
            own = answer_a if own_side == "a" else answer_b
            data["own_answer"] = number(own)
            data["committed"] = own is not None
        return data
    data["answers"] = {"a": number(answer_a), "b": number(answer_b)}
    if reveal.phase == RevealPhase.ANSWERS:
        return data
    evaluation = evaluate_estimates(question.target, answer_a, answer_b)
    data["target"] = number(question.target)
    data["deviations"] = {"a": _text(evaluation.deviation_a) if evaluation.deviation_a is not None else None,
                          "b": _text(evaluation.deviation_b) if evaluation.deviation_b is not None else None}
    if reveal.phase == RevealPhase.RESULT:
        data["availability"] = evaluation.availability.value
        data["evaluable"] = evaluation.evaluable
        data["points"] = {"a": evaluation.points_a, "b": evaluation.points_b}
        data["round_outcome"] = evaluation.round_result.outcome.value
        data['result_reason'] = evaluation.reason
    return data


def apply_estimation_result(
    *, reveal: RevealContext, actor: ActorContext, client_action_id: str,
    expected_revision: int | None = None,
) -> ActionResult:
    """Compatibility command; a supplied RevealContext does NOT grant a phase.

    The persisted state must already be SHOW_SOLUTION. The phase service owns
    this transition and invokes the scoring composition below exactly once.
    """
    if not isinstance(reveal, RevealContext) or reveal.phase != RevealPhase.RESULT:
        raise ValueError("A trusted result-phase release is required.")
    from ..estimation_rounds import advance_estimation_phase
    return advance_estimation_phase(instance_id=reveal.question.instance_id, unit_id=reveal.question.unit_id,
        actor=actor, client_action_id=client_action_id, target_phase=EstimationPhase.SHOW_RESULT,
        expected_revision=expected_revision, expected_question=reveal.question)


def _apply_estimation_result_in_action(context: ActionContext) -> HandlerResult:
    """Scoring composition only, inside the existing SHOW_RESULT command."""
    from ..estimation_questions import get_estimation_question_context
    question = get_estimation_question_context(context.unit.pk)
    _identity(context.instance, context.unit, question)
    state = DuelEstimationRoundState.objects.using(context.using).get(unit=context.unit)
    if state.phase != EstimationPhase.SHOW_SOLUTION:
        return HandlerResult(ReceiptStatus.REJECTED, {"code": "invalid_phase"})
    if (context.unit.status != UnitStatus.ACTIVE or context.unit.completed_at is not None
            or DuelUnitOutcome.objects.using(context.using).filter(unit=context.unit).exists()):
        return HandlerResult(ReceiptStatus.REJECTED, {"code": "unit_already_evaluated"})
    a, b = _answers(question, context.instance, state)
    evaluation = evaluate_estimates(question.target, a, b)
    for party_id, points in ((context.instance.participant_a_id, evaluation.points_a),
                             (context.instance.participant_b_id, evaluation.points_b)):
        entry = DuelScoreEntry(game_instance=context.instance, unit=context.unit,
            participant_id=party_id, delta=points, source_key=f"estimation:{context.unit.uuid}:result")
        entry.full_clean()
        entry.save(using=context.using, force_insert=True)
    match = complete_unit_in_action(context, evaluation.round_result, reason=evaluation.reason)
    return HandlerResult(ReceiptStatus.APPLIED, {
        "unit_id": context.unit.pk, "points": {"a": evaluation.points_a, "b": evaluation.points_b},
        "round_outcome": evaluation.round_result.outcome.value,
        "match_evaluation": match.state.value, "match_status": context.instance.match_status,
    })


def get_estimation_state(
    *, unit_id: int | None = None, instance_id: UUID | None = None,
    reveal: RevealContext | None = None, actor: ActorContext | None = None,
) -> dict[str, Any]:
    """Reloadable internal public projection, never a transport authorization.

    Actor None denotes a trusted spectator reader. Participant identity is
    rechecked and only their own answer can appear in A. Phase D additionally
    requires consistent persisted bookings/outcome; it never previews points.
    Material comes from the binding and publication from the persisted phase.
    Expired clocks are reported without changing any database row.
    """
    if reveal is not None:
        if not isinstance(reveal, RevealContext):
            raise TypeError("reveal must be a legacy typed context, never request parameters.")
        if unit_id is not None and unit_id != reveal.question.unit_id:
            raise CommandRejected("invalid_unit", "Conflicting unit identities.")
        unit_id = reveal.question.unit_id
        if instance_id is not None and instance_id != reveal.question.instance_id:
            raise CommandRejected("invalid_instance", "Conflicting instance identities.")
        instance_id = reveal.question.instance_id
    if type(unit_id) is not int or unit_id <= 0:
        raise ValueError("A concrete positive unit ID is required.")
    def read():
        with transaction.atomic(using=DEFAULT_DB_ALIAS):
            unit = DuelUnit.objects.using(DEFAULT_DB_ALIAS).get(pk=unit_id)
            if instance_id is not None and unit.game_instance_id != instance_id:
                raise CommandRejected("invalid_unit", "Unit belongs to another instance.")
            instance = DuelGameInstance.objects.using(DEFAULT_DB_ALIAS).select_for_update().get(pk=unit.game_instance_id)
            # On row-locking databases a writer may have completed the unit
            # while this reader waited for the parent. Re-read under its lock.
            unit.refresh_from_db(using=DEFAULT_DB_ALIAS)
            from ..estimation_questions import get_estimation_question_context
            question = get_estimation_question_context(unit_id)
            _identity(instance, unit, question)
            if reveal is not None:
                _identity(instance, unit, reveal.question)  # phase/deadline never authorize publication
            state = DuelEstimationRoundState.objects.using(DEFAULT_DB_ALIAS).filter(unit=unit).first()
            if state is None:
                raise CommandRejected("round_not_initialized", "No persisted Estimation phase.")
            state.full_clean()
            own_side = None
            if actor is not None:
                if not isinstance(actor, ActorContext):
                    raise TypeError("actor must be an internal ActorContext.")
                if actor.actor_type == ReceiptActorType.PARTICIPANT:
                    if (actor.actor_id not in (instance.participant_a_id, instance.participant_b_id)
                            or not HubParticipant.objects.using(DEFAULT_DB_ALIAS).filter(
                                pk=actor.actor_id, session_id=instance.hub_session_id).exists()):
                        raise CommandRejected("actor_not_allowed", "Reader is not a party of this duel.")
                    own_side = "a" if actor.actor_id == instance.participant_a_id else "b"
            a, b = _answers(question, instance, state)
            format_phase = {EstimationPhase.SHOW_ANSWERS: RevealPhase.ANSWERS,
                EstimationPhase.SHOW_SOLUTION: RevealPhase.SOLUTION,
                EstimationPhase.SHOW_RESULT: RevealPhase.RESULT, EstimationPhase.COMPLETED: RevealPhase.RESULT}.get(state.phase, RevealPhase.QUESTION)
            data = _project_estimation(reveal=RevealContext(question, format_phase), answer_a=a, answer_b=b, own_side=own_side)
            data["state_revision"] = instance.state_revision
            now = timezone.now()
            expired = state.deadline is not None and now >= state.deadline
            closed = state.phase not in (EstimationPhase.PREPARED, EstimationPhase.ANSWERING)
            data.update(phase=state.phase, started_at=state.started_at.isoformat() if state.started_at else None,
                deadline=state.deadline.isoformat() if state.deadline else None,
                answering_closed_at=state.answering_closed_at.isoformat() if state.answering_closed_at else None,
                phase_changed_at=state.phase_changed_at.isoformat(), server_now=now.isoformat(),
                remaining_seconds=None if state.deadline is None else (
                    max(0, ceil((state.deadline-now).total_seconds())) if state.phase == EstimationPhase.ANSWERING else 0),
                answering_allowed=state.phase == EstimationPhase.ANSWERING and not expired and instance.match_status == MatchStatus.IN_PROGRESS.value,
                answer_phase_complete=closed or expired or (a is not None and b is not None),
                reconciliation_required=state.phase == EstimationPhase.ANSWERING and (expired or (a is not None and b is not None)))
            data["can_answer"] = own_side is not None and data["answering_allowed"] and not data.get("committed", False)
            if state.phase in (EstimationPhase.SHOW_RESULT, EstimationPhase.COMPLETED):
                evaluation = evaluate_estimates(question.target, a, b)
                outcome = DuelUnitOutcome.objects.using(DEFAULT_DB_ALIAS).filter(unit=unit).first()
                entries = list(DuelScoreEntry.objects.using(DEFAULT_DB_ALIAS).filter(game_instance=instance, unit=unit,
                    source_key=f"estimation:{unit.uuid}:result").values_list("participant_id", "delta"))
                if (not evaluation.evaluable or outcome is None or unit.status != UnitStatus.COMPLETED
                        or outcome.outcome != evaluation.round_result.outcome.value
                        or sorted(entries) != sorted([(instance.participant_a_id, evaluation.points_a),
                                                     (instance.participant_b_id, evaluation.points_b)])):
                    raise MatchIntegrityError("Result phase requires consistent persisted Estimation results.")
                data["match"] = get_match_snapshot(instance.pk)
            return data
    return read() if connection.in_atomic_block else _retry_sqlite_locked(read)


def project_estimation(*, reveal: RevealContext, actor: ActorContext | None = None) -> dict[str, Any]:
    """Compatibility projection: a requested RevealContext never overrides DB phase."""
    return get_estimation_state(reveal=reveal, actor=actor)
