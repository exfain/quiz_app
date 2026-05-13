from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Avg, Count, Q
import json
from .models import Quiz, QuizQuestion, QuizParticipant, QuizAnswer, QuizSession


def _get_ordered_quiz_questions(quiz):
    configured_questions = []
    configured_by_id = {}
    if quiz.selected_questions.exists():
        selected = list(quiz.selected_questions.all())
        order = [int(question_id) for question_id in (quiz.question_order or [])]
        if order:
            order_map = {question_id: index for index, question_id in enumerate(order)}
            selected.sort(key=lambda question: order_map.get(question.id, len(order)))
        configured_questions = selected
        configured_by_id = {question.id: question for question in selected}

    questions = []
    seen_ids = set()

    # Participant score boxes should follow the real send order first.
    sent_answers = (
        QuizAnswer.objects
        .filter(quiz=quiz)
        .select_related('question')
        .order_by('submitted_at', 'id')
    )
    for answer in sent_answers:
        if answer.question_id in seen_ids:
            continue
        questions.append(answer.question)
        seen_ids.add(answer.question_id)

    if quiz.current_question_id and quiz.current_question_id not in seen_ids:
        current_question = configured_by_id.get(quiz.current_question_id, quiz.current_question)
        questions.append(current_question)
        seen_ids.add(quiz.current_question_id)

    for question in configured_questions:
        if question.id in seen_ids:
            continue
        questions.append(question)
        seen_ids.add(question.id)

    return questions


def _build_participant_progress_history(quiz, participant):
    ordered_questions = _get_ordered_quiz_questions(quiz)
    question_number_by_id = {
        question.id: index
        for index, question in enumerate(ordered_questions, start=1)
    }
    answers = list(
        QuizAnswer.objects
        .filter(quiz=quiz, participant=participant)
        .select_related('question')
        .order_by('submitted_at', 'id')
    )
    history = []
    seen_question_ids = set()
    for answer in answers:
        if answer.question_id in seen_question_ids:
            continue
        question_number = question_number_by_id.get(answer.question_id)
        if question_number is None:
            continue
        seen_question_ids.add(answer.question_id)
        history.append({
            'question_id': answer.question_id,
            'question_number': question_number,
            'is_correct': answer.is_correct,
        })
    history.sort(key=lambda entry: entry['question_number'])
    return history


