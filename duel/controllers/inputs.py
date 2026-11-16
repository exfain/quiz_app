"""Where rounds / whole Black Jack sets over the existing transactional boundary."""
import math
from datetime import timedelta
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubParticipant
from where_is_this.geo import web_mercator_lat_lng_to_norm, validate_map_location
from black_jack_quiz.runtime import strict_integer
from ..commands import ActorContext, CommandRejected, HandlerResult, execute_action
from ..contracts import RoundOutcome, RoundResult
from ..models import (DuelInputRound, DuelGameInstance, DuelUnit, DuelSubmission, DuelScoreEntry, DuelUnitOutcome,
    InputPhase as Phase, ReceiptActorType, ReceiptStatus, SubmissionKind, SubmissionStatus)
from ..match_service import complete_unit_in_action, get_match_snapshot, MatchIntegrityError
from ..submissions import commit_submission, save_draft
from ..input_material import INPUT_GAMES, freeze_material, distance, stars


def _reject(code): return HandlerResult(ReceiptStatus.REJECTED, {"code": code})


def _host(actor):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "Nur der Host darf die Runde steuern.")


def scope(key, index): return f"question:{index}" if key == "blackjack" else ""


def _state(context):
    if not connection.in_atomic_block or context.unit is None: raise RuntimeError("An existing command/unit is required.")
    if context.instance.game_key not in INPUT_GAMES or context.instance.units.latest("number").pk != context.unit.pk:
        raise CommandRejected("stale_unit", "Die Runde ist nicht mehr aktuell.")
    state = DuelInputRound.objects.select_for_update().filter(unit=context.unit).first()
    if state is None: raise CommandRejected("invalid_unit", "Die Runde ist noch nicht vorbereitet.")
    state.full_clean()
    return state


def bind_input_question_in_action(context, selection):
    _host(context.actor)
    if not connection.in_atomic_block or context.unit is None: raise RuntimeError("Use the existing command transaction.")
    key, step = context.instance.game_key, context.instance.hub_game_step
    if key not in INPUT_GAMES or step.game_key != key or step.session_id != context.instance.hub_session_id: return _reject("invalid_unit")
    if context.unit.status != "prepared" or DuelInputRound.objects.filter(unit=context.unit).exists(): return _reject("unit_not_prepared")
    game = INPUT_GAMES[key][0].objects.select_for_update().get(room_code=step.room_code)
    try: material, image = freeze_material(game, key, selection)
    except (ValueError, OSError): return _reject("invalid_question")
    state = DuelInputRound(unit=context.unit, material=material, image=image)
    state.full_clean(); state.save(force_insert=True)
    return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase, "unit_id": context.unit.pk})


def _save(state, phase):
    state.phase, state.phase_changed_at = str(phase), timezone.now(); state.full_clean(); state.save()


def _window(state, index):
    if index == state.question_index: return state.question_started_at, state.deadline
    row = state.history[index]
    start, deadline = parse_datetime(row["started_at"]), parse_datetime(row["deadline"])
    if not start or not deadline or timezone.is_naive(start) or timezone.is_naive(deadline) or deadline <= start:
        raise MatchIntegrityError("Invalid persisted question window.")
    return start, deadline


def normalize(key, payload):
    if type(payload) is not dict: raise ValueError("Invalid input.")
    if key == "blackjack":
        if set(payload) != {"value"}: raise ValueError("An integer is required.")
        return {"value": strict_integer(payload["value"])}
    if set(payload) != {"latitude", "longitude"} or any(type(v) not in (int, float) or not math.isfinite(v) for v in payload.values()):
        raise ValueError("Finite coordinates are required.")
    lat, lon = payload["latitude"], payload["longitude"]
    web_mercator_lat_lng_to_norm(lat, lon)
    return {"latitude": lat, "longitude": lon}


def _rows(instance, state, index):
    rows = list(DuelSubmission.objects.filter(unit=state.unit, input_key=scope(instance.game_key, index),
        submission_kind="answer", status="committed"))
    start, deadline = _window(state, index); result = {}
    for row in rows:
        row.full_clean()
        if row.participant_id not in (instance.participant_a_id, instance.participant_b_id) or start is None or deadline is None or not start <= row.committed_at < deadline:
            raise MatchIntegrityError("Submission does not belong to the saved question/window.")
        value = normalize(instance.game_key, row.payload)
        if instance.game_key == "where": validate_map_location(state.material["questions"][index]["map_type"], **value)
        result[row.participant_id] = value
    return result


