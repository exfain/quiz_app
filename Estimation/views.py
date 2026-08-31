from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import transaction
from django.db.models import Avg, Count, Q
import json
import math
from urllib.parse import urlencode
from .models import EstimationQuiz, EstimationQuestion, EstimationParticipant, EstimationAnswer, EstimationSession
from games_hub.authoritative_state import current_snapshot, validate_and_reserve_action
from games_hub.unit_tutorial_runtime import (
    get_scorebox_excluded_tutorial_question_ids,
    get_unit_tutorial_state,
    is_current_unit_tutorial_question,
    is_unit_tutorial_question,
)
from games_hub.models import GameRuntimeState, HubGameStep


def _get_run_tutorial_question_id(quiz, session_code=None):
    state = get_unit_tutorial_state('estimation', quiz.room_code, session_code)
    if not state.get('requested'):
        return getattr(quiz, 'tutorial_question_id', None)
    return state.get('tutorial_question_id')


def _get_ordered_quiz_questions(quiz, session_code=None):
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('estimation', quiz.room_code, session_code)
    configured_questions = list(quiz.selected_questions.all())
    configured_order_ids = []
    for raw_question_id in quiz.question_order or []:
        try:
            configured_order_ids.append(int(raw_question_id))
        except (TypeError, ValueError):
            continue
    if configured_order_ids:
        order_map = {question_id: index for index, question_id in enumerate(configured_order_ids)}
        configured_questions.sort(key=lambda question: order_map.get(question.id, len(configured_order_ids)))

    answers_qs = EstimationAnswer.objects.filter(quiz=quiz).select_related('question').order_by('submitted_at', 'id')
    if session_code:
        answers_qs = answers_qs.filter(participant__hub_session_code=session_code)

    fallback_questions = {}
    played_questions = []
    seen_played_ids = set()
    for answer in answers_qs:
        if answer.question_id in tutorial_question_ids:
            continue
        fallback_questions[answer.question_id] = answer.question
        if answer.question_id in seen_played_ids:
            continue
        played_questions.append(answer.question)
        seen_played_ids.add(answer.question_id)

    if quiz.current_question_id and quiz.current_question_id not in fallback_questions and quiz.current_question_id not in tutorial_question_ids:
        fallback_questions[quiz.current_question_id] = quiz.current_question

    configured_by_id = {question.id: question for question in configured_questions}

    ordered_questions = []
    seen_ids = set()

    for question in played_questions:
        resolved_question = configured_by_id.get(question.id, fallback_questions.get(question.id, question))
        if resolved_question and resolved_question.id not in seen_ids:
            ordered_questions.append(resolved_question)
            seen_ids.add(resolved_question.id)

    if quiz.current_question_id and quiz.current_question_id not in seen_ids and quiz.current_question_id not in tutorial_question_ids:
        current_question = configured_by_id.get(quiz.current_question_id, quiz.current_question)
        if current_question:
            ordered_questions.append(current_question)
            seen_ids.add(quiz.current_question_id)

    for question in configured_questions:
        if question.id in tutorial_question_ids:
            continue
        if question.id not in seen_ids:
            ordered_questions.append(question)
            seen_ids.add(question.id)

    for question_id, question in fallback_questions.items():
        if question_id not in seen_ids:
            ordered_questions.append(question)
            seen_ids.add(question_id)

    return ordered_questions


def _get_question_max_points_for_score_box(quiz, question, session_code=None):
    scoring_mode = quiz.get_effective_scoring_mode()
    if scoring_mode == 'rank':
        answer_qs = EstimationAnswer.objects.filter(quiz=quiz, question=question)
        if session_code:
            answer_qs = answer_qs.filter(participant__hub_session_code=session_code)
        played_max_points = answer_qs.order_by('-points_earned').values_list('points_earned', flat=True).first()
        if played_max_points is not None:
            return int(played_max_points)
    return question.get_max_points_for_mode(scoring_mode, quiz.get_participant_count(session_code))


