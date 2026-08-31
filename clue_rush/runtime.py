from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    ClueAnswer,
    CluePendingInput,
    ClueQuestion,
    ClueRushGame,
    ClueRushSession,
)
from games_hub.models import HubGameStep
from games_hub.unit_tutorial_runtime import (
    finish_current_unit_tutorial,
    is_unit_tutorial_question,
)


def _timestamp(value):
    parsed = value if hasattr(value, 'tzinfo') else parse_datetime(str(value or ''))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _serialize_schedule_entry(clue, order, starts_at, ends_at, total_duration, has_next):
    return {
        'id': clue.id,
        'question_id': clue.clue_question_id,
        'order': order,
        'clue_text': clue.clue_text,
        'duration': max(1, int((ends_at - starts_at).total_seconds())),
        'starts_at': starts_at.isoformat(),
        'end_time': ends_at.isoformat(),
        'sequence_end_time': None,
        'sequence_duration': total_duration,
        'has_next_clue': has_next,
    }


def _resolve_question(quiz, question_id):
    question = quiz.selected_questions.filter(pk=question_id).first()
    if question is None:
        question = quiz.tutorial_question if quiz.tutorial_question_id == question_id else None
    if question is None:
        question = ClueQuestion.objects.get(pk=question_id)
    return question


def _build_schedule(question, *, starts_at, duration_override=None):
    clues = list(question.clues.order_by('order', 'id'))
    cursor = starts_at
    raw_schedule = []
    durations = [
        max(1, int(duration_override or clue.duration))
        for clue in clues
    ]
    total_duration = sum(durations)
    for index, (clue, duration) in enumerate(zip(clues, durations), start=1):
        ends_at = cursor + timezone.timedelta(seconds=duration)
        raw_schedule.append(
            _serialize_schedule_entry(
                clue,
                index,
                cursor,
                ends_at,
                total_duration,
                index < len(clues),
            )
        )
        cursor = ends_at
    for entry in raw_schedule:
        entry['sequence_end_time'] = cursor.isoformat()
    return raw_schedule, cursor, total_duration


@transaction.atomic
def prepare_question_schedule(*, quiz_id, question_id, duration_override=None):
    quiz = ClueRushGame.objects.select_for_update().get(pk=quiz_id)
    session, _ = ClueRushSession.objects.select_for_update().get_or_create(quiz=quiz)
    if (
        quiz.current_question_id == question_id
        and not session.is_question_active
        and not session.clue_schedule
        and session.answer_deadline is None
        and session.question_end_time is None
    ):
        question = _resolve_question(quiz, question_id)
        _, _, total_duration = _build_schedule(
            question,
            starts_at=timezone.now(),
            duration_override=duration_override,
        )
        return {
            'prepared': True,
            'duplicate': True,
            'answer_duration_seconds': total_duration,
        }
    if session.is_question_active or quiz.current_question_id:
        return {
            'prepared': False,
            'code': (
                'already_prepared'
                if quiz.current_question_id == question_id and not session.is_question_active
                else 'invalid_phase'
            ),
        }

    question = _resolve_question(quiz, question_id)
    _, _, total_duration = _build_schedule(
        question,
        starts_at=timezone.now(),
        duration_override=duration_override,
    )
    quiz.current_question = question
    quiz.question_start_time = None
    quiz.current_clue = None
    quiz.clue_start_time = None
    quiz.save(update_fields=[
        'current_question',
        'question_start_time',
        'current_clue',
        'clue_start_time',
        'updated_at',
    ])
    session.current_question_number += 1
    session.total_questions_sent += 1
    session.is_question_active = False
    session.current_clue_number = 0
    session.is_clue_active = False
    session.clue_end_time = None
    session.question_end_time = None
    session.answer_deadline = None
    session.clue_duration_override = duration_override
    session.clue_schedule = []
    session.question_finalized_at = None
    session.finalized_question_id = None
    session.total_responses_current_question = 0
    session.correct_responses_current_question = 0
    session.save()
    CluePendingInput.objects.filter(quiz=quiz).delete()
    return {
        'prepared': True,
        'answer_duration_seconds': total_duration,
    }