def quiz_join_view(request):
    """Combined view for quiz join page (GET) and join action (POST)"""
    if request.method == 'GET':
        return render(request, 'quiz/join.html')
    
    elif request.method == 'POST':
        # This is the join_quiz logic
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
                quiz = Quiz.objects.get(room_code=room_code)
            except Quiz.DoesNotExist:
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
            
            # Check if participant already exists (rejoin scenario)
            name_qs = quiz.participants.filter(name__iexact=participant_name)
            if hub_session:
                name_qs = name_qs.filter(hub_session_code=hub_session)
            existing = name_qs.first()
            if existing:
                # Rejoin: reactivate and return success
                existing.is_active = True
                existing.save()
                return JsonResponse({
                    'success': True,
                    'participant_id': existing.id,
                    'quiz_status': quiz.status,
                    'rejoin': True,
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

            # Create new participant
            if hub_session:
                participant = QuizParticipant.objects.create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    is_active=True,
                )
            else:
                participant = QuizParticipant.objects.create(
                    quiz=quiz,
                    name=participant_name,
                    is_active=True,
                )

            return JsonResponse({
                'success': True,
                'participant_id': participant.id,
                'quiz_status': quiz.status,
                'rejoin': False,
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


def quiz_join_page(request):
    """Quiz join page for participants (DEPRECATED - use quiz_join_view)"""
    return render(request, 'quiz/join.html')


def check_room_code(request, room_code):
    """Check if room code is valid and return quiz info"""
    try:
        quiz = Quiz.objects.get(room_code=room_code)
        
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
    except Quiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Invalid room code. Please check and try again.'
        })


@require_POST
@csrf_exempt
def join_quiz(request):
    """Join a quiz session"""
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
            quiz = Quiz.objects.get(room_code=room_code)
        except Quiz.DoesNotExist:
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
        
        # Check if participant already exists (rejoin scenario)
        name_qs = quiz.participants.filter(name__iexact=participant_name)
        if hub_session:
            name_qs = name_qs.filter(hub_session_code=hub_session)
        existing = name_qs.first()
        if existing:
            existing.is_active = True
            existing.save()
            return JsonResponse({
                'success': True,
                'participant_id': existing.id,
                'quiz_status': quiz.status,
                'rejoin': True,
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

        # Create new participant
        if hub_session:
            participant = QuizParticipant.objects.create(
                quiz=quiz,
                name=participant_name,
                hub_session_code=hub_session,
                is_active=True,
            )
        else:
            participant = QuizParticipant.objects.create(
                quiz=quiz,
                name=participant_name,
                is_active=True,
            )

        return JsonResponse({
            'success': True,
            'participant_id': participant.id,
            'quiz_status': quiz.status,
            'rejoin': False,
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


def quiz_play(request, room_code, participant_name):
    """Quiz play page for participants"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(Quiz, room_code=room_code)
        participant = get_object_or_404(
            QuizParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as active
        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()

        ordered_questions = _get_ordered_quiz_questions(quiz)
        initial_progress_history = _build_participant_progress_history(quiz, participant)
        progress_by_number = {
            entry['question_number']: entry
            for entry in initial_progress_history
        }
        current_question_id = quiz.current_question_id if quiz.current_question_id else None
        current_question_number = next(
            (index for index, question in enumerate(ordered_questions, start=1) if question.id == current_question_id),
            None,
        )
        question_scoreboard = []
        for index, question in enumerate(ordered_questions, start=1):
            history_entry = progress_by_number.get(index)
            if history_entry:
                status = 'played'
                is_correct = history_entry['is_correct']
            elif current_question_number == index:
                status = 'current'
                is_correct = None
            else:
                status = 'upcoming'
                is_correct = None
            question_scoreboard.append({
                'id': question.id,
                'number': index,
                'is_correct': is_correct,
                'status': status,
            })

        score_total_correct = sum(1 for entry in initial_progress_history if entry['is_correct'])
        score_total_questions = len(question_scoreboard)
        
        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.get_participant_count(session_code),
            'question_scoreboard': question_scoreboard,
            'initial_progress_history': initial_progress_history,
            'current_question_id': current_question_id,
            'score_total_correct': score_total_correct,
            'score_total_questions': score_total_questions,
        }
        return render(request, 'quiz/play.html', context)
        
    except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
        return redirect('quiz:join')


def quiz_result(request, room_code, participant_name):
    """Quiz result page for participants"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        participant = get_object_or_404(
            QuizParticipant, 
            quiz=quiz, 
            name=participant_name
        )
        
        # Get participant's answers
        participant_answers = QuizAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('submitted_at')
        
        # Calculate statistics
        total_answers = participant_answers.count()
        correct_answers = participant_answers.filter(is_correct=True).count()
        incorrect_answers = total_answers - correct_answers
        
        accuracy_percentage = 0
        if total_answers > 0:
            accuracy_percentage = round((correct_answers / total_answers) * 100, 1)
        
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
            # Format duration as "Xm Ys"
            minutes = int(quiz_duration // 60)
            seconds = int(quiz_duration % 60)
            quiz_duration_formatted = f"{minutes}m {seconds}s"
        
        context = {
            'quiz': quiz,
            'participant': participant,
            'participant_answers': participant_answers,
            'correct_answers': correct_answers,
            'incorrect_answers': incorrect_answers,
            'accuracy_percentage': accuracy_percentage,
            'participant_rank': participant_rank,
            'leaderboard': leaderboard,
            'total_participants': quiz.get_participant_count(),
            'average_time': average_time,
            'fastest_answer': fastest_answer,
            'quiz_duration': quiz_duration,
            'quiz_duration_formatted': quiz_duration_formatted,
        }
        return render(request, 'quiz/result.html', context)
        
    except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
        return redirect('quiz:join')


@require_POST
@csrf_exempt
def submit_answer(request, room_code, participant_name):
    """Submit an answer for the current question"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(Quiz, room_code=room_code)
        participant = get_object_or_404(
            QuizParticipant, 
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
        existing_answer = QuizAnswer.objects.filter(
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
        question = quiz.current_question
        answer_payload = data.get('answer', '')
        if question.get_effective_question_type() == 'short_answer' and question.get_short_answer_field_count() > 1:
            answers = question.parse_short_answer_submission(answer_payload)
            if any(not value for value in answers):
                return JsonResponse({
                    'success': False,
                    'error': 'All answer fields are required.'
                })
            answer_text = question.serialize_short_answer_submission(answer_payload)
        else:
            answer_text = question.parse_short_answer_submission(answer_payload)[0]
        time_taken = data.get('time_taken', 0)
        
        if not answer_text:
            return JsonResponse({
                'success': False,
                'error': 'Answer cannot be empty.'
            })
        
        # Create answer
        answer = QuizAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=quiz.current_question,
            answer_text=answer_text,
            time_taken=time_taken
        )
        
        # Update participant's last activity
        participant.last_activity = timezone.now()
        participant.save()
        
        return JsonResponse({
            'success': True,
            'is_correct': answer.is_correct,
            'points_earned': answer.points_earned,
            'correct_answer': question.get_formatted_correct_answer() if not answer.is_correct else None
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
        quiz = get_object_or_404(Quiz, room_code=room_code)
        participant = get_object_or_404(
            QuizParticipant, 
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
                'question_type': question.get_effective_question_type(),
                'time_limit': question.time_limit,
                'options': [],
                'short_answer_fields': question.get_public_short_answer_fields(),
            }
            
            # Add options for multiple choice questions
            if question.question_type == 'multiple_choice':
                for key, text in question.get_options():
                    status_data['current_question']['options'].append({
                        'key': key,
                        'text': text
                    })
            elif question.question_type == 'true_false':
                status_data['current_question']['options'] = [
                    {'key': 'True', 'text': 'True'},
                    {'key': 'False', 'text': 'False'}
                ]
            # Check if user has already answered
            has_answered = QuizAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question
            ).exists()
            status_data['has_answered'] = has_answered
        
        return JsonResponse({
            'success': True,
            **status_data
        })
        
    except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


def leave_quiz(request, room_code, participant_name):
    """Leave a quiz session"""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(Quiz, room_code=room_code)
        participant = get_object_or_404(
            QuizParticipant, 
            quiz=quiz, 
            name=participant_name,
            hub_session_code=session_code
        )
        
        # Mark participant as inactive instead of deleting
        participant.is_active = False
        participant.save()
        
        return JsonResponse({'success': True})
        
    except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
        return JsonResponse({
            'success': False,
            'error': 'Participant not found.'
        })


# API endpoints for real-time updates
def api_quiz_participants(request, room_code):
    """Get current participants for a quiz (public endpoint)"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
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
        
    except Quiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


def api_quiz_leaderboard(request, room_code):
    """Get leaderboard for a quiz"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Get top 10 participants
        participants = quiz.participants.all().order_by('-total_score', 'name')[:10]
        
        leaderboard_data = []
        for rank, participant in enumerate(participants, 1):
            leaderboard_data.append({
                'rank': rank,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'accuracy': (participant.quiz_answers.filter(is_correct=True).count() / 
                           max(1, participant.questions_answered)) * 100
            })
        
        return JsonResponse({
            'success': True,
            'leaderboard': leaderboard_data
        })
        
    except Quiz.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Quiz not found.'
        })


# Utility functions for WebSocket consumers
def get_quiz_statistics(quiz):
    """Get comprehensive quiz statistics"""
    participants = quiz.participants.all()
    answers = QuizAnswer.objects.filter(quiz=quiz)
    
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
        
        if answers.exists():
            stats['average_accuracy'] = (stats['correct_answers'] / stats['total_answers']) * 100
    
    return stats


def broadcast_to_quiz_participants(quiz, message_type, data):
    """
    Utility function to broadcast messages to quiz participants
    This would be called from WebSocket consumers
    """
    # This function would implement the actual WebSocket broadcasting
    # For now, it's a placeholder that documents the interface
    pass