def _build_participant_progress_history(quiz, participant, session_code=None):
    ordered_questions = _get_ordered_quiz_questions(quiz, session_code)
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('estimation', quiz.room_code, session_code)
    answers_by_question_id = {
        answer.question_id: answer
        for answer in EstimationAnswer.objects.filter(quiz=quiz, participant=participant).select_related('question')
        if answer.question_id not in tutorial_question_ids
    }

    history = []
    for index, question in enumerate(ordered_questions, start=1):
        answer = answers_by_question_id.get(question.id)
        if not answer:
            continue
        history.append({
            'question_id': question.id,
            'question_number': index,
            'points': int(answer.points_earned or 0),
            'max_points': _get_question_max_points_for_score_box(quiz, question, session_code),
        })
    return history


def _build_question_scoreboard(quiz, participant, session_code=None):
    ordered_questions = _get_ordered_quiz_questions(quiz, session_code)
    history = _build_participant_progress_history(quiz, participant, session_code)
    history_by_question_id = {entry['question_id']: entry for entry in history}

    current_question_id = quiz.current_question_id if quiz.current_question_id else None
    current_question_number = next(
        (index for index, question in enumerate(ordered_questions, start=1) if question.id == current_question_id),
        None,
    )
    if current_question_number is None:
        current_question_number = next(
            (
                index for index, question in enumerate(ordered_questions, start=1)
                if question.id not in history_by_question_id
            ),
            None,
        )

    scoreboard = []
    for index, question in enumerate(ordered_questions, start=1):
        history_entry = history_by_question_id.get(question.id)
        if history_entry:
            status = 'played'
            earned_points = history_entry['points']
            max_points = history_entry['max_points']
        elif current_question_id and current_question_number == index:
            status = 'current'
            earned_points = None
            max_points = _get_question_max_points_for_score_box(quiz, question, session_code)
        elif current_question_number == index:
            status = 'current'
            earned_points = None
            max_points = None
        else:
            status = 'upcoming'
            earned_points = None
            max_points = None

        scoreboard.append({
            'id': question.id,
            'number': index,
            'earned_points': earned_points,
            'max_points': max_points,
            'status': status,
        })

    return scoreboard, history, current_question_number


def _get_estimation_time_payload(quiz):
    session = getattr(quiz, 'session', None)
    now = timezone.now()
    remaining_seconds = 90
    if session and session.question_end_time:
        remaining_seconds = max(0, math.ceil((session.question_end_time - now).total_seconds()))

    elapsed_seconds = 0
    if quiz.question_start_time:
        elapsed_seconds = max(0, math.floor((now - quiz.question_start_time).total_seconds()))

    return {
        'time_limit': remaining_seconds,
        'elapsed_seconds': elapsed_seconds,
    }


def _serialize_estimation_answer(answer):
    if not answer:
        return None
    accuracy = answer.get_accuracy_percentage()
    return {
        'user_answer': answer.user_answer,
        'formatted_answer': answer.get_formatted_user_answer(),
        'points_earned': int(answer.points_earned or 0),
        'accuracy_percentage': round(accuracy, 2),
    }


