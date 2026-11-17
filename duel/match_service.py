"""Internal match orchestration using execute_action, never a transport API.

Public commands accept server-confirmed actors and server-selected RoundResult.
They are not authorization adapters for arbitrary client result/winner claims.
Game controllers own scoring, phase/deadline checks, material and reveal timing.
They may call reconcile_match_in_action AFTER their entire score batch, within
the SAME existing ActionContext/transaction. No score-writing service or formula
is introduced here. Direct ORM mutations remain outside this internal contract.
"""
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.db.models import Sum, Q
from django.utils import timezone
from games_hub.authoritative_state import _retry_sqlite_locked

from .commands import ActorContext, ActionContext, ActionResult, HandlerResult, execute_action
from .contracts import MatchStatus, RoundOutcome, RoundResult
from .matches import MatchConfig, MatchMode, MatchEvaluation, MatchEvaluationState as State, evaluate_match, parse_match_config
from .models import DuelGameInstance, DuelUnit, DuelUnitOutcome, DuelMatchOutcome, DuelScoreEntry, ReceiptStatus, UnitStatus
from .read_transactions import snapshot_transaction


class MatchIntegrityError(RuntimeError):
    """Inconsistent persisted history: never repair or invent a winner."""


def _winner(instance, evaluation):
    if evaluation.winner_side == RoundOutcome.PARTY_A_WINS:
        return instance.participant_a_id
    if evaluation.winner_side == RoundOutcome.PARTY_B_WINS:
        return instance.participant_b_id
    return None


def _target_reached_at(instance, unit, target):
    entries = DuelScoreEntry.objects.filter(game_instance=instance).filter(
        Q(unit__number__lte=unit.number) | Q(unit__isnull=True, created_at__lte=unit.completed_at))
    totals = entries.aggregate(a=Sum('delta', filter=Q(participant_id=instance.participant_a_id)),
                              b=Sum('delta', filter=Q(participant_id=instance.participant_b_id)))
    return (totals['a'] or 0) >= target and (totals['b'] or 0) >= target


def _read(instance):
    """Called under the parent lock/read transaction; only authoritative data."""
    alias = instance._state.db or DEFAULT_DB_ALIAS
    scores = DuelScoreEntry.objects.using(alias).filter(game_instance=instance)
    if scores.exclude(participant_id__in=(instance.participant_a_id, instance.participant_b_id)).exists():
        raise MatchIntegrityError("A score entry belongs to a non-party.")
    if scores.filter(unit__isnull=False).exclude(unit__game_instance_id=instance.pk).exists():
        raise MatchIntegrityError("A score entry references another instance's unit.")
    totals = scores.aggregate(a=Sum("delta", filter=Q(participant_id=instance.participant_a_id)),
                              b=Sum("delta", filter=Q(participant_id=instance.participant_b_id)))
    a, b = totals["a"] or 0, totals["b"] or 0
    outcomes = list(DuelUnitOutcome.objects.using(alias).filter(unit__game_instance=instance).select_related("unit").order_by('unit__number'))
    completed_count = DuelUnit.objects.using(alias).filter(game_instance=instance, status=UnitStatus.COMPLETED).count()
    if completed_count != len(outcomes) or any(outcome.unit.status != UnitStatus.COMPLETED for outcome in outcomes):
        raise MatchIntegrityError("Unit lifecycle and final outcome history disagree.")
    try:
        for outcome in outcomes:
            outcome.full_clean()
    except ValidationError as exc:
        raise MatchIntegrityError("Invalid persisted unit outcome.") from exc
    rounds = [RoundResult(RoundOutcome(outcome.outcome)) for outcome in outcomes]
    completion = DuelMatchOutcome.objects.using(alias).filter(game_instance=instance).first()
    if (completion is None) != (instance.match_status != MatchStatus.COMPLETED.value):
        raise MatchIntegrityError("Match status and completion history disagree.")
    try:
        if completion is not None:
            completion.full_clean()
            mode = MatchMode(completion.mode)
            config = MatchConfig(mode, **{"best_of" if mode == MatchMode.BEST_OF_N else "target_score": completion.threshold})
        else:
            config = parse_match_config(instance.rule_config)
    except (TypeError, ValueError, ValidationError) as exc:
        if completion is not None:
            raise MatchIntegrityError("Invalid match completion rule snapshot.") from exc
        return None, MatchEvaluation(State.INVALID_CONFIGURATION,
            wins_a=sum(r.outcome == RoundOutcome.PARTY_A_WINS for r in rounds),
            wins_b=sum(r.outcome == RoundOutcome.PARTY_B_WINS for r in rounds),
            completed_units=len(rounds), reason="invalid_configuration"), a, b, None
    boundary = None
    if instance.tiebreak_unit_id is not None:
        boundary = next((i+1 for i, row in enumerate(outcomes) if row.unit_id == instance.tiebreak_unit_id), None)
        if boundary is None or config.mode != MatchMode.TARGET_SCORE:
            raise MatchIntegrityError('Invalid persisted tiebreak boundary.')
        if not _target_reached_at(instance, outcomes[boundary-1].unit, config.target_score):
            raise MatchIntegrityError('The saved tiebreak boundary predates both target scores.')
    evaluation = evaluate_match(config, score_a=a, score_b=b, round_results=rounds, tiebreak_after=boundary)
    if completion is not None and _winner(instance, evaluation) != completion.winner_id:
        raise MatchIntegrityError("Closed tally contradicts the recorded winner; no automatic reopening.")
    if completion is not None and completion.winner_id is None:
        from .encounters import presentation_finished
        if completion.reason == 'tournament_no_winner':
            if (config.mode != MatchMode.BEST_OF_N or config.best_of != 1 or len(outcomes) != 1
                    or evaluation.wins_a + evaluation.wins_b or not hasattr(instance, 'tournament_pair')
                    or instance.hub_game_step.game_format != 'tournament'):
                raise MatchIntegrityError('Invalid tournament draw history.')
        elif not outcomes or not presentation_finished(instance):
            raise MatchIntegrityError('An exhausted-material draw needs a completed presentation.')
        evaluation = MatchEvaluation(State.DRAW, evaluation.wins_a, evaluation.wins_b, evaluation.completed_units, completion.reason)
    if instance.match_status == MatchStatus.ABORTED.value:
        evaluation = MatchEvaluation(State.NEEDS_RESOLUTION, evaluation.wins_a, evaluation.wins_b,
                                     evaluation.completed_units, "match_aborted")
    return config, evaluation, a, b, completion


