"""Atomic estimation persistence shared by host HTTP, sockets and recovery."""
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import EstimationQuiz, EstimationQuestion, EstimationParticipant, EstimationAnswer, EstimationSession
from .numbers import finite_number, decimal_number
from .progress import participant_count, max_points, scope_code
from games_hub.quiz_rounds import current_round
from games_hub.quiz_pause import session_code_for
from games_hub.authoritative_state import current_snapshot, present_question, open_answering, finish_question_flow, get_runtime_state, observe_snapshot, reset_question_flow
from games_hub.models import GameRuntimeState
from games_hub.unit_tutorial_runtime import is_unit_tutorial_question, finish_current_unit_tutorial
from QuizGame.presentation import question_typewriter_timing

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
    quiz = EstimationQuiz.objects.select_for_update().get(id=quiz_id)
    question = EstimationQuestion.objects.get(id=question_id)
    session, _ = EstimationSession.objects.select_for_update().get_or_create(quiz=quiz)
    decision = present_question(
        game_key='estimation',
        room_code=room_code,
        session_code=hub_session_code,
        action=action,
        answer_duration_seconds=answer_duration_seconds,
        at=at,
        **question_typewriter_timing(question.question_text),
    )
    if decision.accepted and not decision.duplicate:
        runtime = get_runtime_state('estimation', room_code, hub_session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        public_snapshot = dict(runtime.public_snapshot or {})
        public_snapshot.pop('prepared_question_id', None)
        public_snapshot.pop('question_shell_prepared', None)
        runtime.public_snapshot = public_snapshot
        runtime.save(update_fields=['public_snapshot', 'updated_at'])
        session.prepare_question(question)
    return decision

@transaction.atomic
def open_question(
    room_code,
    quiz_id,
    question_id,
    hub_session_code,
    action,
    at=None,
):
    quiz = (
        EstimationQuiz.objects.select_for_update()
        .select_related('current_question')
        .get(id=quiz_id)
    )
    session = EstimationSession.objects.select_for_update().get(quiz=quiz)
    if quiz.current_question_id != question_id:
        return open_answering(
            game_key='estimation',
            room_code=room_code,
            session_code=hub_session_code,
            action=action,
        )
    opened_at = at or timezone.now()
    decision = open_answering(
        game_key='estimation',
        room_code=room_code,
        session_code=hub_session_code,
        action=action,
        at=opened_at,
    )
    if decision.accepted and not decision.duplicate:
        started_at = parse_datetime(decision.snapshot.get('answering_started_at') or '')
        deadline = parse_datetime(decision.snapshot.get('answering_deadline_at') or '')
        if not started_at or not deadline:
            raise ValueError('Authoritative answering timestamps are missing.')
        tutorial = is_unit_tutorial_question('estimation', room_code, hub_session_code, quiz.current_question_id)
        limits = dict(session.round_max_points or {})
        limits[str(session.current_question_number)] = 0 if tutorial else quiz.current_question.get_max_points_for_mode(quiz.get_effective_scoring_mode(), participant_count(quiz, hub_session_code))
        session.round_max_points = limits
        session.save(update_fields=['round_max_points', 'updated_at'])
        session.open_answering(
            quiz.current_question,
            started_at=started_at,
            answer_duration_seconds=(deadline - started_at).total_seconds(),
        )
    return decision

@transaction.atomic
def rank_answers(room_code, quiz_id: int, hub_session_code=None):
    """Pool all answers for the current question, rank by closeness, assign descending points.
    Returns list of dicts: participant_name, points_earned, rank_position, user_answer, formatted_answer, accuracy_percentage, percentage_difference, difference_indicator.
    """
    try:
        quiz = EstimationQuiz.objects.select_for_update().get(id=quiz_id)
        hub_session_code = scope_code(quiz, hub_session_code)
        question = quiz.current_question
        if not question:
            return []
        if is_unit_tutorial_question('estimation', room_code, hub_session_code, question.pk):
            return []

        # Gather all answers for this quiz/question
        answers_qs = EstimationAnswer.objects.filter(
            quiz=quiz,
            question=question,
            round_number=current_round(quiz),
            participant__hub_session_code=hub_session_code or None,
            is_tutorial=False,
        ).select_related('participant', 'question')
        if hub_session_code:
            answers_qs = answers_qs.filter(participant__hub_session_code=hub_session_code or None)
        answers = list(answers_qs)
        point_limit = max(participant_count(quiz, hub_session_code), len(answers))
        session = getattr(quiz, 'session', None)
        if session:
            limits = dict(session.round_max_points or {})
            limits[str(current_round(quiz))] = point_limit
            session.round_max_points = limits
            session.save(update_fields=['round_max_points', 'updated_at'])
        if not answers:
            return []

        # Rank by absolute difference to correct answer; tie-breaker: faster time_taken wins, then earlier submitted_at
        def sort_key(ans: EstimationAnswer):
            diff = abs(decimal_number(ans.user_answer) - decimal_number(question.correct_answer))
            time_val = ans.time_taken if ans.time_taken is not None else float('inf')
            return (diff, time_val, ans.submitted_at)

        answers.sort(key=sort_key)

        results = []
        for idx, ans in enumerate(answers):
            # Descending points from participant count to 1
            points = max(1, point_limit - idx)
            # Update and save; this recalculates participant total via model's save
            ans.points_earned = points
            ans.save()

            results.append({
                'participant_id': ans.participant_id,
                'participant_name': ans.participant.name,
                'hub_session_code': ans.participant.hub_session_code,
                'points_earned': ans.points_earned,
                'rank_position': idx + 1,
                'user_answer': ans.user_answer,
                'formatted_answer': ans.get_formatted_user_answer(),
                'accuracy_percentage': ans.get_accuracy_percentage(),
                'percentage_difference': ans.get_percentage_difference(),
                'difference_indicator': ans.get_difference_indicator(),
                'time_taken': ans.time_taken,
            })

        return results
    except EstimationQuiz.DoesNotExist:
        return []

@transaction.atomic
def save_answer(room_code, participant_name, hub_session_code, user_answer, time_taken, question_id=None):
    try:
        user_answer = finite_number(user_answer)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        quiz = (
            EstimationQuiz.objects.select_for_update()
            .select_related('current_question')
            .get(room_code=room_code)
        )
        participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code or None)
        
        if quiz.status != 'active':
            return None
        if not quiz.current_question:
            return None
        if question_id is None or str(quiz.current_question_id) != str(question_id):
            return None
        session = EstimationSession.objects.select_for_update().filter(quiz=quiz).first()
        received_at = timezone.now()
        if (
            not session
            or not session.is_question_active
            or not quiz.question_start_time
            or received_at < quiz.question_start_time
            or not session.question_end_time
            or (
                session.question_end_time
                and received_at >= session.question_end_time
            )
        ):
            return None
        
        # Check if answer already exists
        existing_answer = EstimationAnswer.objects.filter(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            round_number=current_round(quiz),
        ).first()
        
        if existing_answer:
            return None  # Already answered
        
        # Convert user answer to float
        try:
            user_answer_float = finite_number(user_answer)
        except (ValueError, TypeError):
            return None
        
        server_time_taken = (
            max(0.0, (received_at - quiz.question_start_time).total_seconds())
            if quiz.question_start_time
            else 0.0
        )

        # Create new answer
        answer, created = EstimationAnswer.objects.get_or_create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            round_number=current_round(quiz),
            defaults={
                'user_answer': user_answer_float,
                'is_tutorial': is_unit_tutorial_question('estimation', room_code, hub_session_code, quiz.current_question_id),
                'time_taken': server_time_taken,
            },
        )
        if not created:
            return None
        is_tutorial_answer = is_unit_tutorial_question(
            'estimation',
            room_code,
            hub_session_code,
            quiz.current_question_id,
        )
        if is_tutorial_answer and answer.points_earned:
            answer.points_earned = 0
            answer.save(update_fields=['points_earned', 'updated_at'])

        if session:
            pending_answers = dict(session.pending_answers or {})
            if pending_answers.pop(str(participant.id), None) is not None:
                session.pending_answers = pending_answers
                session.save(update_fields=['pending_answers', 'updated_at'])
        
        return {
            'question_id': answer.question_id, 'round_number': answer.round_number,
            'participant_name': participant.name, 'hub_session_code': participant.hub_session_code,
            'points_earned': answer.points_earned,
            'is_tutorial_round': is_tutorial_answer,
            'accuracy_percentage': answer.get_accuracy_percentage(),
            'user_answer': answer.user_answer,
            'formatted_answer': answer.get_formatted_user_answer(),
            'percentage_difference': answer.get_percentage_difference(),
            'difference_indicator': answer.get_difference_indicator(),
            'time_taken': answer.time_taken,
        }
        
    except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
        return None