def _serialize_estimation_question(
    quiz,
    question,
    participant,
    session_code=None,
    question_number=0,
    max_points=0,
    runtime=None,
):
    runtime = runtime or {}
    question_phase = runtime.get('question_phase')
    manual_flow = (
        runtime.get('question_flow_mode')
        == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
    )
    prompt_details_visible = (
        not manual_flow
        or question_phase in {
            GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
            GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE,
            GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN,
        }
    )
    timing = _get_estimation_time_payload(quiz) if not manual_flow else {}
    answer = EstimationAnswer.objects.filter(
        quiz=quiz,
        participant=participant,
        question=question,
    ).first()
    payload = {
        'id': question.id,
        'question_text': question.question_text,
        'unit': question.unit if prompt_details_visible else '',
        'unit_display': question.get_unit_display_text() if prompt_details_visible else '',
        'question_number': question_number or 0,
        'max_points': max_points,
        'hint_text': question.hint_text if prompt_details_visible else None,
        'time_limit': runtime.get('remaining_answer_time') or timing.get('time_limit', 0),
        'elapsed_seconds': (
            max(0, math.floor((timezone.now() - quiz.question_start_time).total_seconds()))
            if quiz.question_start_time else 0
        ),
        'starts_at': (
            runtime.get('answering_started_at')
            or (quiz.question_start_time.isoformat() if quiz.question_start_time else None)
        ),
        'ends_at': (
            runtime.get('answering_deadline_at')
            or (
                quiz.session.question_end_time.isoformat()
                if not manual_flow and getattr(quiz, 'session', None) and quiz.session.question_end_time
                else None
            )
        ),
        'server_now': runtime.get('server_now'),
        'remaining_seconds': runtime.get('remaining_answer_time') or timing.get('time_limit', 0),
        'question_phase': question_phase,
        'question_presented_at': runtime.get('question_presented_at'),
        'question_visible_at': runtime.get('question_visible_at'),
        'content_revealed_at': runtime.get('content_revealed_at'),
        'answering_started_at': runtime.get('answering_started_at'),
        'answering_deadline_at': runtime.get('answering_deadline_at'),
        'answering_allowed': bool(runtime.get('answering_allowed')),
        'timer_running': bool(runtime.get('timer_running')),
        'state_revision': runtime.get('state_revision'),
        'game_id': runtime.get('game_id'),
    }
    if answer:
        payload['has_answered'] = True
        payload['existing_answer'] = _serialize_estimation_answer(answer)
    else:
        payload['has_answered'] = False
        if (
            not manual_flow
            or question_phase == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
        ):
            try:
                pending = (quiz.session.pending_answers or {}).get(str(participant.id)) or {}
            except EstimationSession.DoesNotExist:
                pending = {}
            if str(pending.get('question_id') or '') == str(question.id):
                payload['pending_answer'] = pending.get('user_answer', '')
    return payload


def _serialize_estimation_correct_answer(quiz, question):
    scoring_mode = quiz.get_effective_scoring_mode()
    return {
        'correct_answer': question.correct_answer,
        'formatted_answer': question.get_formatted_correct_answer(),
        'unit': question.unit,
        'unit_display': question.get_unit_display_text(),
        'explanation': question.explanation,
        'scoring_mode': scoring_mode,
        'zone_scoring': question.get_zone_reveal_data() if scoring_mode == 'zones' else None,
    }


def _get_last_revealed_estimation_question(quiz, session_code=None):
    session = getattr(quiz, 'session', None)
    if (
        not session
        or session.is_question_active
        or session.current_question_number <= 0
        or session.total_questions_sent <= 0
    ):
        return None, 0

    runtime = current_snapshot('estimation', quiz.room_code, session_code)
    if (
        runtime.get('phase') not in {'question_result', 'revealed'}
        or str(runtime.get('game_id') or '') != str(quiz.pk)
    ):
        return None, 0

    revealed_question_id = str(
        runtime.get('current_question_id')
        or (runtime.get('question') or {}).get('id')
        or ''
    )
    if not revealed_question_id:
        return None, 0
    ordered_questions = _get_ordered_quiz_questions(quiz, session_code)
    for index, question in enumerate(ordered_questions, start=1):
        if str(question.id) == revealed_question_id:
            return question, index
    return None, 0


