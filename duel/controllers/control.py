"""Bid, Buzzer and manual score commands; one existing transaction boundary."""
from datetime import timedelta
from django.db import transaction, connection
from django.db.models import Sum
from django.utils import timezone
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubParticipant
from ..commands import ActorContext, CommandRejected, HandlerResult, execute_action
from ..contracts import RoundOutcome, RoundResult
from ..models import (DuelGameInstance, DuelUnit, DuelControlRound, DuelSubmission, DuelBuzz, DuelDecision,
    DuelScoreEntry, DuelUnitOutcome, ControlPhase as Phase, ReceiptActorType, ReceiptStatus, SubmissionKind)
from ..control_games import CONTROL_GAMES, validate_config
from ..submissions import commit_submission
from ..match_service import complete_unit_in_action, get_match_snapshot, MatchIntegrityError
from ..matches import parse_match_config


def _reject(code): return HandlerResult(ReceiptStatus.REJECTED, {"code": code})


def _host(actor):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "Nur der Host darf diese Aktion ausführen.")


def _state(context):
    if not connection.in_atomic_block or context.unit is None: raise RuntimeError("An existing command/unit is required.")
    if context.instance.game_key not in CONTROL_GAMES or context.instance.units.latest("number").pk != context.unit.pk:
        raise CommandRejected("stale_unit", "Diese Runde ist nicht mehr aktuell.")
    validate_config(context.instance.game_key, parse_match_config(context.instance.rule_config))
    state = DuelControlRound.objects.select_for_update().filter(unit=context.unit).first()
    if state is None: raise CommandRejected("invalid_unit", "Die Runde ist noch nicht vorbereitet.")
    state.full_clean()
    return state


def _save(state, phase):
    state.phase, state.phase_changed_at = str(phase), timezone.now(); state.full_clean(); state.save()


def bind_control_in_action(context, selection):
    _host(context.actor)
    if not connection.in_atomic_block or context.unit is None: raise RuntimeError("Use execute_action.")
    key = context.instance.game_key
    if key not in CONTROL_GAMES or context.unit.status != "prepared": return _reject("invalid_unit")
    game = CONTROL_GAMES[key][0].objects.select_for_update().get(room_code=context.instance.hub_game_step.room_code)
    material = {"source_game_id": game.pk}
    if key == "bid":
        task = game.tasks.select_for_update().filter(pk=selection, is_active=True).first()
        if not task: return _reject("invalid_task")
        task.full_clean()
        material.update(task_id=task.pk, text=task.text, bid_seconds=task.bid_seconds, answer_seconds=task.answer_seconds)
    elif selection != 1: return _reject("invalid_task")
    elif key == "buzzer": material["points"] = game.points_per_correct
    state = DuelControlRound(unit=context.unit, material=material)
    state.full_clean(); state.save(force_insert=True)
    return HandlerResult(ReceiptStatus.APPLIED, {"unit_id": context.unit.pk, "phase": state.phase})


def _bids(instance, state):
    rows = list(DuelSubmission.objects.filter(unit=state.unit, status="committed", submission_kind="bid", input_key="").order_by("acceptance_sequence"))
    for row in rows:
        row.full_clean()
        if (row.participant_id not in (instance.participant_a_id, instance.participant_b_id)
                or type(row.payload) is not dict or set(row.payload) != {"value"}
                or type(row.payload["value"]) is not int or row.payload["value"] < 1
                or state.bid_started_at is None or not state.bid_started_at <= row.committed_at < state.bid_deadline):
            raise MatchIntegrityError("Invalid committed bid/window.")
    return rows


def _assign_right(context, state):
    bids = _bids(context.instance, state)
    if not bids: _finish(context, state, None, 0); return
    winner = min(bids, key=lambda b: (-b.payload["value"], b.acceptance_sequence))
    state.right_participant_id = winner.participant_id
    state.answer_started_at = timezone.now()
    state.answer_deadline = state.answer_started_at+timedelta(seconds=state.material["answer_seconds"])
    _save(state, Phase.ANSWERING)


