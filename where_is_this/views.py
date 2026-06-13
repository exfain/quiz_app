from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Avg, Count, Q
import json
from .models import WhereQuiz, WhereQuestion, WhereParticipant, WhereAnswer, WhereSession
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids, is_current_unit_tutorial_question


def _get_ordered_quiz_questions(quiz, session_code=None):
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('where', quiz.room_code, session_code)
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

    answer_qs = WhereAnswer.objects.filter(quiz=quiz).select_related('question').order_by('submitted_at', 'id')
    if session_code:
        answer_qs = answer_qs.filter(participant__hub_session_code=session_code)

    fallback_questions = {}
    played_questions = []
    seen_played_ids = set()
    for answer in answer_qs:
        if answer.question_id in tutorial_question_ids:
            continue
        fallback_questions[answer.question_id] = answer.question
        if answer.question_id in seen_played_ids:
            continue
        played_questions.append(answer.question)
        seen_played_ids.add(answer.question_id)

    if quiz.current_question_id and quiz.current_question_id not in fallback_questions and quiz.current_question_id not in tutorial_question_ids:
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


def _get_total_questions_sent(quiz):
    try:
        return max(0, int(quiz.session.total_questions_sent or 0))
    except WhereSession.DoesNotExist:
        distinct_answer_count = (
            WhereAnswer.objects.filter(quiz=quiz)
            .values('question_id')
            .distinct()
            .count()
        )
        if quiz.current_question_id:
            return distinct_answer_count + 1
        return distinct_answer_count


def _build_question_scoreboard(quiz, participant, session_code=None):
    ordered_questions = _get_ordered_quiz_questions(quiz, session_code)
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('where', quiz.room_code, session_code)
    participant_answers = {
        answer.question_id: answer
        for answer in (
            WhereAnswer.objects.filter(quiz=quiz, participant=participant)
            .select_related('question')
        )
        if answer.question_id not in tutorial_question_ids
    }
    asked_answer_qs = WhereAnswer.objects.filter(quiz=quiz)
    if session_code:
        asked_answer_qs = asked_answer_qs.filter(participant__hub_session_code=session_code)
    asked_question_ids = set(asked_answer_qs.exclude(question_id__in=tutorial_question_ids).values_list('question_id', flat=True))

    current_question_id = quiz.current_question_id if quiz.current_question_id not in tutorial_question_ids else None
    if current_question_id:
        asked_question_ids.add(current_question_id)

    total_questions_sent = _get_total_questions_sent(quiz)
    if total_questions_sent > len(asked_question_ids):
        for question in ordered_questions:
            if question.id in asked_question_ids:
                continue
            asked_question_ids.add(question.id)
            if len(asked_question_ids) >= total_questions_sent:
                break

    history = []
    scoreboard = []
    for index, question in enumerate(ordered_questions, start=1):
        answer = participant_answers.get(question.id)
        if answer:
            status = 'played'
            earned_points = int(answer.points_earned or 0)
            history.append({
                'question_id': question.id,
                'question_number': index,
                'points': earned_points,
                'max_points': int(question.points or 0),
            })
        elif quiz.status == 'active' and current_question_id == question.id:
            status = 'current'
            earned_points = None
        elif question.id in asked_question_ids:
            status = 'played'
            earned_points = 0
            history.append({
                'question_id': question.id,
                'question_number': index,
                'points': 0,
                'max_points': int(question.points or 0),
            })
        else:
            status = 'upcoming'
            earned_points = None

        scoreboard.append({
            'id': question.id,
            'number': index,
            'earned_points': earned_points,
            'max_points': int(question.points or 0),
            'status': status,
        })

    return scoreboard, history, current_question_id


def where_join_view(request):
    """Combined view for where quiz join page (GET) and join action (POST)"""
    if request.method == 'GET':
        return render(request, 'where_is_this/join.html')
    
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
                quiz = WhereQuiz.objects.get(room_code=room_code)
            except WhereQuiz.DoesNotExist:
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
                participant, created = WhereParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': True}
                )
            else:
                participant, created = WhereParticipant.objects.get_or_create(
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
                'error': 'An error occurred. Please try again.'
            })


def check_room_code(request, room_code):
    """Check if room code is valid and return quiz info"""
    try:
        quiz = WhereQuiz.objects.get(room_code=room_code)
        
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
    except WhereQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Invalid room code. Please check and try again.'
        })


