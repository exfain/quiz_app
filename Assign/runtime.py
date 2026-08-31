from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    AssignAnswer,
    AssignParticipant,
    AssignQuiz,
    AssignRoundParticipantState,
    AssignSession,
    AssignSetRuntime,
)


ASSIGN_REVEAL_STAGGER_MS = 120
ASSIGN_REVEAL_ANIMATION_MS = 160


@dataclass(frozen=True)
class RoundActionResult:
    accepted: bool
    code: str
    state: AssignRoundParticipantState | None = None
    all_locked: bool = False


def _session_code(value) -> str:
    return str(value or '').strip()


def _participant_queryset(quiz, hub_session_code):
    participants = quiz.participants.filter(is_active=True)
    if hub_session_code:
        participants = participants.filter(hub_session_code=hub_session_code)
    return participants


def _eligible_participant_ids(quiz, hub_session_code, set_number):
    return list(
        _participant_queryset(quiz, hub_session_code)
        .exclude(eliminated_set_number=set_number)
        .order_by('id')
        .values_list('id', flat=True)
    )


def _materialize_round(runtime, round_index):
    participant_ids = _eligible_participant_ids(
        runtime.quiz,
        runtime.hub_session_code,
        runtime.set_number,
    )
    participant_map = dict(runtime.round_participant_ids or {})
    participant_map[str(round_index)] = participant_ids
    runtime.round_participant_ids = participant_map
    AssignRoundParticipantState.objects.bulk_create(
        [
            AssignRoundParticipantState(
                set_runtime=runtime,
                participant_id=participant_id,
                round_index=round_index,
            )
            for participant_id in participant_ids
        ],
        ignore_conflicts=True,
    )
    return participant_ids


@transaction.atomic
def start_set_runtime(
    *,
    quiz_id,
    question_id,
    hub_session_code,
    effective_time_limit,
    start_answering=True,
):
    quiz = AssignQuiz.objects.select_for_update().get(pk=quiz_id)
    session, _ = AssignSession.objects.select_for_update().get_or_create(quiz=quiz)
    normalized_session = _session_code(hub_session_code)
    if session.is_question_active:
        runtime = (
            AssignSetRuntime.objects
            .filter(
                quiz=quiz,
                hub_session_code=normalized_session,
                set_number=session.current_question_number,
            )
            .first()
        )
        if quiz.current_question_id != question_id:
            return runtime, False
        if runtime:
            return runtime, False
        now = timezone.now()
        duration = max(1, int(effective_time_limit))
        started_at = quiz.question_start_time or now
        ends_at = session.question_end_time or (
            started_at + timezone.timedelta(seconds=duration)
        )
        runtime = AssignSetRuntime.objects.create(
            quiz=quiz,
            question_id=question_id,
            hub_session_code=normalized_session,
            set_number=session.current_question_number,
            effective_time_limit=duration,
            current_round_index=session.current_round_index,
            round_started_at=started_at,
            round_ends_at=ends_at,
        )
        _materialize_round(runtime, session.current_round_index)
        runtime.save(update_fields=['round_participant_ids', 'updated_at'])
        return runtime, False

    now = timezone.now()
    duration = max(1, int(effective_time_limit))
    quiz.current_question_id = question_id
    quiz.question_start_time = now
    quiz.save(update_fields=['current_question', 'question_start_time', 'updated_at'])

    session.current_question_number += 1
    session.total_questions_sent += 1
    session.current_round_index = 0
    session.is_question_active = True
    session.question_end_time = (
        now + timezone.timedelta(seconds=duration)
        if start_answering
        else None
    )
    session.save(update_fields=[
        'current_question_number',
        'total_questions_sent',
        'current_round_index',
        'is_question_active',
        'question_end_time',
        'updated_at',
    ])

    _participant_queryset(quiz, normalized_session).update(
        eliminated_set_number=None,
        elimination_reason='',
    )
    runtime = AssignSetRuntime.objects.create(
        quiz=quiz,
        question_id=question_id,
        hub_session_code=normalized_session,
        set_number=session.current_question_number,
        effective_time_limit=duration,
        current_round_index=0,
        round_started_at=now,
        round_ends_at=session.question_end_time or now,
    )
    _materialize_round(runtime, 0)
    runtime.save(update_fields=['round_participant_ids', 'updated_at'])
    return runtime, True


