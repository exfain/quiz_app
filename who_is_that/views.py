from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Avg, Count, Q
import json
from .models import WhoThatQuiz, WhoThatQuestion, WhoThatParticipant, WhoThatAnswer, WhoThatSession


def _get_current_question_time_context(quiz):
    session = getattr(quiz, 'session', None)
    current_question_time_left = None
    current_question_end_time = None
    if quiz.current_question:
        current_question_time_left = quiz.current_question.time_limit
        if session and session.question_end_time:
            current_question_end_time = session.question_end_time
            current_question_time_left = max(
                0,
                int((session.question_end_time - timezone.now()).total_seconds() + 0.999),
            )
    return current_question_time_left, current_question_end_time


def _get_current_run_answers_queryset(quiz, participant=None):
    answers = WhoThatAnswer.objects.filter(quiz=quiz)
    if participant is not None:
        answers = answers.filter(participant=participant)
    if quiz.status == 'waiting':
        return answers.none()
    if quiz.started_at:
        answers = answers.filter(submitted_at__gte=quiz.started_at)
    return answers


def _has_current_run_session_progress(quiz, session, has_current_run_answers=False):
    if not session or quiz.status == 'waiting':
        return False

    if quiz.current_question_id:
        return True

    if has_current_run_answers:
        return True

    if not quiz.started_at:
        return False

    if not (session.current_question_number or session.total_questions_sent):
        return False

    if session.is_question_active or session.question_end_time:
        return True

    if not session.updated_at:
        return False

    return session.updated_at > quiz.started_at


def _get_ordered_quiz_questions(quiz):
    selected_questions = list(quiz.selected_questions.all())
    configured_order_ids = []
    for raw_question_id in getattr(quiz, 'question_order', []) or []:
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
    for answer in _get_current_run_answers_queryset(quiz).select_related('question').order_by('submitted_at', 'id'):
        fallback_questions[answer.question_id] = answer.question
        if answer.question_id in seen_played_ids:
            continue
        played_questions.append(answer.question)
        seen_played_ids.add(answer.question_id)

    if quiz.current_question_id and quiz.current_question_id not in fallback_questions and quiz.current_question:
        fallback_questions[quiz.current_question_id] = quiz.current_question

    configured_by_id = {question.id: question for question in selected_questions}

    ordered_questions = []
    seen_ids = set()

    for question in played_questions:
        resolved_question = configured_by_id.get(question.id, fallback_questions.get(question.id, question))
        if resolved_question and resolved_question.id not in seen_ids:
            ordered_questions.append(resolved_question)
            seen_ids.add(resolved_question.id)

    if quiz.current_question_id and quiz.current_question_id not in seen_ids:
        current_question = configured_by_id.get(quiz.current_question_id, quiz.current_question)
        if current_question:
            ordered_questions.append(current_question)
            seen_ids.add(quiz.current_question_id)

    for question in selected_questions:
        if question.id not in seen_ids:
            ordered_questions.append(question)
            seen_ids.add(question.id)

    for question_id, question in fallback_questions.items():
        if question_id not in seen_ids:
            ordered_questions.append(question)
            seen_ids.add(question_id)

    return ordered_questions


def _build_question_status_board(quiz, participant):
    ordered_questions = _get_ordered_quiz_questions(quiz)
    session = getattr(quiz, 'session', None)
    has_current_run_answers = _get_current_run_answers_queryset(quiz).exists()
    has_session_progress = _has_current_run_session_progress(
        quiz,
        session,
        has_current_run_answers=has_current_run_answers,
    )
    current_question_number = session.current_question_number if session and has_session_progress else 0
    current_question_id = quiz.current_question_id
    current_question_active = bool(session and session.is_question_active and current_question_id and has_session_progress)

    if quiz.status == 'waiting':
        current_question_number = 0
        current_question_id = None
        current_question_active = False

    answers_by_question_id = {
        answer.question_id: answer
        for answer in _get_current_run_answers_queryset(
            quiz,
            participant=participant,
        ).select_related('question')
    }

    total_questions = len(ordered_questions)
    if total_questions == 0:
        total_questions = max(current_question_number, len(answers_by_question_id))

    revealed_question_count = 0
    if current_question_active:
        revealed_question_count = max(current_question_number - 1, 0)
    elif has_session_progress and current_question_number > 0 and not current_question_id:
        revealed_question_count = current_question_number

    board = []
    for index in range(total_questions):
        question = ordered_questions[index] if index < len(ordered_questions) else None
        question_number = index + 1
        answer = answers_by_question_id.get(question.id) if question else None
        is_current = bool(
            current_question_active and
            question and
            question.id == current_question_id
        )

        if answer and not is_current:
            result = 'correct' if answer.is_correct else 'incorrect'
        elif revealed_question_count and question_number <= revealed_question_count and not is_current:
            result = 'incorrect'
        else:
            result = None

        board.append({
            'number': question_number,
            'id': question.id if question else None,
            'is_current': is_current,
            'result': result,
            'solution_text': question.correct_answer if question and result else '',
            'earned_points': (
                (1 if answer.is_correct else 0)
                if answer and not is_current else
                (0 if result else None)
            ),
            'max_points': 1,
        })

    return board, current_question_number



