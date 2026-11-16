"""Atomic Who is That lifecycle shared by WebSocket, HTTP and host recovery."""
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import WhoThatQuiz, WhoThatQuestion, WhoThatSession, WhoThatParticipant, WhoThatAnswer
from games_hub.authoritative_state import present_question, finish_question_flow, current_snapshot
from games_hub.unit_tutorial_runtime import is_unit_tutorial_question, finish_current_unit_tutorial
from games_hub.quiz_rounds import current_round, progress
from games_hub.quiz_pause import session_code_for

@transaction.atomic
def present(
    room_code,
    quiz_id,
    question_id,
    hub_session_code,
    action,
    answer_duration_seconds,
    at=None,
):
    quiz = WhoThatQuiz.objects.select_for_update().get(id=quiz_id)
    question = WhoThatQuestion.objects.get(id=question_id)
    session, _ = WhoThatSession.objects.select_for_update().get_or_create(quiz=quiz)
    decision = present_question(
        game_key='who_that',
        room_code=room_code,
        session_code=hub_session_code,
        action=action,
        answer_duration_seconds=answer_duration_seconds,
        at=at,
    )
    if decision.accepted and not decision.duplicate:
        session.prepare_question(question)
        started_at = parse_datetime(
            decision.snapshot.get('answering_started_at') or ''
        )
        deadline = parse_datetime(
            decision.snapshot.get('answering_deadline_at') or ''
        )
        if not started_at or not deadline:
            raise ValueError('Authoritative answering timestamps are missing.')
        session.open_answering(
            question,
            started_at=started_at,
            answer_duration_seconds=(deadline - started_at).total_seconds(),
        )
    return decision

@transaction.atomic
def save_answer(room_code, participant_name, hub_session_code, user_answer, time_taken, question_id=None):
    if not isinstance(user_answer, str) or not user_answer.strip() or len(user_answer) > 200:
        return None
    user_answer = user_answer.strip()
    try:
        quiz = (
            WhoThatQuiz.objects.select_for_update()
            .select_related('current_question')
            .get(room_code=room_code)
        )
        participant = WhoThatParticipant.objects.select_for_update().get(
            quiz=quiz,
            name=participant_name,
            hub_session_code=hub_session_code or None,
        )

        if quiz.status != 'active':
            return None
        if not quiz.current_question:
            return None
        if question_id is None or str(quiz.current_question_id) != str(question_id):
            return None
        session = WhoThatSession.objects.select_for_update().filter(quiz=quiz).first()
        received_at = timezone.now()
        if (
            not session
            or not session.is_question_active
            or not quiz.question_start_time
            or received_at < quiz.question_start_time
            or not session.question_end_time
            or (
                received_at >= session.question_end_time
            )
        ):
            return None

        # Check if answer already exists
        existing_answer = WhoThatAnswer.objects.filter(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            round_number=current_round(quiz),
        ).first()

        if existing_answer:
            return None  # Already answered

        server_time_taken = (
            max(0.0, (received_at - quiz.question_start_time).total_seconds())
            if quiz.question_start_time
            else 0.0
        )

        # Create new answer
        answer, created = WhoThatAnswer.objects.get_or_create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            round_number=current_round(quiz),
            defaults={
                'user_answer': user_answer,
                'is_tutorial': is_unit_tutorial_question('who_that', room_code, hub_session_code, quiz.current_question_id),
                'time_taken': server_time_taken,
            },
        )
        if not created:
            return None
        is_tutorial_answer = is_unit_tutorial_question(
            'who_that',
            room_code,
            hub_session_code,
            quiz.current_question_id,
        )
        if is_tutorial_answer and answer.points_earned:
            answer.points_earned = 0
            answer.save(update_fields=['points_earned', 'updated_at'])

        # Record stats in session
        if session:
            session.record_answer(answer.is_correct, server_time_taken)
            pending_answers = dict(session.pending_answers or {})
            pending_answers.pop(str(participant.id), None)
            session.pending_answers = pending_answers
            session.save(update_fields=['pending_answers', 'updated_at'])

        return {
            'answer_id': answer.id,
            'game_id': quiz.id,
            'question_id': answer.question_id,
            'participant_id': participant.id,
            'hub_session_code': participant.hub_session_code,
            'user_answer': answer.user_answer,
            'is_correct': answer.is_correct,
            'can_mark_correct': not is_tutorial_answer and not answer.is_correct,
            'points_earned': answer.points_earned,
            'is_tutorial_round': is_tutorial_answer,
            'accuracy_percentage': answer.get_accuracy_percentage(),
            'match_quality': answer.get_match_quality(),
            'time_taken': answer.time_taken,
            'submitted_at': answer.submitted_at.isoformat(),
        }

    except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
        return None

