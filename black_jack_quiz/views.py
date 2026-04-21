from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Avg, Count, Q
import json
from .models import BlackJackQuiz, BlackJackQuestion, BlackJackParticipant, BlackJackAnswer, BlackJackSession


def blackjack_join_view(request):
    """Combined view for blackjack quiz join page (GET) and join action (POST)"""
    if request.method == 'GET':
        return render(request, 'black_jack_quiz/join.html')
    
    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            participant_name = data.get('participant_name', '').strip()
            room_code = data.get('room_code', '').strip()
            hub_session = data.get('hub_session', '').strip() or None
            
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
                quiz = BlackJackQuiz.objects.get(room_code=room_code)
            except BlackJackQuiz.DoesNotExist:
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
                participant, created = BlackJackParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': True}
                )
            else:
                participant, created = BlackJackParticipant.objects.get_or_create(
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
        quiz = BlackJackQuiz.objects.get(room_code=room_code)
        
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
                'current_question_number': quiz.current_question_number,
                'total_questions': quiz.total_questions,
            }
        })
    except BlackJackQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Invalid room code. Please check and try again.'
        })


def blackjack_play(request, room_code, participant_name):
    """BlackJack quiz play page for participants"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        participant = get_object_or_404(
            BlackJackParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as active
        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()
        
        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.get_participant_count(session_code)
        }
        return render(request, 'black_jack_quiz/play.html', context)
        
    except (BlackJackQuiz.DoesNotExist, BlackJackParticipant.DoesNotExist):
        return redirect('black_jack_quiz:join')


def blackjack_result(request, room_code, participant_name):
    """BlackJack quiz result page for participants"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        participant = get_object_or_404(
            BlackJackParticipant, 
            quiz=quiz, 
            name=participant_name
        )
        
        # Get participant's answers
        participant_answers = BlackJackAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('question_number')
        
        # Calculate statistics
        total_answers = participant_answers.count()
        
        # Get participant rank
        participant_rank = participant.get_rank()
        
        # Get leaderboard (top 10) - non-busted players first, then busted
        non_busted = quiz.participants.filter(is_busted=False).order_by('final_score', 'name')[:5]
        busted = quiz.participants.filter(is_busted=True).order_by('-total_points', 'name')[:5]
        leaderboard = list(non_busted) + list(busted)
        
        # Calculate performance insights
        average_time = None
        fastest_answer = None
        average_points = None
        best_question = None
        
        if participant_answers.exists():
            times = [answer.time_taken for answer in participant_answers if answer.time_taken]
            if times:
                average_time = sum(times) / len(times)
                fastest_answer = min(times)
            
            # Calculate points statistics
            points_list = [answer.points_earned for answer in participant_answers]
            if points_list:
                average_points = sum(points_list) / len(points_list)
                best_question = min(points_list)  # Lower points is better
        
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
            'participant_rank': participant_rank,
            'leaderboard': leaderboard,
            'total_participants': quiz.get_participant_count(),
            'average_time': average_time,
            'fastest_answer': fastest_answer,
            'average_points': average_points,
            'best_question': best_question,
            'quiz_duration': quiz_duration,
            'quiz_duration_formatted': quiz_duration_formatted,
        }
        return render(request, 'black_jack_quiz/result.html', context)
        
    except (BlackJackQuiz.DoesNotExist, BlackJackParticipant.DoesNotExist):
        return redirect('black_jack_quiz:join')