@csrf_exempt
def who_that_join_view(request):
    """Combined view for Who is That quiz join page (GET) and join action (POST)"""
    if request.method == 'GET':
        return render(request, 'who_is_that/join.html')
    
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
                quiz = WhoThatQuiz.objects.get(room_code=room_code)
            except WhoThatQuiz.DoesNotExist:
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
                participant, created = WhoThatParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': True}
                )
            else:
                participant, created = WhoThatParticipant.objects.get_or_create(
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
                'error': 'An error occurred. Please try again.' + str(e)
            })


@csrf_exempt
def check_room_code(request, room_code):
    """Check if room code is valid and return quiz info"""
    try:
        quiz = WhoThatQuiz.objects.get(room_code=room_code)
        
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
    except WhoThatQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Invalid room code. Please check and try again.'
        })


def who_that_play(request, room_code, participant_name):
    """Who is That quiz play page for participants"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoThatParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as active
        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()
        
        current_question_time_left, current_question_end_time = _get_current_question_time_context(quiz)
        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.get_participant_count(session_code),
            'current_question_time_left': current_question_time_left,
            'current_question_end_time': current_question_end_time,
        }
        context['current_participant_answer'] = (
            _get_current_run_answers_queryset(
                quiz,
                participant=participant,
            ).filter(question_id=quiz.current_question_id).first()
            if quiz.current_question_id else
            None
        )
        question_status_board, current_question_number = _build_question_status_board(quiz, participant)
        context['question_status_board'] = question_status_board
        context['current_question_number'] = current_question_number
        return render(request, 'who_is_that/play.html', context)
        
    except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
        return redirect('who_is_that:join')


def who_that_result(request, room_code, participant_name):
    """Who is That quiz result page for participants"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoThatParticipant, 
            quiz=quiz, 
            name=participant_name
        )
        
        # Get participant's answers
        participant_answers = WhoThatAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('submitted_at')
        
        # Calculate statistics
        total_answers = participant_answers.count()
        correct_answers = participant_answers.filter(is_correct=True).count()
        total_score = participant.total_score
        
        # Get participant rank
        participant_rank = participant.get_rank()
        
        # Get leaderboard (top 10)
        leaderboard = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        # Calculate performance insights
        average_time = None
        fastest_answer = None
        accuracy_percentage = None
        best_match_quality = None
        
        if participant_answers.exists():
            times = [answer.time_taken for answer in participant_answers if answer.time_taken]
            if times:
                average_time = sum(times) / len(times)
                fastest_answer = min(times)
            
            # Calculate accuracy
            if total_answers > 0:
                accuracy_percentage = (correct_answers / total_answers) * 100
            
            # Get best match quality
            best_answer = participant_answers.order_by('-points_earned').first()
            if best_answer:
                best_match_quality = best_answer.get_match_quality()
        
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
            'correct_answers': correct_answers,
            'total_score': total_score,
            'participant_rank': participant_rank,
            'leaderboard': leaderboard,
            'total_participants': quiz.get_participant_count(),
            'average_time': average_time,
            'fastest_answer': fastest_answer,
            'accuracy_percentage': accuracy_percentage,
            'best_match_quality': best_match_quality,
            'quiz_duration': quiz_duration,
            'quiz_duration_formatted': quiz_duration_formatted,
        }
        return render(request, 'who_is_that/result.html', context)
        
    except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
        return redirect('who_is_that:join')


