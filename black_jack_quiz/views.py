from games_hub.team_names import validate_join_name
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
from . import runtime
from .identity import issue_player_token, valid_player_token
from .models import BlackJackQuiz, BlackJackQuestion, BlackJackParticipant, BlackJackAnswer, BlackJackSession
from games_hub.authoritative_state import attach_snapshot_metadata, current_snapshot, validate_and_reserve_action
from games_hub.models import GameRuntimeState
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids, get_unit_tutorial_state, is_unit_tutorial_question


def _get_scoped_participants(quiz, session_code):
    participants = quiz.participants.all()
    if session_code is not None:
        participants = participants.filter(hub_session_code=session_code)
    return list(participants.order_by('name', 'id'))


def _get_current_run_answers(quiz, participants=None, session_code=None):
    answers = BlackJackAnswer.objects.filter(quiz=quiz)
    if participants is not None:
        answers = answers.filter(participant__in=participants)
    if quiz.started_at:
        answers = answers.filter(submitted_at__gte=quiz.started_at)
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('blackjack', quiz.room_code, session_code)
    if tutorial_question_ids:
        answers = answers.exclude(question_id__in=tutorial_question_ids)
    return answers


def _get_finalized_set_numbers(quiz):
    tutorial_set_number = getattr(quiz, 'tutorial_set_number', None)
    session = getattr(quiz, 'session', None)
    if session:
        finalized_set_numbers = session.get_finalized_set_numbers()
        if tutorial_set_number:
            finalized_set_numbers = [set_number for set_number in finalized_set_numbers if set_number != tutorial_set_number]
        if finalized_set_numbers:
            return finalized_set_numbers
    if quiz.status == 'completed' and not quiz.current_question_id:
        return list(range(1, quiz.get_total_sets() + 1))
    return []


def _get_ordered_set_numbers(quiz):
    total_sets = quiz.get_total_sets()
    if total_sets <= 0:
        return []
    tutorial_set_number = getattr(quiz, 'tutorial_set_number', None)

    session = getattr(quiz, 'session', None)
    finalized_set_numbers = _get_finalized_set_numbers(quiz)
    current_set_number = quiz.get_current_set_number()
    if session and not quiz.current_question_id:
        current_set_number = session.get_normalized_selected_set_number(active_only=True)

    ordered_set_numbers = []
    seen_set_numbers = set()

    for set_number in finalized_set_numbers:
        if tutorial_set_number and set_number == tutorial_set_number:
            continue
        if set_number not in seen_set_numbers:
            ordered_set_numbers.append(set_number)
            seen_set_numbers.add(set_number)

    if current_set_number and current_set_number != tutorial_set_number and current_set_number not in seen_set_numbers:
        ordered_set_numbers.append(current_set_number)
        seen_set_numbers.add(current_set_number)

    for set_number in range(1, total_sets + 1):
        if tutorial_set_number and set_number == tutorial_set_number:
            continue
        if set_number not in seen_set_numbers:
            ordered_set_numbers.append(set_number)
            seen_set_numbers.add(set_number)

    return ordered_set_numbers


def _get_set_denominator(quiz, participant_count):
    if quiz.uses_ranking_mode():
        return max(participant_count, 0)
    return 21


def _build_set_points_lookup(quiz, participants, finalized_set_numbers):
    results_by_set = {}
    if not finalized_set_numbers or not participants:
        return results_by_set

    answers = list(
        _get_current_run_answers(quiz, participants=participants, session_code=participants[0].hub_session_code if participants else None)
        .select_related('participant')
        .order_by('question_number', 'submitted_at', 'id')
    )
    participant_answers_by_set = {}
    for answer in answers:
        set_number = quiz.get_set_number_for_question_id(answer.question_id, active_only=False)
        if not set_number:
            continue
        participant_answers_by_set.setdefault((answer.participant_id, set_number), []).append(answer)

    participant_count = len(participants)
    for set_number in finalized_set_numbers:
        participant_rows = []
        for participant in participants:
            set_answers = participant_answers_by_set.get((participant.id, set_number), [])
            raw_total_points = sum(answer.points_earned for answer in set_answers)
            if raw_total_points > 21:
                participant_rows.append({
                    'id': participant.id,
                    'name': participant.name,
                    'awarded_points': 0,
                    'is_busted': True,
                    'final_score': raw_total_points,
                })
            else:
                participant_rows.append({
                    'id': participant.id,
                    'name': participant.name,
                    'awarded_points': raw_total_points,
                    'is_busted': False,
                    'final_score': abs(21 - raw_total_points),
                })

        if quiz.uses_ranking_mode():
            participant_rows.sort(
                key=lambda participant_row: (
                    participant_row['is_busted'],
                    participant_row['final_score'],
                    participant_row['name'],
                )
            )
            max_points = participant_count
            previous_rank_key = None
            current_rank = 0
            points_by_participant = {}
            for position, participant_row in enumerate(participant_rows, start=1):
                rank_key = (participant_row['is_busted'], participant_row['final_score'])
                if previous_rank_key is None or rank_key != previous_rank_key:
                    current_rank = position
                    previous_rank_key = rank_key
                points_by_participant[participant_row['id']] = max(max_points - (current_rank - 1), 0)
        else:
            max_points = 21
            points_by_participant = {
                participant_row['id']: participant_row['awarded_points']
                for participant_row in participant_rows
            }

        results_by_set[set_number] = {
            'max_points': max_points,
            'points_by_participant': points_by_participant,
        }

    return results_by_set