def submit(*, instance_id, unit_id, actor, client_action_id, question_index, payload, commit=True):
    if type(question_index) is not int or question_index < 0 or type(commit) is not bool: raise ValueError("A concrete question is required.")
    key = DuelGameInstance.objects.get(pk=instance_id).game_key
    if key not in INPUT_GAMES: raise ValueError("Unsupported game.")
    payload = normalize(key, payload)
    def validate(context, value, now):
        state = _state(context)
        if state.question_index != question_index: return "stale_question"
        if state.phase != Phase.ANSWERING: return "answering_not_open"
        if now < state.question_started_at or now >= state.deadline: return "answer_deadline_passed"
        try:
            value = normalize(key, value)
            if key == "where": validate_map_location(state.material["questions"][question_index]["map_type"], **value)
        except ValueError: return "invalid_input"
        return None
    def after(context, submission):
        state = _state(context)
        if len(_rows(context.instance, state, question_index)) == 2:
            state.closed_at = submission.committed_at; _save(state, Phase.AWAITING_REVEAL)
    options = dict(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        submission_kind=SubmissionKind.ANSWER, payload=payload, input_key=scope(key, question_index), validate=validate)
    return commit_submission(**options, on_committed=after) if commit else save_draft(**options)


def start_round(*, instance_id, unit_id, actor, client_action_id, question_index):
    _host(actor)
    if type(question_index) is not int or question_index < 0: raise ValueError("A concrete question is required.")
    def handler(context):
        state = _state(context)
        if state.phase != Phase.PREPARED or state.question_index != question_index: return _reject("invalid_phase")
        now = timezone.now()
        if context.instance.hub_game_step.intro_ends_at and now < context.instance.hub_game_step.intro_ends_at: return _reject("intro_not_finished")
        if state.started_at is None:
            context.unit.status, context.unit.started_at = "active", now
            context.unit.full_clean(); context.unit.save(update_fields=["status", "started_at", "updated_at"])
            state.started_at = now
        state.question_started_at = now
        state.deadline = now+timedelta(seconds=state.material["questions"][question_index]["duration"])
        _save(state, Phase.ANSWERING)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.input.start", payload={"question_index": question_index}, handler=handler)


def reconcile_deadline(*, instance_id, unit_id, actor, client_action_id, question_index):
    _host(actor)
    if type(question_index) is not int or question_index < 0: raise ValueError("A concrete question is required.")
    def handler(context):
        state = _state(context)
        if state.phase != Phase.ANSWERING or state.question_index != question_index or timezone.now() < state.deadline: return _reject("deadline_not_reached")
        state.closed_at = state.deadline; _save(state, Phase.AWAITING_REVEAL)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.input.deadline", payload={"question_index": question_index}, handler=handler)


def evaluation(instance, state):
    """Both parties' complete question batches first; acceptance order is irrelevant."""
    parties = (instance.participant_a_id, instance.participant_b_id)
    if instance.game_key == "where":
        rows = _rows(instance, state, 0)
        distances = {p: distance(state.material["questions"][0], v) for p, v in rows.items()}
        if len(rows) != 2: return next(iter(rows), None), 'only_valid_answer' if rows else 'no_answers', distances
        if distances[parties[0]] == distances[parties[1]]: return None, "equal_distances", distances
        return min(parties, key=lambda p: distances[p]), None, distances
    totals = dict.fromkeys(parties, 0)
    for index in range(state.question_index+1):
        rows = _rows(instance, state, index)
        if len(rows) != 2: return None, "missing_answers", totals
        for p in parties: totals[p] += stars(state.material["questions"][index], rows[p]["value"])
    a, b = (totals[p] for p in parties)
    if a > 21 and b > 21: return None, "both_bust", totals
    if a > 21 or b > 21: return parties[1] if a > 21 else parties[0], None, totals  # Includes confirmed exact 21.
    if state.question_index+1 < len(state.material["questions"]): return None, "set_continues", totals
    if a == b: return None, "equal_stars", totals
    return max(parties, key=lambda p: totals[p]), None, totals


