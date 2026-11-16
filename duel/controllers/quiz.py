"""Quick Quiz duel rules, composed exclusively in the shared command transaction.

Material/clock/buzz are persisted per occurrence. No Multiplayer runtime or
score model is read or written. GET projections neither reconcile nor reveal.
"""
from datetime import timedelta
from math import ceil
from uuid import uuid4

from django.db import connection, transaction
from django.utils import timezone

from QuizGame.models import Quiz
from QuizGame.presentation import question_typewriter_timing
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubParticipant
from ..commands import ActorContext, CommandRejected, HandlerResult, execute_action
from ..contracts import MatchStatus, RoundOutcome, RoundResult
from ..match_service import complete_unit_in_action, get_match_snapshot, MatchIntegrityError
from ..models import (
    DuelQuizQuestionBinding, DuelQuizRoundState, DuelSubmission, DuelScoreEntry,
    DuelUnit, DuelUnitOutcome, DuelDecision, DecisionSubjectType, DecisionVerdict,
    QuizPhase as Phase, UnitStatus, ReceiptActorType, ReceiptStatus,
    SubmissionKind, SubmissionStatus, DuelGameInstance,
)
from ..submissions import commit_submission


def _reject(code):
    return HandlerResult(ReceiptStatus.REJECTED, {"code": code})


def _host(actor):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "Nur der Host darf diese Aktion ausführen.")


def _state(context):
    if not connection.in_atomic_block or context.unit is None:
        raise RuntimeError("Quiz mutations require an existing command and unit.")
    if (context.instance.game_key != "quiz" or context.instance.units.order_by("-number").first().pk != context.unit.pk):
        raise CommandRejected("invalid_unit", "Die Aktion gehört nicht zur aktuellen Quizrunde.")
    state = DuelQuizRoundState.objects.select_for_update().select_related("unit__quiz_question_binding").get(unit=context.unit)
    state.full_clean()
    return state, state.unit.quiz_question_binding


def bind_quiz_question_in_action(context, question_id):
    _host(context.actor)
    if not connection.in_atomic_block or context.unit is None:
        raise RuntimeError("Binding requires the command transaction.")
    step = context.instance.hub_game_step
    if context.instance.game_key != "quiz" or step.game_key != "quiz":
        return _reject("not_quiz")
    if context.unit.status != UnitStatus.PREPARED or context.unit.started_at or context.unit.completed_at:
        return _reject("unit_not_prepared")
    if DuelQuizQuestionBinding.objects.filter(unit=context.unit).exists():
        return _reject("already_bound")
    quiz = Quiz.objects.select_for_update().get(room_code=step.room_code)
    question = quiz.selected_questions.select_for_update().filter(pk=question_id, is_active=True).first()
    if question is None:
        return _reject("question_not_in_quiz")
    kind = question.get_effective_question_type()
    if kind not in ("short_answer", "true_false", "multiple_choice"):
        return _reject("unsupported_question_type")
    if kind == "true_false" and question.correct_answer.strip().lower() not in {"true", "false"}:
        return _reject("invalid_question")
    options = ([{"key": k, "text": k} for k in ("True", "False")] if kind == "true_false"
               else [{"key": k, "text": text} for k, text in question.get_options()] if kind == "multiple_choice" else [])
    correct = (question.get_formatted_correct_answer() if kind == "true_false" else question.correct_answer.strip().upper()) if kind != "short_answer" else ""
    binding = DuelQuizQuestionBinding(unit=context.unit, quiz=quiz, question=question,
        question_text=question.question_text, question_type=kind, options=options,
        solution=question.get_formatted_correct_answer(), correct_option=correct,
        time_limit=question.time_limit,
        reveal_ms=question_typewriter_timing(question.question_text)["question_reveal_ms_per_character"])
    binding.full_clean()
    binding.save(force_insert=True)
    state = DuelQuizRoundState(unit=context.unit)
    state.full_clean()
    state.save(force_insert=True)
    return HandlerResult(ReceiptStatus.APPLIED, {"unit_id": context.unit.pk, "phase": state.phase})


def _change(state, phase, *, closed_at=None):
    state.phase = str(phase)
    state.phase_changed_at = timezone.now()
    if closed_at is not None:
        state.answering_closed_at = closed_at
    state.full_clean()
    state.save()


