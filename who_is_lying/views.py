import json
import logging
from urllib.parse import urlencode

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.urls import reverse
from .models import (
    WhoQuiz,
    WhoQuestion,
    WhoParticipant,
    WhoAnswer,
    WhoSession,
    get_question_timer_state,
    ensure_who_participant_for_hub,
)
from games_hub.authoritative_state import current_snapshot, validate_and_reserve_action
from games_hub.models import GameRuntimeState
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids, is_current_unit_tutorial_question


logger = logging.getLogger(__name__)


def _get_authorized_hub_who_context(quiz, participant_name, session_code, request=None):
    """Resolve a current server-side hub identity; intro query values are only consistency checks."""
    from games_hub.models import HubParticipant, HubSession

    normalized_session = str(session_code or '').strip()
    normalized_name = str(participant_name or '').strip()
    if not normalized_session or not normalized_name or quiz.status != 'active':
        return None

    session = HubSession.objects.filter(
        code=normalized_session,
        is_active=True,
        ended_at__isnull=True,
    ).first()
    if not session:
        return None

    ordered_steps = list(session.steps.order_by('order', 'id'))
    if not ordered_steps:
        return None
    current_step_index = max(0, min(session.current_step_index, len(ordered_steps) - 1))
    step = ordered_steps[current_step_index]
    if step.game_key != 'who' or step.room_code != quiz.room_code:
        return None

    if request is not None:
        requested_key = (request.GET.get('game_start_key') or '').strip()
        requested_room = (request.GET.get('game_start_room') or '').strip()
        if requested_key and requested_key != 'who':
            return None
        if requested_room and requested_room != quiz.room_code:
            return None

    hub_participant = HubParticipant.objects.filter(
        session=session,
        nickname=normalized_name,
        left_permanently_at__isnull=True,
        check_in_excluded_by_host=False,
    ).first()
    if not hub_participant:
        return None

    snapshots = step.participant_snapshots.all()
    if snapshots.exists() and not snapshots.filter(
        participant=hub_participant,
        active_player=True,
    ).exists():
        return None

    return session, step, hub_participant


def _redirect_from_invalid_who_play(session_code, participant_name):
    from games_hub.models import HubSession

    if session_code and HubSession.objects.filter(code=session_code, ended_at__isnull=True).exists():
        lobby_url = reverse('games_hub:lobby', args=[session_code])
        query = urlencode({
            'nickname': participant_name,
            'who_join_error': 'Teilnahme am aktuellen Spiel konnte nicht bestaetigt werden.',
        })
        return redirect(f'{lobby_url}?{query}')
    if session_code:
        return redirect('games_hub:join_session')
    return redirect('who_is_lying:join')


def _get_ordered_quiz_questions(quiz, session_code=None):
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('who', quiz.room_code, session_code)
    selected_questions = list(quiz.selected_questions.all())
    configured_order_ids = []
    for raw_question_id in quiz.question_order or []:
        try:
            configured_order_ids.append(int(raw_question_id))
        except (TypeError, ValueError):
            continue
    if configured_order_ids:
        order_map = {question_id: index for index, question_id in enumerate(configured_order_ids)}
        selected_questions.sort(key=lambda question: order_map.get(question.id, len(configured_order_ids)))

    fallback_questions = {}
    played_questions = []
    seen_played_ids = set()
    answers_qs = WhoAnswer.objects.filter(quiz=quiz).select_related('question').order_by('submitted_at', 'id')
    if session_code:
        answers_qs = answers_qs.filter(participant__hub_session_code=session_code)
    for answer in answers_qs:
        if answer.question_id in tutorial_question_ids:
            continue
        fallback_questions[answer.question_id] = answer.question
        if answer.question_id in seen_played_ids:
            continue
        played_questions.append(answer.question)
        seen_played_ids.add(answer.question_id)

    if quiz.current_question_id and quiz.current_question_id not in fallback_questions and quiz.current_question and quiz.current_question_id not in tutorial_question_ids:
        fallback_questions[quiz.current_question_id] = quiz.current_question

    configured_by_id = {question.id: question for question in selected_questions}

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

    for question in selected_questions:
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


def _build_participant_progress_history(quiz, participant, session_code=None):
    ordered_questions = _get_ordered_quiz_questions(quiz, session_code)
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('who', quiz.room_code, session_code)
    answers_by_question_id = {
        answer.question_id: answer
        for answer in WhoAnswer.objects.filter(quiz=quiz, participant=participant).select_related('question')
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
            'max_points': question.get_total_possible_points(),
        })
    return history