def _reject(code):
    return HandlerResult(ReceiptStatus.REJECTED, {"code": code})


def _in_action(context):
    if not isinstance(context, ActionContext) or not connection.in_atomic_block or context.using != DEFAULT_DB_ALIAS:
        raise RuntimeError("Match mutations require the existing execute_action context and transaction.")
    if context.instance.match_status != MatchStatus.IN_PROGRESS.value:
        raise MatchIntegrityError("Only a running match may be changed.")


def reconcile_match_in_action(context: ActionContext) -> MatchEvaluation:
    """Evaluate once AFTER a controller's complete score/result batch.

    No partial-score callback, nested execute_action, own revision increment or
    automatic next round. A clear result records completion in this transaction;
    ambiguity is returned unchanged, with neither winner nor terminal status.
    """
    _in_action(context)
    config, evaluation, _, _, completion = _read(context.instance)
    if config is None:
        raise MatchIntegrityError("A valid match configuration is required before mutation.")
    if completion is not None:
        raise MatchIntegrityError("Match already has a completion record.")
    winner_id = _winner(context.instance, evaluation)
    if evaluation.state == State.SUDDEN_DEATH and context.instance.tiebreak_unit_id is None:
        trigger = context.instance.units.filter(status=UnitStatus.COMPLETED).order_by('-number').first()
        # Generic score-only handlers may not have completed a unit yet. They
        # report sudden death; the boundary is latched by the final unit batch.
        if trigger is not None and _target_reached_at(context.instance, trigger, config.target_score):
            context.instance.tiebreak_unit = trigger
            context.instance.save(update_fields=['tiebreak_unit', 'updated_at'])
    tournament_draw = (winner_id is None and context.instance.hub_game_step.game_format == 'tournament'
        and evaluation.completed_units == 1 and evaluation.wins_a + evaluation.wins_b == 0
        and hasattr(context.instance, 'tournament_pair'))
    if winner_id is not None or tournament_draw:
        completion = DuelMatchOutcome(game_instance=context.instance, winner_id=winner_id,
            mode=config.mode.value, threshold=config.best_of if config.best_of is not None else config.target_score,
            reason='tournament_no_winner' if tournament_draw else 'win')
        completion.full_clean()
        completion.save(using=context.using, force_insert=True)
        context.instance.match_status = MatchStatus.COMPLETED.value
        context.instance.save(using=context.using, update_fields=["match_status", "updated_at"])
        if tournament_draw:
            evaluation = MatchEvaluation(State.DRAW, evaluation.wins_a, evaluation.wins_b, evaluation.completed_units, 'tournament_no_winner')
    return evaluation