@transaction.atomic
def save_pending(room_code, participant_name, hub_session_code, user_answer, question_id=None):
    try:
        cleaned = '' if user_answer is None else str(user_answer).strip()
        if cleaned:
            finite_number(cleaned)
    except (TypeError, ValueError, OverflowError):
        return False
    try:
        quiz = EstimationQuiz.objects.select_for_update().get(room_code=room_code)
        if quiz.status != 'active':
            return False
        if not quiz.current_question:
            return False
        if question_id is None or str(quiz.current_question_id) != str(question_id):
            return False

        participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code or None)
        session, _ = EstimationSession.objects.select_for_update().get_or_create(quiz=quiz)
        received_at = timezone.now()
        if (
            not session.is_question_active
            or not quiz.question_start_time
            or received_at < quiz.question_start_time
            or not session.question_end_time
            or received_at >= session.question_end_time
        ):
            return False
        pending_answers = dict(session.pending_answers or {})
        entry_key = str(participant.id)
        cleaned_answer = '' if user_answer is None else str(user_answer).strip()

        existing_answer = EstimationAnswer.objects.filter(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            round_number=current_round(quiz),
        ).first()
        if existing_answer:
            if pending_answers.pop(entry_key, None) is not None:
                session.pending_answers = pending_answers
                session.save(update_fields=['pending_answers', 'updated_at'])
            return False

        if cleaned_answer:
            pending_answers[entry_key] = {
                'question_id': quiz.current_question_id,
                'round_number': current_round(quiz),
                'question_started_at': quiz.question_start_time.isoformat(),
                'user_answer': cleaned_answer,
                'updated_at': received_at.isoformat(),
                'participant_name': participant.name,
                'hub_session_code': participant.hub_session_code,
            }
        else:
            pending_answers.pop(entry_key, None)

        session.pending_answers = pending_answers
        session.save(update_fields=['pending_answers', 'updated_at'])
        return True
    except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
        return False