def commit_bid(*, instance_id, unit_id, actor, client_action_id, payload):
    if type(payload) is not dict or set(payload) != {"value"} or type(payload["value"]) is not int or payload["value"] < 1:
        raise ValueError("Ein Gebot muss eine positive ganze Zahl sein.")
    def validate(context, value, now):
        state = _state(context)
        if context.instance.game_key != "bid" or state.phase != Phase.BIDDING: return "bidding_closed"
        if not state.bid_started_at <= now < state.bid_deadline: return "bid_deadline_passed"
        return None
    def after(context, submission):
        state = _state(context)
        if len(_bids(context.instance, state)) == 2: _assign_right(context, state)
    return commit_submission(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        submission_kind=SubmissionKind.BID, payload=payload, validate=validate, on_committed=after)


def _finish(context, state, winner, points=1):
    for party in (context.instance.participant_a_id, context.instance.participant_b_id):
        entry = DuelScoreEntry(game_instance=context.instance, unit=context.unit, participant_id=party,
            delta=points if party == winner else 0, source_key=f"control:{state.pk}:result")
        entry.full_clean(); entry.save(force_insert=True)
    result = RoundOutcome.NO_WINNER if winner is None else RoundOutcome.PARTY_A_WINS if winner == context.instance.participant_a_id else RoundOutcome.PARTY_B_WINS
    reason = ''
    if context.instance.game_key == 'bid':
        reason = 'no_bids' if winner is None else 'bid_fulfilled' if winner == state.right_participant_id else 'bid_timeout'
    complete_unit_in_action(context, RoundResult(result), reason=reason); _save(state, Phase.SHOW_RESULT)


def _expire(context, state):
    now = timezone.now()
    if context.instance.game_key != "bid": return False
    if state.phase in {Phase.BIDDING, Phase.NEEDS_RESOLUTION} and now >= state.bid_deadline:
        _assign_right(context, state); return True
    if state.phase == Phase.ANSWERING and now >= state.answer_deadline:
        opponent = context.instance.participant_b_id if state.right_participant_id == context.instance.participant_a_id else context.instance.participant_a_id
        _finish(context, state, opponent); return True
    return False


def start_round(*, instance_id, unit_id, actor, client_action_id):
    _host(actor)
    def handler(context):
        state = _state(context)
        if state.phase != Phase.PREPARED or context.instance.match_status != "in_progress": return _reject("invalid_phase")
        now = timezone.now()
        if context.instance.hub_game_step.intro_ends_at and now < context.instance.hub_game_step.intro_ends_at: return _reject("intro_not_finished")
        context.unit.status, context.unit.started_at = "active", now
        context.unit.full_clean(); context.unit.save(update_fields=["status", "started_at", "updated_at"])
        if context.instance.game_key == "bid":
            state.bid_started_at = now; state.bid_deadline = now+timedelta(seconds=state.material["bid_seconds"])
        _save(state, Phase.BIDDING if context.instance.game_key == "bid" else Phase.READY if context.instance.game_key == "buzzer" else Phase.ANSWERING)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.control.start", payload={}, handler=handler)


def buzz(*, instance_id, unit_id, actor, client_action_id):
    if actor.actor_type != ReceiptActorType.PARTICIPANT: raise CommandRejected("participant_required", "Ein Teilnehmerkontext ist erforderlich.")
    def handler(context):
        state = _state(context)
        if context.instance.game_key != "buzzer" or state.phase != Phase.OPEN: return _reject("buzzer_closed")
        if context.unit.buzzes.filter(participant=context.participant).exists(): return _reject("party_blocked")
        row = DuelBuzz(unit=context.unit, participant=context.participant, sequence=context.unit.buzzes.count()+1,
            elapsed_us=max(0, int((timezone.now()-context.unit.started_at).total_seconds()*1_000_000)))
        row.full_clean(); row.save(force_insert=True); _save(state, Phase.LOCKED)
        return HandlerResult(ReceiptStatus.APPLIED, {"buzz_id": str(row.pk)})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.control.buzz", payload={}, handler=handler)


