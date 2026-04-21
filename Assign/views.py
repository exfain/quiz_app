from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Avg, Count, Q
import json
from .models import AssignQuiz, AssignQuestion, AssignParticipant, AssignAnswer, AssignSession


def assign_join_view(request):
    """Combined view for assign quiz join page (GET) and join action (POST)"""
    if request.method == 'GET':
        return render(request, 'assign/join.html')
    
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
                quiz = AssignQuiz.objects.get(room_code=room_code)
            except AssignQuiz.DoesNotExist:
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
                participant, created = AssignParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': True}
                )
            else:
                participant, created = AssignParticipant.objects.get_or_create(
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
        quiz = AssignQuiz.objects.get(room_code=room_code)
        
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
    except AssignQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Invalid room code. Please check and try again.'
        })


def assign_play(request, room_code, participant_name):
    """Assign quiz play page for participants"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        participant = get_object_or_404(
            AssignParticipant, 
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
        return render(request, 'assign/play.html', context)
        
    except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
        return redirect('assign:join')


def assign_result(request, room_code, participant_name):
    """Assign quiz result page for participants"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        participant = get_object_or_404(
            AssignParticipant, 
            quiz=quiz, 
            name=participant_name
        )
        
        # Get participant's answers
        participant_answers = AssignAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('submitted_at')
        
        # Process each answer with detailed match analysis
        processed_answers = []
        for answer in participant_answers:
            question = answer.question
            user_matches = answer.user_matches or {}
            correct_matches = question.correct_matches or {}
            
            # Build match analysis
            match_analysis = []
            correct_answer_hints = []
            
            for left_idx_str, right_idx in user_matches.items():
                left_idx = int(left_idx_str)
                
                # Get the items safely
                left_item = question.left_items[left_idx] if left_idx < len(question.left_items) else "Unknown"
                right_item = question.right_items[int(right_idx)] if int(right_idx) < len(question.right_items) else "Unknown"
                
                # Check if correct
                correct_right_idx = correct_matches.get(left_idx_str)
                is_correct = correct_right_idx is not None and correct_right_idx == int(right_idx)
                
                match_analysis.append({
                    'left_item': left_item,
                    'right_item': right_item,
                    'is_correct': is_correct,
                    'left_idx': left_idx,
                    'right_idx': right_idx
                })
                
                # Add hint for incorrect matches
                if not is_correct and correct_right_idx is not None:
                    correct_right_item = question.right_items[correct_right_idx] if correct_right_idx < len(question.right_items) else "Unknown"
                    correct_answer_hints.append({
                        'left_item': left_item,
                        'correct_right_item': correct_right_item
                    })
            
            processed_answers.append({
                'answer': answer,
                'match_analysis': match_analysis,
                'correct_answer_hints': correct_answer_hints
            })
        
        # Calculate statistics
        total_answers = participant_answers.count()
        total_correct_matches = sum(answer.get_correct_matches_count() for answer in participant_answers)
        total_possible_matches = sum(len(answer.question.correct_matches) for answer in participant_answers)
        
        accuracy_percentage = 0
        if total_possible_matches > 0:
            accuracy_percentage = round((total_correct_matches / total_possible_matches) * 100, 1)
        
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
            'total_correct_matches': total_correct_matches,
            'total_possible_matches': total_possible_matches,
            'accuracy_percentage': accuracy_percentage,
            'participant_rank': participant_rank,
            'leaderboard': leaderboard,
            'total_participants': quiz.get_participant_count(),
            'average_time': average_time,
            'fastest_answer': fastest_answer,
            'quiz_duration': quiz_duration,
            'quiz_duration_formatted': quiz_duration_formatted,
        }
        return render(request, 'assign/result.html', context)
        
    except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
        return redirect('assign:join')


@require_POST
@csrf_exempt
def submit_answer(request, room_code, participant_name):
    """Submit an answer for the current question"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        participant = get_object_or_404(
            AssignParticipant, 
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
        existing_answer = AssignAnswer.objects.filter(
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
        user_matches = data.get('user_matches', {})
        time_taken = data.get('time_taken', 0)
        
        if not user_matches:
            return JsonResponse({
                'success': False,
                'error': 'Please make at least one match before submitting.'
            })
        
        # Create answer
        answer = AssignAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            user_matches=user_matches,
            time_taken=time_taken
        )
        
        # Update participant's last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        return JsonResponse({
            'success': True,
            'points_earned': answer.points_earned,
            'correct_matches': answer.get_correct_matches_count(),
            'total_matches': answer.get_total_matches_count(),
            'accuracy': answer.get_accuracy_percentage(),
            'correct_answers': quiz.current_question.correct_matches
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
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        participant = get_object_or_404(
            AssignParticipant, 
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
                'left_items': [{'id': i, 'text': item} for i, item in enumerate(question.left_items)],
                'right_items': [{'id': i, 'text': item} for i, item in enumerate(question.right_items)],
                'total_possible_points': question.get_total_possible_points()
            }
            
            # Check if user has already answered
            has_answered = AssignAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question
            ).exists()
            status_data['has_answered'] = has_answered
        
        return JsonResponse({
            'success': True,
            **status_data
        })
        
    except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


def leave_quiz(request, room_code, participant_name):
    """Leave a quiz session"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        participant = get_object_or_404(
            AssignParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as inactive instead of deleting
        participant.is_active = False
        participant.save()
        
        return JsonResponse({'success': True})
        
    except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


# API endpoints for real-time updates
def api_quiz_participants(request, room_code):
    """Get current participants for a quiz (public endpoint)"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        participants = quiz.participants.filter(is_active=True).order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'rank': participant.get_rank(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data,
            'count': len(participants_data)
        })
        
    except AssignQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


def api_quiz_leaderboard(request, room_code):
    """Get leaderboard for a quiz"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Get top 10 participants
        participants = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        leaderboard_data = []
        for rank, participant in enumerate(participants, 1):
            # Calculate accuracy
            participant_answers = participant.assign_answers.all()
            total_correct = sum(answer.get_correct_matches_count() for answer in participant_answers)
            total_possible = sum(len(answer.question.correct_matches) for answer in participant_answers)
            accuracy = (total_correct / max(1, total_possible)) * 100 if total_possible > 0 else 0
            
            leaderboard_data.append({
                'rank': rank,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'accuracy': round(accuracy, 1)
            })
        
        return JsonResponse({
            'success': True,
            'leaderboard': leaderboard_data
        })
        
    except AssignQuiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


# Utility functions for WebSocket consumers
def get_quiz_statistics(quiz):
    """Get comprehensive quiz statistics"""
    participants = quiz.participants.all()
    answers = AssignAnswer.objects.filter(quiz=quiz)
    
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
        total_correct = sum(answer.get_correct_matches_count() for answer in answers)
        total_possible = sum(len(answer.question.correct_matches) for answer in answers)
        if total_possible > 0:
            stats['average_accuracy'] = (total_correct / total_possible) * 100
    
    return stats