def current_set_runtime(room_code, hub_session_code=None, *, lock=False):
    queryset = AssignSetRuntime.objects.select_related('quiz', 'question')
    if lock:
        queryset = queryset.select_for_update()
    queryset = queryset.filter(
        quiz__room_code=room_code,
        set_number=models_current_set_number(room_code),
    )
    normalized_session = _session_code(hub_session_code)
    if normalized_session:
        queryset = queryset.filter(hub_session_code=normalized_session)
    return queryset.order_by('-id').first()


def models_current_set_number(room_code):
    return (
        AssignSession.objects
        .filter(quiz__room_code=room_code)
        .values_list('current_question_number', flat=True)
        .first()
        or 0
    )


def _current_context(room_code, hub_session_code, *, lock=False):
    quiz_qs = AssignQuiz.objects.select_related('current_question')
    session_qs = AssignSession.objects
    if lock:
        quiz_qs = quiz_qs.select_for_update()
        session_qs = session_qs.select_for_update()
    quiz = quiz_qs.get(room_code=room_code)
    session = session_qs.get(quiz=quiz)
    runtime_qs = AssignSetRuntime.objects.select_related('question')
    if lock:
        runtime_qs = runtime_qs.select_for_update()
    runtime = runtime_qs.filter(
        quiz=quiz,
        set_number=session.current_question_number,
        hub_session_code=_session_code(hub_session_code),
    ).first()
    return quiz, session, runtime


@transaction.atomic
def store_round_selection(
    *,
    room_code,
    participant_name,
    hub_session_code,
    question_id,
    round_index,
    left_item_index,
    user_match,
    lock_selection,
):
    try:
        quiz, session, runtime = _current_context(room_code, hub_session_code, lock=True)
    except (AssignQuiz.DoesNotExist, AssignSession.DoesNotExist):
        return RoundActionResult(False, 'invalid_phase')
    if (
        quiz.status != 'active'
        or not runtime
        or runtime.phase != AssignSetRuntime.PHASE_ACTIVE
        or not session.is_question_active
    ):
        return RoundActionResult(False, 'invalid_phase')
    if str(quiz.current_question_id) != str(question_id):
        return RoundActionResult(False, 'stale_action')
    try:
        normalized_round = int(round_index)
    except (TypeError, ValueError):
        return RoundActionResult(False, 'stale_action')
    if normalized_round != session.current_round_index or normalized_round != runtime.current_round_index:
        return RoundActionResult(False, 'stale_action')
    from games_hub.authoritative_state import current_snapshot
    from games_hub.models import GameRuntimeState

    question_state = current_snapshot('assign', room_code, hub_session_code)
    if (
        question_state.get('question_flow_mode')
        == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        and question_state.get('question_phase')
        != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
    ):
        return RoundActionResult(False, 'invalid_phase')
    now = timezone.now()
    if now < runtime.round_started_at:
        return RoundActionResult(False, 'invalid_phase')
    if now >= runtime.round_ends_at:
        return RoundActionResult(False, 'deadline_expired')

    try:
        participant = AssignParticipant.objects.select_for_update().get(
            quiz=quiz,
            name=participant_name,
            hub_session_code=hub_session_code,
        )
    except AssignParticipant.DoesNotExist:
        return RoundActionResult(False, 'invalid_participant')
    if participant.eliminated_set_number == runtime.set_number:
        return RoundActionResult(False, 'invalid_phase')

    expected_ids = set((runtime.round_participant_ids or {}).get(str(normalized_round), []))
    if participant.id not in expected_ids:
        return RoundActionResult(False, 'invalid_phase')
    state, _ = AssignRoundParticipantState.objects.select_for_update().get_or_create(
        set_runtime=runtime,
        participant=participant,
        round_index=normalized_round,
    )
    if state.is_locked:
        return RoundActionResult(False, 'already_submitted', state)

    state.left_item_index = left_item_index
    state.user_match = user_match if isinstance(user_match, dict) else {}
    if lock_selection:
        state.is_locked = True
        state.locked_at = timezone.now()
    state.save(update_fields=[
        'left_item_index',
        'user_match',
        'is_locked',
        'locked_at',
        'updated_at',
    ])
    expected_count = len(expected_ids)
    locked_count = AssignRoundParticipantState.objects.filter(
        set_runtime=runtime,
        round_index=normalized_round,
        participant_id__in=expected_ids,
        is_locked=True,
    ).count()
    return RoundActionResult(
        True,
        'accepted',
        state,
        all_locked=bool(expected_count and locked_count == expected_count),
    )


