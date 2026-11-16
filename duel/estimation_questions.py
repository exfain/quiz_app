"""Internal Estimation material binding/resolution; no start/timer/reveal API.

Binding is a HOST execute_action command. It freezes only the three fields
currently consumed by the controller; hints/explanations and quiz rules are not
copied. The quiz FK records origin where HubGameStep has only a room code.
Source questions and their quiz selection can subsequently change; the old
binding is never refreshed. A fresh unit receives a fresh snapshot.

All normal writes use the existing parent lock, IMMEDIATE/lock retry, receipt
and revision boundary. Raw ORM/SQL updates remain outside the authorized API.
Resolver contexts are internal secrets. Browser projections reconstruct the
persisted phase and never accept client reveal permissions. Binding composition
also supports creation of a prepared unit in one existing command transaction.
"""
from decimal import Decimal, InvalidOperation
from sys import float_info
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import DEFAULT_DB_ALIAS
from django.core.exceptions import ValidationError
from Estimation.models import EstimationQuestion, EstimationQuiz
from games_hub.models import HubGameStep

from .commands import ActorContext, ActionResult, CommandRejected, HandlerResult, execute_action
from .models import (
    DuelEstimationQuestionBinding, DuelUnit, DuelSubmission, DuelScoreEntry,
    DuelUnitOutcome, DuelEstimationRoundState, ReceiptActorType, ReceiptStatus, UnitStatus,
)

if TYPE_CHECKING:
    from .controllers.estimation import QuestionContext


def canonical_target(value: str | float | Decimal) -> str:
    """Decimal(str(stored float)), fixed notation, no rounding/float round-trip.

    Str(float) is the existing source precision; no lost input digits are
    guessed. Decimal.normalize() is deliberately avoided (ambient rounding).
    512 characters cover every finite FloatField value including subnormals.
    """
    if type(value) not in (str, float, Decimal, int):
        raise ValueError("A finite decimal target is required.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid target.") from exc
    if (not number.is_finite() or number.copy_abs() > Decimal(str(float_info.max))
            or abs(number.as_tuple().exponent) > 510 or len(number.as_tuple().digits) > 512):
        raise ValueError("Unsupported source target.")
    if not number:
        return "0"
    text = format(number, "f")
    text = text.rstrip("0").rstrip(".") if "." in text else text
    if len(text) > 512:
        raise ValueError("Unsupported source target length.")
    return text


def bind_estimation_question(
    *, instance_id: UUID, unit_id: int, question_id: int,
    actor: ActorContext, client_action_id: str, expected_revision: int | None = None,
) -> ActionResult:
    """Bind once before activation; never select/activate a next unit.

    Same action ID replays its APPLIED receipt even after source edits/start.
    A fresh ID for an existing identical binding yields REJECTED/already_bound
    with its original ID and no new revision. A different question conflicts.
    """
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "Binding requires an internally authorized host.")
    if type(question_id) is not int or question_id <= 0 or type(unit_id) is not int or unit_id <= 0:
        raise ValueError("Concrete positive question/unit IDs are required.")
    def handler(context):
        return bind_estimation_question_in_action(context, question_id)
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.estimation.bind_question", payload={"question_id": question_id},
        expected_revision=expected_revision, handler=handler)



def _binding_reject(code, **data):
    return HandlerResult(ReceiptStatus.REJECTED, {"code": code, **data})