def _finish(context, state, winner):
    reason = evaluation(context.instance, state)[1]
    tied = reason == 'equal_distances'
    for party in (context.instance.participant_a_id, context.instance.participant_b_id):
        score = DuelScoreEntry(game_instance=context.instance, unit=context.unit, participant_id=party,
            delta=int(tied or party == winner), source_key=f"input:{state.pk}:result")
        score.full_clean(); score.save(force_insert=True)
    outcome = (RoundOutcome.DRAW if tied else RoundOutcome.NO_WINNER) if winner is None else RoundOutcome.PARTY_A_WINS if winner == context.instance.participant_a_id else RoundOutcome.PARTY_B_WINS
    complete_unit_in_action(context, RoundResult(outcome), reason=reason or ('closer_location' if context.instance.game_key == 'where' else 'closer_to_twenty_one'))
    _save(state, Phase.SHOW_RESULT)


def advance(*, instance_id, unit_id, actor, client_action_id, question_index, action):
    _host(actor)
    if type(question_index) is not int or question_index < 0: raise ValueError("A concrete question is required.")
    if action not in {"reveal", "show_answers", "show_solution", "evaluate", "next", "complete"}: raise ValueError("Unknown host command.")
    def handler(context):
        state = _state(context); key = context.instance.game_key
        if state.question_index != question_index: return _reject("stale_question")
        if action == "reveal" and key == "where" and state.phase == Phase.AWAITING_REVEAL:
            state.reveal_started_at = timezone.now(); _save(state, Phase.SHOW_SOLUTION)
        elif action == "show_answers" and key == "blackjack" and state.phase == Phase.AWAITING_REVEAL:
            state.reveal_started_at = timezone.now(); _save(state, Phase.SHOW_ANSWERS)
        elif action == "show_solution" and key == "blackjack" and state.phase == Phase.SHOW_ANSWERS:
            _save(state, Phase.SHOW_SOLUTION)
        elif action == "evaluate" and state.phase in {Phase.SHOW_SOLUTION, Phase.NEEDS_RESOLUTION}:
            winner, reason, _ = evaluation(context.instance, state)
            if winner is not None or reason in {'no_answers', 'equal_distances', 'both_bust', 'equal_stars'}: _finish(context, state, winner)
            else: _save(state, Phase.QUESTION_RESULT if reason == "set_continues" else Phase.NEEDS_RESOLUTION)
        elif action == "next" and key == "blackjack" and state.phase == Phase.QUESTION_RESULT:
            if evaluation(context.instance, state)[1] != "set_continues": raise MatchIntegrityError("The set cannot continue.")
            state.history.append({"started_at": state.question_started_at.isoformat(), "deadline": state.deadline.isoformat(),
                "closed_at": state.closed_at.isoformat(), "revealed_at": state.reveal_started_at.isoformat()})
            state.question_index += 1
            state.question_started_at = state.deadline = state.closed_at = state.reveal_started_at = None
            _save(state, Phase.PREPARED)
        elif action == "complete" and state.phase == Phase.SHOW_RESULT:
            get_state(instance_id=instance_id, unit_id=unit_id, actor=actor)
            _save(state, Phase.COMPLETED)
        else: return _reject("result_unresolved" if state.phase == Phase.NEEDS_RESOLUTION else "invalid_phase")
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.input.complete_round" if action == "complete" else "duel.input."+action,
        payload={"question_index": question_index, "action": action}, handler=handler, _completed_presentation=action == "complete")