def start_match(*, instance_id: UUID, actor: ActorContext, client_action_id: str, config: MatchConfig,
                expected_revision: int | None = None) -> ActionResult:
    if not isinstance(config, MatchConfig):
        raise TypeError("An explicit validated MatchConfig is required.")
    def handler(context):
        return start_match_in_action(context, config)
    return execute_action(instance_id=instance_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.match.start", payload={"config": config.to_dict()}, handler=handler, expected_revision=expected_revision)



def start_match_in_action(context, config):
    """Same match-start rules, composable with locked Hub provisioning."""
    if not connection.in_atomic_block or not isinstance(config, MatchConfig):
        raise RuntimeError("A command transaction and validated MatchConfig are required.")
    if context.instance.match_status != MatchStatus.NOT_STARTED.value:
        return _reject("match_already_started")
    if (DuelScoreEntry.objects.filter(game_instance=context.instance).exists()
            or DuelUnitOutcome.objects.filter(unit__game_instance=context.instance).exists()
            or DuelMatchOutcome.objects.filter(game_instance=context.instance).exists()):
        return _reject("existing_match_history")
    context.instance.rule_config = {**context.instance.rule_config, "match": config.to_dict()}
    context.instance.match_status = MatchStatus.IN_PROGRESS.value
    context.instance.save(update_fields=["rule_config", "match_status", "updated_at"])
    return HandlerResult(ReceiptStatus.APPLIED, {"code": "match_started", "config": config.to_dict()})

def complete_unit_in_action(context: ActionContext, result: RoundResult, *, reason: str = '') -> MatchEvaluation:
    """Trusted controller composition point; creates no points or new unit."""
    _in_action(context)
    if context.unit is None or context.unit.game_instance_id != context.instance.pk:
        raise MatchIntegrityError("A concrete unit of the locked instance is required.")
    if not isinstance(result, RoundResult) or result.outcome == RoundOutcome.UNDECIDED:
        raise ValueError("Only a final, explicitly supplied RoundResult may be persisted.")
    if context.unit.status != UnitStatus.ACTIVE or DuelUnitOutcome.objects.filter(unit=context.unit).exists():
        raise MatchIntegrityError("Unit must be active and have no previous outcome.")
    parse_match_config(context.instance.rule_config)  # validates before mutation
    winner_id = {RoundOutcome.PARTY_A_WINS: context.instance.participant_a_id,
                 RoundOutcome.PARTY_B_WINS: context.instance.participant_b_id}.get(result.outcome)
    now = timezone.now()
    context.unit.status = UnitStatus.COMPLETED
    context.unit.completed_at = now
    context.unit.full_clean()
    context.unit.save(update_fields=["status", "completed_at", "updated_at"])
    outcome = DuelUnitOutcome(unit=context.unit, outcome=result.outcome.value, winner_id=winner_id, completed_at=now, reason=reason)
    outcome.full_clean()
    outcome.save(force_insert=True)
    return reconcile_match_in_action(context)


def complete_unit(*, instance_id: UUID, unit_id: int, actor: ActorContext, client_action_id: str,
                  result: RoundResult, expected_revision: int | None = None) -> ActionResult:
    if not isinstance(result, RoundResult):
        raise TypeError("result must be the existing RoundResult contract.")
    def handler(context):
        if context.instance.match_status != MatchStatus.IN_PROGRESS.value:
            return _reject("match_not_running")
        if context.unit.status != UnitStatus.ACTIVE or DuelUnitOutcome.objects.filter(unit=context.unit).exists():
            return _reject("unit_already_completed" if context.unit.status == UnitStatus.COMPLETED else "unit_not_active")
        if result.outcome == RoundOutcome.UNDECIDED:
            return _reject("unit_undecided")
        try:
            parse_match_config(context.instance.rule_config)
        except (TypeError, ValueError):
            return _reject("invalid_configuration")
        evaluation = complete_unit_in_action(context, result)
        return HandlerResult(ReceiptStatus.APPLIED, {"code": "unit_completed", "unit_id": unit_id,
            "outcome": result.outcome.value, "evaluation": evaluation.state.value,
            "reason": evaluation.reason, "winner_id": _winner(context.instance, evaluation)})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.match.complete_unit", payload={"outcome": result.outcome.value},
        expected_revision=expected_revision, handler=handler)


def finalize_match(*, instance_id: UUID, actor: ActorContext, client_action_id: str,
                   expected_winner_id: int | None = None, expected_revision: int | None = None) -> ActionResult:
    """Winner is derived; expected_winner_id is only an optional precondition."""
    if expected_winner_id is not None and (type(expected_winner_id) is not int or expected_winner_id <= 0):
        raise ValueError("Expected winner must be a positive participant ID or None.")
    def handler(context):
        if context.instance.match_status != MatchStatus.IN_PROGRESS.value:
            return _reject("match_not_running")
        config, evaluation, _, _, _ = _read(context.instance)
        if config is None:
            return _reject("invalid_configuration")
        winner_id = _winner(context.instance, evaluation)
        if winner_id is None:
            return _reject("match_needs_resolution" if evaluation.state == State.NEEDS_RESOLUTION else "match_not_decided")
        if expected_winner_id is not None and winner_id != expected_winner_id:
            return _reject("winner_conflict")
        reconcile_match_in_action(context)
        return HandlerResult(ReceiptStatus.APPLIED, {"code": "match_completed", "winner_id": winner_id,
                                                   "evaluation": evaluation.state.value})
    return execute_action(instance_id=instance_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.match.finalize", payload={"expected_winner_id": expected_winner_id},
        expected_revision=expected_revision, handler=handler)