def _build_estimation_initial_state(quiz, participant, session_code, current_question_number, current_question_max_points):
    state = {
        'phase': 'waiting',
        'quiz_status': quiz.status,
        'participant_score': participant.total_score,
    }

    if quiz.status in ['completed', 'cancelled']:
        state['phase'] = 'finished'
        return state

    if quiz.status != 'active':
        return state

    if quiz.current_question:
        current_question = quiz.current_question
        runtime = current_snapshot('estimation', quiz.room_code, session_code)
        question_payload = _serialize_estimation_question(
            quiz,
            current_question,
            participant,
            session_code,
            question_number=current_question_number or 0,
            max_points=current_question_max_points or 0,
            runtime=runtime,
        )
        state['current_question'] = question_payload
        question_phase = runtime.get('question_phase')
        if (
            runtime.get('question_flow_mode')
            != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        ):
            state['phase'] = 'answered_waiting' if question_payload.get('has_answered') else 'answering'
            return state
        visible_at = parse_datetime(runtime.get('question_visible_at') or '')
        server_now = parse_datetime(runtime.get('server_now') or '') or timezone.now()
        if question_phase == GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE:
            state['phase'] = (
                'prompt_visible'
                if not visible_at or server_now >= visible_at
                else 'presentation_delay'
            )
        elif question_phase == GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE:
            state['phase'] = 'content_visible'
        elif question_phase == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN:
            state['phase'] = 'answered_waiting' if question_payload.get('has_answered') else 'answering'
        else:
            state['phase'] = 'waiting'
        return state

    revealed_question, revealed_question_number = _get_last_revealed_estimation_question(quiz, session_code)
    if not revealed_question:
        return state

    answer = EstimationAnswer.objects.filter(
        quiz=quiz,
        participant=participant,
        question=revealed_question,
    ).first()
    state.update({
        'phase': 'reveal',
        'revealed_question': {
            'id': revealed_question.id,
            'question_number': revealed_question_number,
            'unit_display': revealed_question.get_unit_display_text(),
            'max_points': _get_question_max_points_for_score_box(quiz, revealed_question, session_code),
        },
        'correct_answer': _serialize_estimation_correct_answer(quiz, revealed_question),
        'participant_answer': _serialize_estimation_answer(answer),
        'points_for_question': int(answer.points_earned or 0) if answer else 0,
    })
    return state


def estimation_join_view(request):
    """Combined view for estimation quiz join page (GET) and join action (POST)"""
    if request.method == 'GET':
        return render(request, 'estimation/join.html')
    
    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            participant_name = data.get('participant_name', '').strip()
            room_code = data.get('room_code', '').strip()
            hub_session = (data.get('hub_session') or '').strip() or None
            
            # Validate input
            if not participant_name or not room_code:
                return JsonResponse({
                    'success': False,
                    'error': 'Name and room code are required.'
                })
            
            if len(participant_name) > 50:
                return JsonResponse({
                    'success': False,
                    'error': 'Name must be 50 characters or less.'
                })
            
            if len(room_code) != 4 or not room_code.isdigit():
                return JsonResponse({
                    'success': False,
                    'error': 'Room code must be exactly 4 digits.'
                })
            
            # Get quiz
            try:
                quiz = EstimationQuiz.objects.get(room_code=room_code)
            except EstimationQuiz.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': 'Quiz not found. Please check the room code.'
                })
            
            # Check if quiz is joinable
            if quiz.status not in ['waiting', 'active', 'inactive']:
                return JsonResponse({
                    'success': False,
                    'error': 'This quiz is no longer accepting participants.'
                })
            
            # Check participant limit (scope by session if provided)
            if hub_session:
                current_count = quiz.participants.filter(hub_session_code=hub_session).count()
            else:
                current_count = quiz.get_participant_count()
            if current_count >= quiz.max_participants:
                return JsonResponse({
                    'success': False,
                    'error': 'This quiz is full. Maximum participants reached.'
                })
            
            # Check if name is already taken in this quiz (scope by session if provided)
            name_qs = quiz.participants.filter(name__iexact=participant_name)
            if hub_session:
                name_qs = name_qs.filter(hub_session_code=hub_session)
            if name_qs.exists():
                return JsonResponse({
                    'success': False,
                    'error': 'This name is already taken in this quiz. Please choose another name.'
                })
            
            # Create participant (scope by session if provided)
            if hub_session:
                participant, created = EstimationParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': True}
                )
            else:
                participant, created = EstimationParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    defaults={'is_active': True}
                )
            
            if not created:
                # Reactivate existing participant
                participant.is_active = True
                if hub_session and not participant.hub_session_code:
                    participant.hub_session_code = hub_session
                participant.save()
            
            return JsonResponse({
                'success': True,
                'participant_id': participant.id,
                'quiz_status': quiz.status
            })
            
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid request format.'
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': 'An error occurred. Please try again. ' + str(e)
            })