@transaction.atomic
def start_prepared_question_schedule(*, quiz_id, question_id, starts_at):
    quiz = ClueRushGame.objects.select_for_update().get(pk=quiz_id)
    session = ClueRushSession.objects.select_for_update().get(quiz=quiz)
    if quiz.current_question_id != question_id:
        return {'started': False, 'code': 'stale_action'}
    if session.is_question_active:
        return {
            'started': False,
            'code': 'already_started',
            'question_started_at': quiz.question_start_time,
            'answer_deadline': session.answer_deadline,
            'schedule': list(session.clue_schedule or []),
        }

    resolved_start = _timestamp(starts_at)
    if resolved_start is None:
        return {'started': False, 'code': 'invalid_start_time'}
    raw_schedule, cursor, _ = _build_schedule(
        quiz.current_question,
        starts_at=resolved_start,
        duration_override=session.clue_duration_override,
    )
    if not raw_schedule:
        return {'started': False, 'code': 'missing_clues'}

    quiz.question_start_time = resolved_start
    quiz.current_clue = None
    quiz.clue_start_time = None
    quiz.save(update_fields=[
        'question_start_time',
        'current_clue',
        'clue_start_time',
        'updated_at',
    ])
    session.is_question_active = True
    session.current_clue_number = 0
    session.is_clue_active = False
    session.clue_end_time = None
    session.question_end_time = cursor
    session.answer_deadline = cursor
    session.clue_schedule = raw_schedule
    session.save(update_fields=[
        'is_question_active',
        'current_clue_number',
        'is_clue_active',
        'clue_end_time',
        'question_end_time',
        'answer_deadline',
        'clue_schedule',
        'updated_at',
    ])
    return {
        'started': True,
        'question_started_at': resolved_start,
        'answer_deadline': cursor,
        'schedule': raw_schedule,
    }


@transaction.atomic
def start_question_schedule(*, quiz_id, question_id, duration_override=None):
    quiz = ClueRushGame.objects.select_for_update().get(pk=quiz_id)
    session, _ = ClueRushSession.objects.select_for_update().get_or_create(quiz=quiz)
    if session.is_question_active:
        return {
            'started': False,
            'question_started_at': quiz.question_start_time,
            'answer_deadline': session.answer_deadline,
            'schedule': list(session.clue_schedule or []),
            'code': (
                'already_started'
                if quiz.current_question_id == question_id
                else 'invalid_phase'
            ),
        }

    question = _resolve_question(quiz, question_id)
    now = timezone.now()
    raw_schedule, cursor, _ = _build_schedule(
        question,
        starts_at=now,
        duration_override=duration_override,
    )

    quiz.current_question = question
    quiz.question_start_time = now
    quiz.current_clue = None
    quiz.clue_start_time = None
    quiz.save(update_fields=[
        'current_question',
        'question_start_time',
        'current_clue',
        'clue_start_time',
        'updated_at',
    ])
    session.current_question_number += 1
    session.total_questions_sent += 1
    session.is_question_active = True
    session.current_clue_number = 0
    session.is_clue_active = False
    session.clue_end_time = None
    session.question_end_time = cursor if raw_schedule else now
    session.answer_deadline = cursor if raw_schedule else now
    session.clue_duration_override = duration_override
    session.clue_schedule = raw_schedule
    session.question_finalized_at = None
    session.finalized_question_id = None
    session.total_responses_current_question = 0
    session.correct_responses_current_question = 0
    session.save()
    return {
        'started': True,
        'question_started_at': now,
        'answer_deadline': session.answer_deadline,
        'schedule': raw_schedule,
    }