def get_match_snapshot(instance_id: UUID) -> dict:
    """Internal aggregate projection in a consistent read transaction.

    Reads no submissions, decisions, receipt payloads, nicknames or full rule
    configuration. winner_id is a PERSISTED match winner, not a provisional
    threshold candidate. Scores remain signed/unclamped, and no totals are
    cached or written. Standalone SQLite reads do not reserve the writer slot;
    row-locking databases retain the parent lock. A surrounding command
    transaction is reused if present.
    """
    if not isinstance(instance_id, UUID):
        raise TypeError("instance_id must be a concrete duel instance UUID.")

    def read_once():
        with snapshot_transaction(using=DEFAULT_DB_ALIAS):
            instance = DuelGameInstance.objects.using(DEFAULT_DB_ALIAS).select_for_update().get(pk=instance_id)
            try:
                # Persisted UNIQUE/CHECK invariants are enforced by the DB.
                # Retain field and cross-table checks without re-querying the
                # same constraints on each read-only device poll.
                instance.full_clean(validate_unique=False, validate_constraints=False)
            except ValidationError as exc:
                raise MatchIntegrityError("Invalid persisted match identity.") from exc
            config, evaluation, a, b, completion = _read(instance)
            return {
                "instance_id": str(instance.pk), "match_status": instance.match_status,
                "mode": config.mode.value if config else None,
                "best_of": config.best_of if config else None,
                "target_score": config.target_score if config else None,
                "required_wins": config.required_wins if config else None,
                "party_a": {"participant_id": instance.participant_a_id, "score": a, "round_wins": evaluation.wins_a},
                "party_b": {"participant_id": instance.participant_b_id, "score": b, "round_wins": evaluation.wins_b},
                "completed_units": evaluation.completed_units,
                "winner_id": completion.winner_id if completion else None,
                "completed_at": completion.completed_at.isoformat() if completion else None,
                "evaluation": evaluation.state.value, "resolution_reason": evaluation.reason,
                "tiebreak_unit_id": instance.tiebreak_unit_id,
                "draw": bool(completion and completion.winner_id is None),
                "completion_reason": completion.reason if completion else None,
                "state_revision": instance.state_revision,
            }

    if connection.in_atomic_block:
        return read_once()
    return _retry_sqlite_locked(read_once)


def evaluate_current_match(instance_id: UUID) -> MatchEvaluation:
    snapshot = get_match_snapshot(instance_id)
    return MatchEvaluation(State(snapshot["evaluation"]), snapshot["party_a"]["round_wins"],
        snapshot["party_b"]["round_wins"], snapshot["completed_units"], snapshot["resolution_reason"])


def get_match_result(instance_id: UUID) -> dict | None:
    """Only a verified persisted completion is a final result."""
    snapshot = get_match_snapshot(instance_id)
    return snapshot if snapshot["match_status"] == MatchStatus.COMPLETED.value else None


def finish_exhausted_match(*, instance_id, actor, client_action_id):
    """Explicit Host draw, only after presentation and verified material exhaustion."""
    from .models import ReceiptActorType
    from .encounters import presentation_finished
    from .material_supply import exhausted
    if actor.actor_type != ReceiptActorType.HOST:
        raise ValueError('A confirmed Host is required.')
    def handler(context):
        if context.instance.hub_game_step.game_format == 'tournament':
            return _reject('tournament_manages_completion')
        config, evaluation, _, _, _ = _read(context.instance)
        if config is None or evaluation.winner_side is not None or evaluation.state not in {State.RUNNING, State.SUDDEN_DEATH}:
            return _reject('match_not_drawable')
        if not presentation_finished(context.instance): return _reject('unit_not_finished')
        if not exhausted(context.instance): return _reject('material_available')
        completion = DuelMatchOutcome(game_instance=context.instance, mode=config.mode.value,
            threshold=config.best_of if config.best_of is not None else config.target_score, reason='material_exhausted')
        completion.full_clean(); completion.save(force_insert=True)
        context.instance.match_status = MatchStatus.COMPLETED.value
        context.instance.save(update_fields=['match_status', 'updated_at'])
        return HandlerResult(ReceiptStatus.APPLIED, {'code':'match_draw', 'winner_id':None})
    return execute_action(instance_id=instance_id, actor=actor, client_action_id=client_action_id,
        action_type='duel.match.draw', payload={}, handler=handler)
