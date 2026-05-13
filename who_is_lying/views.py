from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Avg, Count, Q
import json
from .models import WhoQuiz, WhoQuestion, WhoParticipant, WhoAnswer, WhoSession


def _get_ordered_quiz_questions(quiz):
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
    for answer in WhoAnswer.objects.filter(quiz=quiz).select_related('question').order_by('submitted_at', 'id'):
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


def _build_participant_progress_history(quiz, participant):
    ordered_questions = _get_ordered_quiz_questions(quiz)
    answers_by_question_id = {
        answer.question_id: answer
        for answer in WhoAnswer.objects.filter(quiz=quiz, participant=participant).select_related('question')
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


def _build_question_scoreboard(quiz, participant):
    ordered_questions = _get_ordered_quiz_questions(quiz)
    history = _build_participant_progress_history(quiz, participant)
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
            max_points = question.get_total_possible_points()
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
            
            if len(room_code) != 4 or not room_code.isdigit():
                return JsonResponse({
                    'success': False,
                    'error': 'Room code must be exactly 4 digits.'
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
                participant, created = WhoParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': True}
                )
            else:
                participant, created = WhoParticipant.objects.get_or_create(
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
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as active
        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()

        question_scoreboard, initial_progress_history, current_question_number = _build_question_scoreboard(
            quiz,
            participant,
        )
        
        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.get_participant_count(session_code),
            'question_scoreboard': question_scoreboard,
            'initial_progress_history': initial_progress_history,
            'current_question_number': current_question_number,
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
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        participant = get_object_or_404(
            WhoParticipant, 
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
        existing_answer = WhoAnswer.objects.filter(
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
        selected_liars = data.get('selected_liars', [])
        time_taken = data.get('time_taken', 0)
        
        # Create answer (can be empty list if no one is selected as lying)
        answer = WhoAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            selected_liars=selected_liars,
            time_taken=time_taken
        )
        
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