def bind_estimation_question_in_action(context, question_id):
    """Compose material binding inside an already-authorized command transaction."""
    from django.db import connection
    if not connection.in_atomic_block or context.unit is None or context.actor.actor_type != ReceiptActorType.HOST:
        raise RuntimeError("Binding composition requires a host command and concrete unit.")
    step = HubGameStep.objects.using(context.using).select_for_update().get(pk=context.instance.hub_game_step_id)
    if context.instance.game_key != "estimation" or step.game_key != "estimation":
        return _binding_reject("not_estimation")
    old = DuelEstimationQuestionBinding.objects.using(context.using).filter(unit=context.unit).first()
    if old is not None:
        return _binding_reject("already_bound" if old.question_id == question_id else "question_binding_conflict",
                      binding_id=str(old.pk))
    if context.unit.status != UnitStatus.PREPARED or context.unit.started_at is not None or context.unit.completed_at is not None:
        return _binding_reject("unit_not_prepared")
    if (DuelSubmission.objects.using(context.using).filter(unit=context.unit).exists()
            or DuelScoreEntry.objects.using(context.using).filter(unit=context.unit).exists()
            or DuelUnitOutcome.objects.using(context.using).filter(unit=context.unit).exists()):
        return _binding_reject("unit_has_history")
    quiz = EstimationQuiz.objects.using(context.using).select_for_update().filter(room_code=step.room_code).first()
    if quiz is None:
        return _binding_reject("quiz_not_found")
    question = EstimationQuestion.objects.using(context.using).select_for_update().filter(pk=question_id).first()
    if question is None:
        return _binding_reject("question_not_found")
    # Lock the actual selection row too; on SQLite the existing IMMEDIATE
    # transaction serializes edits, on row-locking DBs this blocks deletion.
    selection = EstimationQuiz.selected_questions.through.objects.using(context.using).select_for_update().filter(
        estimationquiz_id=quiz.pk, estimationquestion_id=question.pk,
    ).first()
    if selection is None:
        return _binding_reject("question_not_in_quiz")
    try:
        target = canonical_target(question.correct_answer)
    except ValueError:
        return _binding_reject("invalid_question_target")
    binding = DuelEstimationQuestionBinding(unit=context.unit, quiz=quiz, question=question,
        question_text=question.question_text, target_value=target, unit_key=question.unit)
    try:
        binding.full_clean()
    except ValidationError:
        return _binding_reject("invalid_question_material")
    binding.save(using=context.using, force_insert=True)
    state = DuelEstimationRoundState(unit=context.unit)
    state.full_clean()
    state.save(using=context.using, force_insert=True)
    # No snapshot/solution in the receipt acknowledgement.
    return HandlerResult(ReceiptStatus.APPLIED, {"binding_id": str(binding.pk), "unit_id": context.unit.pk,
        "question_id": question.pk, "quiz_id": quiz.pk})

def get_estimation_question_context(unit_id: int) -> "QuestionContext":
    """Resolve the immutable snapshot, NEVER reread the mutable question text/target.

    The deadline is read from the independent persisted round state, never from
    the source question or browser. Unstarted/legacy uninitialized units have no
    deadline; the phase service must explicitly initialize/start them first.
    """
    from .controllers.estimation import QuestionContext
    if type(unit_id) is not int or unit_id <= 0:
        raise ValueError("A concrete positive unit ID is required.")
    binding = DuelEstimationQuestionBinding.objects.using(DEFAULT_DB_ALIAS).filter(unit_id=unit_id).select_related(
        "unit__game_instance__hub_game_step").first()
    if binding is None:
        raise CommandRejected("question_not_bound", "This unit has no Estimation material binding.")
    instance, step = binding.unit.game_instance, binding.unit.game_instance.hub_game_step
    if instance.game_key != "estimation" or step.game_key != "estimation" or step.session_id != instance.hub_session_id:
        raise CommandRejected("invalid_estimation_binding", "Binding does not belong to an Estimation instance.")
    if canonical_target(binding.target_value) != binding.target_value:
        raise CommandRejected("invalid_estimation_binding", "Invalid persisted target snapshot.")
    deadline = DuelEstimationRoundState.objects.using(DEFAULT_DB_ALIAS).filter(unit_id=unit_id).values_list("deadline", flat=True).first()
    return QuestionContext(instance.pk, unit_id, binding.question_id, binding.question_text,
                           Decimal(binding.target_value), binding.unit_key, deadline)


def get_public_estimation_question(unit_id: int) -> dict:
    """Public material ONLY: no solution, submissions, receipts or reveal switch."""
    from .controllers.estimation import RevealContext, RevealPhase, _project_estimation
    return _project_estimation(reveal=RevealContext(get_estimation_question_context(unit_id), RevealPhase.QUESTION),
                              answer_a=None, answer_b=None)