def get_state(*, instance_id, unit_id, actor=None):
    def read():
        with transaction.atomic():
            instance = DuelGameInstance.objects.select_for_update().get(pk=instance_id)
            if instance.game_key not in INPUT_GAMES: raise ValueError("Unsupported input game.")
            unit = DuelUnit.objects.get(pk=unit_id, game_instance=instance)
            state = DuelInputRound.objects.filter(unit=unit).first()
            if state is None: raise MatchIntegrityError("No bound question/set exists for this unit.")
            state.full_clean()
            own = None
            if actor is not None:
                if not isinstance(actor, ActorContext): raise TypeError("An internal actor is required.")
                if actor.actor_type == ReceiptActorType.PARTICIPANT:
                    if actor.actor_id not in (instance.participant_a_id, instance.participant_b_id) or not HubParticipant.objects.filter(pk=actor.actor_id, session_id=instance.hub_session_id).exists():
                        raise CommandRejected("actor_not_allowed", "Not a match party.")
                    own = actor.actor_id
            q = state.material["questions"][state.question_index]; phase = state.phase
            rows = _rows(instance, state, state.question_index) if state.question_started_at else {}
            data = {"instance_id": str(instance.pk), "unit_id": unit.pk, "phase": phase,
                "question_index": state.question_index, "question_count": len(state.material["questions"]),
                "state_revision": instance.state_revision, "server_now": timezone.now().isoformat(),
                "deadline": state.deadline.isoformat() if state.deadline else None,
                "reveal_started_at": state.reveal_started_at.isoformat() if state.reveal_started_at else None,
                "submitted": {"a": instance.participant_a_id in rows, "b": instance.participant_b_id in rows},
                "can_answer": own is not None and phase == Phase.ANSWERING and timezone.now() < state.deadline and own not in rows and instance.match_status == "in_progress"}
            if own:
                data["own_answer"] = rows.get(own)
                draft = DuelSubmission.objects.filter(unit=unit, participant_id=own, submission_kind="answer", input_key=scope(instance.game_key, state.question_index), status="draft").order_by("-created_at", "-pk").first()
                data["draft"] = normalize(instance.game_key, draft.payload) if draft and own not in rows else None
            if phase != Phase.PREPARED or (actor and actor.actor_type == ReceiptActorType.HOST):
                data["question"] = q["text"]
                if instance.game_key == "where": data.update(map_type=q["map_type"], hint=q["hint"], has_image=state.image is not None)
            answer_reveal = phase in {Phase.SHOW_ANSWERS, Phase.SHOW_SOLUTION, Phase.QUESTION_RESULT, Phase.NEEDS_RESOLUTION, Phase.SHOW_RESULT, Phase.COMPLETED}
            solution_reveal = answer_reveal and phase != Phase.SHOW_ANSWERS
            if answer_reveal: data["answers"] = {"a": rows.get(instance.participant_a_id), "b": rows.get(instance.participant_b_id)}
            if solution_reveal:
                data.update(target=q["target"], explanation=q["explanation"])
                if instance.game_key == "where": data["distances"] = {"a": distance(q, rows[instance.participant_a_id]) if instance.participant_a_id in rows else None,
                    "b": distance(q, rows[instance.participant_b_id]) if instance.participant_b_id in rows else None}
            if instance.game_key == "blackjack":
                published = state.question_index + int(phase in {Phase.QUESTION_RESULT, Phase.NEEDS_RESOLUTION, Phase.SHOW_RESULT, Phase.COMPLETED})
                totals = {"a": 0, "b": 0}
                for index in range(published):
                    inputs = _rows(instance, state, index)
                    for side, party in (("a", instance.participant_a_id), ("b", instance.participant_b_id)):
                        if party in inputs: totals[side] += stars(state.material["questions"][index], inputs[party]["value"])
                data.update(stars=totals, bust={side: value > 21 for side, value in totals.items()})
                if phase in {Phase.QUESTION_RESULT, Phase.NEEDS_RESOLUTION, Phase.SHOW_RESULT, Phase.COMPLETED}:
                    data["question_stars"] = {side: stars(q, rows[party]["value"]) if party in rows else None
                        for side, party in (("a", instance.participant_a_id), ("b", instance.participant_b_id))}
            if phase == Phase.NEEDS_RESOLUTION: data["resolution_reason"] = evaluation(instance, state)[1]
            if phase in {Phase.SHOW_RESULT, Phase.COMPLETED}:
                winner, reason, _ = evaluation(instance, state)
                outcome = DuelUnitOutcome.objects.get(unit=unit)
                points = dict(DuelScoreEntry.objects.filter(game_instance=instance, unit=unit, source_key=f"input:{state.pk}:result").values_list("participant_id", "delta"))
                expected = {p: int(reason == 'equal_distances' or p == winner) for p in (instance.participant_a_id, instance.participant_b_id)}
                expected_outcome = ('draw' if reason == 'equal_distances' else 'no_winner') if winner is None else 'party_a_wins' if winner == instance.participant_a_id else 'party_b_wins'
                if reason in {'missing_answers', 'set_continues'} or points != expected or outcome.winner_id != winner or outcome.outcome != expected_outcome:
                    raise MatchIntegrityError("Saved result contradicts the complete input batch.")
                data.update(points={"a": points[instance.participant_a_id], "b": points[instance.participant_b_id]}, round_outcome=outcome.outcome,
                    result_reason=reason or ('closer_location' if instance.game_key == 'where' else 'closer_to_twenty_one'), match=get_match_snapshot(instance.pk))
            return data
    return read() if connection.in_atomic_block else _retry_sqlite_locked(read)
