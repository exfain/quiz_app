"""Shared oral duel mechanics with four explicit source/rule adapters.

Every write composes in execute_action; no Multiplayer runtime or score writes.
Clue Rush pauses its logical clock during assessment and resumes the remaining
microseconds. Names and tolerances are captured at server acceptance, not taken
from display state or request payloads. Projections never reconcile on GET.
"""
from datetime import timedelta
from decimal import Decimal, localcontext

from django.db import connection, transaction
from django.utils import timezone
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubParticipant
from ..commands import ActorContext, CommandRejected, HandlerResult, execute_action
from ..contracts import RoundOutcome, RoundResult
from ..match_service import complete_unit_in_action, get_match_snapshot, MatchIntegrityError
from ..models import (DuelGameInstance, DuelUnit, DuelOralRound, DuelBuzz, DuelDecision,
    DuelScoreEntry, DuelUnitOutcome, OralPhase as Phase, ReceiptActorType, ReceiptStatus,
    DecisionSubjectType, DecisionVerdict)
from ..oral_material import ORAL_GAMES, freeze_material

# Explicitly confirmed for these four variants, never a default for future games.
NO_BUZZ_ZERO = frozenset({"who", "clue_rush", "who_that", "wann_war_das"})


def _reject(code):
    return HandlerResult(ReceiptStatus.REJECTED, {"code": code})


def _host(actor):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "Nur der Host darf diese Aktion ausführen.")


def _micros(delta):
    return delta.days*86_400_000_000 + delta.seconds*1_000_000 + delta.microseconds


def elapsed(state, now):
    running = max(0, _micros(now-state.resumed_at)) if state.phase == Phase.ANSWERING else 0
    return min(state.material["duration_us"], state.elapsed_us+running)