def _build_participant_set_scoreboard(quiz, participant, session_code):
    session = getattr(quiz, 'session', None)
    if session and session.set_rounds:
        rows = runtime.scoreboard(quiz, session, participant, runtime.pool_code(quiz, session_code))
        return rows, sum(row['earned_points'] or 0 for row in rows), sum(row['max_points'] for row in rows if row['status'] == 'played')
    total_sets = quiz.get_total_sets()
    participants = _get_scoped_participants(quiz, session_code)
    finalized_set_numbers = _get_finalized_set_numbers(quiz)
    set_points_lookup = _build_set_points_lookup(quiz, participants, finalized_set_numbers)
    denominator = _get_set_denominator(quiz, len(participants))
    current_set_number = quiz.get_current_set_number()
    session = getattr(quiz, 'session', None)
    if session and not quiz.current_question_id:
        current_set_number = session.get_normalized_selected_set_number(active_only=True)

    scoreboard = []
    total_earned_points = 0
    total_max_points = 0

    for set_number in _get_ordered_set_numbers(quiz):
        set_result = set_points_lookup.get(set_number)
        if set_result:
            earned_points = set_result['points_by_participant'].get(participant.id, 0)
            max_points = set_result['max_points']
            status = 'played'
            total_earned_points += earned_points
            total_max_points += max_points
        else:
            earned_points = None
            max_points = denominator
            status = 'current' if set_number == current_set_number else 'upcoming'

        scoreboard.append({
            'set_number': set_number,
            'earned_points': earned_points,
            'max_points': max_points,
            'status': status,
        })

    return scoreboard, total_earned_points, total_max_points


def _build_last_completed_set_summary(quiz, participant, session_code):
    session = getattr(quiz, 'session', None)
    if session and session.set_rounds:
        entries = runtime.progress(quiz, session, runtime.pool_code(quiz, session_code))['entries']
        entry = next((r for r in reversed(entries) if r['status'] == 'completed'), None)
        if not entry:
            return None
        st = entry['states'].get(str(participant.pk), {})
        return {'set_number': entry['number'], 'set_stars': 0 if st.get('busted') else runtime.stars(st),
            'awarded_points': st.get('awarded', 0), 'overall_points': participant.overall_points,
            'is_busted': st.get('busted', False), 'max_points': len(entry['states']) if quiz.uses_ranking_mode() else 21}
    finalized_set_numbers = _get_finalized_set_numbers(quiz)
    if not finalized_set_numbers:
        return None

    last_set_number = finalized_set_numbers[-1]
    participants = _get_scoped_participants(quiz, session_code)
    set_points_lookup = _build_set_points_lookup(quiz, participants, finalized_set_numbers)
    set_result = set_points_lookup.get(last_set_number)
    if not set_result:
        return None

    set_question_ids = quiz.get_set_question_ids(last_set_number, active_only=False)
    set_answers = list(
        _get_current_run_answers(quiz, session_code=session_code).filter(
            participant=participant,
            question_id__in=set_question_ids,
        )
    )
    raw_total_points = sum(answer.points_earned for answer in set_answers)
    set_stars = 0 if raw_total_points > 21 else raw_total_points

    return {
        'set_number': last_set_number,
        'set_stars': set_stars,
        'awarded_points': set_result['points_by_participant'].get(participant.id, 0),
        'overall_points': participant.overall_points,
        'is_busted': raw_total_points > 21,
        'max_points': set_result['max_points'],
    }