def check_room_code(request, room_code):
    """Check if room code is valid and return quiz info"""
    try:
        quiz = EstimationQuiz.objects.get(room_code=room_code)
        
        # Only allow joining waiting or active quizzes
        if quiz.status not in ['waiting', 'active', 'inactive']:
            return JsonResponse({
                'success': False,
                'error': 'This quiz is no longer accepting participants.'
            })
        
        return JsonResponse({
            'success': True,
            'quiz': {
                'id': quiz.id,
                'title': quiz.title,
                'room_code': quiz.room_code,
                'status': quiz.get_status_display(),
                'participant_count': quiz.get_participant_count(),
                'max_participants': quiz.max_participants,
            }
        })
    except EstimationQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Invalid room code. Please check and try again.'
        })


def estimation_play(request, room_code, participant_name):
    """Estimation quiz play page for participants"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        participant = get_object_or_404(
            EstimationParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )

        if session_code and not participant.is_active:
            lobby_url = reverse('games_hub:lobby', args=[session_code])
            return redirect(f"{lobby_url}?{urlencode({'nickname': participant.name, 'return': '1'})}")
        
        # Mark participant as active
        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()

        question_scoreboard, initial_progress_history, current_question_number = _build_question_scoreboard(
            quiz,
            participant,
            session_code,
        )
        
        is_current_tutorial = (
            quiz.current_question_id
            and _get_run_tutorial_question_id(quiz, session_code) == quiz.current_question_id
        )
        current_question_max_points = (
            0 if is_current_tutorial else
            _get_question_max_points_for_score_box(quiz, quiz.current_question, session_code)
            if quiz.current_question else 0
        )
        estimation_initial_state = _build_estimation_initial_state(
            quiz,
            participant,
            session_code,
            current_question_number or 0,
            current_question_max_points,
        )

        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.get_participant_count(session_code),
            'question_scoreboard': question_scoreboard,
            'initial_progress_history': initial_progress_history,
            'current_unit_is_tutorial': is_current_unit_tutorial_question('estimation', quiz.room_code, session_code, quiz.current_question_id),
            'current_question_number': current_question_number or 0,
            'current_question_max_points': current_question_max_points,
            'estimation_initial_state': estimation_initial_state,
            'initial_participant_phase': estimation_initial_state.get('phase', 'waiting'),
        }
        return render(request, 'estimation/play.html', context)
        
    except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
        return redirect('estimation:join')


def estimation_result(request, room_code, participant_name):
    """Estimation quiz result page for participants"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        session_code = request.GET.get('hub_session')
        if not session_code:
            session_steps = (
                HubGameStep.objects.select_related('session')
                .filter(game_key='estimation', room_code=room_code)
            )
            session_step = (
                session_steps.filter(session__ended_at__isnull=True).order_by('-id').first()
                or session_steps.order_by('-id').first()
            )
            session_code = session_step.session.code if session_step else None

        participant_scope = quiz.participants.all()
        if session_code:
            participant_scope = participant_scope.filter(hub_session_code=session_code)
        else:
            participant_scope = participant_scope.filter(hub_session_code__isnull=True)
        participant = get_object_or_404(
            participant_scope,
            name=participant_name,
        )
        
        # Get participant's answers
        participant_answers = EstimationAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('submitted_at')
        
        # Calculate statistics
        total_answers = participant_answers.count()
        total_score = participant.total_score
        
        # Get participant rank
        participant_rank = participant_scope.filter(total_score__gt=participant.total_score).count() + 1
        
        # Get leaderboard (top 10)
        leaderboard = participant_scope.order_by('-total_score', 'name')[:10]
        
        # Calculate performance insights
        average_time = None
        fastest_answer = None
        average_accuracy = None
        best_accuracy = None
        
        if participant_answers.exists():
            times = [answer.time_taken for answer in participant_answers if answer.time_taken]
            if times:
                average_time = sum(times) / len(times)
                fastest_answer = min(times)
            
            # Calculate accuracy statistics
            accuracies = [answer.get_accuracy_percentage() for answer in participant_answers]
            if accuracies:
                average_accuracy = sum(accuracies) / len(accuracies)
                best_accuracy = max(accuracies)
        
        # Calculate quiz duration
        quiz_duration = None
        quiz_duration_formatted = None
        if quiz.started_at and quiz.ended_at:
            quiz_duration = (quiz.ended_at - quiz.started_at).total_seconds()
            minutes = int(quiz_duration // 60)
            seconds = int(quiz_duration % 60)
            quiz_duration_formatted = f"{minutes}m {seconds}s"
        
        context = {
            'quiz': quiz,
            'participant': participant,
            'participant_answers': participant_answers,
            'total_answers': total_answers,
            'total_score': total_score,
            'participant_rank': participant_rank,
            'leaderboard': leaderboard,
            'total_participants': participant_scope.count(),
            'average_time': average_time,
            'fastest_answer': fastest_answer,
            'average_accuracy': average_accuracy,
            'best_accuracy': best_accuracy,
            'quiz_duration': quiz_duration,
            'quiz_duration_formatted': quiz_duration_formatted,
        }
        return render(request, 'estimation/result.html', context)
        
    except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
        return redirect('estimation:join')


@require_POST
@csrf_exempt
def submit_answer(request, room_code, participant_name):
    """Submit an answer for the current question"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        participant = get_object_or_404(
            EstimationParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Check if there's an active question
        if not quiz.current_question or quiz.status != 'active':
            return JsonResponse({
                'success': False,
                'error': 'No active question available.'
            })
        
        # Check if participant has already answered this question
        existing_answer = EstimationAnswer.objects.filter(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question
        ).first()
        
        if existing_answer:
            return JsonResponse({
                'success': False,
                'error': 'You have already answered this question.'
            })
        
        data = json.loads(request.body)
        user_answer = data.get('user_answer')
        if user_answer is None or user_answer == '':
            return JsonResponse({
                'success': False,
                'error': 'Please provide an answer before submitting.'
            })
        
        # Convert to float
        try:
            user_answer_float = float(user_answer)
        except (ValueError, TypeError):
            return JsonResponse({
                'success': False,
                'error': 'Please provide a valid number.'
            })
        
        question = quiz.current_question
        decision = validate_and_reserve_action(
            game_key='estimation',
            room_code=room_code,
            session_code=session_code,
            participant_name=participant_name,
            action_type='participant_submit_answer',
            action=data,
        )
        if not decision.accepted:
            return JsonResponse({
                'success': False,
                'type': 'action_rejected',
                'code': decision.code,
                'error': decision.message,
            }, status=409)
        with transaction.atomic():
            locked_quiz = EstimationQuiz.objects.select_for_update().get(pk=quiz.pk)
            locked_session = EstimationSession.objects.select_for_update().filter(quiz=locked_quiz).first()
            received_at = timezone.now()
            if (
                locked_quiz.status != 'active'
                or locked_quiz.current_question_id != question.id
                or not locked_session
                or not locked_session.is_question_active
                or not locked_session.question_end_time
                or received_at >= locked_session.question_end_time
            ):
                return JsonResponse({
                    'success': False,
                    'error': 'The answer deadline has expired.'
                })
            server_time_taken = (
                max(0.0, (received_at - locked_quiz.question_start_time).total_seconds())
                if locked_quiz.question_start_time
                else 0.0
            )
            answer, created = EstimationAnswer.objects.get_or_create(
                quiz=locked_quiz,
                participant=participant,
                question=question,
                defaults={
                    'user_answer': user_answer_float,
                    'time_taken': server_time_taken,
                },
            )
            if not created:
                return JsonResponse({
                    'success': False,
                    'error': 'You have already answered this question.'
                })
        is_tutorial_answer = is_unit_tutorial_question(
            'estimation',
            quiz.room_code,
            session_code,
            quiz.current_question_id,
        )
        if is_tutorial_answer and answer.points_earned:
            answer.points_earned = 0
            answer.save(update_fields=['points_earned', 'updated_at'])

        if hasattr(quiz, 'session'):
            pending_answers = dict(quiz.session.pending_answers or {})
            if pending_answers.pop(str(participant.id), None) is not None:
                quiz.session.pending_answers = pending_answers
                quiz.session.save(update_fields=['pending_answers', 'updated_at'])
        
        # Update participant's last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        return JsonResponse({
            'success': True,
            'points_earned': answer.points_earned,
            'is_tutorial_round': is_tutorial_answer,
            'accuracy_percentage': answer.get_accuracy_percentage(),
            'formatted_answer': answer.get_formatted_user_answer(),
            'percentage_difference': answer.get_percentage_difference(),
            'difference_indicator': answer.get_difference_indicator()
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid request format.'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': 'An error occurred while submitting your answer.'
        })


def get_quiz_status(request, room_code, participant_name):
    """Get current quiz status for participant"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        participant = get_object_or_404(
            EstimationParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Update last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        status_data = {
            'quiz_status': quiz.status,
            'current_question': None,
            'participant_score': participant.total_score,
            'participant_count': quiz.get_participant_count(),
            'questions_answered': participant.questions_answered
        }
        
        # Include current question if active
        if quiz.current_question and quiz.status == 'active':
            question = quiz.current_question
            runtime = current_snapshot('estimation', room_code, session_code)
            quiz_session = getattr(quiz, 'session', None)
            status_data['current_question'] = _serialize_estimation_question(
                quiz,
                question,
                participant,
                session_code,
                question_number=(quiz_session.current_question_number if quiz_session else 0),
                max_points=question.get_max_points_for_mode(
                    quiz.get_effective_scoring_mode(),
                    quiz.get_participant_count(session_code),
                ),
                runtime=runtime,
            )
            status_data['question_phase'] = runtime.get('question_phase')
            status_data['state_revision'] = runtime.get('state_revision')
            status_data['server_now'] = runtime.get('server_now')
            
            # Check if user has already answered
            has_answered = EstimationAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question
            ).exists()
            status_data['has_answered'] = has_answered
        
        return JsonResponse({
            'success': True,
            **status_data
        })
        
    except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