def _text(value):
    text = format(value, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


def capture(game_key, material, elapsed_us):
    """Pure server clock projection; exact person/tolerance boundaries."""
    if game_key == "who":
        index = min(elapsed_us//material["person_us"], len(material["people"])-1)
        return {"person_index": index, "name": material["people"][index]["name"]}
    if game_key == "wann_war_das":
        with localcontext() as ctx:
            ctx.prec = 1024
            step = elapsed_us//material["step_us"]
            tolerance = min(Decimal(material["max_tolerance"]), Decimal(material["start_tolerance"])+step*Decimal(material["tolerance_increment"]))
            return {"step": step, "tolerance": _text(tolerance)}
    return {}


def _state(context):
    if not connection.in_atomic_block or context.unit is None:
        raise RuntimeError("An existing command transaction/unit is required.")
    if context.instance.game_key not in ORAL_GAMES or context.instance.units.latest("number").pk != context.unit.pk:
        raise CommandRejected("invalid_unit", "Die Aktion gehört nicht zur aktuellen Runde.")
    state = DuelOralRound.objects.select_for_update().get(unit=context.unit)
    state.full_clean()
    return state


def bind_oral_question_in_action(context, question_id):
    _host(context.actor)
    if not connection.in_atomic_block or context.unit is None:
        raise RuntimeError("Binding requires the existing command transaction.")
    key, step = context.instance.game_key, context.instance.hub_game_step
    if key not in ORAL_GAMES or step.game_key != key or step.session_id != context.instance.hub_session_id:
        return _reject("invalid_unit")
    if context.unit.status != "prepared" or context.unit.started_at or context.unit.completed_at:
        return _reject("unit_not_prepared")
    if DuelOralRound.objects.filter(unit=context.unit).exists():
        return _reject("already_bound")
    game = ORAL_GAMES[key][0].objects.select_for_update().get(room_code=step.room_code)
    question = game.selected_questions.select_for_update().filter(pk=question_id, is_active=True).first()
    if question is None:
        return _reject("question_not_in_quiz")
    try:
        material, image = freeze_material(key, game, question)
    except (ValueError, OSError):
        return _reject("invalid_question")
    state = DuelOralRound(unit=context.unit, source_question_id=question.pk, material=material, image=image)
    state.full_clean(); state.save(force_insert=True)
    return HandlerResult(ReceiptStatus.APPLIED, {"unit_id": context.unit.pk, "phase": state.phase})


def _save(state, phase, now):
    state.phase, state.phase_changed_at = str(phase), now
    state.full_clean(); state.save()


def start_round(*, instance_id, unit_id, actor, client_action_id):
    _host(actor)
    def handler(context):
        state = _state(context)
        if state.phase != Phase.PREPARED or context.instance.match_status != "in_progress":
            return _reject("round_already_started")
        now = timezone.now()
        if context.instance.hub_game_step.intro_ends_at and now < context.instance.hub_game_step.intro_ends_at:
            return _reject("intro_not_finished")
        context.unit.status, context.unit.started_at = "active", now
        context.unit.full_clean(); context.unit.save(update_fields=["status", "started_at", "updated_at"])
        state.started_at = state.resumed_at = now
        state.deadline = now+timedelta(microseconds=state.material["duration_us"])
        _save(state, Phase.ANSWERING, now)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.oral.start", payload={}, handler=handler)


def _finish(context, state, winner_id, now):
    if context.unit.status != "active" or DuelUnitOutcome.objects.filter(unit=context.unit).exists():
        raise MatchIntegrityError("An oral unit can be evaluated once.")
    for participant in (context.instance.participant_a_id, context.instance.participant_b_id):
        entry = DuelScoreEntry(game_instance=context.instance, unit=context.unit, participant_id=participant,
            delta=int(participant == winner_id), source_key=f"oral:{context.unit.uuid}:result")
        entry.full_clean(); entry.save(force_insert=True)
    outcome = RoundOutcome.NO_WINNER if winner_id is None else RoundOutcome.PARTY_A_WINS if winner_id == context.instance.participant_a_id else RoundOutcome.PARTY_B_WINS
    complete_unit_in_action(context, RoundResult(outcome))
    state.closed_at = state.closed_at or now
    state.resumed_at = state.deadline = None
    _save(state, Phase.SHOW_RESULT, now)


def buzz(*, instance_id, unit_id, actor, client_action_id):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.PARTICIPANT:
        raise CommandRejected("participant_required", "Nur eine Duellpartei darf buzzern.")
    def handler(context):
        state = _state(context)
        if state.phase != Phase.ANSWERING:
            return _reject("answering_not_open")
        if DuelBuzz.objects.filter(unit=context.unit, participant=context.participant).exists():
            return _reject("party_locked")
        now = timezone.now()
        if now < state.resumed_at or now >= state.deadline:
            return _reject("answer_deadline_passed")
        passed = elapsed(state, now)
        attempt = DuelBuzz(unit=context.unit, participant=context.participant,
            sequence=context.unit.buzzes.count()+1, accepted_at=now, elapsed_us=passed,
            captured=capture(context.instance.game_key, state.material, passed))
        attempt.full_clean(); attempt.save(force_insert=True)
        state.elapsed_us, state.resumed_at, state.deadline = passed, None, None
        if context.instance.game_key == "who":
            lying = state.material["people"][attempt.captured["person_index"]]["is_lying"]
            opponent = context.instance.participant_b_id if attempt.participant_id == context.instance.participant_a_id else context.instance.participant_a_id
            _finish(context, state, attempt.participant_id if lying else opponent, now)
        else:
            _save(state, Phase.AWAITING_DECISION, now)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase, "buzz_id": str(attempt.pk)})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.oral.buzz", payload={}, handler=handler)


def judge(*, instance_id, unit_id, actor, client_action_id, buzz_id, verdict):
    _host(actor)
    if type(buzz_id) is not str or type(verdict) is not str or verdict not in DecisionVerdict.values:
        raise ValueError("A concrete buzz and valid verdict are required.")
    def handler(context):
        state = _state(context)
        attempt = context.unit.buzzes.order_by("-sequence").first()
        if state.phase != Phase.AWAITING_DECISION or attempt is None or str(attempt.pk) != buzz_id:
            return _reject("invalid_answer_right")
        decision = DuelDecision(game_instance=context.instance, unit=context.unit,
            subject_type=DecisionSubjectType.BUZZ, subject_key=str(attempt.pk), verdict=verdict, host_key=actor.key)
        decision.full_clean(); decision.save(force_insert=True)
        now = timezone.now()
        if context.instance.game_key == "clue_rush" and verdict == DecisionVerdict.INCORRECT:
            if context.unit.buzzes.count() < 2 and state.elapsed_us < state.material["duration_us"]:
                state.resumed_at = now
                state.deadline = now+timedelta(microseconds=state.material["duration_us"]-state.elapsed_us)
                _save(state, Phase.ANSWERING, now)
            else:
                _finish(context, state, None, now)
        else:
            opponent = context.instance.participant_b_id if attempt.participant_id == context.instance.participant_a_id else context.instance.participant_a_id
            _finish(context, state, attempt.participant_id if verdict == DecisionVerdict.CORRECT else opponent, now)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.oral.judge", payload={"buzz_id": buzz_id, "verdict": verdict}, handler=handler)