def _build_question_scoreboard(quiz, participant, session_code=None):
    ordered_questions = _get_ordered_quiz_questions(quiz, session_code)
    history = _build_participant_progress_history(quiz, participant, session_code)
    history_by_question_id = {entry['question_id']: entry for entry in history}

    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('who', quiz.room_code, session_code)
    current_question_id = quiz.current_question_id if quiz.current_question_id not in tutorial_question_ids else None
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
            max_points = None
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


def who_join_view(request):
    """Combined view for who is lying quiz join page (GET) and join action (POST)"""
    if request.method == 'GET':
        return render(request, 'who_is_lying/join.html')
    
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
            
            room_code_length = WhoQuiz._meta.get_field('room_code').max_length
            if len(room_code) != room_code_length:
                return JsonResponse({
                    'success': False,
                    'error': f'Room code must be exactly {room_code_length} characters.'
                })
            
            # Get quiz
            try:
                quiz = WhoQuiz.objects.get(room_code=room_code)
            except WhoQuiz.DoesNotExist:
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

            if hub_session:
                hub_context = _get_authorized_hub_who_context(
                    quiz,
                    participant_name,
                    hub_session,
                )
                if not hub_context:
                    return JsonResponse({
                        'success': False,
                        'error': 'The current hub game could not authorize this participant.',
                    }, status=403)

                session, _step, hub_participant = hub_context
                participant = ensure_who_participant_for_hub(
                    quiz,
                    hub_participant.nickname,
                    session.code,
                )
                return JsonResponse({
                    'success': True,
                    'participant_id': participant.id,
                    'quiz_status': quiz.status,
                })

            # Standalone joins retain the existing room-capacity and name checks.
            current_count = quiz.get_participant_count()
            if current_count >= quiz.max_participants:
                return JsonResponse({
                    'success': False,
                    'error': 'This quiz is full. Maximum participants reached.'
                })
            
            # Check if name is already taken in this standalone quiz.
            name_qs = quiz.participants.filter(name__iexact=participant_name)
            if name_qs.exists():
                return JsonResponse({
                    'success': False,
                    'error': 'This name is already taken in this quiz. Please choose another name.'
                })
            
            participant, created = WhoParticipant.objects.get_or_create(
                quiz=quiz,
                name=participant_name,
                defaults={'is_active': True}
            )
            
            if not created:
                # Reactivate existing participant
                participant.is_active = True
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
        except Exception:
            logger.exception('Who join failed unexpectedly')
            return JsonResponse({
                'success': False,
                'error': 'An error occurred. Please try again.'
            }, status=500)


def check_room_code(request, room_code):
    """Check if room code is valid and return quiz info"""
    try:
        quiz = WhoQuiz.objects.get(room_code=room_code)
        
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
    except WhoQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Invalid room code. Please check and try again.'
        })