def leave_quiz(request, room_code, participant_name):
    """Leave a quiz session"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        participant = get_object_or_404(
            EstimationParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as inactive instead of deleting
        participant.is_active = False
        participant.save()
        
        return JsonResponse({'success': True})
        
    except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


# API endpoints for real-time updates
def api_quiz_participants(request, room_code):
    """Get current participants for a quiz (public endpoint)"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        participants = quiz.participants.filter(is_active=True).order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'rank': participant.get_rank(),
                'average_accuracy': participant.get_average_accuracy(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data,
            'count': len(participants_data)
        })
        
    except EstimationQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


def api_quiz_leaderboard(request, room_code):
    """Get leaderboard for a quiz"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Get top 10 participants
        participants = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        leaderboard_data = []
        for rank, participant in enumerate(participants, 1):
            leaderboard_data.append({
                'rank': rank,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'average_accuracy': participant.get_average_accuracy()
            })
        
        return JsonResponse({
            'success': True,
            'leaderboard': leaderboard_data
        })
        
    except EstimationQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


# Utility functions for WebSocket consumers
def get_quiz_statistics(quiz):
    """Get comprehensive quiz statistics"""
    participants = quiz.participants.all()
    answers = EstimationAnswer.objects.filter(quiz=quiz)
    
    stats = {
        'total_participants': participants.count(),
        'active_participants': participants.filter(is_active=True).count(),
        'total_questions_sent': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_score': 0,
        'average_accuracy': 0,
    }
    
    if participants.exists():
        scores = [p.total_score for p in participants]
        stats['average_score'] = sum(scores) / len(scores)
    
    if answers.exists():
        accuracies = [answer.get_accuracy_percentage() for answer in answers]
        stats['average_accuracy'] = sum(accuracies) / len(accuracies)
    
    return stats