@transaction.atomic
def save_pending(room_code, participant_name, hub_session_code, user_answer, question_id=None):
    if not isinstance(user_answer, str) or len(user_answer) > 200:
        return False
    try:
        quiz = WhoThatQuiz.objects.select_for_update().get(room_code=room_code)
        if quiz.status != 'active':
            return False
        if not quiz.current_question:
            return False
        if question_id is None or str(quiz.current_question_id) != str(question_id):
            return False

        participant = quiz.participants.select_for_update().get(
            name=participant_name,
            hub_session_code=hub_session_code or None,
        )
        session = WhoThatSession.objects.select_for_update().filter(quiz=quiz).first()
        now = timezone.now()
        if (
            not session
            or not session.is_question_active
            or not quiz.question_start_time
            or now < quiz.question_start_time
            or not session.question_end_time
            or now >= session.question_end_time
        ):
            return False
        if WhoThatAnswer.objects.filter(quiz=quiz, participant=participant,
                question_id=quiz.current_question_id, round_number=current_round(quiz)).exists():
            return False
        pending_answers = dict(session.pending_answers or {})
        entry_key = str(participant.id)
        cleaned_answer = (user_answer or '').strip()

        if cleaned_answer:
            pending_answers[entry_key] = {
                'question_id': quiz.current_question_id,
                'round_number': current_round(quiz),
                'question_started_at': quiz.question_start_time.isoformat(),
                'user_answer': cleaned_answer,
                'updated_at': now.isoformat(),
                'participant_name': participant.name,
                'hub_session_code': participant.hub_session_code,
            }
        else:
            pending_answers.pop(entry_key, None)

        session.pending_answers = pending_answers
        session.save(update_fields=['pending_answers', 'updated_at'])
        return True
    except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
        return False

@transaction.atomic
def finalize_pending(room_code, quiz_id):
    try:
        quiz = WhoThatQuiz.objects.select_for_update().select_related('current_question').get(id=quiz_id)
    except WhoThatQuiz.DoesNotExist:
        return []

    question = quiz.current_question
    session = WhoThatSession.objects.select_for_update().filter(quiz=quiz).first()
    if not question or not session:
        return []

    pending_answers = dict(session.pending_answers or {})
    finalized = []
    changed = False

    for participant_id, pending in list(pending_answers.items()):
        if (str(pending.get('question_id')) != str(question.id)
            or pending.get('round_number', current_round(quiz)) != current_round(quiz)
            or pending.get('question_started_at', quiz.question_start_time.isoformat() if quiz.question_start_time else None) != (quiz.question_start_time.isoformat() if quiz.question_start_time else None)):
            continue

        user_answer = (pending.get('user_answer') or '').strip()
        if not user_answer:
            pending_answers.pop(participant_id, None)
            changed = True
            continue

        try:
            participant = quiz.participants.get(id=int(participant_id))
        except (WhoThatParticipant.DoesNotExist, ValueError, TypeError):
            pending_answers.pop(participant_id, None)
            changed = True
            continue

        if participant.hub_session_code != (pending.get('hub_session_code') or None):
            continue

        existing_answer = WhoThatAnswer.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
            round_number=current_round(quiz),
        ).first()

        if existing_answer:
            pending_answers.pop(participant_id, None)
            changed = True
            continue

        updated_at = parse_datetime(pending.get('updated_at') or '')
        if updated_at and timezone.is_naive(updated_at):
            updated_at = timezone.make_aware(updated_at, timezone.get_current_timezone())
        if not updated_at or not quiz.question_start_time or not session.question_end_time:
            continue
        if not quiz.question_start_time <= updated_at < session.question_end_time:
            continue

        time_taken = 0
        if quiz.question_start_time and updated_at:
            time_taken = max(0, (updated_at - quiz.question_start_time).total_seconds())

        answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            round_number=current_round(quiz),
            is_tutorial=is_unit_tutorial_question('who_that', room_code, participant.hub_session_code, question.id),
            user_answer=user_answer,
            time_taken=time_taken,
        )

        session.record_answer(answer.is_correct, time_taken)

        finalized.append({
            'participant_name': participant.name,
            'hub_session_code': participant.hub_session_code,
            'is_correct': answer.is_correct,
            'points_earned': answer.points_earned,
            'accuracy_percentage': answer.get_accuracy_percentage(),
            'match_quality': answer.get_match_quality(),
        })

        pending_answers.pop(participant_id, None)
        changed = True

    if changed:
        session.pending_answers = pending_answers
        session.save(update_fields=['pending_answers', 'updated_at'])

    return finalized


