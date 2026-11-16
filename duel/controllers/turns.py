"""Host-recorded alternating oral duels. No Multiplayer model writes."""
from django.db import connection, transaction
from django.utils import timezone
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubParticipant
from ..commands import ActorContext, CommandRejected, HandlerResult, execute_action
from ..contracts import RoundOutcome, RoundResult
from ..match_service import complete_unit_in_action, get_match_snapshot, MatchIntegrityError
from ..models import (DuelGameInstance, DuelUnit, DuelTurnRound, TurnPhase as Phase,
    DuelDecision, DecisionSubjectType, DecisionVerdict, DuelScoreEntry, DuelUnitOutcome,
    ReceiptActorType, ReceiptStatus)
from ..turn_material import TURN_GAMES, freeze_material, board, check_selection, exhausted


def _reject(code):
    return HandlerResult(ReceiptStatus.REJECTED, {"code": code})


def _host(actor):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "Nur der Host darf mündliche Antworten erfassen.")


def _opponent(instance, party):
    return instance.participant_b_id if party == instance.participant_a_id else instance.participant_a_id


def current_party(instance, state):
    return _opponent(instance, state.starting_participant_id) if len(state.actions) % 2 else state.starting_participant_id


def _state(context):
    if not connection.in_atomic_block or context.unit is None:
        raise RuntimeError("The existing command transaction and unit are required.")
    if context.instance.game_key not in TURN_GAMES or context.instance.units.latest("number").pk != context.unit.pk:
        raise CommandRejected("stale_unit", "Die Aktion gehört nicht zur aktuellen Runde.")
    state = DuelTurnRound.objects.select_for_update().get(unit=context.unit)
    state.full_clean()
    _validate_history(context.instance, state)
    return state


def bind_turn_question_in_action(context, question_id):
    _host(context.actor)
    if not connection.in_atomic_block or context.unit is None:
        raise RuntimeError("Binding requires the existing command transaction.")
    key, step = context.instance.game_key, context.instance.hub_game_step
    if key not in TURN_GAMES or step.game_key != key or step.session_id != context.instance.hub_session_id:
        return _reject("invalid_unit")
    if context.unit.status != "prepared" or DuelTurnRound.objects.filter(unit=context.unit).exists():
        return _reject("unit_not_prepared")
    game = TURN_GAMES[key][0].objects.select_for_update().get(room_code=step.room_code)
    question = game.selected_questions.select_for_update().filter(pk=question_id, is_active=True).first()
    if question is None:
        return _reject("question_not_in_quiz")
    try:
        material = freeze_material(key, game, question)
    except (ValueError, TypeError):
        return _reject("invalid_question")
    state = DuelTurnRound(unit=context.unit, source_question_id=question.pk, material=material)
    state.full_clean(); state.save(force_insert=True)
    return HandlerResult(ReceiptStatus.APPLIED, {"unit_id": context.unit.pk, "phase": state.phase})


def _save(state, phase):
    state.phase, state.phase_changed_at = str(phase), timezone.now()
    state.full_clean(); state.save()


def start_round(*, instance_id, unit_id, actor, client_action_id, starting_participant_id):
    _host(actor)
    if type(starting_participant_id) is not int:
        raise ValueError("Choose the starting party explicitly.")
    def handler(context):
        state = _state(context)
        if state.phase != Phase.PREPARED:
            return _reject("invalid_phase")
        if starting_participant_id not in (context.instance.participant_a_id, context.instance.participant_b_id):
            return _reject("invalid_starting_party")
        now = timezone.now()
        if context.instance.hub_game_step.intro_ends_at and now < context.instance.hub_game_step.intro_ends_at:
            return _reject("intro_not_finished")
        context.unit.status, context.unit.started_at = "active", now
        context.unit.full_clean(); context.unit.save(update_fields=["status", "started_at", "updated_at"])
        state.started_at, state.starting_participant_id = now, starting_participant_id
        _save(state, Phase.PLAYING)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.turn.start", payload={"starting_participant_id": starting_participant_id}, handler=handler)