@validate_join_name('blackjack', 'black_jack_quiz/join.html')
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
        try:
            runtime.pool_code(quiz, session_code)
        except ValueError:
            return JsonResponse({'success': False, 'error': 'Spiel und Session stimmen nicht ueberein.'}, status=404)
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

        private_state = runtime.build_state(quiz, session_code, participant)
        quiz.refresh_from_db()
        set_scoreboard, score_total_earned, score_total_max = _build_participant_set_scoreboard(
            quiz,
            participant,
            session_code,
        )
        last_completed_set_summary = _build_last_completed_set_summary(
            quiz,
            participant,
            session_code,
        )
        quiz_session = getattr(quiz, 'session', None)
        current_question_end_time = quiz_session.question_end_time if quiz_session else None
        question_runtime = current_snapshot('blackjack', quiz.room_code, session_code)
        visible_at = parse_datetime(question_runtime.get('question_visible_at') or '')
        server_now = parse_datetime(question_runtime.get('server_now') or '') or timezone.now()
        manual_flow = (
            question_runtime.get('question_flow_mode')
            == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        )
        question_prompt_visible = bool(
            quiz.current_question
            and (
                not manual_flow
                or question_runtime.get('question_phase')
                != GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
                or not visible_at
                or server_now >= visible_at
            )
        )
        question_answering_open = bool(
            quiz.current_question
            and (
                not manual_flow
                or (
                    question_runtime.get('question_phase')
                    == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
                    and question_runtime.get('answering_allowed')
                )
            )
        )
        current_set_number = quiz.get_current_set_number()
        if quiz_session and not quiz.current_question_id:
            current_set_number = quiz_session.get_normalized_selected_set_number(active_only=True)
        tutorial_state = get_unit_tutorial_state('blackjack', quiz.room_code, session_code)
        
        context = {
            'player_token': issue_player_token(participant),
            'private_state': private_state,
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'current_question_end_time': current_question_end_time,
            'question_runtime': question_runtime,
            'question_prompt_visible': question_prompt_visible,
            'question_answering_open': question_answering_open,
            'participant_count': quiz.get_participant_count(session_code),
            'current_question_in_set': private_state['question_in_set'] or quiz.get_current_question_position_in_set() or 1,
            'current_set_question_count': private_state['set_question_count'] or quiz.get_set_question_count(
                question_id=quiz.current_question_id if quiz.current_question_id else None,
                set_number=current_set_number,
            ),
            'current_set_number': private_state['set_number'] or current_set_number,
            'current_unit_is_tutorial': bool(
                tutorial_state.get('current_unit_is_tutorial')
                and current_set_number == quiz.tutorial_set_number
            ),
            'total_sets': private_state['total_sets'],
            'set_scoreboard': set_scoreboard,
            'score_total_earned': score_total_earned,
            'score_total_max': score_total_max,
            'last_completed_set_summary': last_completed_set_summary,
            'show_initial_set_end_state': bool(
                quiz.status == 'active'
                and not quiz.current_question_id
                and not private_state.get('own_reveal')
                and not quiz.is_quiz_complete()
                and last_completed_set_summary
            ),
            'show_initial_quiz_end_state': bool(
                quiz.status == 'completed'
                and not private_state.get('own_reveal')
                and not quiz.current_question_id
            ),
        }
        participant.total_points = private_state['participant_points']
        participant.is_busted = private_state['is_busted']
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
            name=participant_name,
            hub_session_code=request.GET.get('hub_session') or None,
        )
        
        runtime.build_state(quiz, participant.hub_session_code, participant)
        # Get participant's answers
        participant_answers = BlackJackAnswer.objects.filter(
            quiz=quiz,
            participant=participant
        ).select_related('question').order_by('question_number')
        if quiz.status != 'completed':
            return redirect(reverse('black_jack_quiz:play', args=[quiz.room_code, participant.name])
                + ('?hub_session=' + participant.hub_session_code if participant.hub_session_code else ''))
        tutorial_state = get_unit_tutorial_state('blackjack', quiz.room_code, participant.hub_session_code)
        tutorial_question_id = tutorial_state.get('tutorial_question_id') if tutorial_state.get('requested') else None
        if tutorial_question_id:
            participant_answers = participant_answers.exclude(question_id=tutorial_question_id)
        history = [entry for entry in quiz.session.set_rounds
            if (entry.get('hub_session_code') or '') == (participant.hub_session_code or '')
            and entry['status'] == 'completed' and not entry['is_tutorial']]
        if quiz.session.set_rounds:
            included = Q(pk__in=[])
            for entry in history:
                included |= Q(set_round_number=0 if entry.get('legacy') else entry['number'],
                    question_id__in=entry['question_ids'])
            participant_answers = participant_answers.filter(included)

        set_scoreboard, _, _ = _build_participant_set_scoreboard(
            quiz,
            participant,
            participant.hub_session_code,
        )
        set_raw_points = {}
        for answer in participant_answers:
            set_number = quiz.get_set_number_for_question_id(answer.question_id, active_only=False)
            if not set_number:
                continue
            set_raw_points[set_number] = set_raw_points.get(set_number, 0) + answer.points_earned
        busted_set_numbers = {
            set_number
            for set_number, raw_points in set_raw_points.items()
            if raw_points > 21
        }
        if quiz.session.set_rounds:
            busted_set_numbers = {entry['number'] for entry in quiz.session.set_rounds
                if entry['states'].get(str(participant.pk), {}).get('busted')}
        result_set_log = [
            {
                **set_entry,
                'is_busted': set_entry['set_number'] in busted_set_numbers,
            }
            for set_entry in set_scoreboard
            if set_entry['status'] == 'played'
        ]
        
        # Calculate statistics
        total_answers = participant_answers.count()
        
        # Get participant rank
        participant_rank = participant.get_rank()
        
        leaderboard = quiz.get_ordered_participants()[:10]
        
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
        
        displayed_answers = list(participant_answers)
        for answer in displayed_answers:
            entry = next((entry for entry in history
                if (0 if entry.get('legacy') else entry['number']) == answer.set_round_number
                and answer.question_id in entry['question_ids']), None)
            snapshot = next((q for q in entry['questions'] if q['id'] == answer.question_id), None) if entry else None
            if snapshot:
                for field in ['question_text', 'correct_answer', 'explanation']:
                    setattr(answer.question, field, snapshot[field])
        context = {
            'quiz': quiz,
            'participant': participant,
            'participant_answers': displayed_answers,
            'result_set_log': result_set_log,
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
def submit_answer(request, room_code, participant_name):
    try:
        data = json.loads(request.body or '{}')
        quiz, participant = player_request(request, room_code, participant_name, data)
        answer = runtime.submit_action(quiz, participant, data.get('user_answer'), data)
        state = runtime.build_state(quiz, participant.hub_session_code, participant)
        runtime.broadcast(quiz, {'type': 'state_updated'})
        return JsonResponse({'success': True, 'user_answer': answer.user_answer, 'answer_locked': True, 'state': state,
            'questions_remaining': max(0, quiz.get_total_game_questions() - quiz.current_question_number)})
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except (ValueError, TypeError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=409)


def get_quiz_status(request, room_code, participant_name):
    try:
        quiz, participant = player_request(request, room_code, participant_name)
        state = runtime.build_state(quiz, participant.hub_session_code, participant)
        return JsonResponse({**state, 'quiz_status': state['game_status'], 'current_question': state['question'],
            'current_question_number': quiz.current_question_number, 'total_questions': state['set_question_count']})
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=409)


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
    quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
    rows = public_participants(quiz)
    return JsonResponse({'success': True, 'participants': rows, 'count': len(rows)})