@transaction.atomic
def finalize_pending(room_code, quiz_id, hub_session_code=None):
    try:
        quiz = EstimationQuiz.objects.select_for_update().select_related('current_question').get(id=quiz_id)
    except EstimationQuiz.DoesNotExist:
        return []

    question = quiz.current_question
    session = EstimationSession.objects.select_for_update().filter(quiz=quiz).first()
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

        raw_user_answer = '' if pending.get('user_answer') is None else str(pending.get('user_answer')).strip()
        if not raw_user_answer:
            pending_answers.pop(participant_id, None)
            changed = True
            continue

        try:
            user_answer_float = finite_number(raw_user_answer)
        except (TypeError, ValueError):
            pending_answers.pop(participant_id, None)
            changed = True
            continue

        try:
            participant = quiz.participants.get(id=int(participant_id))
        except (EstimationParticipant.DoesNotExist, ValueError, TypeError):
            pending_answers.pop(participant_id, None)
            changed = True
            continue
        if participant.hub_session_code != (hub_session_code or None):
            continue

        existing_answer = EstimationAnswer.objects.filter(
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

        if (not updated_at or not quiz.question_start_time or not session.question_end_time
                or not quiz.question_start_time <= updated_at < session.question_end_time):
            continue
        time_taken = 0
        if quiz.question_start_time and updated_at:
            time_taken = max(0, (updated_at - quiz.question_start_time).total_seconds())

        answer = EstimationAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            round_number=current_round(quiz),
            is_tutorial=is_unit_tutorial_question('estimation', room_code, hub_session_code, question.id),
            user_answer=user_answer_float,
            time_taken=time_taken,
        )

        finalized.append({
            'participant_id': participant.id,
            'participant_name': participant.name,
            'hub_session_code': participant.hub_session_code,
            'user_answer': answer.user_answer,
            'formatted_answer': answer.get_formatted_user_answer(),
            'points_earned': answer.points_earned,
            'accuracy_percentage': answer.get_accuracy_percentage(),
            'percentage_difference': answer.get_percentage_difference(),
            'difference_indicator': answer.get_difference_indicator(),
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
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(f'estimation_{room_code}', event))


def serialize_answer(answer):
    return {'participant_id': answer.participant_id, 'participant_name': answer.participant.name,
        'hub_session_code': answer.participant.hub_session_code,
        'question_id': answer.question_id, 'round_number': answer.round_number,
        'user_answer': answer.user_answer, 'formatted_answer': answer.get_formatted_user_answer(),
        'points_earned': answer.points_earned, 'accuracy_percentage': answer.get_accuracy_percentage(),
        'percentage_difference': answer.get_percentage_difference(), 'difference_indicator': answer.get_difference_indicator()}


@transaction.atomic
def end_question(quiz_id, session_code=None, *, question_id=None, round_number=None, expired_only=False, emit=True):
    quiz = EstimationQuiz.objects.select_for_update().select_related('current_question').get(pk=quiz_id)
    session = EstimationSession.objects.select_for_update().filter(quiz=quiz).first()
    if quiz.status != 'active' or not quiz.current_question_id:
        return None
    code = scope_code(quiz, session_code)
    snapshot = current_snapshot('estimation', quiz.room_code, code)
    manual = snapshot.get('question_flow_mode') == 'manual_three_phase'
    if manual and (not session or not session.is_question_active):
        return None
    if not session:
        session = EstimationSession.objects.create(quiz=quiz)
    if question_id is not None and str(question_id) != str(quiz.current_question_id):
        return None
    if round_number is not None and str(round_number) != str(current_round(quiz)):
        return None
    if expired_only and (not session.question_end_time or timezone.now() < session.question_end_time):
        return None
    if snapshot.get('question_flow_mode') == 'manual_three_phase' and snapshot.get('question_phase') != 'answering_open':
        return None
    question = quiz.current_question
    finalize_pending(quiz.room_code, quiz.pk, code)
    tutorial = is_unit_tutorial_question('estimation', quiz.room_code, code, question.pk)
    rank_results = None
    if not tutorial and quiz.get_effective_scoring_mode() == 'rank':
        ranks = rank_answers(quiz.room_code, quiz.pk, code)
        rank_results = [{key: row[key] for key in ('participant_name', 'points_earned', 'rank_position')} for row in ranks]
    answers = list(quiz.estimation_answers.filter(question=question, round_number=current_round(quiz),
        participant__hub_session_code=code).select_related('question', 'participant'))
    results = [serialize_answer(answer) for answer in answers]
    quiz._state.fields_cache.pop('session', None)
    point_limit = 0 if tutorial else max_points(quiz, question, code)
    finish_current_unit_tutorial('estimation', quiz.room_code, code)
    session.refresh_from_db()
    session.end_current_question()
    quiz.refresh_from_db()
    finish_question_flow(game_key='estimation', room_code=quiz.room_code, session_code=code, question_id=question.pk)
    from .progress import question_progress, scoreboard
    event = {'type': 'question_ended', 'phase': 'question_result', 'revealed': True,
        'current_question_id': question.pk, 'question': {'id': question.pk},
        'round_number': session.current_question_number,
        'question_number': question_progress(quiz, code, question.pk)['current'],
        'message': 'Frage beendet.', 'is_tutorial_round': tutorial, 'max_points': point_limit,
        'correct_answer': {'correct_answer': question.correct_answer, 'formatted_answer': question.get_formatted_correct_answer(),
            'unit': question.unit, 'explanation': question.explanation,
            'scoring_mode': quiz.get_effective_scoring_mode(),
            'zone_scoring': question.get_zone_reveal_data() if quiz.get_effective_scoring_mode() == 'zones' else None},
        'rank_results': rank_results, 'evaluated_pending_answers': results,
        'scoreboard': scoreboard(quiz, code)}
    observe_snapshot('estimation', quiz.room_code, event, code)
    if emit:
        broadcast(quiz.room_code, event)
    return event


@transaction.atomic
def end_game(quiz_id, status='completed'):
    quiz = EstimationQuiz.objects.select_for_update().get(pk=quiz_id)
    code = scope_code(quiz)
    session = EstimationSession.objects.select_for_update().filter(quiz=quiz).first()
    if quiz.current_question_id and quiz.get_effective_scoring_mode() == 'rank':
        # Confirmed answers are scored; open drafts are deliberately discarded.
        rank_answers(quiz.room_code, quiz.pk, code)
    if session:
        session.end_current_question()
    quiz.status = status
    quiz.ended_at = timezone.now()
    quiz.current_question = None
    quiz.question_start_time = None
    quiz.save(update_fields=['status', 'ended_at', 'current_question', 'question_start_time', 'updated_at'])
    reset_question_flow(game_key='estimation', room_code=quiz.room_code, session_code=code, mode='manual_three_phase')


def reveal_state(room_code, participant_name, code):
    from .views import _get_last_revealed_estimation_question
    from .progress import metadata
    quiz = EstimationQuiz.objects.get(room_code=room_code)
    if quiz.status != 'active' or quiz.current_question_id:
        return None
    question, number = _get_last_revealed_estimation_question(quiz, code)
    if not question:
        return None
    answers = list(quiz.estimation_answers.filter(question=question, round_number=current_round(quiz),
        participant__hub_session_code=code).select_related('question', 'participant'))
    own = [serialize_answer(answer) for answer in answers if answer.participant.name == participant_name]
    ranks = None
    if quiz.get_effective_scoring_mode() == 'rank':
        answers.sort(key=lambda answer: (-answer.points_earned, answer.time_taken or 0, answer.pk))
        ranks = [{'participant_name': answer.participant.name, 'points_earned': answer.points_earned,
            'rank_position': index} for index, answer in enumerate(answers, 1)]
    from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids
    tutorial = question.pk in get_scorebox_excluded_tutorial_question_ids('estimation', room_code, code)
    return {'type': 'question_ended', 'restored': True, 'current_question_id': question.pk, 'question_number': number,
        'round_number': current_round(quiz), 'is_tutorial_round': tutorial,
        'evaluated_pending_answers': own, 'rank_results': ranks,
        'max_points': 0 if tutorial else max_points(quiz, question, code, current_round(quiz)),
        'correct_answer': {'correct_answer': question.correct_answer, 'formatted_answer': question.get_formatted_correct_answer(),
            'explanation': question.explanation, 'scoring_mode': quiz.get_effective_scoring_mode(),
            'zone_scoring': question.get_zone_reveal_data() if quiz.get_effective_scoring_mode() == 'zones' else None},
        'scoreboard': metadata(quiz, code)['scoreboard']}