def round_status(room_code, hub_session_code, round_index):
    runtime = current_set_runtime(room_code, hub_session_code)
    if not runtime:
        return []
    expected_ids = (runtime.round_participant_ids or {}).get(str(round_index), [])
    states = {
        state.participant_id: state
        for state in AssignRoundParticipantState.objects.filter(
            set_runtime=runtime,
            round_index=round_index,
            participant_id__in=expected_ids,
        ).select_related('participant')
    }
    participants = AssignParticipant.objects.filter(id__in=expected_ids).order_by('name')
    return [
        {
            'participant_id': participant.id,
            'participant_name': participant.name,
            'logged': bool(states.get(participant.id) and states[participant.id].is_locked),
        }
        for participant in participants
    ]


def _check_match(question, room_code, left_item_index, user_match):
    randomized = question.get_randomized_items(room_code=room_code)
    correct_original_idx = question.correct_matches.get(str(left_item_index))
    if correct_original_idx is None:
        return False, None
    shuffled_right_pos = (user_match or {}).get(str(left_item_index))
    if shuffled_right_pos is None:
        shuffled_right_pos = (user_match or {}).get(left_item_index)
    if shuffled_right_pos is None:
        return False, None
    try:
        original_right_idx = randomized['position_to_original'].get(int(shuffled_right_pos))
    except (TypeError, ValueError):
        return False, None
    if original_right_idx is None:
        return False, None
    return int(original_right_idx) == int(correct_original_idx), int(original_right_idx)