def where_play(request, room_code, participant_name):
    """Where quiz play page for participants"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        print(participant_name, " ", session_code, " ", quiz)
        participant = get_object_or_404(
            WhereParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as active
        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()

        question_scoreboard, initial_progress_history, current_question_id = _build_question_scoreboard(
            quiz,
            participant,
            session_code,
        )
        
        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.get_participant_count(session_code),
            'question_scoreboard': question_scoreboard,
            'initial_progress_history': initial_progress_history,
            'current_question_id': current_question_id,
            'current_unit_is_tutorial': is_current_unit_tutorial_question('where', quiz.room_code, session_code, quiz.current_question_id),
            'score_total_earned': sum(entry['points'] for entry in initial_progress_history),
            'score_total_max': sum(entry['max_points'] for entry in initial_progress_history),
        }
        return render(request, 'where_is_this/play.html', context)
    
    except (WhereQuiz.DoesNotExist, WhereParticipant.DoesNotExist):
        return redirect('where_is_this:join') 


def where_result(request, room_code, participant_name):
    """Where quiz result page for participants"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhereParticipant, 
            quiz=quiz, 
            name=participant_name
        )
        
        # Get participant's answers
        participant_answers = WhereAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('submitted_at')
        
        # Calculate statistics
        total_answers = participant_answers.count()
        average_accuracy = participant.get_average_accuracy()
        
        # Get participant rank
        participant_rank = participant.get_rank()
        
        # Get leaderboard (top 10)
        leaderboard = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        # Calculate performance insights
        average_time = None
        fastest_answer = None
        total_distance = 0
        best_accuracy = 0
        
        if participant_answers.exists():
            times = [answer.time_taken for answer in participant_answers if answer.time_taken]
            if times:
                average_time = sum(times) / len(times)
                fastest_answer = min(times)
            
            total_distance = sum(answer.distance_km for answer in participant_answers)
            best_accuracy = max(answer.accuracy_percentage for answer in participant_answers)
        
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
            'average_accuracy': average_accuracy,
            'participant_rank': participant_rank,
            'leaderboard': leaderboard,
            'total_participants': quiz.get_participant_count(),
            'average_time': average_time,
            'fastest_answer': fastest_answer,
            'total_distance': total_distance,
            'best_accuracy': best_accuracy,
            'quiz_duration': quiz_duration,
            'quiz_duration_formatted': quiz_duration_formatted,
        }
        return render(request, 'where_is_this/results.html', context)
        
    except (WhereQuiz.DoesNotExist, WhereParticipant.DoesNotExist):
        return redirect('where_is_this:join')


@require_POST
@csrf_exempt
def submit_answer(request, room_code, participant_name):
    """Submit an answer for the current question"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhereParticipant, 
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
        existing_answer = WhereAnswer.objects.filter(
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
        user_latitude = float(data.get('latitude'))
        user_longitude = float(data.get('longitude'))
        time_taken = data.get('time_taken', 0)
        
        if user_latitude is None or user_longitude is None:
            return JsonResponse({
                'success': False,
                'error': 'Please select a location on the map before submitting.'
            })
        
        # Create answer
        answer = WhereAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            user_latitude=user_latitude,
            user_longitude=user_longitude,
            time_taken=time_taken
        )
        
        # Update participant's last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        return JsonResponse({
            'success': True,
            'points_earned': answer.points_earned,
            'distance_km': answer.distance_km,
            'formatted_distance': answer.get_formatted_distance(),
            'accuracy_percentage': answer.accuracy_percentage,
            'accuracy_category': answer.get_accuracy_category(),
            'correct_latitude': quiz.current_question.correct_latitude,
            'correct_longitude': quiz.current_question.correct_longitude,
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid request format.'
        })
    except (ValueError, TypeError):
        return JsonResponse({
            'success': False,
            'error': 'Invalid coordinates provided.'
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
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhereParticipant, 
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
            status_data['current_question'] = {
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': question.time_limit,
                'points': question.points,
                'hint_text': question.hint_text,
                'image_url': question.image.url if question.image else None,
            }
            
            # Check if user has already answered
            has_answered = WhereAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question
            ).exists()
            status_data['has_answered'] = has_answered
        
        return JsonResponse({
            'success': True,
            **status_data
        })
        
    except (WhereQuiz.DoesNotExist, WhereParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


def leave_quiz(request, room_code, participant_name):
    """Leave a quiz session"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhereParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as inactive instead of deleting
        participant.is_active = False
        participant.save()
        
        return JsonResponse({'success': True})
        
    except (WhereQuiz.DoesNotExist, WhereParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


# API endpoints for real-time updates
def api_quiz_participants(request, room_code):
    """Get current participants for a quiz (public endpoint)"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
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
        
    except WhereQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


def api_quiz_leaderboard(request, room_code):
    """Get leaderboard for a quiz"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
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
        
    except WhereQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


# Utility functions for WebSocket consumers
def get_quiz_statistics(quiz):
    """Get comprehensive quiz statistics"""
    participants = quiz.participants.all()
    answers = WhereAnswer.objects.filter(quiz=quiz)
    
    stats = {
        'total_participants': participants.count(),
        'active_participants': participants.filter(is_active=True).count(),
        'total_questions_sent': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_score': 0,
        'average_accuracy': 0,
        'average_distance': 0,
    }
    
    if participants.exists():
        scores = [p.total_score for p in participants]
        stats['average_score'] = sum(scores) / len(scores)
    
    if answers.exists():
        # Calculate overall averages
        stats['average_accuracy'] = sum(answer.accuracy_percentage for answer in answers) / answers.count()
        stats['average_distance'] = sum(answer.distance_km for answer in answers) / answers.count()
    
    return stats