def record_turn(*, instance_id, unit_id, actor, client_action_id, expected_turn, selection):
    _host(actor)
    if type(expected_turn) is not int or expected_turn <= 0 or type(selection) is not dict:
        raise ValueError("A concrete turn and selection are required.")
    def handler(context):
        state = _state(context)
        if state.phase != Phase.PLAYING:
            return _reject("invalid_phase")
        if expected_turn != len(state.actions)+1:
            return _reject("stale_turn")
        try:
            correct = check_selection(context.instance.game_key, state.material, state.actions, selection)
        except ValueError:
            return _reject("invalid_selection")
        decision = DuelDecision(game_instance=context.instance, unit=context.unit,
            subject_type=DecisionSubjectType.TURN, subject_key=f"{state.pk}:{expected_turn}",
            verdict=DecisionVerdict.CORRECT if correct else DecisionVerdict.INCORRECT, host_key=actor.key)
        decision.full_clean(); decision.save(force_insert=True)
        state.actions.append({"turn": expected_turn, "party_id": current_party(context.instance, state),
            "selection": selection, "correct": correct})
        phase = Phase.AWAITING_RESULT if not correct or exhausted(context.instance.game_key, state.material, state.actions) else Phase.PLAYING
        _save(state, phase)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase, "turn": expected_turn})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.turn.record", payload={"expected_turn": expected_turn, "selection": selection}, handler=handler)


def _validate_history(instance, state):
    prefix = []
    for index, action in enumerate(state.actions, 1):
        party = state.starting_participant_id if index % 2 else _opponent(instance, state.starting_participant_id)
        if (type(action) is not dict or set(action) != {"turn", "party_id", "selection", "correct"}
                or action["turn"] != index or action["party_id"] != party or type(action["correct"]) is not bool
                or (prefix and (not prefix[-1]["correct"] or exhausted(instance.game_key, state.material, prefix)))):
            raise MatchIntegrityError("Invalid alternating turn history.")
        try:
            correct = check_selection(instance.game_key, state.material, prefix, action["selection"])
        except ValueError as exc:
            raise MatchIntegrityError("Invalid saved turn selection.") from exc
        if correct != action["correct"] or not DuelDecision.objects.filter(unit=state.unit,
                game_instance=instance, subject_type=DecisionSubjectType.TURN, subject_key=f"{state.pk}:{index}",
                verdict="correct" if correct else "incorrect").exists():
            raise MatchIntegrityError("Turn history contradicts its decision.")
        prefix.append(action)
    if state.phase in {Phase.AWAITING_RESULT, Phase.SHOW_RESULT, Phase.COMPLETED}:
        if not prefix or (prefix[-1]["correct"] and not exhausted(instance.game_key, state.material, prefix)):
            raise MatchIntegrityError("A normal result requires a concrete incorrect turn.")
    elif state.phase in {Phase.NEEDS_RESOLUTION, Phase.RESOLUTION_REVEALED}:
        if not prefix or not all(a["correct"] for a in prefix) or not exhausted(instance.game_key, state.material, prefix):
            raise MatchIntegrityError("Only a fully solved task can require this resolution.")
    elif state.phase == Phase.PLAYING and prefix and (not prefix[-1]["correct"] or exhausted(instance.game_key, state.material, prefix)):
        raise MatchIntegrityError("An ended task cannot remain playable.")


def advance(*, instance_id, unit_id, actor, client_action_id, action):
    _host(actor)
    if action not in {"show_result", "complete"}:
        raise ValueError("Invalid turn release.")
    def handler(context):
        state = _state(context)
        if action == "show_result":
            if state.phase in {Phase.AWAITING_RESULT, Phase.NEEDS_RESOLUTION, Phase.RESOLUTION_REVEALED}:
                _validate_history(context.instance, state)
                winner = None if state.actions[-1]['correct'] else _opponent(context.instance, state.actions[-1]["party_id"])
                for party in (context.instance.participant_a_id, context.instance.participant_b_id):
                    score = DuelScoreEntry(game_instance=context.instance, unit=context.unit, participant_id=party,
                        delta=int(party == winner), source_key=f"turn:{state.pk}:result")
                    score.full_clean(); score.save(force_insert=True)
                outcome = RoundOutcome.NO_WINNER if winner is None else RoundOutcome.PARTY_A_WINS if winner == context.instance.participant_a_id else RoundOutcome.PARTY_B_WINS
                complete_unit_in_action(context, RoundResult(outcome), reason='task_fully_solved' if winner is None else 'opponent_error')
                _save(state, Phase.SHOW_RESULT)
            else:
                return _reject("invalid_phase")
        else:
            if state.phase in {Phase.NEEDS_RESOLUTION, Phase.RESOLUTION_REVEALED}:
                return _reject("solved_task_policy_unresolved")
            if state.phase != Phase.SHOW_RESULT:
                return _reject("invalid_phase")
            get_state(instance_id=instance_id, unit_id=unit_id, actor=actor)
            _save(state, Phase.COMPLETED)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.turn.complete_round" if action == "complete" else "duel.turn.show_result",
        payload={"action": action}, handler=handler, _completed_presentation=action == "complete")