def who_play(request, room_code, participant_name):
    """Who is lying quiz play page for participants"""
    try:
        session_code = (request.GET.get('hub_session') or '').strip() or None
        quiz = WhoQuiz.objects.filter(room_code=room_code).first()
        if not quiz:
            logger.warning(
                'Who play rejected: room not found',
                extra={
                    'who_room_code': room_code,
                    'hub_session_code': session_code,
                    'game_start_nonce': request.GET.get('game_start_nonce'),
                    'participant_name': participant_name,
                },
            )
            return _redirect_from_invalid_who_play(session_code, participant_name)

        participant = WhoParticipant.objects.filter(
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code,
        ).first()
        has_game_start_context = any(
            request.GET.get(key)
            for key in (
                'game_start_intro',
                'game_start_key',
                'game_start_room',
                'game_start_nonce',
                'game_start_order',
            )
        )
        from games_hub.models import HubSession
        is_known_hub_session = bool(session_code) and HubSession.objects.filter(code=session_code).exists()
        hub_context = None
        if session_code and (participant is None or has_game_start_context or is_known_hub_session):
            hub_context = _get_authorized_hub_who_context(
                quiz,
                participant_name,
                session_code,
                request=request,
            )
            if not hub_context:
                logger.warning(
                    'Who play rejected: hub game context is not current',
                    extra={
                        'who_quiz_id': quiz.id,
                        'who_room_code': room_code,
                        'who_status': quiz.status,
                        'hub_session_code': session_code,
                        'game_start_nonce': request.GET.get('game_start_nonce'),
                        'game_start_order': request.GET.get('game_start_order'),
                        'participant_name': participant_name,
                    },
                )
                return _redirect_from_invalid_who_play(session_code, participant_name)

        if participant is None:
            if not hub_context:
                logger.warning(
                    'Who play rejected: participant binding missing',
                    extra={
                        'who_quiz_id': quiz.id,
                        'who_room_code': room_code,
                        'who_status': quiz.status,
                        'hub_session_code': session_code,
                        'game_start_nonce': request.GET.get('game_start_nonce'),
                        'participant_name': participant_name,
                    },
                )
                return _redirect_from_invalid_who_play(session_code, participant_name)

            session, _step, hub_participant = hub_context
            participant = ensure_who_participant_for_hub(
                quiz,
                hub_participant.nickname,
                session.code,
            )
        
        was_active = participant.is_active
        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()
        logger.info(
            'Who play authorized',
            extra={
                'hub_session_code': session_code,
                'hub_participant_id': hub_context[2].id if hub_context else None,
                'who_participant_id': participant.id,
                'who_quiz_id': quiz.id,
                'who_room_code': room_code,
                'game_start_nonce': request.GET.get('game_start_nonce'),
                'participant_was_active': was_active,
                'participant_is_active': participant.is_active,
            },
        )

        question_scoreboard, initial_progress_history, current_question_number = _build_question_scoreboard(
            quiz,
            participant,
            session_code,
        )
        
        current_question_timer_state = None
        current_question_people = []
        current_question_started_at = None
        current_question_end_time = None
        server_now = timezone.now()
        question_runtime = current_snapshot('who', room_code, session_code)
        manual_question_flow = (
            question_runtime.get('question_flow_mode')
            == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        )
        question_phase = question_runtime.get('question_phase')
        question_answering_open = (
            not manual_question_flow
            or question_phase == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
        )
        question_visible_at = parse_datetime(
            question_runtime.get('question_visible_at') or ''
        )
        question_prompt_visible = bool(quiz.current_question) and (
            question_answering_open
            or (
                question_phase == GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
                and (not question_visible_at or server_now >= question_visible_at)
            )
        )
        if quiz.current_question:
            all_question_people = quiz.current_question.get_randomized_people(room_code=quiz.room_code).get('people', [])
            current_question_people = all_question_people if question_answering_open else []
            try:
                quiz_session = quiz.session
            except WhoSession.DoesNotExist:
                quiz_session = None
            current_question_timer_state = get_question_timer_state(
                quiz.current_question,
                question_start_time=quiz.question_start_time,
                question_end_time=quiz_session.question_end_time if quiz_session else None,
                people_count=len(all_question_people),
                server_now=server_now,
            )
            current_question_started_at = quiz.question_start_time
            current_question_end_time = quiz_session.question_end_time if quiz_session else None

        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.get_participant_count(session_code),
            'question_scoreboard': question_scoreboard,
            'initial_progress_history': initial_progress_history,
            'current_question_number': current_question_number,
            'total_sets': len(question_scoreboard),
            'current_question_people': current_question_people,
            'current_question_timer_state': current_question_timer_state,
            'current_question_started_at': current_question_started_at,
            'current_question_end_time': current_question_end_time,
            'who_timer_server_now': server_now,
            'question_runtime': question_runtime,
            'question_prompt_visible': question_prompt_visible,
            'question_answering_open': question_answering_open,
            'current_unit_is_tutorial': is_current_unit_tutorial_question('who', quiz.room_code, session_code, quiz.current_question_id),
        }
        return render(request, 'who_is_lying/play.html', context)
        
    except (WhoQuiz.DoesNotExist, WhoParticipant.DoesNotExist):
        return redirect('who_is_lying:join')