def host_action(*, instance_id, unit_id, actor, client_action_id, action, expected_counter=None, buzz_id=None, verdict=None, participant_id=None, delta=None):
    _host(actor)
    if action not in {"open", "judge", "increment", "adjust", "end_round", "complete", "reconcile"}: raise ValueError("Invalid host action.")
    def handler(context):
        state = _state(context); key = context.instance.game_key
        if action == "complete":
            if state.phase != Phase.SHOW_RESULT: return _reject("invalid_phase")
            get_state(instance_id=instance_id, unit_id=unit_id, actor=actor)
            _save(state, Phase.COMPLETED)
        elif action == "reconcile":
            if not _expire(context, state): return _reject("deadline_not_reached")
        elif action == "increment" and key == "bid" and state.phase == Phase.ANSWERING:
            if _expire(context, state): return HandlerResult(ReceiptStatus.APPLIED, {"code": "answer_deadline_passed"})
            if type(expected_counter) is not int or expected_counter != state.counter: return _reject("stale_counter")
            state.counter += 1
            bid = next(b for b in _bids(context.instance, state) if b.participant_id == state.right_participant_id)
            if state.counter >= bid.payload["value"]: _finish(context, state, state.right_participant_id)
            else: _save(state, Phase.ANSWERING)
        elif action == "open" and key == "buzzer" and state.phase == Phase.READY: _save(state, Phase.OPEN)
        elif action == "judge" and key == "buzzer" and state.phase == Phase.LOCKED:
            latest = context.unit.buzzes.order_by("-sequence").first()
            if not latest or str(latest.pk) != buzz_id or verdict not in {"correct", "incorrect"}: return _reject("invalid_attempt")
            decision = DuelDecision(game_instance=context.instance, unit=context.unit, subject_type="buzz", subject_key=str(latest.pk), verdict=verdict, host_key=actor.key)
            decision.full_clean(); decision.save(force_insert=True)
            if verdict == "correct": _finish(context, state, latest.participant_id, state.material["points"])
            else: _save(state, Phase.OPEN if context.unit.buzzes.count() < 2 else Phase.BLOCKED)
        elif action == "adjust" and key == "host_points" and state.phase == Phase.ANSWERING:
            if type(expected_counter) is not int or expected_counter != state.counter: return _reject("stale_counter")
            if type(participant_id) is not int or participant_id not in (context.instance.participant_a_id, context.instance.participant_b_id): return _reject("invalid_party")
            if type(delta) is not int or delta == 0 or not -2147483648 <= delta <= 2147483647: return _reject("invalid_delta")
            tally = get_match_snapshot(instance_id)
            old = tally["party_a" if participant_id == context.instance.participant_a_id else "party_b"]["score"]
            if not -2147483648 <= old+delta <= 2147483647: return _reject("invalid_delta")
            entry = DuelScoreEntry(game_instance=context.instance, unit=context.unit, participant_id=participant_id, delta=delta,
                source_key=f"host:{actor.key}:{client_action_id}")
            entry.full_clean(); entry.save(force_insert=True)
            state.counter += 1
            tally = get_match_snapshot(instance_id)
            if tally["party_a"]["score"] >= tally["target_score"] or tally["party_b"]["score"] >= tally["target_score"]:
                complete_unit_in_action(context, RoundResult(RoundOutcome.NO_WINNER)); _save(state, Phase.SHOW_RESULT)
            else: _save(state, Phase.ANSWERING)
        elif action == "end_round" and key in {"buzzer", "host_points"} and state.phase in {Phase.READY, Phase.OPEN, Phase.LOCKED, Phase.BLOCKED, Phase.ANSWERING}:
            if key == "host_points":
                complete_unit_in_action(context, RoundResult(RoundOutcome.NO_WINNER)); _save(state, Phase.SHOW_RESULT)
            else: _finish(context, state, None, 0)
        else: return _reject("invalid_phase")
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.control.complete_round" if action == "complete" else "duel.control."+action,
        payload={"expected_counter": expected_counter, "buzz_id": buzz_id, "verdict": verdict, "participant_id": participant_id, "delta": delta},
        handler=handler, _completed_presentation=action == "complete")