@transaction.atomic
def evaluate_and_advance(
    *,
    room_code,
    hub_session_code,
    expected_round,
    set_number,
    prepare_next_round=False,
):
    try:
        quiz, session, runtime = _current_context(room_code, hub_session_code, lock=True)
    except (AssignQuiz.DoesNotExist, AssignSession.DoesNotExist):
        return {'advanced': False, 'code': 'invalid_phase'}
    if (
        not runtime
        or not quiz.current_question_id
        or runtime.question_id != quiz.current_question_id
        or runtime.set_number != int(set_number)
        or runtime.current_round_index != int(expected_round)
        or session.current_round_index != int(expected_round)
        or runtime.phase != AssignSetRuntime.PHASE_ACTIVE
    ):
        return {'advanced': False, 'code': 'stale_action'}

    normalized_round = int(expected_round)
    evaluated_rounds = [int(value) for value in (runtime.evaluated_rounds or [])]
    if normalized_round in evaluated_rounds:
        return {'advanced': False, 'code': 'already_evaluated'}

    expected_ids = (runtime.round_participant_ids or {}).get(str(normalized_round), [])
    states = {
        state.participant_id: state
        for state in AssignRoundParticipantState.objects.select_for_update().filter(
            set_runtime=runtime,
            round_index=normalized_round,
            participant_id__in=expected_ids,
        ).select_related('participant')
    }
    participants = {
        participant.id: participant
        for participant in AssignParticipant.objects.select_for_update().filter(id__in=expected_ids)
    }
    solved_matches = {
        str(left_idx): int(right_idx)
        for left_idx, right_idx in (runtime.solved_matches or {}).items()
    }
    outcomes = []
    now = timezone.now()
    for participant_id in expected_ids:
        participant = participants.get(participant_id)
        if not participant:
            continue
        state = states.get(participant_id)
        if not state:
            state = AssignRoundParticipantState.objects.create(
                set_runtime=runtime,
                participant=participant,
                round_index=normalized_round,
            )
        left_item_index = (
            state.left_item_index
            if state.left_item_index is not None
            else normalized_round
        )
        user_match = state.user_match or {}
        is_correct, original_right_idx = _check_match(
            runtime.question,
            room_code,
            left_item_index,
            user_match,
        )
        elimination_reason = '' if is_correct else (
            'incorrect_assignment' if user_match else 'no_assignment'
        )
        state.evaluated_at = now
        state.is_correct = is_correct
        state.original_right_index = original_right_idx
        state.elimination_reason = elimination_reason
        state.save(update_fields=[
            'evaluated_at',
            'is_correct',
            'original_right_index',
            'elimination_reason',
            'updated_at',
        ])
        if is_correct and original_right_idx is not None:
            solved_matches[str(left_item_index)] = int(original_right_idx)
        elif participant.eliminated_set_number != runtime.set_number:
            participant.eliminated_set_number = runtime.set_number
            participant.elimination_reason = elimination_reason
            participant.save(update_fields=[
                'eliminated_set_number',
                'elimination_reason',
                'updated_at',
            ])
        outcomes.append({
            'participant_id': participant.id,
            'participant_name': participant.name,
            'hub_session_code': participant.hub_session_code or '',
            'logged': state.is_locked,
            'is_correct': is_correct,
            'elimination_reason': elimination_reason or None,
        })

    evaluated_rounds.append(normalized_round)
    runtime.evaluated_rounds = sorted(set(evaluated_rounds))
    runtime.solved_matches = solved_matches
    total_rounds = len(runtime.question.correct_matches or {})
    next_round = normalized_round + 1
    completed = next_round >= total_rounds
    if completed:
        runtime.phase = AssignSetRuntime.PHASE_WAITING_REVEAL
        session.is_question_active = False
        session.question_end_time = None
        session.current_round_index = normalized_round
    else:
        runtime.current_round_index = next_round
        session.current_round_index = next_round
        runtime.round_started_at = now
        runtime.round_ends_at = (
            now
            if prepare_next_round
            else now + timezone.timedelta(seconds=runtime.effective_time_limit)
        )
        session.question_end_time = (
            None if prepare_next_round else runtime.round_ends_at
        )
        _materialize_round(runtime, next_round)
    runtime.save()
    session.save(update_fields=[
        'current_round_index',
        'is_question_active',
        'question_end_time',
        'updated_at',
    ])
    return {
        'advanced': True,
        'completed': completed,
        'next_round': None if completed else next_round,
        'set_number': runtime.set_number,
        'question_id': runtime.question_id,
        'effective_time_limit': runtime.effective_time_limit,
        'round_started_at': runtime.round_started_at,
        'round_ends_at': runtime.round_ends_at,
        'solved_matches': dict(runtime.solved_matches or {}),
        'outcomes': outcomes,
    }


def assign_reveal_counts(runtime):
    if not runtime or not runtime.question:
        return 0, 0
    solved_left = {
        int(value)
        for value in (runtime.solved_matches or {}).keys()
    }
    solved_right = {
        int(value)
        for value in (runtime.solved_matches or {}).values()
    }
    target_count = sum(
        1
        for index, _ in enumerate(runtime.question.right_items or [])
        if index not in solved_right
    )
    element_count = sum(
        1
        for index, _ in enumerate(runtime.question.left_items or [])
        if index not in solved_left
    )
    return target_count, element_count


def assign_reveal_duration_ms(runtime):
    target_count, element_count = assign_reveal_counts(runtime)
    item_count = target_count + element_count
    if item_count <= 0:
        return 0
    return (
        (item_count - 1) * ASSIGN_REVEAL_STAGGER_MS
        + ASSIGN_REVEAL_ANIMATION_MS
    )


def assign_reveal_ready_at(runtime, content_revealed_at):
    if not content_revealed_at:
        return None
    return content_revealed_at + timedelta(
        milliseconds=assign_reveal_duration_ms(runtime)
    )