def who_result(request, room_code, participant_name):
    """Who is lying quiz result page for participants"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoParticipant, 
            quiz=quiz, 
            name=participant_name
        )
        
        # Get participant's answers
        participant_answers = WhoAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('submitted_at')
        
        # Process each answer with detailed analysis
        processed_answers = []
        for answer in participant_answers:
            question = answer.question
            selected_liars = answer.selected_liars or []
            analysis = answer.get_detailed_analysis()
            
            processed_answers.append({
                'answer': answer,
                'analysis': analysis,
                'selected_liars_names': answer.get_selected_liars_names(),
                'actual_liars_names': answer.get_actual_liars_names(),
                'question': question
            })
        
        # Calculate statistics
        total_answers = participant_answers.count()
        total_correct = sum(answer.get_correct_identifications_count() for answer in participant_answers)
        total_possible = sum(answer.get_total_people_count() for answer in participant_answers)
        
        accuracy_percentage = 0
        if total_possible > 0:
            accuracy_percentage = round((total_correct / total_possible) * 100, 1)
        
        # Get participant rank
        participant_rank = participant.get_rank()
        
        # Get leaderboard (top 10)
        leaderboard = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        # Calculate performance insights
        average_time = None
        fastest_answer = None
        if participant_answers.exists():
            times = [answer.time_taken for answer in participant_answers if answer.time_taken]
            if times:
                average_time = sum(times) / len(times)
                fastest_answer = min(times)
        
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
            'participant_answers': participant_answers,  # Keep original for backward compatibility
            'processed_answers': processed_answers,      # Add processed version
            'total_correct': total_correct,
            'total_possible': total_possible,
            'accuracy_percentage': accuracy_percentage,
            'participant_rank': participant_rank,
            'leaderboard': leaderboard,
            'total_participants': quiz.get_participant_count(),
            'average_time': average_time,
            'fastest_answer': fastest_answer,
            'quiz_duration': quiz_duration,
            'quiz_duration_formatted': quiz_duration_formatted,
        }
        return render(request, 'who_is_lying/result.html', context)
        
    except (WhoQuiz.DoesNotExist, WhoParticipant.DoesNotExist):
        return redirect('who_is_lying:join')


@require_POST
@csrf_exempt
def submit_answer(request, room_code, participant_name):
    """Submit an answer for the current question"""
    try:
        session_code = request.GET.get('hub_session')
        data = json.loads(request.body)
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )

        if quiz.status != 'active':
            return JsonResponse({
                'success': False,
                'error': 'No active question available.'
            })

        submitted_question_id = data.get('question_id')
        if not quiz.current_question_id:
            return JsonResponse({
                'success': False,
                'error': 'No active question available.'
            })
        if submitted_question_id is not None and str(submitted_question_id) != str(quiz.current_question_id):
            return JsonResponse({
                'success': False,
                'error': 'The active question has changed.'
            })
        target_question = quiz.current_question

        # Check if participant has already answered this question
        existing_answer = WhoAnswer.objects.filter(
            quiz=quiz,
            participant=participant,
            question=target_question
        ).first()
        
        if existing_answer:
            return JsonResponse({
                'success': False,
                'error': 'You have already answered this question.'
            })

        selected_liars = data.get('selected_liars', [])

        decision = validate_and_reserve_action(
            game_key='who',
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
            locked_quiz = WhoQuiz.objects.select_for_update().get(pk=quiz.pk)
            locked_session = WhoSession.objects.select_for_update().filter(quiz=locked_quiz).first()
            received_at = timezone.now()
            if (
                locked_quiz.status != 'active'
                or locked_quiz.current_question_id != target_question.id
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
            answer, created = WhoAnswer.objects.get_or_create(
                quiz=locked_quiz,
                participant=participant,
                question=target_question,
                defaults={
                    'selected_liars': selected_liars,
                    'time_taken': server_time_taken,
                },
            )
            if not created:
                return JsonResponse({
                    'success': False,
                    'error': 'You have already answered this question.'
                })
        
        # Update participant's last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        # Get detailed analysis
        analysis = answer.get_detailed_analysis()
        
        return JsonResponse({
            'success': True,
            'points_earned': answer.points_earned,
            'correct_identifications': answer.get_correct_identifications_count(),
            'total_people': answer.get_total_people_count(),
            'accuracy': answer.get_accuracy_percentage(),
            'analysis': analysis,
            'selected_liars_names': answer.get_selected_liars_names(),
            'actual_liars_names': answer.get_actual_liars_names()
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
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoParticipant, 
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
            randomized_people = question.get_randomized_people(room_code=quiz.room_code)
            
            status_data['current_question'] = {
                'id': question.id,
                'statement': question.statement,
                'time_limit': question.time_limit,
                'people': randomized_people['people'],
                'total_possible_points': question.get_total_possible_points()
            }
            
            # Check if user has already answered
            has_answered = WhoAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question
            ).exists()
            status_data['has_answered'] = has_answered
        
        return JsonResponse({
            'success': True,
            **status_data
        })
        
    except (WhoQuiz.DoesNotExist, WhoParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


def leave_quiz(request, room_code, participant_name):
    """Leave a quiz session"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as inactive instead of deleting
        participant.is_active = False
        participant.save()
        
        return JsonResponse({'success': True})
        
    except (WhoQuiz.DoesNotExist, WhoParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


# API endpoints for real-time updates
def api_quiz_participants(request, room_code):
    """Get current participants for a quiz (public endpoint)"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
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
        
    except WhoQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


def api_quiz_leaderboard(request, room_code):
    """Get leaderboard for a quiz"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Get top 10 participants
        participants = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        leaderboard_data = []
        for rank, participant in enumerate(participants, 1):
            leaderboard_data.append({
                'rank': rank,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'accuracy': participant.get_average_accuracy()
            })
        
        return JsonResponse({
            'success': True,
            'leaderboard': leaderboard_data
        })
        
    except WhoQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


# Utility functions for WebSocket consumers
def get_quiz_statistics(quiz):
    """Get comprehensive quiz statistics"""
    participants = quiz.participants.all()
    answers = WhoAnswer.objects.filter(quiz=quiz)
    
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
        
        # Calculate overall accuracy
        if answers.exists():
            total_accuracy = sum(answer.get_accuracy_percentage() for answer in answers)
            stats['average_accuracy'] = total_accuracy / answers.count()
    
    return stats