@transaction.atomic
def _reconcile_clue_schedule_state(room_code, *, at=None):
    now = at or timezone.now()
    try:
        quiz = (
            ClueRushGame.objects
            .select_for_update()
            .select_related('current_question')
            .get(room_code=room_code)
        )
        session = ClueRushSession.objects.select_for_update().get(quiz=quiz)
    except (ClueRushGame.DoesNotExist, ClueRushSession.DoesNotExist):
        return {'new_clues': [], 'deadline_reached': False}
    if (
        quiz.status != 'active'
        or not quiz.current_question_id
        or not session.is_question_active
        or session.question_finalized_at
    ):
        return {
            'new_clues': [],
            'deadline_reached': bool(
                session.answer_deadline and now >= session.answer_deadline
            ),
        }

    schedule = [
        entry
        for entry in (session.clue_schedule or [])
        if str(entry.get('question_id')) == str(quiz.current_question_id)
    ]
    due_count = sum(
        1
        for entry in schedule
        if _timestamp(entry.get('starts_at')) and _timestamp(entry['starts_at']) <= now
    )
    previous_count = min(session.current_clue_number, len(schedule))
    new_entries = schedule[previous_count:due_count]
    if due_count > previous_count:
        latest = schedule[due_count - 1]
        quiz.current_clue_id = latest['id']
        quiz.clue_start_time = _timestamp(latest['starts_at'])
        quiz.save(update_fields=['current_clue', 'clue_start_time', 'updated_at'])
        session.current_clue_number = due_count
        session.is_clue_active = True
        session.clue_end_time = _timestamp(latest['end_time'])
        session.save(update_fields=[
            'current_clue_number',
            'is_clue_active',
            'clue_end_time',
            'updated_at',
        ])

    return {
        'new_clues': [
            {
                **entry,
                'server_now': now.isoformat(),
            }
            for entry in new_entries
        ],
        'visible_clues': [
            {
                **entry,
                'server_now': now.isoformat(),
            }
            for entry in schedule[:due_count]
        ],
        'deadline_reached': bool(
            session.answer_deadline and now >= session.answer_deadline
        ),
        'answer_deadline': (
            session.answer_deadline.isoformat()
            if session.answer_deadline
            else None
        ),
        'question_id': quiz.current_question_id,
    }


def _resolve_hub_session_code(room_code):
    step = (
        HubGameStep.objects
        .select_related('session')
        .filter(
            game_key='clue_rush',
            room_code=room_code,
            session__ended_at__isnull=True,
        )
        .order_by('-id')
        .first()
    )
    if not step:
        step = (
            HubGameStep.objects
            .select_related('session')
            .filter(game_key='clue_rush', room_code=room_code)
            .order_by('-id')
            .first()
        )
    return step.session.code if step else None