@require_POST
@csrf_exempt
def submit_answer(request, room_code, participant_name):
    """Submit an answer for the current question"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoThatParticipant, 
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
        existing_answer = WhoThatAnswer.objects.filter(
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
        user_answer = data.get('user_answer', '').strip()
        time_taken = data.get('time_taken', 0)
        
        if not user_answer:
            return JsonResponse({
                'success': False,
                'error': 'Please provide an answer before submitting.'
            })
        
        # Create answer
        answer = WhoThatAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            user_answer=user_answer,
            time_taken=time_taken
        )
        
        # Record statistics in session
        if hasattr(quiz, 'session'):
            quiz.session.record_answer(answer.is_correct, time_taken)
        
        # Update participant's last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        return JsonResponse({
            'success': True,
            'is_correct': answer.is_correct,
            'points_earned': answer.points_earned,
            'accuracy_percentage': answer.get_accuracy_percentage(),
            'match_quality': answer.get_match_quality()
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
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoThatParticipant, 
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
            'questions_answered': participant.questions_answered,
            'correct_answers': participant.correct_answers
        }
        
        # Include current question if active
        if quiz.current_question and quiz.status == 'active':
            question = quiz.current_question
            current_question_time_left, current_question_end_time = _get_current_question_time_context(quiz)
            status_data['current_question'] = {
                'id': question.id,
                'question_text': question.question_text,
                'image_url': question.image.url if question.image else None,
                'points': 1,
                'time_limit': question.time_limit,
                'time_left': current_question_time_left,
                'question_end_time': current_question_end_time.isoformat() if current_question_end_time else None,
                'hint_text': question.hint_text,
                'category': question.category,
            }
            
            # Check if user has already answered
            has_answered = WhoThatAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question
            ).exists()
            status_data['has_answered'] = has_answered
        
        return JsonResponse({
            'success': True,
            **status_data
        })
        
    except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


def leave_quiz(request, room_code, participant_name):
    """Leave a quiz session"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoThatParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as inactive instead of deleting
        participant.is_active = False
        participant.save()
        
        return JsonResponse({'success': True})
        
    except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


# API endpoints for real-time updates
def api_quiz_participants(request, room_code):
    """Get current participants for a quiz (public endpoint)"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        participants = quiz.participants.filter(is_active=True).order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'correct_answers': participant.correct_answers,
                'rank': participant.get_rank(),
                'accuracy_percentage': participant.get_accuracy_percentage(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data,
            'count': len(participants_data)
        })
        
    except WhoThatQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


def api_quiz_leaderboard(request, room_code):
    """Get leaderboard for a quiz"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        # Get top 10 participants
        participants = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        leaderboard_data = []
        for rank, participant in enumerate(participants, 1):
            leaderboard_data.append({
                'rank': rank,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'correct_answers': participant.correct_answers,
                'accuracy_percentage': participant.get_accuracy_percentage()
            })
        
        return JsonResponse({
            'success': True,
            'leaderboard': leaderboard_data
        })
        
    except WhoThatQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


# Utility functions for WebSocket consumers
def get_quiz_statistics(quiz):
    """Get comprehensive quiz statistics"""
    participants = quiz.participants.all()
    answers = WhoThatAnswer.objects.filter(quiz=quiz)
    
    stats = {
        'total_participants': participants.count(),
        'active_participants': participants.filter(is_active=True).count(),
        'total_questions_sent': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'correct_answers': answers.filter(is_correct=True).count(),
        'average_score': 0,
        'average_accuracy': 0,
    }
    
    if participants.exists():
        scores = [p.total_score for p in participants]
        stats['average_score'] = sum(scores) / len(scores)
    
    if participants.filter(questions_answered__gt=0).exists():
        participants_with_answers = participants.filter(questions_answered__gt=0)
        accuracies = [p.get_accuracy_percentage() for p in participants_with_answers]
        stats['average_accuracy'] = sum(accuracies) / len(accuracies)
    
    return stats