def get_state(*, instance_id, unit_id, actor=None):
    def read():
        with transaction.atomic():
            instance = DuelGameInstance.objects.select_for_update().get(pk=instance_id)
            if instance.game_key not in TURN_GAMES:
                raise CommandRejected("invalid_instance", "Not an alternating game.")
            unit = DuelUnit.objects.get(pk=unit_id, game_instance=instance)
            state = DuelTurnRound.objects.get(unit=unit); state.full_clean(); _validate_history(instance, state)
            if actor is not None:
                if not isinstance(actor, ActorContext):
                    raise TypeError("An internal actor context is required.")
                if actor.actor_type == ReceiptActorType.PARTICIPANT and (actor.actor_id not in (instance.participant_a_id, instance.participant_b_id)
                        or not HubParticipant.objects.filter(pk=actor.actor_id, session_id=instance.hub_session_id).exists()):
                    raise CommandRejected("actor_not_allowed", "Not a duel party.")
            host = actor is not None and actor.actor_type == ReceiptActorType.HOST
            phase, material = state.phase, state.material
            revealed = phase in {Phase.SHOW_RESULT, Phase.COMPLETED, Phase.RESOLUTION_REVEALED}
            data = {"instance_id": str(instance.pk), "unit_id": unit.pk, "phase": phase,
                "state_revision": instance.state_revision, "turn": len(state.actions)+1,
                "current_participant_id": current_party(instance, state) if phase == Phase.PLAYING else None}
            if phase != Phase.PREPARED or host:
                data["question"] = material["question"]
                progress = board(instance.game_key, material, state.actions)
                if instance.game_key == "assign":
                    data.update(left=material["left"], right=material["right"], confirmed=progress)
                    if revealed: data["solution"] = material["solution"]
                elif instance.game_key == "sorting_ladder":
                    data.update(items=material["items"], confirmed=progress, description=material["description"],
                        upper_label=material["upper_label"], lower_label=material["lower_label"])
                    if revealed: data["solution"] = material["solution"]
                else:
                    data["cards"] = [{"id": a["id"], "text": a["text"] if a["id"] in progress or host or revealed else None,
                        "revealed": a["id"] in progress} for a in material["answers"]]
            if phase in {Phase.NEEDS_RESOLUTION, Phase.RESOLUTION_REVEALED}:
                data["resolution_reason"] = "solved_task_policy_unresolved"
            if phase in {Phase.SHOW_RESULT, Phase.COMPLETED}:
                winner = None if state.actions[-1]['correct'] else _opponent(instance, state.actions[-1]["party_id"])
                outcome = DuelUnitOutcome.objects.get(unit=unit)
                points = dict(DuelScoreEntry.objects.filter(game_instance=instance, unit=unit,
                    source_key=f"turn:{state.pk}:result").values_list("participant_id", "delta"))
                expected = {instance.participant_a_id: int(winner == instance.participant_a_id), instance.participant_b_id: int(winner == instance.participant_b_id)}
                expected_outcome = 'no_winner' if winner is None else "party_a_wins" if winner == instance.participant_a_id else "party_b_wins"
                if points != expected or outcome.winner_id != winner or outcome.outcome != expected_outcome:
                    raise MatchIntegrityError("Turn result contradicts the authoritative error.")
                data.update(points={"a": points[instance.participant_a_id], "b": points[instance.participant_b_id]},
                    round_outcome=outcome.outcome, result_reason='task_fully_solved' if winner is None else 'opponent_error', match=get_match_snapshot(instance.pk))
            return data
    return read() if connection.in_atomic_block else _retry_sqlite_locked(read)