def start_quiz_round(*, instance_id, unit_id, actor, client_action_id):
    _host(actor)
    def handler(context):
        state, material = _state(context)
        if state.phase != Phase.PREPARED or context.instance.match_status != MatchStatus.IN_PROGRESS:
            return _reject("round_already_started")
        now = timezone.now()
        if context.instance.hub_game_step.intro_ends_at and now < context.instance.hub_game_step.intro_ends_at:
            return _reject("intro_not_finished")
        context.unit.status, context.unit.started_at = UnitStatus.ACTIVE, now
        context.unit.full_clean()
        context.unit.save(update_fields=["status", "started_at", "updated_at"])
        state.started_at, state.deadline = now, now + timedelta(seconds=material.time_limit)
        _change(state, Phase.ANSWERING)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
                          action_type="duel.quiz.start_round", payload={}, handler=handler)


def _answers(instance, state, material):
    answers = {}
    for row in DuelSubmission.objects.filter(unit_id=state.unit_id, submission_kind=SubmissionKind.ANSWER, status=SubmissionStatus.COMMITTED):
        if (row.participant_id not in (instance.participant_a_id, instance.participant_b_id)
                or state.started_at is None or not state.started_at <= row.committed_at < state.deadline
                or type(row.payload) is not dict or set(row.payload) != {"option_id"}
                or row.payload["option_id"] not in [o["key"] for o in material.options]):
            raise MatchIntegrityError("Invalid committed Quick Quiz answer.")
        answers[row.participant_id] = row.payload["option_id"]
    return answers


def commit_quiz_answer(*, instance_id, unit_id, actor, client_action_id, payload):
    def validate(context, data, now):
        state, material = _state(context)
        if material.question_type == "short_answer":
            return "buzz_required"
        if state.phase != Phase.ANSWERING:
            return "answering_not_open"
        if now >= state.deadline:
            return "answer_deadline_passed"
        if set(data) != {"option_id"} or type(data["option_id"]) is not str or data["option_id"] not in [o["key"] for o in material.options]:
            return "invalid_option"
    def committed(context, submission):
        state, material = _state(context)
        if len(_answers(context.instance, state, material)) == 2:
            _change(state, Phase.AWAITING_REVEAL, closed_at=submission.committed_at)
    return commit_submission(instance_id=instance_id, unit_id=unit_id, actor=actor,
        submission_kind=SubmissionKind.ANSWER, client_action_id=client_action_id, payload=payload,
        validate=validate, on_committed=committed)


def buzz(*, instance_id, unit_id, actor, client_action_id):
    if not isinstance(actor, ActorContext) or actor.actor_type != ReceiptActorType.PARTICIPANT:
        raise CommandRejected("participant_required", "Nur eine Duellpartei darf buzzern.")
    def handler(context):
        state, material = _state(context)
        if material.question_type != "short_answer":
            return _reject("not_buzzer_question")
        if state.phase != Phase.ANSWERING:
            return _reject("buzz_already_taken" if state.buzz_id else "answering_not_open")
        now = timezone.now()
        if now >= state.deadline:
            return _reject("answer_deadline_passed")
        state.buzz_id, state.buzz_participant_id, state.buzzed_at = uuid4(), context.participant.pk, now
        state.frozen_characters = min(len(material.question_text), max(0, int((now-state.started_at).total_seconds()*1000)//material.reveal_ms))
        _change(state, Phase.AWAITING_DECISION, closed_at=now)
        return HandlerResult(ReceiptStatus.APPLIED, {"buzz_id": str(state.buzz_id), "participant_id": state.buzz_participant_id})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
                          action_type="duel.quiz.buzz", payload={}, handler=handler)


def evaluate_choices(correct_option, answer_a, answer_b):
    """Independent 1/0 scoring; missing is 0, never an inferred opponent point."""
    a, b = int(answer_a is not None and answer_a == correct_option), int(answer_b is not None and answer_b == correct_option)
    outcome = (RoundOutcome.DRAW if a == b == 1 else RoundOutcome.NO_WINNER if a == b == 0 else
               RoundOutcome.PARTY_A_WINS if a else RoundOutcome.PARTY_B_WINS)
    return a, b, RoundResult(outcome)


def _book(context, points_a, points_b, result):
    if DuelUnitOutcome.objects.filter(unit=context.unit).exists():
        raise MatchIntegrityError("This quiz unit already has a final result.")
    for participant, delta in ((context.instance.participant_a_id, points_a), (context.instance.participant_b_id, points_b)):
        entry = DuelScoreEntry(game_instance=context.instance, unit=context.unit, participant_id=participant,
                              delta=delta, source_key=f"quiz:{context.unit.uuid}:result")
        entry.full_clean()
        entry.save(force_insert=True)
    complete_unit_in_action(context, result)


def judge_quiz_buzz(*, instance_id, unit_id, actor, client_action_id, buzz_id, verdict):
    _host(actor)
    if type(buzz_id) is not str or verdict not in DecisionVerdict.values:
        raise ValueError("A concrete buzz and a valid verdict are required.")
    def handler(context):
        state, material = _state(context)
        if material.question_type != "short_answer" or state.phase != Phase.AWAITING_DECISION or str(state.buzz_id) != buzz_id:
            return _reject("invalid_answer_right")
        decision = DuelDecision(game_instance=context.instance, unit=context.unit, subject_type=DecisionSubjectType.BUZZ,
            subject_key=str(state.buzz_id), verdict=verdict, host_key=context.actor.key)
        decision.full_clean()
        decision.save(force_insert=True)
        a_wins = (state.buzz_participant_id == context.instance.participant_a_id) == (verdict == DecisionVerdict.CORRECT)
        _book(context, int(a_wins), int(not a_wins), RoundResult(RoundOutcome.PARTY_A_WINS if a_wins else RoundOutcome.PARTY_B_WINS))
        _change(state, Phase.SHOW_RESULT)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.quiz.judge", payload={"buzz_id": buzz_id, "verdict": verdict}, handler=handler)


def reconcile_quiz_deadline(*, instance_id, unit_id, actor, client_action_id):
    _host(actor)
    def handler(context):
        state, _ = _state(context)
        if state.phase != Phase.ANSWERING:
            return _reject("phase_not_answering")
        if timezone.now() < state.deadline:
            return _reject("deadline_not_reached")
        _change(state, Phase.AWAITING_REVEAL, closed_at=state.deadline)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
                          action_type="duel.quiz.reconcile_deadline", payload={}, handler=handler)