def broadcast(room_code, event):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    layer = get_channel_layer()
    if layer:
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(f'who_that_{room_code}', event))


@transaction.atomic
def end_question(quiz_id, session_code=None, *, question_id=None, round_number=None, expired_only=False):
    """Close once, preserving drafts, including when a host returns after expiry."""
    quiz = WhoThatQuiz.objects.select_for_update().select_related('current_question').get(pk=quiz_id)
    session = WhoThatSession.objects.select_for_update().filter(quiz=quiz).first()
    question = quiz.current_question
    if not question or not session or quiz.status != 'active':
        return None
    if question_id is not None and str(question.id) != str(question_id):
        return None
    if round_number is not None and str(current_round(quiz)) != str(round_number):
        return None
    now = timezone.now()
    if not session.is_question_active or not quiz.question_start_time or now < quiz.question_start_time:
        return None
    if expired_only and (not session.question_end_time or now < session.question_end_time):
        return None
    session_code = session_code or session_code_for(quiz, 'who_that')
    finalize_pending(quiz.room_code, quiz.pk)
    answers = list(quiz.who_that_answers.filter(question=question, round_number=current_round(quiz),
        participant__hub_session_code=session_code).select_related('participant'))
    results = [{
        'participant_name': answer.participant.name,
        'participant_id': answer.participant_id,
        'hub_session_code': answer.participant.hub_session_code,
        'question_id': question.id, 'round_number': answer.round_number,
        'user_answer': answer.user_answer, 'is_correct': answer.is_correct,
        'points_earned': answer.points_earned,
        'match_quality': answer.get_match_quality(),
    } for answer in answers]
    tutorial = finish_current_unit_tutorial('who_that', quiz.room_code, session_code)
    session.refresh_from_db()
    session.end_current_question()
    finish_question_flow(game_key='who_that', room_code=quiz.room_code,
                         session_code=session_code, question_id=question.id)
    event = {
        'type': 'question_ended', 'message': 'Frage beendet.',
        'question_id': question.id, 'round_number': session.current_question_number,
        'question_number': progress(quiz, 'who_that', session_code, question_id=question.id)['current'],
        'correct_answer': {'id': question.id, 'correct_answer': question.correct_answer,
            'alternative_answers': question.alternative_answers, 'explanation': question.explanation,
            'image_url': question.image.url if question.image else None},
        'evaluated_pending_answers': results,
        'is_tutorial_round': bool(tutorial.get('is_tutorial_round')),
    }
    broadcast(quiz.room_code, event)
    return event


@transaction.atomic
def end_game(quiz_id, status='completed'):
    """A direct game end discards open drafts and never evaluates them."""
    quiz = WhoThatQuiz.objects.select_for_update().get(pk=quiz_id)
    question_id = quiz.current_question_id
    session = WhoThatSession.objects.select_for_update().filter(quiz=quiz).first()
    if session:
        session.end_current_question()
    quiz.status = status
    quiz.ended_at = timezone.now()
    quiz.current_question = None
    quiz.question_start_time = None
    quiz.save(update_fields=['status', 'ended_at', 'current_question', 'question_start_time'])
    if question_id:
        finish_question_flow(game_key='who_that', room_code=quiz.room_code,
            session_code=session_code_for(quiz, 'who_that'), question_id=question_id)


def reveal_state(room_code, session_code, participant_id=None):
    quiz = WhoThatQuiz.objects.get(room_code=room_code)
    if quiz.current_question_id or quiz.status != 'active':
        return None
    snapshot = current_snapshot('who_that', room_code, session_code)
    question = WhoThatQuestion.objects.filter(pk=snapshot.get('completed_question_id') or None).first()
    if not question:
        return None
    from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids
    tutorial = question.id in get_scorebox_excluded_tutorial_question_ids('who_that', room_code, session_code)
    number = progress(quiz, 'who_that', session_code, question_id=question.id)['current']
    answers = quiz.who_that_answers.filter(question=question, round_number=current_round(quiz),
        participant_id=participant_id, participant__hub_session_code=session_code)
    return {'type': 'question_ended', 'question_id': question.id,
        'round_number': current_round(quiz), 'question_number': number, 'is_tutorial_round': tutorial,
        'correct_answer': {'id': question.id, 'correct_answer': question.correct_answer,
            'alternative_answers': question.alternative_answers, 'explanation': question.explanation,
            'image_url': question.image.url if question.image else None},
        'evaluated_pending_answers': [{'participant_name': answer.participant.name,
            'hub_session_code': answer.participant.hub_session_code, 'user_answer': answer.user_answer,
            'is_correct': answer.is_correct, 'points_earned': answer.points_earned,
            'match_quality': answer.get_match_quality()} for answer in answers.select_related('participant')]}