def reconcile_deadline(*, instance_id, unit_id, actor, client_action_id):
    _host(actor)
    def handler(context):
        state = _state(context)
        if state.phase != Phase.ANSWERING or timezone.now() < state.deadline:
            return _reject("deadline_not_reached")
        state.elapsed_us, state.closed_at = state.material["duration_us"], state.deadline
        state.resumed_at = state.deadline = None
        _save(state, Phase.AWAITING_RESULT, timezone.now())
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.oral.deadline", payload={}, handler=handler)


def advance(*, instance_id, unit_id, actor, client_action_id, action):
    _host(actor)
    if action not in {"show_result", "complete"}:
        raise ValueError("Invalid oral release.")
    def handler(context):
        state = _state(context)
        if action == "show_result":
            if state.phase != Phase.AWAITING_RESULT:
                return _reject("invalid_phase")
            if context.instance.game_key not in NO_BUZZ_ZERO:
                return _reject("no_buzz_policy_unresolved")
            _finish(context, state, None, timezone.now())
        else:
            if state.phase != Phase.SHOW_RESULT:
                return _reject("invalid_phase")
            get_state(instance_id=instance_id, unit_id=unit_id, actor=actor)
            _save(state, Phase.COMPLETED, timezone.now())
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.oral.complete_round" if action == "complete" else "duel.oral.show_result",
        payload={"action": action}, handler=handler, _completed_presentation=action == "complete")


def _recorded_winner(instance, state, attempts):
    key=instance.game_key
    if len(attempts) > (2 if key == "clue_rush" else 1):
        raise MatchIntegrityError("Too many oral attempts.")
    if key == "who":
        if not attempts:return None
        attempt=attempts[0]
        lying=state.material["people"][attempt.captured["person_index"]]["is_lying"]
        return attempt.participant_id if lying else instance.participant_b_id if attempt.participant_id == instance.participant_a_id else instance.participant_a_id
    decisions={d.subject_key:d for d in DuelDecision.objects.filter(game_instance=instance,unit=state.unit,subject_type=DecisionSubjectType.BUZZ)}
    for attempt in attempts:
        if str(attempt.pk) not in decisions:
            raise MatchIntegrityError("An oral result requires every accepted attempt's host decision.")
        decisions[str(attempt.pk)].full_clean()
    if key == "clue_rush":
        correct=[a for a in attempts if decisions[str(a.pk)].verdict == DecisionVerdict.CORRECT]
        if len(correct)>1:raise MatchIntegrityError("A clue round can have one correct answer.")
        if correct:return correct[0].participant_id
        if len(attempts)==2 or key in NO_BUZZ_ZERO:return None
    elif attempts:
        attempt=attempts[0]
        return attempt.participant_id if decisions[str(attempt.pk)].verdict == DecisionVerdict.CORRECT else instance.participant_b_id if attempt.participant_id == instance.participant_a_id else instance.participant_a_id
    if key not in NO_BUZZ_ZERO:
        raise MatchIntegrityError("This no-buzz result has no authorized rule.")
    return None