@transaction.atomic
def finalize_clue_question(
    room_code,
    *,
    expected_question_id=None,
    hub_session_code=None,
    at=None,
    force=False,
):
    now = at or timezone.now()
    try:
        quiz = (
            ClueRushGame.objects
            .select_for_update()
            .select_related('current_question', 'current_clue')
            .get(room_code=room_code)
        )
        session = ClueRushSession.objects.select_for_update().get(quiz=quiz)
    except (ClueRushGame.DoesNotExist, ClueRushSession.DoesNotExist):
        return {
            'newly_finalized': False,
            'question_id': None,
            'code': 'invalid_phase',
        }

    if (
        expected_question_id is not None
        and str(quiz.current_question_id or session.finalized_question_id or '')
        != str(expected_question_id)
    ):
        return {
            'newly_finalized': False,
            'question_id': quiz.current_question_id or session.finalized_question_id,
            'code': 'stale_action',
        }
    if session.question_finalized_at:
        return {
            'newly_finalized': False,
            'question_id': session.finalized_question_id,
            'code': 'already_finalized',
        }
    question = quiz.current_question
    if not question:
        return {
            'newly_finalized': False,
            'question_id': None,
            'code': 'invalid_phase',
        }
    if not force and (
        not session.answer_deadline
        or now < session.answer_deadline
    ):
        return {
            'newly_finalized': False,
            'question_id': question.id,
            'code': 'deadline_open',
        }

    resolved_session_code = (
        hub_session_code
        if hub_session_code is not None
        else _resolve_hub_session_code(room_code)
    )
    tutorial_round = is_unit_tutorial_question(
        'clue_rush',
        room_code,
        resolved_session_code,
        question.id,
    )
    participants = quiz.participants.filter(is_active=True)
    if resolved_session_code is not None:
        participants = participants.filter(hub_session_code=resolved_session_code)
    participants = list(participants.select_for_update().order_by('name'))
    existing_answers = {
        answer.participant_id: answer
        for answer in ClueAnswer.objects.filter(
            quiz=quiz,
            question=question,
            participant__in=participants,
        )
    }
    pending_inputs = {
        pending.participant_id: pending
        for pending in CluePendingInput.objects.filter(
            quiz=quiz,
            question=question,
            participant__in=participants,
        )
    }
    total_clues = question.clues.count()
    current_clue_number = min(session.current_clue_number, total_clues)
    if current_clue_number <= 0 and total_clues:
        current_clue_number = 1
    answer_ids = []
    for participant in participants:
        answer = existing_answers.get(participant.id)
        if not answer:
            pending = pending_inputs.get(participant.id)
            answer = ClueAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=question,
                answer_text=((pending.answer_text if pending else '') or '')[:200],
                time_taken=pending.time_taken if pending else None,
                submitted_clue_number=(
                    pending.submitted_clue_number
                    if pending and pending.submitted_clue_number
                    else current_clue_number
                ),
                total_clues_at_submission=(
                    pending.total_clues_at_input
                    if pending and pending.total_clues_at_input
                    else total_clues
                ),
            )
        if tutorial_round and answer.points_earned:
            answer.points_earned = 0
            answer.save(update_fields=['points_earned', 'updated_at'])
        answer_ids.append(answer.id)

    CluePendingInput.objects.filter(
        quiz=quiz,
        question=question,
        participant__in=participants,
    ).delete()
    session.total_responses_current_question = len(answer_ids)
    session.correct_responses_current_question = ClueAnswer.objects.filter(
        id__in=answer_ids,
        is_correct=True,
    ).count()
    session.is_question_active = False
    session.is_clue_active = False
    session.question_end_time = None
    session.clue_end_time = None
    session.question_finalized_at = now
    session.finalized_question_id = question.id
    session.save(update_fields=[
        'total_responses_current_question',
        'correct_responses_current_question',
        'is_question_active',
        'is_clue_active',
        'question_end_time',
        'clue_end_time',
        'question_finalized_at',
        'finalized_question_id',
        'updated_at',
    ])
    quiz.current_question = None
    quiz.question_start_time = None
    quiz.current_clue = None
    quiz.clue_start_time = None
    quiz.save(update_fields=[
        'current_question',
        'question_start_time',
        'current_clue',
        'clue_start_time',
        'updated_at',
    ])
    unit_tutorial = finish_current_unit_tutorial(
        'clue_rush',
        room_code,
        resolved_session_code,
    )
    return {
        'newly_finalized': True,
        'question_id': question.id,
        'answer_ids': answer_ids,
        'is_tutorial_round': bool(unit_tutorial.get('is_tutorial_round')),
        'code': 'finalized',
    }


def reconcile_clue_schedule(room_code, *, at=None):
    now = at or timezone.now()
    result = _reconcile_clue_schedule_state(room_code, at=now)
    if result.get('deadline_reached') and result.get('question_id'):
        result['finalization'] = finalize_clue_question(
            room_code,
            expected_question_id=result['question_id'],
            at=now,
        )
    return result


def current_clue_runtime(room_code):
    try:
        quiz = ClueRushGame.objects.select_related('current_question').get(room_code=room_code)
        session = quiz.session
    except (ClueRushGame.DoesNotExist, ClueRushSession.DoesNotExist):
        return None
    schedule = [
        entry
        for entry in (session.clue_schedule or [])
        if str(entry.get('question_id')) == str(quiz.current_question_id)
    ]
    return {
        'quiz': quiz,
        'session': session,
        'schedule': schedule,
        'visible_clues': schedule[:session.current_clue_number],
    }