@transaction.atomic
def open_round_answering(
    *,
    room_code,
    hub_session_code,
    expected_round,
    set_number,
    started_at,
    ends_at,
):
    try:
        quiz, session, runtime = _current_context(
            room_code,
            hub_session_code,
            lock=True,
        )
    except (AssignQuiz.DoesNotExist, AssignSession.DoesNotExist):
        return None
    if (
        not runtime
        or runtime.phase != AssignSetRuntime.PHASE_ACTIVE
        or runtime.set_number != int(set_number)
        or runtime.current_round_index != int(expected_round)
        or session.current_round_index != int(expected_round)
        or runtime.question_id != quiz.current_question_id
    ):
        return None
    if session.question_end_time:
        return runtime
    runtime.round_started_at = started_at
    runtime.round_ends_at = ends_at
    session.question_end_time = ends_at
    runtime.save(update_fields=['round_started_at', 'round_ends_at', 'updated_at'])
    session.save(update_fields=['question_end_time', 'updated_at'])
    return runtime


@transaction.atomic
def mark_revealed(room_code, hub_session_code):
    runtime = current_set_runtime(room_code, hub_session_code, lock=True)
    if not runtime:
        return None
    if runtime.phase == AssignSetRuntime.PHASE_REVEALED:
        return runtime
    if runtime.phase != AssignSetRuntime.PHASE_WAITING_REVEAL:
        return None
    runtime.phase = AssignSetRuntime.PHASE_REVEALED
    runtime.revealed_at = timezone.now()
    runtime.save(update_fields=['phase', 'revealed_at', 'updated_at'])
    return runtime


@transaction.atomic
def mark_ended(room_code, hub_session_code):
    runtime = current_set_runtime(room_code, hub_session_code, lock=True)
    if not runtime:
        return None
    if runtime.phase != AssignSetRuntime.PHASE_ENDED:
        runtime.phase = AssignSetRuntime.PHASE_ENDED
        runtime.ended_at = timezone.now()
        runtime.save(update_fields=['phase', 'ended_at', 'updated_at'])
    return runtime


def participant_round_snapshot(runtime, participant):
    if not runtime or not participant:
        return None
    return (
        runtime.participant_round_states
        .filter(
            participant=participant,
            round_index=runtime.current_round_index,
        )
        .first()
    )


@transaction.atomic
def persist_final_answers(runtime):
    if not runtime:
        return []
    runtime = AssignSetRuntime.objects.select_for_update().select_related(
        'quiz',
        'question',
    ).get(pk=runtime.pk)
    participant_ids = sorted({
        participant_id
        for ids in (runtime.round_participant_ids or {}).values()
        for participant_id in ids
    })
    participants = AssignParticipant.objects.select_for_update().filter(id__in=participant_ids)
    states = list(AssignRoundParticipantState.objects.filter(
        set_runtime=runtime,
        participant_id__in=participant_ids,
    ).order_by('round_index'))
    matches_by_participant = {}
    completed_at_by_participant = {}
    for state in states:
        completed_at = state.locked_at or state.evaluated_at
        if completed_at:
            previous = completed_at_by_participant.get(state.participant_id)
            if previous is None or completed_at > previous:
                completed_at_by_participant[state.participant_id] = completed_at
        if (
            state.is_correct
            and state.left_item_index is not None
            and state.original_right_index is not None
        ):
            matches_by_participant.setdefault(state.participant_id, {})[
                str(state.left_item_index)
            ] = state.original_right_index

    answers = []
    for participant in participants:
        completed_at = completed_at_by_participant.get(
            participant.id,
            min(timezone.now(), runtime.round_ends_at),
        )
        answer, _ = AssignAnswer.objects.get_or_create(
            quiz=runtime.quiz,
            participant=participant,
            question=runtime.question,
            defaults={
                'user_matches': matches_by_participant.get(participant.id, {}),
                'time_taken': max(
                    0,
                    (completed_at - runtime.created_at).total_seconds(),
                ),
            },
        )
        answers.append(answer)
    return answers