def advance_quiz_phase(*, instance_id, unit_id, actor, client_action_id, target_phase):
    _host(actor)
    sources = {Phase.SHOW_ANSWERS: Phase.AWAITING_REVEAL, Phase.SHOW_SOLUTION: Phase.SHOW_ANSWERS,
               Phase.SHOW_RESULT: Phase.SHOW_SOLUTION, Phase.COMPLETED: Phase.SHOW_RESULT}
    if target_phase not in sources:
        raise ValueError("Invalid reveal target.")
    def handler(context):
        state, material = _state(context)
        if state.phase != sources[target_phase]:
            return _reject("invalid_phase")
        if target_phase == Phase.SHOW_RESULT:
            if material.question_type == "short_answer":
                # Only the no-buzz deadline path reaches this phase; a buzz
                # requires an explicit host verdict and cannot become 0/0 here.
                if state.buzz_id is not None:
                    raise MatchIntegrityError("An accepted buzz requires a host decision.")
                a, b, result = 0, 0, RoundResult(RoundOutcome.NO_WINNER)
            else:
                answers = _answers(context.instance, state, material)
                a, b, result = evaluate_choices(material.correct_option, answers.get(context.instance.participant_a_id), answers.get(context.instance.participant_b_id))
            _book(context, a, b, result)
        if target_phase == Phase.COMPLETED:
            get_quiz_state(instance_id=instance_id, unit_id=unit_id, actor=actor)
        _change(state, target_phase)
        return HandlerResult(ReceiptStatus.APPLIED, {"phase": state.phase})
    return execute_action(instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.quiz.complete_round" if target_phase == Phase.COMPLETED else f"duel.quiz.{target_phase}",
        payload={"target_phase": str(target_phase)}, handler=handler, _completed_presentation=target_phase == Phase.COMPLETED)