def api_quiz_leaderboard(request, room_code):
    quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
    return JsonResponse({'success': True, 'leaderboard': public_participants(quiz)[:10]})


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


def player_request(request, room_code, name, data=None):
    data = data or {}
    quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
    code = data.get('hub_session', request.GET.get('hub_session'))
    runtime.pool_code(quiz, code)
    participant = quiz.participants.filter(name=name).filter(runtime.pool_filter(code)).first()
    if not participant or not valid_player_token(data.get('player_token') or request.GET.get('player_token'), participant):
        raise PermissionError('Teilnahme nicht bestaetigt.')
    return quiz, participant


@require_POST
def pending_input(request, room_code, participant_name):
    try:
        data = json.loads(request.body or '{}')
        quiz, participant = player_request(request, room_code, participant_name, data)
        saved = runtime.store_pending(quiz, participant, data.get('user_answer', ''), data)
        return JsonResponse({'success': True, 'pending_saved': saved})
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except (ValueError, TypeError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=409)


@require_POST
def acknowledge_reveal(request, room_code, participant_name):
    try:
        data = json.loads(request.body or '{}')
        quiz, participant = player_request(request, room_code, participant_name, data)
        runtime.acknowledge(quiz, participant, data)
        return JsonResponse(runtime.build_state(quiz, participant.hub_session_code, participant))
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except (ValueError, TypeError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=409)


def public_participants(quiz):
    runtime.build_state(quiz)
    quiz.refresh_from_db()
    session = quiz.session
    entry = runtime.current_run(session)
    rows = []
    for p in runtime.participants(quiz):
        st = entry['states'].get(str(p.pk), {}) if entry else {}
        visible = st.get('visible_stars', 0) if entry else p.total_points
        rows.append({'name': p.name, 'total_points': p.overall_points if quiz.status == 'completed' else visible,
            'overall_points': p.overall_points, 'is_busted': st.get('visible_busted', False),
            'questions_answered': 0, 'status': 'busted' if st.get('visible_busted') else 'playing'})
    if quiz.uses_ranking_mode() and quiz.status != 'completed':
        rows.sort(key=lambda row: (row['is_busted'], abs(21 - row['total_points']), row['name']))
    else:
        rows.sort(key=lambda row: (-row['total_points'], row['name']))
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    return rows