def get_state(*, instance_id, unit_id, actor=None):
    def read():
        with transaction.atomic():
            instance = DuelGameInstance.objects.select_for_update().get(pk=instance_id)
            if instance.game_key not in ORAL_GAMES:
                raise CommandRejected("invalid_instance", "Not an oral game.")
            unit = DuelUnit.objects.get(pk=unit_id, game_instance=instance)
            state = DuelOralRound.objects.get(unit=unit); state.full_clean()
            own = None
            if actor is not None:
                if not isinstance(actor, ActorContext):
                    raise TypeError("An internal actor context is required.")
                if actor.actor_type == ReceiptActorType.PARTICIPANT:
                    if actor.actor_id not in (instance.participant_a_id, instance.participant_b_id) or not HubParticipant.objects.filter(pk=actor.actor_id, session_id=instance.hub_session_id).exists():
                        raise CommandRejected("actor_not_allowed", "Not a party of this match.")
                    own = actor.actor_id
            now = timezone.now(); material = state.material; passed = elapsed(state, now)
            attempts = list(unit.buzzes.order_by("sequence"))
            for attempt in attempts:
                attempt.full_clean()
                if attempt.captured != capture(instance.game_key, material, attempt.elapsed_us):
                    raise MatchIntegrityError("Buzz context contradicts the frozen material.")
            last = attempts[-1] if attempts else None
            live = state.phase == Phase.ANSWERING and now < state.deadline and instance.match_status == "in_progress"
            data = {"instance_id": str(instance.pk), "unit_id": unit.pk, "phase": state.phase,
                "state_revision": instance.state_revision, "server_now": now.isoformat(),
                "deadline": state.deadline.isoformat() if state.deadline else None,
                "remaining_seconds": max(0, (material["duration_us"]-passed+999_999)//1_000_000),
                "can_buzz": own is not None and live and own not in [a.participant_id for a in attempts],
                "blocked_participant_ids": [a.participant_id for a in attempts],
                "answer_right_participant_id": last.participant_id if last and state.phase == Phase.AWAITING_DECISION else None,
                "last_buzz_participant_id": last.participant_id if last else None}
            if state.phase == Phase.AWAITING_RESULT and instance.game_key not in NO_BUZZ_ZERO:
                data["resolution_reason"] = "no_buzz_policy_unresolved"
            host = actor is not None and actor.actor_type == ReceiptActorType.HOST
            if state.phase != Phase.PREPARED or host:
                data["question"] = material["question"]
                if instance.game_key == "clue_rush":
                    visible=[];cursor=0
                    for clue in material["clues"]:
                        if passed < cursor:break
                        visible.append(clue["text"]);cursor+=clue["duration_us"]
                    data["clues"] = visible
                if instance.game_key == "who":
                    data["person"] = capture("who", material, passed)
                    if state.started_at:
                        data["person_deadline"] = (state.started_at+timedelta(microseconds=(data["person"]["person_index"]+1)*material["person_us"])).isoformat()
                if instance.game_key == "who_that":
                    data["has_image"] = True
                if instance.game_key == "wann_war_das":
                    data.update(tolerance=capture("wann_war_das", material, passed)["tolerance"], unit=material["unit"])
            if state.phase == Phase.AWAITING_DECISION and host:
                data.update(buzz_id=str(last.pk), host_solution=material["solution"])
                if instance.game_key == "wann_war_das":
                    with localcontext() as ctx:
                        ctx.prec=1024
                        target,tol=Decimal(material["solution"]),Decimal(last.captured["tolerance"])
                        data["accepted_range"] = {"min": _text(target-tol), "max": _text(target+tol)}
            if state.phase in {Phase.SHOW_RESULT, Phase.COMPLETED}:
                outcome = DuelUnitOutcome.objects.get(unit=unit)
                points = dict(DuelScoreEntry.objects.filter(unit=unit, game_instance=instance,
                    source_key=f"oral:{unit.uuid}:result").values_list("participant_id", "delta"))
                if set(points) != {instance.participant_a_id, instance.participant_b_id}:
                    raise MatchIntegrityError("Incomplete oral score batch.")
                winner=_recorded_winner(instance,state,attempts)
                expected_outcome = RoundOutcome.NO_WINNER if winner is None else RoundOutcome.PARTY_A_WINS if winner == instance.participant_a_id else RoundOutcome.PARTY_B_WINS
                if (points[instance.participant_a_id],points[instance.participant_b_id],outcome.winner_id) != (
                        int(winner==instance.participant_a_id),int(winner==instance.participant_b_id),winner) or outcome.outcome != expected_outcome:
                    raise MatchIntegrityError("Oral scores/outcome contradict the original attempts and decisions.")
                data.update(solution=material["solution"], points={"a":points[instance.participant_a_id],"b":points[instance.participant_b_id]}, round_outcome=outcome.outcome,
                    match=get_match_snapshot(instance.pk))
                if instance.game_key == "who":
                    data["people"] = material["people"]
                    if last:data["buzz_person"] = {**last.captured,"is_lying":material["people"][last.captured["person_index"]]["is_lying"]}
                if last and instance.game_key != "who":
                    data["verdict"] = DuelDecision.objects.get(unit=unit,subject_type=DecisionSubjectType.BUZZ,subject_key=str(last.pk)).verdict
            return data
    return read() if connection.in_atomic_block else _retry_sqlite_locked(read)