def get_quiz_state(*, instance_id, unit_id, actor=None):
    """Safe, persisted-phase projection, shared by initial GET and polling."""
    def read():
        with transaction.atomic():
            instance = DuelGameInstance.objects.select_for_update().get(pk=instance_id, game_key="quiz")
            unit = DuelUnit.objects.get(pk=unit_id, game_instance=instance)
            state = DuelQuizRoundState.objects.get(unit=unit)
            state.full_clean()
            material = unit.quiz_question_binding
            own = None
            if actor is not None:
                if not isinstance(actor, ActorContext):
                    raise TypeError("Expected an internally confirmed actor.")
                if actor.actor_type == ReceiptActorType.PARTICIPANT:
                    if actor.actor_id not in (instance.participant_a_id, instance.participant_b_id) or not HubParticipant.objects.filter(pk=actor.actor_id, session_id=instance.hub_session_id).exists():
                        raise CommandRejected("actor_not_allowed", "Not a party of this match.")
                    own = actor.actor_id
            now = timezone.now()
            answering = state.phase == Phase.ANSWERING and now < state.deadline and instance.match_status == MatchStatus.IN_PROGRESS
            answers = _answers(instance, state, material)
            data = {"instance_id": str(instance.pk), "unit_id": unit.pk, "phase": state.phase,
                "question_type": material.question_type, "state_revision": instance.state_revision,
                "server_now": now.isoformat(), "started_at": state.started_at.isoformat() if state.started_at else None,
                "deadline": state.deadline.isoformat() if state.deadline else None,
                "remaining_seconds": max(0, ceil((state.deadline-now).total_seconds())) if answering else 0,
                "buzz_participant_id": state.buzz_participant_id,
                "can_buzz": own is not None and answering and material.question_type == "short_answer",
                "can_answer": own is not None and answering and material.question_type != "short_answer" and own not in answers,
                "committed": own in answers if own else False,
                "question": material.question_text, "options": material.options,
                "reveal_ms": material.reveal_ms, "frozen_characters": state.frozen_characters}
            if state.phase == Phase.PREPARED and (actor is None or actor.actor_type != ReceiptActorType.HOST):
                for key in ("question", "options"):
                    data.pop(key)
            if own in answers:
                data["own_answer"] = answers[own]
            if state.phase == Phase.AWAITING_DECISION:
                # Attempt identity belongs to host commands; participants only
                # need the publicly accepted party, never host credentials.
                if actor is not None and actor.actor_type == ReceiptActorType.HOST:
                    data["buzz_id"] = str(state.buzz_id)
                    data["host_solution"] = material.solution
            revealed = (Phase.SHOW_ANSWERS, Phase.SHOW_SOLUTION, Phase.SHOW_RESULT, Phase.COMPLETED)
            if state.phase in revealed and material.question_type != "short_answer":
                labels = {o["key"]: o["text"] for o in material.options}
                data["answers"] = {side: {"value": answers[party], "formatted": labels[answers[party]]} if party in answers else None
                                   for side, party in (("a", instance.participant_a_id), ("b", instance.participant_b_id))}
            if state.phase in (Phase.SHOW_SOLUTION, Phase.SHOW_RESULT, Phase.COMPLETED):
                data["solution"] = material.solution
            if state.phase in (Phase.SHOW_RESULT, Phase.COMPLETED):
                outcome = DuelUnitOutcome.objects.get(unit=unit)
                entries = list(DuelScoreEntry.objects.filter(unit=unit, game_instance=instance,
                    source_key=f"quiz:{unit.uuid}:result").values_list("participant_id", "delta"))
                if len(entries) != 2 or set(p for p, d in entries) != {instance.participant_a_id, instance.participant_b_id}:
                    raise MatchIntegrityError("A quiz result requires the full score batch.")
                points = dict(entries)
                data["points"] = {"a": points[instance.participant_a_id], "b": points[instance.participant_b_id]}
                data["round_outcome"] = outcome.outcome
                decision = DuelDecision.objects.filter(unit=unit, subject_type=DecisionSubjectType.BUZZ, subject_key=str(state.buzz_id)).first() if state.buzz_id else None
                if decision:
                    decision.full_clean()
                    data["verdict"] = decision.verdict
                if material.question_type == "short_answer":
                    if state.buzz_id and decision is None:
                        raise MatchIntegrityError("A buzz result requires its original host decision.")
                    if decision:
                        a_wins = (state.buzz_participant_id == instance.participant_a_id) == (decision.verdict == DecisionVerdict.CORRECT)
                        expected = (int(a_wins), int(not a_wins), RoundOutcome.PARTY_A_WINS if a_wins else RoundOutcome.PARTY_B_WINS)
                    else:
                        expected = (0, 0, RoundOutcome.NO_WINNER)
                else:
                    a, b, result = evaluate_choices(material.correct_option, answers.get(instance.participant_a_id), answers.get(instance.participant_b_id))
                    expected = (a, b, result.outcome)
                if (data["points"]["a"], data["points"]["b"], outcome.outcome) != expected:
                    raise MatchIntegrityError("Quiz result history contradicts its original answers/decision.")
                data["match"] = get_match_snapshot(instance.pk)
            return data
    return read() if connection.in_atomic_block else _retry_sqlite_locked(read)