def get_state(*, instance_id, unit_id, actor=None):
    def read():
        with transaction.atomic():
            instance = DuelGameInstance.objects.select_for_update().get(pk=instance_id)
            unit = DuelUnit.objects.get(pk=unit_id, game_instance=instance)
            state = DuelControlRound.objects.filter(unit=unit).first()
            if state is None: raise MatchIntegrityError("No control state exists for this unit.")
            state.full_clean()
            own = actor.actor_id if actor and actor.actor_type == ReceiptActorType.PARTICIPANT else None
            if own and (own not in (instance.participant_a_id, instance.participant_b_id) or not HubParticipant.objects.filter(pk=own, session_id=instance.hub_session_id).exists()):
                raise CommandRejected("actor_not_allowed", "Keine beteiligte Partei.")
            if instance.game_key not in CONTROL_GAMES: raise MatchIntegrityError("Wrong game.")
            data = {"unit_id": unit.pk, "phase": state.phase, "state_revision": instance.state_revision,
                "server_now": timezone.now().isoformat(), "question": state.material.get("text", ""), "counter": state.counter,
                "deadline": (state.bid_deadline.isoformat() if state.phase == Phase.BIDDING else state.answer_deadline.isoformat() if state.phase == Phase.ANSWERING and state.answer_deadline else None)}
            if instance.game_key == "bid":
                bids = _bids(instance, state); own_bid = next((b for b in bids if b.participant_id == own), None)
                if own: data.update(own_bid=own_bid.payload["value"] if own_bid else None, can_bid=state.phase == Phase.BIDDING and timezone.now() < state.bid_deadline and own_bid is None)
                data["submitted"] = {"a": any(b.participant_id == instance.participant_a_id for b in bids), "b": any(b.participant_id == instance.participant_b_id for b in bids)}
                if state.phase not in {Phase.PREPARED, Phase.BIDDING}:
                    data["bids"] = {"a": next((b.payload["value"] for b in bids if b.participant_id == instance.participant_a_id), None), "b": next((b.payload["value"] for b in bids if b.participant_id == instance.participant_b_id), None)}
                    data["right_participant_id"] = state.right_participant_id
                    data["winning_bid"] = next((b.payload["value"] for b in bids if b.participant_id == state.right_participant_id), None)
                if state.phase == Phase.NEEDS_RESOLUTION: data["resolution_reason"] = "missing_bids"
            if instance.game_key == "buzzer":
                latest = unit.buzzes.order_by("-sequence").first()
                # Convert the textual subject keys through UUIDField's normal
                # preparation; SQLite stores UUIDs without their hyphens.
                incorrect = list(unit.decisions.filter(verdict="incorrect", subject_type="buzz").values_list("subject_key", flat=True))
                blocked = list(unit.buzzes.filter(pk__in=incorrect).values_list("participant_id", flat=True))
                data.update(buzz_id=str(latest.pk) if latest and state.phase == Phase.LOCKED else None,
                    right_participant_id=latest.participant_id if latest and state.phase == Phase.LOCKED else None,
                    blocked=blocked, own_blocked=own in blocked,
                    own_has_right=own is not None and latest is not None and latest.participant_id == own and state.phase == Phase.LOCKED,
                    can_buzz=own is not None and state.phase == Phase.OPEN and not unit.buzzes.filter(participant_id=own).exists())
            if state.phase in {Phase.SHOW_RESULT, Phase.COMPLETED}:
                outcome = DuelUnitOutcome.objects.get(unit=unit); outcome.full_clean()
                data.update(round_outcome=outcome.outcome, winner_id=outcome.winner_id)
                if instance.game_key == 'bid':
                    data['result_reason'] = 'no_bids' if not _bids(instance, state) else 'bid_fulfilled' if state.counter >= data['winning_bid'] else 'bid_timeout'
            return data
    return read() if connection.in_atomic_block else _retry_sqlite_locked(read)