@require_POST
@csrf_exempt
def submit_answer(request, room_code, participant_name):
    """Submit an answer for the current question"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        participant = get_object_or_404(
            BlackJackParticipant, 
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
        existing_answer = BlackJackAnswer.objects.filter(
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
        time_taken = data.get('time_taken', 0)
        
        if user_answer is None or user_answer == '':
            return JsonResponse({
                'success': False,
                'error': 'Please provide an answer before submitting.'
            })
        
        # Convert to integer
        try:
            user_answer_int = int(user_answer)
        except (ValueError, TypeError):
            return JsonResponse({
                'success': False,
                'error': 'Please provide a valid whole number.'
            })
        
        # Create answer
        answer = BlackJackAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            user_answer=user_answer_int,
            time_taken=time_taken,
            question_number=quiz.current_question_number
        )
        
        # Refresh participant to get updated totals
        participant.refresh_from_db()
        
        # Update participant's last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        return JsonResponse({
            'success': True,
            'points_earned': answer.points_earned,
            'user_answer': answer.user_answer,
            'difference': answer.get_difference(),
            'total_points': participant.total_points,
            'is_busted': participant.is_busted,
            'status': participant.get_status(),
            'questions_remaining': max(0, 5 - participant.questions_answered)
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
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        participant = get_object_or_404(
            BlackJackParticipant, 
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
            'participant_points': participant.total_points,
            'participant_count': quiz.get_participant_count(),
            'questions_answered': participant.questions_answered,
            'is_busted': participant.is_busted,
            'status': participant.get_status(),
            'current_question_number': quiz.current_question_number,
            'total_questions': quiz.total_questions,
        }
        
        # Include current question if active
        if quiz.current_question and quiz.status == 'active':
            question = quiz.current_question
            status_data['current_question'] = {
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': question.time_limit,
                'question_number': quiz.current_question_number
            }
            
            # Check if user has already answered
            has_answered = BlackJackAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question
            ).exists()
            status_data['has_answered'] = has_answered
        
        return JsonResponse({
            'success': True,
            **status_data
        })
        
    except (BlackJackQuiz.DoesNotExist, BlackJackParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


def leave_quiz(request, room_code, participant_name):
    """Leave a quiz session"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        participant = get_object_or_404(
            BlackJackParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as inactive instead of deleting
        participant.is_active = False
        participant.save()
        
        return JsonResponse({'success': True})
        
    except (BlackJackQuiz.DoesNotExist, BlackJackParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


# API endpoints for real-time updates
def api_quiz_participants(request, room_code):
    """Get current participants for a quiz (public endpoint)"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Non-busted participants first (sorted by final_score), then busted (sorted by total_points desc)
        non_busted = quiz.participants.filter(
            is_active=True, 
            is_busted=False
        ).order_by('final_score', 'name')
        
        busted = quiz.participants.filter(
            is_active=True, 
            is_busted=True
        ).order_by('-total_points', 'name')
        
        participants = list(non_busted) + list(busted)
        
        participants_data = []
        for rank, participant in enumerate(participants, 1):
            participants_data.append({
                'name': participant.name,
                'total_points': participant.total_points,
                'questions_answered': participant.questions_answered,
                'rank': participant.get_rank(),
                'is_busted': participant.is_busted,
                'status': participant.get_status(),
                'final_score': participant.final_score,
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data,
            'count': len(participants_data)
        })
        
    except BlackJackQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


def api_quiz_leaderboard(request, room_code):
    """Get leaderboard for a quiz"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Get top 10 participants (non-busted first, then busted)
        non_busted = quiz.participants.filter(is_busted=False).order_by('final_score', 'name')[:5]
        busted = quiz.participants.filter(is_busted=True).order_by('-total_points', 'name')[:5]
        participants = list(non_busted) + list(busted)
        
        leaderboard_data = []
        for rank, participant in enumerate(participants, 1):
            leaderboard_data.append({
                'rank': rank,
                'name': participant.name,
                'total_points': participant.total_points,
                'questions_answered': participant.questions_answered,
                'is_busted': participant.is_busted,
                'status': participant.get_status(),
                'distance_from_21': participant.get_distance_from_21()
            })
        
        return JsonResponse({
            'success': True,
            'leaderboard': leaderboard_data
        })
        
    except BlackJackQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


# Utility functions for WebSocket consumers
def get_quiz_statistics(quiz):
    """Get comprehensive quiz statistics"""
    participants = quiz.participants.all()
    answers = BlackJackAnswer.objects.filter(quiz=quiz)
    
    stats = {
        'total_participants': participants.count(),
        'active_participants': participants.filter(is_active=True).count(),
        'total_questions_sent': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_points': 0,
        'busted_participants': participants.filter(is_busted=True).count(),
        'blackjack_participants': participants.filter(total_points=21).count(),
    }
    
    if participants.exists():
        points = [p.total_points for p in participants]
        stats['average_points'] = sum(points) / len(points)
    
    return stats