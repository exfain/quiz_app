from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import Count, Q, Avg
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.contrib import messages
from django.urls import NoReverseMatch, reverse
import json
import math
import re
import uuid
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from QuizGame.models import Quiz, QuizQuestion, QuizParticipant, QuizAnswer, QuizSession, QuizBundle
from sorting_ladder.models import SortingLadderGame, SortingLadderParticipant, SortingQuestion, SortingItem, SortingLadderSession, SortingBundle
from sorting_ladder.runtime import sorting_ladder_reveal_ready_at
from Assign.models import AssignQuiz, AssignQuestion, AssignParticipant, AssignBundle
from Assign.scoreboard import build_question_scoreboard
from Estimation.models import EstimationQuiz, EstimationQuestion, EstimationParticipant, EstimationBundle
from where_is_this.models import WhereQuiz, WhereQuestion, WhereParticipant, WhereBundle, WhereDistanceZone
from where_is_this.geo import WEB_MERCATOR_MAX_LATITUDE
from black_jack_quiz.models import BlackJackQuiz, BlackJackQuestion, BlackJackParticipant, BlackJackBundle
from clue_rush.models import ClueRushGame, ClueRushParticipant, ClueQuestion, Clue, ClueAnswer, ClueRushSession
from who_is_that.models import WhoThatQuiz, WhoThatQuestion, WhoThatParticipant, WhoThatBundle
from who_is_lying.models import WhoQuiz, WhoQuestion, WhoParticipant, WhoBundle
from wer_weiss_mehr.models import (
    WerWeissMehrAnswerOption,
    WerWeissMehrGame,
    WerWeissMehrParticipant,
    WerWeissMehrQuestion,
    WerWeissMehrSession,
    normalize_answer_text,
)
from buzzer.models import BuzzerGame, BuzzerParticipant
from host_points.models import HostPointsGame, HostPointsParticipant
from wann_war_das.models import WannWarDasAnswer, WannWarDasGame, WannWarDasParticipant, WannWarDasQuestion
from wer_weiss_mehr.services import build_game_state
from games_hub.active_game_guard import (
    _end_game_cleanly,
    get_game_model_map,
    resolve_session_game_activation_for_room,
)
from games_hub.models import GameRuntimeState, HubSession, HubParticipant, HubGameStep
from games_hub.host_permissions import authorize_game_host, user_can_manage_hub_session
from games_hub.authoritative_state import current_snapshot, present_question, reset_question_flow
from games_hub.unit_tutorial_runtime import get_unit_tutorial_state, is_current_unit_tutorial_question
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase


def is_admin(user):
    """Check if user is admin/staff"""
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def _get_lobby_url(request, room_code):
    """Return the full participant lobby URL for a game room_code, or None."""
    # Prefer hub_session from the URL parameter (set when navigating from the hub monitor)
    hub_session = request.GET.get('hub_session')
    if hub_session:
        return f"{request.scheme}://{request.get_host()}/hub/lobby/{hub_session}/"
    step = HubGameStep.objects.filter(room_code=room_code).select_related('session').first()
    if step:
        return f"{request.scheme}://{request.get_host()}/hub/lobby/{step.session.code}/"
    return None


def _extract_hub_session_code(request):
    session_code = (request.GET.get('hub_session') or request.POST.get('hub_session') or '').strip()
    if session_code:
        return session_code
    try:
        data = json.loads(request.body or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    session_code = (data.get('hub_session') or data.get('session_code') or '').strip()
    return session_code or None


def _get_active_hub_session_code_for_room(game_key, room_code):
    steps = HubGameStep.objects.select_related('session').filter(
        game_key=game_key,
        room_code=room_code,
    )
    active_step = steps.filter(session__ended_at__isnull=True).order_by('-id').first()
    step = active_step or steps.order_by('-id').first()
    return step.session.code if step else None


def _guard_session_game_start(request, game_key, room_code):
    activation = resolve_session_game_activation_for_room(
        game_key,
        room_code,
        check_only=True,
        session_code=_extract_hub_session_code(request),
    )
    if activation.get('success'):
        return None

    payload = {
        'success': False,
        'error': activation.get('message') or activation.get('error') or 'Unable to start this game.',
    }
    if activation.get('conflict'):
        payload['conflict'] = True
    if activation.get('active_game'):
        payload['active_game'] = activation['active_game']
    if activation.get('check_in_required'):
        payload['check_in_required'] = True
        payload['check_in_status'] = activation.get('check_in_status')
        payload['locked_participant_count'] = activation.get('locked_participant_count')
        return JsonResponse(payload, status=428)
    return JsonResponse(payload, status=409 if activation.get('conflict') else 400)


def admin_required(view_func):
    """Decorator to require admin access"""
    return user_passes_test(is_admin, login_url='/admin/login/')(view_func)


def admin_login(request):
    """Admin login page"""
    if request.user.is_authenticated and is_admin(request.user):
        return redirect('admin_dashboard:home')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            if is_admin(user):
                login(request, user)
                messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
                next_url = request.GET.get('next', 'admin_dashboard:home')
                return redirect(next_url)
            else:
                messages.error(request, 'You do not have admin privileges to access this dashboard.')
        else:
            messages.error(request, 'Invalid username or password.')
    
    return render(request, 'admin_dashboard/login.html')


def admin_logout_view(request):
    """Admin logout view"""
    logout(request)
    messages.info(request, 'You have been logged out successfully.')
    return redirect('admin_dashboard:login')


@login_required
@require_POST
def end_session(request):
    """End (close) a single hub session by code"""
    if not is_admin(request.user):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    try:
        import json
        data = json.loads(request.body)
        session_code = data.get('session_code')
        if not session_code:
            return JsonResponse({'success': False, 'error': 'session_code required'}, status=400)
        session = HubSession.objects.get(code=session_code)
        if not user_can_manage_hub_session(request.user, session):
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        session_room_codes = list(
            session.steps.exclude(room_code='').values_list('room_code', flat=True)
        )
        if session_room_codes:
            game_models = [
                Quiz, EstimationQuiz, AssignQuiz, WhereQuiz, WhoQuiz,
                WhoThatQuiz, BlackJackQuiz, ClueRushGame, SortingLadderGame,
                BuzzerGame, HostPointsGame, WannWarDasGame,
            ]
            now = timezone.now()
            for model in game_models:
                model.objects.filter(
                    room_code__in=session_room_codes,
                    status__in=['active', 'inactive'],
                ).update(status='completed', ended_at=now)
        session.ended_at = timezone.now()
        session.is_active = False
        session.save(update_fields=['ended_at', 'is_active'])
        return JsonResponse({'success': True})
    except HubSession.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Session nicht gefunden'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def end_all_active_games(request):
    """End all currently active games across every supported game type."""
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    try:
        ended_games_count = 0
        with transaction.atomic():
            for model in get_game_model_map().values():
                for game in model.objects.filter(status='active'):
                    _end_game_cleanly(game)
                    ended_games_count += 1

        return JsonResponse({'success': True, 'ended_games_count': ended_games_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def delete_session(request):
    """Delete a single hub session by code"""
    if not is_admin(request.user):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    try:
        import json
        data = json.loads(request.body)
        session_code = data.get('session_code')
        if not session_code:
            return JsonResponse({'success': False, 'error': 'session_code required'}, status=400)
        session = get_object_or_404(HubSession, code=session_code)
        if not user_can_manage_hub_session(request.user, session):
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        session.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def duplicate_session(request):
    """Create a copy of an ended session with status 'planned' (started_at=None, ended_at=None).
    Each game step gets a fresh game instance; selected questions are copied over."""
    if not is_admin(request.user):
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    try:
        import random, string as _string
        data = json.loads(request.body)
        session_code = data.get('session_code')
        if not session_code:
            return JsonResponse({'success': False, 'error': 'session_code required'}, status=400)

        original = get_object_or_404(HubSession, code=session_code)
        if not user_can_manage_hub_session(request.user, original):
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        # Generate a unique new code
        def _gen():
            return ''.join(random.choices(_string.ascii_uppercase + _string.digits, k=6))
        new_code = _gen()
        while HubSession.objects.filter(code=new_code).exists():
            new_code = _gen()

        new_session = HubSession.objects.create(
            code=new_code,
            name=original.name,
            creator=request.user,
            games_weight=original.games_weight,
            is_active=False,
            # started_at and ended_at default to None → planned
        )

        # Map game_key → (GameModel, QuestionModel)
        game_model_map = {
            'quiz':           (Quiz,              QuizQuestion),
            'assign':         (AssignQuiz,        None),
            'estimation':     (EstimationQuiz,    None),
            'where':          (WhereQuiz,         None),
            'who':            (WhoQuiz,           None),
            'who_that':       (WhoThatQuiz,       None),
            'blackjack':      (BlackJackQuiz,     None),
            'sorting_ladder': (SortingLadderGame, None),
            'clue_rush':      (ClueRushGame,      None),
            'wer_weiss_mehr': (WerWeissMehrGame,  None),
            'buzzer':         (BuzzerGame,        None),
            'host_points':    (HostPointsGame,    None),
            'wann_war_das':   (WannWarDasGame,   None),
        }

        for step in original.steps.order_by('order'):
            entry = game_model_map.get(step.game_key)
            new_room_code = ''

            if entry:
                game_model, _ = entry
                # Create fresh game instance
                new_game = game_model.objects.create(
                    title=step.title or step.game_key,
                    creator=request.user,
                    status='waiting',
                )
                new_room_code = new_game.room_code

                # Copy selected questions from original game
                if step.room_code:
                    try:
                        orig_game = game_model.objects.get(room_code=step.room_code)
                        if hasattr(new_game, 'selected_questions') and hasattr(orig_game, 'selected_questions'):
                            new_game.selected_questions.set(orig_game.selected_questions.all())
                        if step.game_key == 'buzzer':
                            new_game.points_per_correct = orig_game.points_per_correct
                            new_game.planned_rounds = orig_game.planned_rounds
                            new_game.save(update_fields=['points_per_correct', 'planned_rounds'])
                        elif step.game_key == 'host_points':
                            pass
                        elif step.game_key == 'wann_war_das':
                            new_game.question_order = getattr(orig_game, 'question_order', [])
                            new_game.save(update_fields=['question_order'])
                        elif step.game_key == 'blackjack' and getattr(orig_game, 'tutorial_set_number', None):
                            new_game.tutorial_set_number = orig_game.tutorial_set_number
                            new_game.save(update_fields=['tutorial_set_number'])
                        elif getattr(orig_game, 'tutorial_question_id', None):
                            new_game.tutorial_question_id = orig_game.tutorial_question_id
                            new_game.save(update_fields=['tutorial_question'])
                    except game_model.DoesNotExist:
                        pass

            HubGameStep.objects.create(
                session=new_session,
                order=step.order,
                game_key=step.game_key,
                title=step.title,
                room_code=new_room_code,
            )

        return JsonResponse({'success': True, 'new_code': new_code})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
def clear_all_sessions(request):
    """Clear all quiz sessions"""
    if not request.user.is_superuser:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    
    try:
        # Delete all quiz sessions
        QuizSession.objects.all().delete()
        # Delete all hub sessions
        HubSession.objects.all().delete()
        
        messages.success(request, 'All sessions have been cleared successfully.')
        return JsonResponse({'success': True})
    except Exception as e:
        messages.error(request, f'Error clearing sessions: {str(e)}')
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@admin_required
@require_POST
def sync_supabase(request):
    """Trigger a sync of all data from the default DB to Supabase.

    Returns JSON: {"status": "ok", "synced": <count>} on success.
    """
    try:
        logs = []

        class _Stdout:
            def write(self, msg):  # noqa: D401
                logs.append(str(msg))

        class _Stderr:
            def write(self, msg):  # noqa: D401
                logs.append(str(msg))

        synced_count, synced_models = sync_all_models_to_supabase(stdout=_Stdout(), stderr=_Stderr())
        return JsonResponse(
            {
                "status": "ok",
                "synced": synced_count,
                "synced_models": synced_models,
                "log": logs,
            }
        )
    except Exception as e:  # pylint: disable=broad-except
        return JsonResponse({"status": "error", "error": str(e)}, status=500)


@admin_required
@require_POST
def restore_supabase(request):
    """Restore all data from Supabase into the local database.

    Returns JSON: {"status": "ok", "restored": <count>, "restored_models": [...]} on success.
    """
    try:
        logs = []

        class _Stdout:
            def write(self, msg):  # noqa: D401
                logs.append(str(msg))

        class _Stderr:
            def write(self, msg):  # noqa: D401
                logs.append(str(msg))

        restored_count, restored_models = restore_all_models_from_supabase(stdout=_Stdout(), stderr=_Stderr())
        return JsonResponse(
            {
                "status": "ok",
                "restored": restored_count,
                "restored_models": restored_models,
                "log": logs,
            }
        )
    except Exception as e:  # pylint: disable=broad-except
        return JsonResponse({"status": "error", "error": str(e)}, status=500)


# =====================
# Clue Rush Admin Views
# =====================
@admin_required
def clue_rush_management(request):
    games = ClueRushGame.objects.all().order_by('-created_at')
    total_questions = ClueQuestion.objects.filter(is_active=True).count()

    context = {
        'games': games,
        'total_questions': total_questions,
    }
    return render(request, 'admin_dashboard/clue_rush_management.html', context)


@admin_required
@require_POST
def create_clue_rush_game(request):
    try:
        data = json.loads(request.body or '{}')
        title = (data.get('title') or '').strip() or 'Clue Rush'
        question_ids = data.get('question_ids') or []

        quiz = ClueRushGame.objects.create(
            title=title,
            creator=request.user,
            status='waiting',
        )

        # Attach selected questions (only active ones the user can access)
        if question_ids:
            qs = ClueQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        # Create session
        ClueRushSession.objects.create(quiz=quiz)

        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'game_id': quiz.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def create_clue_rush_custom_game(request):
    """Create a custom Clue Rush quiz with a title and selected question IDs."""
    try:
        data = json.loads(request.body or '{}')
        title = (data.get('title') or '').strip() or 'Clue Rush'
        question_ids = data.get('question_ids') or []
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = ClueRushGame.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=[int(i) for i in question_ids],
            creator=request.user,
        )

        if question_ids:
            qs = ClueQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        # Always create a session on creation for admin monitor
        ClueRushSession.objects.create(quiz=quiz)

        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'game_id': quiz.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_clue_rush_custom_game(request):
    """Update a custom Clue Rush quiz: title and selected questions."""
    try:
        data = json.loads(request.body or '{}')
        game_id = data.get('game_id')
        if not game_id:
            return JsonResponse({'success': False, 'error': 'game_id is required'}, status=400)
        quiz = get_object_or_404(ClueRushGame, id=game_id)

        # Restrict to owner or superuser
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        title = (data.get('title') or '').strip()
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        if isinstance(data.get('question_ids'), list):
            qs = ClueQuestion.objects.filter(id__in=data['question_ids'], is_active=True)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in data['question_ids']]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def delete_clue_rush_game(request):
    try:
        data = json.loads(request.body or '{}')
        game_id = data.get('quiz_id')
        if not game_id:
            return JsonResponse({'success': False, 'error': 'game_id is required'}, status=400)
        quiz = get_object_or_404(ClueRushGame, id=game_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        quiz.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def clue_rush_monitor(request, room_code):
    hub_session = request.GET.get('hub_session')
    from clue_rush.runtime import reconcile_clue_schedule

    reconcile_clue_schedule(room_code)
    quiz = get_object_or_404(ClueRushGame, room_code=room_code)

    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:clue_rush_management')

    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    # If the quiz has a predefined set of selected questions, show only those
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().prefetch_related('clues').order_by('-created_at')
    else:
        available_questions = ClueQuestion.objects.filter(created_by=request.user).prefetch_related('clues').order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = ClueRushSession.objects.get_or_create(quiz=quiz)
    for question in available_questions:
        first_clue = next(iter(question.clues.all()), None)
        question.host_timer_seconds = first_clue.duration if first_clue else question.time_limit

    current_clue_time_left = 0
    current_clue_has_next = False
    current_revealed_clue_count = 0
    current_question = quiz.current_question if quiz.current_question_id else None
    current_clue = quiz.current_clue if (
        current_question and
        quiz.current_clue_id and
        quiz.current_clue.clue_question_id == current_question.id
    ) else None
    current_clue_order = quiz_session.current_clue_number if quiz_session.is_clue_active else None
    if current_question:
        current_revealed_clue_count = current_question.get_revealed_clue_count(
            current_clue=current_clue,
            current_clue_order=current_clue_order,
        )
    if quiz.current_question_id and quiz.current_clue_id:
        clue_ids = list(quiz.current_question.clues.order_by('order', 'id').values_list('id', flat=True))
        try:
            current_index = clue_ids.index(quiz.current_clue_id)
        except ValueError:
            current_index = -1
        current_clue_has_next = current_index >= 0 and current_index < (len(clue_ids) - 1)
        if current_clue_has_next and quiz_session.clue_end_time:
            current_clue_time_left = max(
                math.ceil((quiz_session.clue_end_time - timezone.now()).total_seconds()),
                0,
            )

    response_session_code = hub_session or _get_active_hub_session_code_for_room('clue_rush', room_code)
    live_response_question = quiz.current_question
    scoped_live_answers = ClueAnswer.objects.filter(quiz=quiz).select_related('participant', 'question')
    if quiz.started_at:
        scoped_live_answers = scoped_live_answers.filter(submitted_at__gte=quiz.started_at)
    if response_session_code is not None:
        scoped_live_answers = scoped_live_answers.filter(participant__hub_session_code=response_session_code)
    if live_response_question is None:
        latest_answer = scoped_live_answers.order_by('-submitted_at', '-id').first()
        live_response_question = latest_answer.question if latest_answer else None

    initial_live_responses = []
    if live_response_question is not None:
        question_answers = scoped_live_answers.filter(question=live_response_question).order_by('-submitted_at', '-id')
        for response in question_answers:
            initial_live_responses.append({
                'answer_id': response.id,
                'participant_id': response.participant_id,
                'participant_name': response.participant.name,
                'question_id': response.question_id,
                'answer_text': response.answer_text,
                'is_correct': response.is_correct,
                'is_manual_override': response.is_manually_corrected,
                'can_mark_correct': not response.is_correct,
                'points_earned': response.points_earned,
                'time_taken': response.time_taken,
                'total_score': response.participant.total_score,
                'submitted_at': response.submitted_at.isoformat() if response.submitted_at else None,
                'submitted_clue_number': response.submitted_clue_number,
            })

    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'current_clue_time_left': current_clue_time_left,
        'current_clue_has_next': current_clue_has_next,
        'current_revealed_clue_count': current_revealed_clue_count,
        'question_runtime': current_snapshot(
            'clue_rush',
            room_code,
            response_session_code,
        ),
        'initial_live_responses': initial_live_responses,
        'lobby_url': _get_lobby_url(request, room_code),
        'current_unit_is_tutorial': is_current_unit_tutorial_question('clue_rush', quiz.room_code, response_session_code, quiz.current_question_id),
    }
    return render(request, 'admin_dashboard/clue_rush_monitor.html', context)


def _build_clue_rush_progress_history(quiz, participant):
    answers = ClueAnswer.objects.filter(
        quiz=quiz,
        participant=participant,
    ).select_related('question').order_by('submitted_at', 'id')
    if quiz.started_at:
        answers = answers.filter(submitted_at__gte=quiz.started_at)

    history = []
    for idx, answer in enumerate(answers, start=1):
        max_points = answer.total_clues_at_submission or answer.question.clues.count()
        achieved_points = answer.points_earned if answer.is_correct else 0
        if max_points > 0:
            achieved_points = max(0, min(achieved_points, max_points))
        history.append({
            'question_number': idx,
            'correct_answer': answer.question.answer,
            'achieved_points': achieved_points,
            'max_points': max_points,
        })
    return history


@admin_required
@require_POST
def promote_clue_rush_answer_correct(request, room_code):
    try:
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = json.loads(request.body or '{}')
        answer_id = data.get('answer_id')
        if not answer_id:
            return JsonResponse({'success': False, 'error': 'answer_id is required'}, status=400)

        session_code = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('clue_rush', room_code)

        with transaction.atomic():
            answer = get_object_or_404(
                ClueAnswer.objects.select_for_update().select_related('participant', 'question'),
                id=answer_id,
                quiz=quiz,
            )

            if quiz.started_at and answer.submitted_at and answer.submitted_at < quiz.started_at:
                return JsonResponse({
                    'success': False,
                    'error': 'Only answers from the current Clue Rush run can be corrected.',
                }, status=400)

            if session_code is not None and answer.participant.hub_session_code != session_code:
                return JsonResponse({
                    'success': False,
                    'error': 'This answer does not belong to the active session.',
                }, status=400)

            if answer.is_correct:
                return JsonResponse({
                    'success': False,
                    'error': 'This answer is already marked correct.',
                }, status=400)

            if answer.submitted_clue_number <= 0:
                if quiz.current_question_id == answer.question_id:
                    answer.submitted_clue_number = quiz.current_question.get_revealed_clue_count(
                        current_clue=quiz.current_clue if quiz.current_clue_id else None,
                        current_clue_order=getattr(getattr(quiz, 'session', None), 'current_clue_number', None),
                    )
                if answer.submitted_clue_number <= 0:
                    return JsonResponse({
                        'success': False,
                        'error': 'This answer has no stored clue position and cannot be corrected safely.',
                    }, status=400)

            if answer.total_clues_at_submission <= 0:
                answer.total_clues_at_submission = answer.question.clues.count()

            answer.is_correct = True
            answer.is_manually_corrected = True
            answer.points_earned = answer.calculate_points_from_submission_state()
            answer.save()
            answer.participant.refresh_from_db(fields=['total_score'])

        response_payload = {
            'answer_id': answer.id,
            'participant_id': answer.participant_id,
            'participant_name': answer.participant.name,
            'question_id': answer.question_id,
            'answer_text': answer.answer_text,
            'is_correct': True,
            'is_manual_override': True,
            'can_mark_correct': False,
            'points_earned': answer.points_earned,
            'time_taken': answer.time_taken,
            'total_score': answer.participant.total_score,
            'submitted_at': answer.submitted_at.isoformat() if answer.submitted_at else None,
            'submitted_clue_number': answer.submitted_clue_number,
        }
        progress_history = _build_clue_rush_progress_history(quiz, answer.participant)

        channel_layer = get_channel_layer()
        if channel_layer is not None:
            async_to_sync(channel_layer.group_send)(
                f'cluerush_{quiz.room_code}',
                {
                    'type': 'answer_corrected',
                    'response': response_payload,
                    'participant_id': answer.participant_id,
                    'participant_name': answer.participant.name,
                    'question_id': answer.question_id,
                    'total_score': answer.participant.total_score,
                    'progress_history': progress_history,
                }
            )

        return JsonResponse({
            'success': True,
            **response_payload,
            'progress_history': progress_history,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def end_clue_rush_game_by_room_code(request, room_code):
    """End a Clue Rush game by room code."""
    try:
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)

        session = getattr(quiz, 'session', None)
        if session:
            session.is_question_active = False
            session.is_clue_active = False
            session.question_end_time = None
            session.clue_end_time = None
            session.current_clue_number = 0
            session.save(update_fields=[
                'is_question_active',
                'is_clue_active',
                'question_end_time',
                'clue_end_time',
                'current_clue_number',
            ])

        quiz.current_question = None
        quiz.question_start_time = None
        quiz.current_clue = None
        quiz.clue_start_time = None
        quiz.end_quiz('completed')
        quiz.save(update_fields=[
            'status',
            'ended_at',
            'current_question',
            'question_start_time',
            'current_clue',
            'clue_start_time',
        ])

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def sorting_ladder_monitor(request, room_code):
    """Admin monitor for a live Sorting Ladder game session."""
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(SortingLadderGame, room_code=room_code)

    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:sorting_ladder_management')

    if quiz.status == 'waiting':
        # A waiting monitor always starts from question selection. Persisted
        # state from a previous run is cleared by the authoritative start action.
        quiz.current_question = None
        quiz.tutorial_active = False

    participants_qs = quiz.participants.all()
    if hub_session:
        participants_qs = participants_qs.filter(hub_session_code=hub_session)
    participants = participants_qs.order_by('-rounds_survived', 'name')

    # If the game has predefined selected topics, show only those
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = SortingQuestion.objects.filter(
            created_by=request.user,
            is_active=True,
        ).order_by('-created_at')
    
    #Calculate total time per available question
    for question in available_questions:
        question.total_time = question.round_time_limit * (question.elements.count() - 1)
    
    #Get current question
    if quiz.current_question:
        quiz.current_question.total_time = quiz.current_question.round_time_limit * (quiz.current_question.elements.count() - 1) + 10

    session, _ = SortingLadderSession.objects.get_or_create(quiz=quiz)
    question_runtime = current_snapshot('sorting_ladder', room_code, hub_session)
    reveal_ready_at = sorting_ladder_reveal_ready_at(
        content_revealed_at=parse_datetime(
            question_runtime.get('content_revealed_at') or ''
        ),
        item_count=(quiz.current_question.elements.count() if quiz.current_question else 0),
        round_number=max(int(session.current_round or 0), 1),
    )
    current_round_time_left = 0
    if quiz.current_question and session.is_round_active and session.round_end_time:
        current_round_time_left = max(
            math.ceil((session.round_end_time - timezone.now()).total_seconds()),
            0,
        )

    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'session': session,
        'question_runtime': question_runtime,
        'sorting_reveal_ready_at': reveal_ready_at,
        'current_round_time_left': current_round_time_left,
        'lobby_url': _get_lobby_url(request, room_code),
        'hub_session': hub_session or '',
        'current_unit_is_tutorial': is_current_unit_tutorial_question('sorting_ladder', quiz.room_code, hub_session, quiz.current_question_id),
    }
    return render(request, 'admin_dashboard/sorting_ladder_monitor.html', context)


@admin_required
@require_POST
def start_sorting_ladder_game(request, room_code):
    """Start a Sorting Ladder game (admin-only)."""
    try:
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this game.',
            }, status=403)

        guard_response = _guard_session_game_start(request, 'sorting_ladder', room_code)
        if guard_response:
            return guard_response

        # Use the model helper, similar to quiz.start_quiz()
        if hasattr(quiz, 'start_quiz'):
            quiz.start_quiz()
        else:
            quiz.status = 'active'
            quiz.save(update_fields=['status'])

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e),
        }, status=400)


@admin_required
@require_POST
def end_sorting_ladder_game_by_room_code(request, room_code):
    """End a Sorting Ladder game by room code (admin-only)."""
    try:
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this game.',
            }, status=403)

        # Use the model helper, similar to quiz.end_quiz('completed')
        if hasattr(quiz, 'end_quiz'):
            quiz.end_quiz()
        else:
            quiz.status = 'completed'
            quiz.save(update_fields=['status'])

        # Ensure any active round is stopped in the session
        try:
            session = quiz.session
            if session.is_round_active:
                session.end_round()
        except (SortingLadderSession.DoesNotExist, AttributeError):
            pass

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e),
        }, status=400)


@admin_required
@require_POST
def send_sorting_ladder_topic(request, room_code):
    """Select a topic for the Sorting Ladder game and initialize its session.

    This is analogous to send_question for quizzes but operates on SortingQuestion
    and SortingLadderSession.
    """
    try:
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this game.',
            }, status=403)

        data = json.loads(request.body or '{}')
        topic_id = data.get('topic_id')
        time_limit = data.get('time_limit_seconds')

        if not topic_id:
            return JsonResponse({
                'success': False,
                'error': 'topic_id is required.',
            }, status=400)

        topic = get_object_or_404(SortingQuestion, id=topic_id, is_active=True)

        # If the game has predefined selected topics, enforce membership
        if quiz.selected_questions.exists() and not quiz.selected_questions.filter(id=topic.id).exists():
            return JsonResponse({
                'success': False,
                'error': 'This topic is not part of the selected set for this game.',
            }, status=400)

        # Initialize / reset session similar to consumer.initialize_session_for_topic
        elements = list(topic.elements.order_by('correct_rank'))
        if len(elements) < 3:
            return JsonResponse({
                'success': False,
                'error': 'Not enough items for this topic (need at least 3).',
            }, status=400)

        smallest = elements[0]
        largest = elements[-1]

        session, _ = SortingLadderSession.objects.get_or_create(quiz=quiz)
        session.placed_elements.clear()
        session.placed_elements.add(smallest, largest)
        session.active_element = None
        session.current_round = 0
        session.is_round_active = False

        if time_limit is not None:
            try:
                session.time_limit_seconds = int(time_limit)
            except (TypeError, ValueError):
                pass

        session.round_start_time = None
        session.round_end_time = None
        session.save()

        quiz.current_question = topic
        quiz.save(update_fields=['current_question'])

        payload = {
            'current_round': session.current_round,
            'time_limit_seconds': session.time_limit_seconds,
            'placed_elements': [
                {'id': smallest.id, 'text': smallest.text},
                {'id': largest.id, 'text': largest.text},
            ],
            'active_element': None,
        }

        return JsonResponse({
            'success': True,
            'topic': {
                'id': topic.id,
                'title': topic.title,
                'description': topic.description,
            },
            'session': payload,
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e),
        }, status=400)


@admin_required
@require_POST
def end_sorting_ladder_round(request, room_code):
    """End the current Sorting Ladder round and return surviving participants.

    This is analogous to end_question for quizzes.
    """
    try:
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this game.',
            }, status=403)

        try:
            session = quiz.session
        except (SortingLadderSession.DoesNotExist, AttributeError):
            return JsonResponse({
                'success': False,
                'error': 'No active session for this game.',
            }, status=400)

        # End the round and collect survivors, similar to consumer.end_round_db
        session.end_round()
        survivors_qs = quiz.participants.filter(is_eliminated=False, is_active=True)
        survivors = list(survivors_qs.values('id', 'name', 'rounds_survived'))

        return JsonResponse({
            'success': True,
            'survivors': survivors,
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e),
        }, status=400)


@admin_required
def api_clue_rush_participants(request, room_code):
    try:
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        participants = quiz.participants.all().order_by('-total_score', 'name')
        data = [{
            'id': p.id,
            'name': p.name,
            'total_score': p.total_score,
            'has_guessed': p.has_guessed,
            'guess_correct': p.guess_correct,
            'points_earned': p.points_earned,
            'is_active': p.is_active,
        } for p in participants]
        return JsonResponse({'success': True, 'participants': data, 'count': len(data)})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def api_clue_rush_stats(request, room_code):
    try:
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        participants = quiz.participants.all()
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'current_round': quiz.current_round,
            'total_clues': quiz.clues.count(),
        }
        return JsonResponse({'success': True, 'stats': stats})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def add_clue_rush_question(request):
    """Create a ClueQuestion with multiple clues."""
    try:
        data = json.loads(request.body or '{}')
        question_text = (data.get('question_text') or '').strip()
        answer = (data.get('answer') or '').strip()
        points = int(data.get('points') or 10)
        time_limit = int(data.get('time_limit') or 30)
        clues = data.get('clues') or []

        if not question_text or not answer or not isinstance(clues, list) or len(clues) == 0:
            return JsonResponse({'success': False, 'error': 'question_text, answer and at least one clue are required.'}, status=400)

        q = ClueQuestion.objects.create(
            question_text=question_text,
            points=points,
            time_limit=time_limit,
            answer=answer,
            created_by=request.user,
        )
        # Normalize and create clues
        for idx, c in enumerate(clues, start=1):
            clue_text = (c.get('clue_text') or '').strip()
            order = int(c.get('order') or idx)
            duration = int(c.get('duration') or 10)
            if not clue_text:
                continue
            Clue.objects.create(
                clue_question=q,
                clue_text=clue_text,
                order=order,
                duration=duration,
            )

        return JsonResponse({'success': True, 'question_id': q.id})
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid numeric values.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_clue_rush_question(request):
    """Update fields of ClueQuestion and optionally replace its clues."""
    try:
        data = json.loads(request.body or '{}')
        qid = data.get('question_id')
        if not qid:
            return JsonResponse({'success': False, 'error': 'question_id is required'}, status=400)
        q = get_object_or_404(ClueQuestion, id=qid)
        if not request.user.is_superuser and q.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if 'question_text' in data:
            q.question_text = (data.get('question_text') or q.question_text).strip()
            fields_to_update.append('question_text')
        if 'answer' in data:
            q.answer = (data.get('answer') or q.answer).strip()
            fields_to_update.append('answer')
        if 'points' in data:
            q.points = int(data.get('points'))
            fields_to_update.append('points')
        if 'time_limit' in data:
            q.time_limit = int(data.get('time_limit'))
            fields_to_update.append('time_limit')
        if fields_to_update:
            q.save(update_fields=fields_to_update)

        # Replace clues if provided
        if isinstance(data.get('clues'), list):
            q.clues.all().delete()
            for idx, c in enumerate(data['clues'], start=1):
                clue_text = (c.get('clue_text') or '').strip()
                if not clue_text:
                    continue
                order = int(c.get('order') or idx)
                duration = int(c.get('duration') or 10)
                Clue.objects.create(
                    clue_question=q,
                    clue_text=clue_text,
                    order=order,
                    duration=duration,
                )

        return JsonResponse({'success': True})
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid numeric values.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def delete_clue_rush_question(request):
    """Delete or deactivate a ClueQuestion depending on usage."""
    try:
        data = json.loads(request.body or '{}')
        qid = data.get('question_id')
        if not qid:
            return JsonResponse({'success': False, 'error': 'question_id is required'}, status=400)
        q = get_object_or_404(ClueQuestion, id=qid)
        if not request.user.is_superuser and q.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        in_use = ClueAnswer.objects.filter(question=q).exists() or q.games.exists()
        if in_use:
            q.is_active = False
            q.save(update_fields=['is_active'])
            return JsonResponse({'success': True, 'message': 'Question deactivated (in use).'})
        else:
            q.delete()
            return JsonResponse({'success': True, 'message': 'Question deleted.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
def get_clue_rush_selected_questions(request, quiz_id: int):
    """Return questions attached to a specific quiz (selected_questions ManyToMany)."""
    try:
        quiz = get_object_or_404(ClueRushGame, id=quiz_id)
        # Restrict to owner or superuser
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'question_text': q.question_text,
            'answer': q.answer,
            'points': q.points,
            'time_limit': q.time_limit,
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': q.is_active,
            'is_tutorial': q.id == quiz.tutorial_question_id,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({
            'success': True,
            'questions': questions,
            'count': len(questions),
            'total_questions': quiz.total_questions,
            'scoring_mode': getattr(quiz, 'scoring_mode', 'simple'),
            'tutorial_question_id': quiz.tutorial_question_id,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)



@admin_required
def get_clue_rush_question_detail(request, question_id):
    """Return full details of a quiz question for editing"""
    try:
        question = get_object_or_404(ClueQuestion, id=question_id)
        # Restrict to owner or superuser
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = {
            'id': question.id,
            'question_text': question.question_text,
            'answer': question.answer,
            'points': question.points,
            'time_limit': question.time_limit,
            'is_active': question.is_active,
            'clues': [
                {
                    'id': c.id,
                    'clue_text': c.clue_text,
                    'order': c.order,
                    'duration': c.duration,
                }
                for c in question.clues.order_by('order', 'id')
            ],
        }
        return JsonResponse({'success': True, 'question': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


def get_clue_rush_questions(request):
    # Get page number from request
    page_number = request.GET.get('page', 1)
    
    # Get all active questions
    questions = ClueQuestion.objects.filter(is_active=True).order_by('-created_at')
    
    # Add search functionality
    search_query = request.GET.get('search', '')
    if search_query:
        questions = questions.filter(
            Q(question_text__icontains=search_query) |
            Q(points__icontains=search_query) |
            Q(time_limit__icontains=search_query)
        )
    
    # Paginate results (10 per page)
    paginator = Paginator(questions, 20)
    page_obj = paginator.get_page(page_number)
    
    # Prepare response data
    questions_data = []
    for question in page_obj:
        questions_data.append({
            'id': question.id,
            'question_text': question.question_text,
            'answer': question.answer,
            'no_of_clues': question.clues.count(),
            'points': question.points,
            'time_limit': question.time_limit,
            'created_at': question.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': question.is_active,
        })
    
    return JsonResponse({
        'questions': questions_data,
        'count': paginator.count,
        'pages': paginator.num_pages,
        'current_page': page_obj.number,
    })

# =====================
# Sorting Ladder Admin Views
# =====================
@admin_required
def sorting_ladder_management(request):
    quizzes = SortingLadderGame.objects.all().order_by('-created_at')
    total_topics = SortingQuestion.objects.filter(is_active=True).count()
    bundles = SortingBundle.objects.filter(creator=request.user).prefetch_related('questions')
    return render(request, 'admin_dashboard/sorting_ladder_management.html', {
        'quizzes': quizzes,
        'total_topics': total_topics,
        'bundles': bundles,
    })

@admin_required
@require_POST
def create_sorting_ladder_game(request):
    data = json.loads(request.body or '{}')
    title = (data.get('title') or '').strip() or 'Sorting Ladder'
    quiz = SortingLadderGame.objects.create(
        title=title,
        creator=request.user,
        status='waiting',
    )
    if not hasattr(quiz, 'session'):
        from sorting_ladder.models import SortingLadderSession
        SortingLadderSession.objects.create(quiz=quiz)
    return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})


@admin_required
@require_POST
def create_sorting_ladder_custom_game(request):
    """Create a custom Sorting Ladder game with a title and selected topic IDs."""
    try:
        data = json.loads(request.body or '{}')
        title = (data.get('title') or '').strip() or 'Sorting Ladder'
        topic_ids = data.get('topic_ids') or []
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = SortingLadderGame.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=[int(i) for i in topic_ids],
            creator=request.user,
            status='waiting',
        )

        if isinstance(topic_ids, list) and topic_ids:
            qs = SortingQuestion.objects.filter(id__in=topic_ids, is_active=True)
            # selected_questions is a ManyToMany to SortingQuestion on the game model
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        # Ensure a session exists for monitoring
        if not hasattr(quiz, 'session'):
            from sorting_ladder.models import SortingLadderSession
            SortingLadderSession.objects.create(quiz=quiz)

        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})
    except Exception as e:  # pylint: disable=broad-except
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_sorting_selected_topics(request, quiz_id: int):
    """Return topics attached to a specific Sorting Ladder game (selected_questions M2M)."""
    try:
        quiz = get_object_or_404(SortingLadderGame, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        qs = quiz.selected_questions.all().order_by('-created_at')
        topics = [{
            'id': t.id,
            'title': t.question_text,
            'description': t.description,
            'item_count': t.elements.count(),
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': t.is_active,
            'is_tutorial': t.id == quiz.tutorial_question_id,
        } for t in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            topics.sort(key=lambda t: _omap.get(t['id'], len(_order)))
        return JsonResponse({'success': True, 'topics': topics, 'count': len(topics), 'tutorial_question_id': quiz.tutorial_question_id})
    except Exception as e:  # pylint: disable=broad-except
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_sorting_ladder_custom_game(request):
    """Update a custom Sorting Ladder game: title and selected topics."""
    try:
        data = json.loads(request.body or '{}')
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(SortingLadderGame, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        title = (data.get('title') or '').strip()
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        if isinstance(data.get('topic_ids'), list):
            qs = SortingQuestion.objects.filter(id__in=data['topic_ids'], is_active=True)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in data['topic_ids']]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except Exception as e:  # pylint: disable=broad-except
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def delete_sorting_ladder_game(request):
    data = json.loads(request.body or '{}')
    quiz_id = data.get('quiz_id') or data.get('quiz_id')
    if not quiz_id:
        return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)
    quiz = get_object_or_404(SortingLadderGame, id=quiz_id)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    quiz.delete()
    return JsonResponse({'success': True})

@admin_required
def get_sorting_topics(request):
    try:
        page = int(request.GET.get('page', 1))
    except ValueError:
        page = 1
    qs = SortingQuestion.objects.filter(is_active=True).order_by('-created_at')
    from django.core.paginator import Paginator
    paginator = Paginator(qs, 10)
    page_obj = paginator.get_page(page)
    topics = [{
        'id': t.id,
        'title': t.question_text,
        'description': t.description,
        'created_at': t.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'is_active': t.is_active,
        'item_count': t.elements.count(),
    } for t in page_obj.object_list]
    return JsonResponse({
        'success': True,
        'topics': topics,
        'count': qs.count(),
        'pages': paginator.num_pages,
        'current_page': page_obj.number,
    })

@admin_required
@require_POST
def add_sorting_topic(request):
    title = (request.POST.get('title') or '').strip()
    description = (request.POST.get('description') or '').strip()
    points = (request.POST.get('points') or '').strip()
    round_time_limit = (request.POST.get('round_time_limit') or '').strip()
    upper_label = (request.POST.get('upper_label') or 'Ascending').strip()
    lower_label = (request.POST.get('lower_label') or 'Descending').strip()

    if not title:
        return JsonResponse({'success': False, 'error': 'Title is required.'}, status=400)

    topic = SortingQuestion.objects.create(
        question_text=title,
        description=description,
        created_by=request.user,
        points=points,
        round_time_limit=round_time_limit,
        upper_label=upper_label,
        lower_label=lower_label,
    )

    # Optional items payload (JSON string from the modal)
    items_json = request.POST.get('items_json') or '[]'
    try:
        items = json.loads(items_json)
    except json.JSONDecodeError:
        items = []

    if isinstance(items, list):
        bulk_items = []
        for idx, item in enumerate(items, start=1):
            text = (item.get('text') or '').strip()
            if not text:
                continue
            try:
                order = item.get('order')
                rank = float(order) if order is not None else float(idx)
            except (TypeError, ValueError):
                rank = float(idx)
            bulk_items.append(SortingItem(
                topic=topic,
                text=text,
                correct_rank=rank,
            ))
        if bulk_items:
            SortingItem.objects.bulk_create(bulk_items)

    # Set starting item if specified
    starting_item_order_raw = request.POST.get('starting_item_order')
    if starting_item_order_raw:
        try:
            starting_rank = float(starting_item_order_raw)
            starting_item = topic.elements.filter(correct_rank=starting_rank).first()
            if starting_item:
                topic.starting_item = starting_item
                topic.save(update_fields=['starting_item'])
        except (TypeError, ValueError):
            pass

    return JsonResponse({'success': True, 'topic_id': topic.id})

@admin_required
@require_POST
def update_sorting_topic(request):
    topic_id = request.POST.get('topic_id')
    if not topic_id:
        return JsonResponse({'success': False, 'error': 'Missing topic_id'}, status=400)
    topic = get_object_or_404(SortingQuestion, id=topic_id, created_by=request.user)
    title = (request.POST.get('title') or topic.question_text).strip()
    description = (request.POST.get('description') or topic.description).strip()
    points = (request.POST.get('points') or topic.points).strip()
    round_time_limit = (request.POST.get('round_time_limit') or topic.round_time_limit).strip()
    is_active_raw = request.POST.get('is_active')

    upper_label = (request.POST.get('upper_label') or topic.upper_label).strip()
    lower_label = (request.POST.get('lower_label') or topic.lower_label).strip()

    topic.question_text = title
    topic.description = description
    topic.points = points
    topic.round_time_limit = round_time_limit
    topic.upper_label = upper_label
    topic.lower_label = lower_label
    if is_active_raw is not None:
        topic.is_active = str(is_active_raw).lower() in ('1', 'true', 'on', 'yes')
    topic.save()

    # Optional items payload: replace existing elements if provided
    items_json = request.POST.get('items_json')
    if items_json is not None:
        try:
            items = json.loads(items_json or '[]')
        except json.JSONDecodeError:
            items = []

        if isinstance(items, list):
            # Remove current items and recreate from payload
            topic.elements.all().delete()
            topic.starting_item = None
            bulk_items = []
            for idx, item in enumerate(items, start=1):
                text = (item.get('text') or '').strip()
                if not text:
                    continue
                try:
                    order = item.get('order')
                    rank = float(order) if order is not None else float(idx)
                except (TypeError, ValueError):
                    rank = float(idx)
                bulk_items.append(SortingItem(
                    topic=topic,
                    text=text,
                    correct_rank=rank,
                ))
            if bulk_items:
                SortingItem.objects.bulk_create(bulk_items)

    # Set starting item if specified
    starting_item_order_raw = request.POST.get('starting_item_order')
    if starting_item_order_raw:
        try:
            starting_rank = float(starting_item_order_raw)
            starting_item = topic.elements.filter(correct_rank=starting_rank).first()
            if starting_item:
                topic.starting_item = starting_item
                topic.save(update_fields=['starting_item'])
        except (TypeError, ValueError):
            pass
    else:
        topic.starting_item = None
        topic.save(update_fields=['starting_item'])

    return JsonResponse({'success': True})

@admin_required
@require_POST
def delete_sorting_topic(request):
    data = json.loads(request.body or '{}')
    topic_id = data.get('topic_id')
    if not topic_id:
        return JsonResponse({'success': False, 'error': 'topic_id is required'}, status=400)
    topic = get_object_or_404(SortingQuestion, id=topic_id, created_by=request.user)
    if topic.elements.exists() or topic.active_in_games.exists():
        topic.is_active = False
        topic.save(update_fields=['is_active'])
    else:
        topic.delete()
    return JsonResponse({'success': True})

@admin_required
def get_sorting_topic_detail(request, topic_id):
    topic = get_object_or_404(SortingQuestion, id=topic_id)
    if not request.user.is_superuser and topic.created_by != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    data = {
        'id': topic.id,
        'title': topic.question_text,
        'description': topic.description,
        'points': topic.points,
        'round_time_limit': topic.round_time_limit,
        'upper_label': topic.upper_label,
        'lower_label': topic.lower_label,
        'is_active': topic.is_active,
        'item_count': topic.elements.count(),
        'starting_item_order': float(topic.starting_item.correct_rank) if topic.starting_item else None,
        'items': [
            {
                'id': element.id,
                'text': element.text,
                'order': float(element.correct_rank),
            }
            for element in topic.elements.all().order_by('correct_rank', 'id')
        ],
    }
    return JsonResponse({'success': True, 'topic': data})


# =====================
# Wer weiß mehr Admin Views
# =====================

def _parse_wer_weiss_mehr_answers(raw_answers):
    answers = []
    if isinstance(raw_answers, str):
        raw_answers = [
            line
            for line in raw_answers.splitlines()
            if line.strip()
        ]

    for item in raw_answers or []:
        if isinstance(item, dict):
            canonical = (item.get('canonical_text') or item.get('text') or '').strip()
            aliases = item.get('aliases') or []
            if isinstance(aliases, str):
                aliases = [part.strip() for part in re.split(r'[;,]', aliases) if part.strip()]
        else:
            parts = [part.strip() for part in str(item).split('|', 1)]
            canonical = parts[0]
            aliases = [part.strip() for part in re.split(r'[;,]', parts[1])] if len(parts) > 1 else []
            aliases = [alias for alias in aliases if alias]
        if canonical:
            answers.append({'canonical_text': canonical, 'aliases': aliases})
    return answers


def _replace_wer_weiss_mehr_answers(question, answers):
    parsed_answers = _parse_wer_weiss_mehr_answers(answers)
    if not parsed_answers:
        raise ValueError('Mindestens eine korrekte Antwort ist erforderlich.')

    question.answers.all().delete()
    seen = set()
    for answer in parsed_answers:
        normalized_answer = normalize_answer_text(answer['canonical_text'])
        if normalized_answer in seen:
            continue
        seen.add(normalized_answer)
        WerWeissMehrAnswerOption.objects.create(
            question=question,
            canonical_text=answer['canonical_text'],
            aliases=answer.get('aliases') or [],
        )
    question.recalculate_answer_sort_order()


def _create_wer_weiss_mehr_inline_question_if_present(data, user):
    prompt = (data.get('question_text') or data.get('prompt') or '').strip()
    answers = data.get('answers') or data.get('answers_text') or []
    if not prompt and not answers:
        return None
    if not prompt:
        raise ValueError('Frage ist erforderlich.')

    parsed_answers = _parse_wer_weiss_mehr_answers(answers)
    if not parsed_answers:
        raise ValueError('Mindestens eine korrekte Antwort ist erforderlich.')

    question = WerWeissMehrQuestion.objects.create(
        question_text=prompt,
        round_time_limit=int(data.get('round_time_limit') or data.get('time_limit') or 30),
        created_by=user,
    )
    _replace_wer_weiss_mehr_answers(question, parsed_answers)
    return question


def _serialize_wer_weiss_mehr_question(question):
    answers = list(question.answers.order_by('sort_order', 'canonical_text', 'id'))
    return {
        'id': question.id,
        'question_text': question.question_text,
        'round_time_limit': question.round_time_limit,
        'answer_count': len(answers),
        'answers_preview': ', '.join(answer.canonical_text for answer in answers[:6]),
        'answers': [
            {
                'id': answer.id,
                'canonical_text': answer.canonical_text,
                'aliases': answer.aliases if isinstance(answer.aliases, list) else [],
                'sort_order': answer.sort_order,
            }
            for answer in answers
        ],
        'created_at': question.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'is_active': question.is_active,
    }


@admin_required
def wer_weiss_mehr_monitor(request, room_code):
    hub_session = request.GET.get('hub_session') or ''
    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:manage_games')
    if hub_session and not authorize_game_host(
        request.user, 'wer_weiss_mehr', room_code, hub_session
    ).allowed:
        return redirect('admin_dashboard:manage_games')

    participants = quiz.participants.all()
    if hub_session:
        participants = participants.filter(hub_session_code=hub_session)

    available_questions = quiz.selected_questions.filter(is_active=True).prefetch_related('answers')
    if not available_questions.exists():
        available_questions = WerWeissMehrQuestion.objects.filter(
            created_by=request.user,
            is_active=True,
        ).prefetch_related('answers')

    WerWeissMehrSession.objects.get_or_create(quiz=quiz)
    tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', quiz.room_code, hub_session)
    return render(request, 'admin_dashboard/wer_weiss_mehr_monitor.html', {
        'quiz': quiz,
        'participants': participants.order_by('-total_score', 'name'),
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'lobby_url': _get_lobby_url(request, room_code),
        'hub_session': hub_session,
        'current_unit_is_tutorial': bool(
            tutorial_state.get('current_unit_is_tutorial')
            and str(tutorial_state.get('tutorial_question_id') or '') == str(quiz.current_question_id or '')
        ),
    })


@admin_required
@require_POST
def end_wer_weiss_mehr_game_by_room_code(request, room_code):
    """End a Wer weiss mehr game by room code."""
    try:
        quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this game.',
            }, status=403)

        hub_session = (
            _extract_hub_session_code(request)
            or _get_active_hub_session_code_for_room('wer_weiss_mehr', room_code)
        )
        authorization = authorize_game_host(
            request.user, 'wer_weiss_mehr', room_code, hub_session
        )
        if not authorization.allowed:
            return JsonResponse({
                'success': False,
                'error': authorization.message,
                'code': authorization.code,
            }, status=403)
        quiz.end_quiz('completed')
        quiz.refresh_from_db()

        final_scores_qs = quiz.participants.all()
        if hub_session is not None:
            final_scores_qs = final_scores_qs.filter(hub_session_code=hub_session)
        final_scores = list(final_scores_qs.order_by('-total_score', 'name').values('name', 'total_score'))
        _broadcast_wer_weiss_mehr_game_ended(quiz, hub_session, final_scores)

        return JsonResponse({
            'success': True,
            'game_status': quiz.status,
            'final_scores': final_scores,
        })
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


def _broadcast_wer_weiss_mehr_game_ended(quiz, hub_session, final_scores):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    room_group = f'werweissmehr_{quiz.room_code}'
    async_to_sync(channel_layer.group_send)(room_group, {
        'type': 'quiz_ended',
        'message': 'Wer weiss mehr wurde beendet.',
        'final_scores': final_scores,
    })
    async_to_sync(channel_layer.group_send)(room_group, {
        'type': 'state_updated',
        'payload': {'hub_session_code': hub_session},
    })
    if hub_session:
        async_to_sync(channel_layer.group_send)(f'hub_{hub_session}', {
            'type': 'hub_event',
            'event': {
                'type': 'quiz_ended',
                'game_key': 'wer_weiss_mehr',
                'room_code': quiz.room_code,
                'title': quiz.title,
                'final_scores': final_scores,
            },
        })


@admin_required
@require_POST
def create_wer_weiss_mehr_game(request):
    data = json.loads(request.body or '{}')
    title = (data.get('title') or '').strip() or 'Wer weiß mehr?'
    quiz = WerWeissMehrGame.objects.create(
        title=title,
        creator=request.user,
        status='waiting',
    )
    WerWeissMehrSession.objects.create(quiz=quiz)
    return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})


@admin_required
@require_POST
def create_wer_weiss_mehr_custom_game(request):
    try:
        data = json.loads(request.body or '{}')
        question_ids = [int(item) for item in (data.get('question_ids') or [])]
        inline_question = _create_wer_weiss_mehr_inline_question_if_present(data, request.user)
        if inline_question and inline_question.id not in question_ids:
            question_ids.append(inline_question.id)
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)
        quiz = WerWeissMehrGame.objects.create(
            title=(data.get('title') or '').strip() or 'Wer weiß mehr?',
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=question_ids,
            creator=request.user,
            status='waiting',
        )
        if question_ids:
            qs = WerWeissMehrQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))
        WerWeissMehrSession.objects.create(quiz=quiz)
        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_wer_weiss_mehr_custom_game(request):
    try:
        data = json.loads(request.body or '{}')
        quiz = get_object_or_404(WerWeissMehrGame, id=data.get('quiz_id'))
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        title = (data.get('title') or '').strip()
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data.get('internal_description') or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if isinstance(data.get('question_ids'), list):
            question_ids = [int(item) for item in data['question_ids']]
            inline_question = _create_wer_weiss_mehr_inline_question_if_present(data, request.user)
            if inline_question and inline_question.id not in question_ids:
                question_ids.append(inline_question.id)
            qs = WerWeissMehrQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            quiz.question_order = question_ids
            fields_to_update.append('question_order')
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_wer_weiss_mehr_questions(request):
    page = int(request.GET.get('page', 1) or 1)
    search = (request.GET.get('search') or '').strip()
    qs = WerWeissMehrQuestion.objects.filter(is_active=True).prefetch_related('answers').order_by('-created_at')
    if search:
        qs = qs.filter(Q(question_text__icontains=search) | Q(answers__canonical_text__icontains=search)).distinct()
    paginator = Paginator(qs, 10)
    page_obj = paginator.get_page(page)
    return JsonResponse({
        'success': True,
        'questions': [_serialize_wer_weiss_mehr_question(question) for question in page_obj.object_list],
        'count': qs.count(),
        'pages': paginator.num_pages,
        'current_page': page_obj.number,
    })


@admin_required
def get_wer_weiss_mehr_selected_questions(request, quiz_id):
    quiz = get_object_or_404(WerWeissMehrGame, id=quiz_id)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    questions = list(quiz.selected_questions.prefetch_related('answers'))
    order = quiz.question_order or []
    if order:
        order_map = {int(item): index for index, item in enumerate(order)}
        questions.sort(key=lambda question: order_map.get(question.id, len(order_map)))
    return JsonResponse({
        'success': True,
        'questions': [
            {
                **_serialize_wer_weiss_mehr_question(question),
                'is_tutorial': question.id == quiz.tutorial_question_id,
            }
            for question in questions
        ],
        'count': len(questions),
        'tutorial_question_id': quiz.tutorial_question_id,
    })


@admin_required
@require_POST
def add_wer_weiss_mehr_question(request):
    try:
        data = json.loads(request.body or '{}')
        prompt = (data.get('question_text') or '').strip()
        if not prompt:
            return JsonResponse({'success': False, 'error': 'Frage ist erforderlich.'}, status=400)
        question = WerWeissMehrQuestion.objects.create(
            question_text=prompt,
            round_time_limit=int(data.get('round_time_limit') or data.get('time_limit') or 30),
            created_by=request.user,
        )
        _replace_wer_weiss_mehr_answers(question, data.get('answers') or data.get('answers_text') or [])
        return JsonResponse({'success': True, 'question_id': question.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_wer_weiss_mehr_question(request):
    try:
        data = json.loads(request.body or '{}')
        question = get_object_or_404(WerWeissMehrQuestion, id=data.get('question_id'))
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        question.question_text = (data.get('question_text') or question.question_text).strip()
        question.round_time_limit = int(data.get('round_time_limit') or data.get('time_limit') or question.round_time_limit)
        question.save(update_fields=['question_text', 'round_time_limit'])
        if 'answers' in data or 'answers_text' in data:
            _replace_wer_weiss_mehr_answers(question, data.get('answers') or data.get('answers_text') or [])
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_wer_weiss_mehr_question_detail(request, question_id):
    question = get_object_or_404(WerWeissMehrQuestion.objects.prefetch_related('answers'), id=question_id)
    if not request.user.is_superuser and question.created_by != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    return JsonResponse({'success': True, 'question': _serialize_wer_weiss_mehr_question(question)})


@admin_required
@require_POST
def delete_wer_weiss_mehr_question(request):
    data = json.loads(request.body or '{}')
    question = get_object_or_404(WerWeissMehrQuestion, id=data.get('question_id'), created_by=request.user)
    question.is_active = False
    question.save(update_fields=['is_active'])
    return JsonResponse({'success': True})


@admin_required
@require_POST
def delete_wer_weiss_mehr_game(request):
    data = json.loads(request.body or '{}')
    quiz = get_object_or_404(WerWeissMehrGame, id=data.get('quiz_id'))
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    quiz.delete()
    return JsonResponse({'success': True})


@admin_required
def api_wer_weiss_mehr_state(request, room_code):
    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    return JsonResponse(build_game_state(quiz, hub_session_code=request.GET.get('hub_session') or None))

    
@admin_required
def admin_home(request):
    """Admin dashboard landing page with main navigation buttons"""
    return render(request, 'admin_dashboard/landing.html')


@admin_required
def sessions_overview(request):
    """Sessions overview page (active, planned, recent sessions)"""
    # Get total sessions count
    total_sessions = HubSession.objects.count()
    
    # Get active sessions (single-active rule: active flag + not ended)
    active_sessions = HubSession.objects.filter(
        is_active=True,
        ended_at__isnull=True
    ).count()
    
    # Get total unique players across all sessions
    total_players = HubParticipant.objects.values('nickname').distinct().count()
    
    # Get most played game type
    most_played = HubGameStep.objects.values('game_key')\
        .annotate(count=Count('id'))\
        .order_by('-count')
    most_played_game = most_played.first()
    
    # Convert game key to display name
    game_display_names = dict(HubGameStep.GAME_CHOICES)

    # Collect all currently active/running games across all game types
    # Build mapping: game room_code -> hub session code (single query)
    room_code_to_hub_session = {
        step.room_code: step.session.code
        for step in HubGameStep.objects.select_related('session').exclude(room_code='')
    }

    active_games = []

    def _resolve_game_end_url(end_url_name, room_code):
        if not end_url_name:
            return None

        candidate_names = [end_url_name]
        if ':' not in end_url_name:
            candidate_names.append(f'admin_dashboard:{end_url_name}')

        for candidate_name in candidate_names:
            try:
                return reverse(candidate_name, args=[room_code])
            except NoReverseMatch:
                continue
        return None

    def _add_games(queryset, game_type, game_type_display, monitor_url_name, end_url_name, score_field='total_score'):
        for game in queryset:
            try:
                participant_count = game.participants.filter(is_active=True).count()
            except Exception:
                participant_count = 0
            hub_session_code = room_code_to_hub_session.get(game.room_code)
            end_url = _resolve_game_end_url(end_url_name, game.room_code)
            active_games.append({
                'title': game.title,
                'room_code': game.room_code,
                'game_type': game_type,
                'game_type_display': game_type_display,
                'monitor_url_name': monitor_url_name,
                'end_url_name': end_url_name,
                'end_url': end_url,
                'participant_count': participant_count,
                'started_at': getattr(game, 'started_at', None),
                'hub_session_code': hub_session_code,
            })

    _add_games(Quiz.objects.filter(status='active'), 'quiz', 'Quick Quiz', 'admin_dashboard:quiz_monitor', 'admin_dashboard:end_quiz_by_room_code')
    _add_games(EstimationQuiz.objects.filter(status='active'), 'estimation', 'Estimation', 'admin_dashboard:estimation_monitor', 'admin_dashboard:end_estimation_quiz_by_room_code')
    _add_games(AssignQuiz.objects.filter(status='active'), 'assign', 'Assign', 'admin_dashboard:assign_monitor', 'admin_dashboard:end_assign_quiz_by_room_code')
    _add_games(WhereQuiz.objects.filter(status='active'), 'where', 'Where Is This?', 'admin_dashboard:where_monitor', 'admin_dashboard:end_where_quiz_by_room_code')
    _add_games(WhoQuiz.objects.filter(status='active'), 'who', 'Who Is Lying?', 'admin_dashboard:who_monitor', 'admin_dashboard:end_who_quiz_by_room_code')
    _add_games(WhoThatQuiz.objects.filter(status='active'), 'who_that', 'Who Is That?', 'admin_dashboard:who_that_monitor', 'admin_dashboard:end_who_that_quiz_by_room_code')
    _add_games(BlackJackQuiz.objects.filter(status='active'), 'blackjack', 'Black Jack Quiz', 'admin_dashboard:blackjack_monitor', 'admin_dashboard:end_blackjack_quiz_by_room_code')
    _add_games(ClueRushGame.objects.filter(status='active'), 'clue_rush', 'Clue Rush', 'admin_dashboard:clue_rush_monitor', 'admin_dashboard:end_clue_rush_game_by_room_code')
    _add_games(BuzzerGame.objects.filter(status='active'), 'buzzer', 'Buzzer', 'admin_dashboard:buzzer_monitor', 'admin_dashboard:end_buzzer_game_by_room_code')
    _add_games(HostPointsGame.objects.filter(status='active'), 'host_points', 'Host-Punktevergabe', 'admin_dashboard:host_points_monitor', 'admin_dashboard:end_host_points_game_by_room_code')
    _add_games(WannWarDasGame.objects.filter(status='active'), 'wann_war_das', 'Wann war das?', 'admin_dashboard:wann_war_das_monitor', 'admin_dashboard:end_wann_war_das_game_by_room_code')
    _add_games(SortingLadderGame.objects.filter(status='active'), 'sorting_ladder', 'Sorting Ladder', 'admin_dashboard:sorting_ladder_monitor', 'admin_dashboard:end_sorting_ladder_game_by_room_code')
    _add_games(WerWeissMehrGame.objects.filter(status='active'), 'wer_weiss_mehr', 'Wer weiß mehr?', 'admin_dashboard:wer_weiss_mehr_monitor', 'admin_dashboard:end_wer_weiss_mehr_game_by_room_code')

    # Session codes that have at least one active game running
    active_game_session_codes = {g['hub_session_code'] for g in active_games if g['hub_session_code']}

    # Active hub sessions: explicitly active and not ended
    active_sessions_qs = HubSession.objects.filter(
        is_active=True,
        ended_at__isnull=True
    ).order_by('-started_at').annotate(
        players_count=Count('participants', distinct=True)
    )

    # Planned hub sessions: not started yet and not ended
    planned_sessions_qs = HubSession.objects.filter(
        started_at__isnull=True,
        ended_at__isnull=True
    ).order_by('-created_at').annotate(
        players_count=Count('participants', distinct=True)
    )

    # Inactive hub sessions: started before, currently not active, not ended
    inactive_sessions_qs = HubSession.objects.filter(
        started_at__isnull=False,
        is_active=False,
        ended_at__isnull=True
    ).order_by('-started_at').annotate(
        players_count=Count('participants', distinct=True)
    )

    # Get recent sessions (last 10, only ended)
    recent_sessions = HubSession.objects.filter(
        ended_at__isnull=False
    ).order_by('-ended_at')[:10].annotate(
        games_count=Count('steps', distinct=True),
        players_count=Count('participants', distinct=True)
    )

    active_games.sort(key=lambda g: g['started_at'] or timezone.now(), reverse=True)

    # Prepare context
    context = {
        'total_sessions': total_sessions,
        'active_sessions': active_sessions,
        'total_players': total_players,
        'most_played_game': game_display_names.get(most_played_game['game_key'], 'N/A') if most_played_game else 'N/A',
        'active_games': active_games,
        'active_hub_sessions': active_sessions_qs,
        'planned_hub_sessions': planned_sessions_qs,
        'inactive_hub_sessions': inactive_sessions_qs,
        'recent_sessions': [{
            'name': session.name,
            'code': session.code,
            'is_active': session.is_active and not session.ended_at,
            'games_count': session.games_count,
            'players_count': session.players_count,
            'created_at': session.created_at,
            'started_at': session.started_at,
            'ended_at': session.ended_at
        } for session in recent_sessions]
    }

    return render(request, 'admin_dashboard/index.html', context)


@admin_required
def manage_games(request):
    """Unified game management page — all game types in one view."""
    all_games = []

    def _add(qs, game_type, game_type_display, monitor_url_name):
        for game in qs:
            try:
                q_count = game.selected_questions.count()
            except Exception:
                q_count = 0
            all_games.append({
                'id': game.id,
                'title': game.title,
                'game_type': game_type,
                'game_type_display': game_type_display,
                'room_code': game.room_code,
                'monitor_url_name': monitor_url_name,
                'created_at': game.created_at,
                'question_count': q_count,
            })

    _add(Quiz.objects.all().order_by('-created_at'), 'quiz', 'Quick Quiz', 'admin_dashboard:quiz_monitor')
    _add(EstimationQuiz.objects.all().order_by('-created_at'), 'estimation', 'Estimation', 'admin_dashboard:estimation_monitor')
    _add(AssignQuiz.objects.all().order_by('-created_at'), 'assign', 'Assign', 'admin_dashboard:assign_monitor')
    _add(WhereQuiz.objects.all().order_by('-created_at'), 'where', 'Where Is This?', 'admin_dashboard:where_monitor')
    _add(WhoQuiz.objects.all().order_by('-created_at'), 'who', 'Who Is Lying?', 'admin_dashboard:who_monitor')
    _add(WhoThatQuiz.objects.all().order_by('-created_at'), 'who_that', 'Who Is That?', 'admin_dashboard:who_that_monitor')
    _add(BlackJackQuiz.objects.all().order_by('-created_at'), 'blackjack', 'Black Jack Quiz', 'admin_dashboard:blackjack_monitor')
    _add(SortingLadderGame.objects.all().order_by('-created_at'), 'sorting_ladder', 'Sorting Ladder', 'admin_dashboard:sorting_ladder_monitor')
    _add(ClueRushGame.objects.all().order_by('-created_at'), 'clue_rush', 'Clue Rush', 'admin_dashboard:clue_rush_monitor')
    _add(BuzzerGame.objects.all().order_by('-created_at'), 'buzzer', 'Buzzer', 'admin_dashboard:buzzer_monitor')
    _add(HostPointsGame.objects.all().order_by('-created_at'), 'host_points', 'Host-Punktevergabe', 'admin_dashboard:host_points_monitor')
    _add(WannWarDasGame.objects.all().order_by('-created_at'), 'wann_war_das', 'Wann war das?', 'admin_dashboard:wann_war_das_monitor')
    _add(WerWeissMehrGame.objects.all().order_by('-created_at'), 'wer_weiss_mehr', 'Wer weiß mehr?', 'admin_dashboard:wer_weiss_mehr_monitor')

    all_games.sort(key=lambda g: g['created_at'], reverse=True)
    return render(request, 'admin_dashboard/games_overview.html', {'all_games': all_games})


@admin_required
def create_game(request):
    """New game creation form."""
    return render(request, 'admin_dashboard/manage_games.html')


def _parse_optional_positive_int(value):
    if value in (None, ''):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@require_POST
@admin_required
def create_buzzer_game(request):
    try:
        data = json.loads(request.body or '{}')
        title = (data.get('title') or '').strip()
        if not title:
            return JsonResponse({'success': False, 'error': 'title is required'}, status=400)
        points_per_correct = _parse_optional_positive_int(data.get('points_per_correct')) or 1
        planned_rounds = _parse_optional_positive_int(data.get('planned_rounds'))
        game = BuzzerGame.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            points_per_correct=points_per_correct,
            planned_rounds=planned_rounds,
            creator=request.user,
        )
        fields_to_update = []
        _apply_tutorial_fields(game, data, fields_to_update)
        if fields_to_update:
            game.save(update_fields=fields_to_update)
        return JsonResponse({'success': True, 'game_id': game.id, 'room_code': game.room_code})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@require_POST
@admin_required
def update_buzzer_game(request):
    try:
        data = json.loads(request.body or '{}')
        game = get_object_or_404(BuzzerGame, id=data.get('game_id'))
        if not request.user.is_superuser and game.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if 'title' in data:
            game.title = (data.get('title') or '').strip() or game.title
            fields_to_update.append('title')
        if 'internal_description' in data:
            game.internal_description = (data.get('internal_description') or '').strip()
            fields_to_update.append('internal_description')
        if 'points_per_correct' in data:
            game.points_per_correct = _parse_optional_positive_int(data.get('points_per_correct')) or 1
            fields_to_update.append('points_per_correct')
        if 'planned_rounds' in data:
            game.planned_rounds = _parse_optional_positive_int(data.get('planned_rounds'))
            fields_to_update.append('planned_rounds')
        _apply_tutorial_fields(game, data, fields_to_update)
        if fields_to_update:
            game.save(update_fields=list(dict.fromkeys(fields_to_update)))
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@admin_required
def buzzer_monitor(request, room_code):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    if not request.user.is_superuser and game.creator != request.user:
        return redirect('admin_dashboard:manage_games')
    hub_session = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('buzzer', room_code)
    if hub_session and not authorize_game_host(request.user, 'buzzer', room_code, hub_session).allowed:
        return redirect('admin_dashboard:manage_games')
    if hub_session:
        game.ensure_snapshot_participants(hub_session)
    return render(request, 'admin_dashboard/buzzer_monitor.html', {
        'game': game,
        'hub_session': hub_session or '',
        'state': game.serialize_state(hub_session),
    })


@require_POST
@admin_required
def end_buzzer_game_by_room_code(request, room_code):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    if not request.user.is_superuser and game.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    hub_session = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('buzzer', room_code)
    authorization = authorize_game_host(request.user, 'buzzer', room_code, hub_session)
    if not authorization.allowed:
        return JsonResponse({'success': False, 'error': authorization.message, 'code': authorization.code}, status=403)
    game.end_quiz()
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
        return JsonResponse({'success': True})
    return redirect('admin_dashboard:sessions_overview')


@require_POST
@admin_required
def create_host_points_game(request):
    try:
        data = json.loads(request.body or '{}')
        title = (data.get('title') or '').strip()
        if not title:
            return JsonResponse({'success': False, 'error': 'title is required'}, status=400)
        game = HostPointsGame.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            creator=request.user,
        )
        fields_to_update = []
        _apply_tutorial_fields(game, data, fields_to_update)
        if fields_to_update:
            game.save(update_fields=fields_to_update)
        return JsonResponse({'success': True, 'game_id': game.id, 'room_code': game.room_code})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@require_POST
@admin_required
def update_host_points_game(request):
    try:
        data = json.loads(request.body or '{}')
        game = get_object_or_404(HostPointsGame, id=data.get('game_id'))
        if not request.user.is_superuser and game.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if 'title' in data:
            game.title = (data.get('title') or '').strip() or game.title
            fields_to_update.append('title')
        if 'internal_description' in data:
            game.internal_description = (data.get('internal_description') or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(game, data, fields_to_update)
        if fields_to_update:
            game.save(update_fields=list(dict.fromkeys(fields_to_update)))
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@admin_required
def host_points_monitor(request, room_code):
    game = get_object_or_404(HostPointsGame, room_code=room_code)
    if not request.user.is_superuser and game.creator != request.user:
        return redirect('admin_dashboard:manage_games')
    hub_session = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('host_points', room_code)
    if hub_session and not authorize_game_host(request.user, 'host_points', room_code, hub_session).allowed:
        return redirect('admin_dashboard:manage_games')
    if hub_session:
        game.ensure_snapshot_participants(hub_session)
    return render(request, 'admin_dashboard/host_points_monitor.html', {
        'game': game,
        'hub_session': hub_session or '',
        'state': game.serialize_state(hub_session),
    })


@require_POST
@admin_required
def end_host_points_game_by_room_code(request, room_code):
    game = get_object_or_404(HostPointsGame, room_code=room_code)
    if not request.user.is_superuser and game.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    hub_session = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('host_points', room_code)
    authorization = authorize_game_host(request.user, 'host_points', room_code, hub_session)
    if not authorization.allowed:
        return JsonResponse({'success': False, 'error': authorization.message, 'code': authorization.code}, status=403)
    game.end_quiz()
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
        return JsonResponse({'success': True})
    return redirect('admin_dashboard:sessions_overview')


def _parse_required_float(data, key, label):
    value = data.get(key)
    if value in (None, ''):
        raise ValueError(f'{label} is required.')
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be numeric.')


def _parse_float_default(data, key, default):
    value = data.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int_default(data, key, default):
    value = data.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _wann_question_payload(question):
    return {
        'id': question.id,
        'question_text': question.question_text,
        'correct_answer': question.correct_answer,
        'unit': question.unit,
        'start_tolerance': question.start_tolerance,
        'tolerance_increment': question.tolerance_increment,
        'seconds_per_step': question.seconds_per_step,
        'max_tolerance': question.max_tolerance,
        'max_points': question.max_points,
        'min_points': question.min_points,
        'time_limit': question.time_limit,
        'explanation': question.explanation,
        'tolerance_display': f'{question.start_tolerance:g} -> {question.max_tolerance:g}',
        'points_display': f'{question.max_points}/{question.min_points}',
    }


def _build_wann_question_from_payload(data, user, question=None):
    target = question or WannWarDasQuestion(created_by=user)
    target.question_text = (data.get('question_text') or data.get('question') or '').strip()
    if not target.question_text:
        raise ValueError('Question text is required.')
    target.correct_answer = _parse_required_float(data, 'correct_answer', 'Correct answer')
    target.unit = (data.get('unit') or 'Jahr').strip()
    target.start_tolerance = _parse_float_default(data, 'start_tolerance', 0)
    target.tolerance_increment = _parse_float_default(data, 'tolerance_increment', 1)
    target.seconds_per_step = _parse_int_default(data, 'seconds_per_step', 5)
    target.max_tolerance = _parse_float_default(data, 'max_tolerance', 10)
    target.max_points = _parse_int_default(data, 'max_points', 10)
    target.min_points = _parse_int_default(data, 'min_points', 1)
    raw_time_limit = data.get('time_limit')
    target.time_limit = _parse_int_default(data, 'time_limit', None) if raw_time_limit not in (None, '') else None
    target.explanation = (data.get('explanation') or '').strip()
    target.is_active = True
    target.save()
    return target


@require_POST
@admin_required
def create_wann_war_das_game(request):
    try:
        data = json.loads(request.body or '{}')
        title = (data.get('title') or '').strip()
        if not title:
            return JsonResponse({'success': False, 'error': 'title is required'}, status=400)
        question_ids = _normalize_question_ids(data.get('question_ids') or [])
        game = WannWarDasGame.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            creator=request.user,
            question_order=question_ids,
        )
        game.selected_questions.set(WannWarDasQuestion.objects.filter(id__in=question_ids))
        fields_to_update = ['question_order']
        _apply_tutorial_fields(game, data, fields_to_update)
        try:
            _apply_tutorial_question_selection(game, data, question_ids, fields_to_update)
        except ValueError as exc:
            game.delete()
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        if fields_to_update:
            game.save(update_fields=list(dict.fromkeys(fields_to_update)))
        return JsonResponse({'success': True, 'game_id': game.id, 'room_code': game.room_code})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@require_POST
@admin_required
def update_wann_war_das_game(request):
    try:
        data = json.loads(request.body or '{}')
        game = get_object_or_404(WannWarDasGame, id=data.get('game_id') or data.get('quiz_id'))
        if not request.user.is_superuser and game.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        question_ids = _normalize_question_ids(data.get('question_ids') or [])
        fields_to_update = []
        if 'title' in data:
            game.title = (data.get('title') or '').strip() or game.title
            fields_to_update.append('title')
        if 'internal_description' in data:
            game.internal_description = (data.get('internal_description') or '').strip()
            fields_to_update.append('internal_description')
        game.question_order = question_ids
        fields_to_update.append('question_order')
        game.selected_questions.set(WannWarDasQuestion.objects.filter(id__in=question_ids))
        _apply_tutorial_fields(game, data, fields_to_update)
        try:
            _apply_tutorial_question_selection(game, data, question_ids, fields_to_update)
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=400)
        if fields_to_update:
            game.save(update_fields=list(dict.fromkeys(fields_to_update)))
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@admin_required
def wann_war_das_monitor(request, room_code):
    game = get_object_or_404(WannWarDasGame, room_code=room_code)
    if not request.user.is_superuser and game.creator != request.user:
        return redirect('admin_dashboard:manage_games')
    hub_session = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('wann_war_das', room_code)
    if hub_session and not authorize_game_host(request.user, 'wann_war_das', room_code, hub_session).allowed:
        return redirect('admin_dashboard:manage_games')
    if hub_session:
        game.ensure_snapshot_participants(hub_session)
    available_questions = game.get_ordered_questions(hub_session, include_tutorial=True)
    if not available_questions:
        available_questions = list(WannWarDasQuestion.objects.filter(created_by=request.user, is_active=True).order_by('-created_at'))
    return render(request, 'admin_dashboard/wann_war_das_monitor.html', {
        'game': game,
        'hub_session': hub_session or '',
        'available_questions': available_questions,
        'state': game.serialize_state(hub_session),
    })


@require_POST
@admin_required
def end_wann_war_das_game_by_room_code(request, room_code):
    game = get_object_or_404(WannWarDasGame, room_code=room_code)
    if not request.user.is_superuser and game.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    hub_session = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('wann_war_das', room_code)
    authorization = authorize_game_host(request.user, 'wann_war_das', room_code, hub_session)
    if not authorization.allowed:
        return JsonResponse({'success': False, 'error': authorization.message, 'code': authorization.code}, status=403)
    game.end_quiz()
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
        return JsonResponse({'success': True})
    return redirect('admin_dashboard:sessions_overview')


@admin_required
def get_wann_war_das_questions(request):
    search = (request.GET.get('search') or '').strip()
    qs = WannWarDasQuestion.objects.filter(is_active=True)
    if search:
        qs = qs.filter(question_text__icontains=search)
    paginator = Paginator(qs.order_by('-created_at'), 25)
    page_obj = paginator.get_page(request.GET.get('page') or 1)
    return JsonResponse({
        'success': True,
        'questions': [_wann_question_payload(question) for question in page_obj.object_list],
        'count': paginator.count,
        'pages': paginator.num_pages,
        'current_page': page_obj.number,
    })


@require_POST
@admin_required
def add_wann_war_das_question(request):
    try:
        data = json.loads(request.body or '{}')
        question = _build_wann_question_from_payload(data, request.user)
        return JsonResponse({'success': True, 'question_id': question.id, 'question': _wann_question_payload(question)})
    except (json.JSONDecodeError, ValueError, ValidationError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


@admin_required
def get_wann_war_das_question_detail(request, question_id):
    question = get_object_or_404(WannWarDasQuestion, id=question_id)
    return JsonResponse({'success': True, 'question': _wann_question_payload(question)})


@require_POST
@admin_required
def update_wann_war_das_question(request):
    try:
        data = json.loads(request.body or '{}')
        question = get_object_or_404(WannWarDasQuestion, id=data.get('question_id'))
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        question = _build_wann_question_from_payload(data, request.user, question)
        return JsonResponse({'success': True, 'question_id': question.id, 'question': _wann_question_payload(question)})
    except (json.JSONDecodeError, ValueError, ValidationError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


@require_POST
@admin_required
def delete_wann_war_das_question(request):
    try:
        data = json.loads(request.body or '{}')
        question = get_object_or_404(WannWarDasQuestion, id=data.get('question_id'))
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        question.is_active = False
        question.save(update_fields=['is_active', 'updated_at'])
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@admin_required
def get_wann_war_das_selected_questions(request, quiz_id):
    game = get_object_or_404(WannWarDasGame, id=quiz_id)
    questions_by_id = {question.id: question for question in game.selected_questions.filter(is_active=True)}
    ordered = []
    for question_id in _normalize_question_ids(game.question_order):
        question = questions_by_id.pop(question_id, None)
        if question:
            ordered.append(question)
    ordered.extend(questions_by_id.values())
    return JsonResponse({
        'success': True,
        'questions': [_wann_question_payload(question) for question in ordered],
        'tutorial_question_id': game.tutorial_question_id,
    })


@require_POST
@admin_required
def delete_game_instance(request):
    """Generic delete for any game instance from the games overview."""
    try:
        data = json.loads(request.body)
        game_type = data.get('game_type')
        game_id = data.get('game_id')
        if not game_type or not game_id:
            return JsonResponse({'success': False, 'error': 'game_type and game_id are required'}, status=400)

        MODEL_MAP = {
            'quiz':           Quiz,
            'estimation':     EstimationQuiz,
            'assign':         AssignQuiz,
            'where':          WhereQuiz,
            'who':            WhoQuiz,
            'who_that':       WhoThatQuiz,
            'blackjack':      BlackJackQuiz,
            'clue_rush':      ClueRushGame,
            'sorting_ladder': SortingLadderGame,
            'wer_weiss_mehr': WerWeissMehrGame,
            'buzzer':         BuzzerGame,
            'host_points':    HostPointsGame,
            'wann_war_das':   WannWarDasGame,
        }
        model = MODEL_MAP.get(game_type)
        if not model:
            return JsonResponse({'success': False, 'error': f'Unknown game type: {game_type}'}, status=400)

        obj = get_object_or_404(model, id=game_id)
        creator_field = 'creator' if hasattr(obj, 'creator') else None
        if creator_field and not request.user.is_superuser and getattr(obj, creator_field) != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        obj.delete()
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)


@require_POST
@admin_required
def delete_all_game_instances(request):
    """Delete all game instances of every type."""
    MODEL_MAP = {
        'quiz':           Quiz,
        'estimation':     EstimationQuiz,
        'assign':         AssignQuiz,
        'where':          WhereQuiz,
        'who':            WhoQuiz,
        'who_that':       WhoThatQuiz,
        'blackjack':      BlackJackQuiz,
        'clue_rush':      ClueRushGame,
        'sorting_ladder': SortingLadderGame,
        'wer_weiss_mehr': WerWeissMehrGame,
        'buzzer':         BuzzerGame,
        'host_points':    HostPointsGame,
        'wann_war_das':   WannWarDasGame,
    }
    for model in MODEL_MAP.values():
        model.objects.all().delete()
    return JsonResponse({'success': True})


@admin_required
def edit_game(request, game_type, game_id):
    """Edit an existing game instance."""
    import json as _json
    type_to_model = {
        'quiz': Quiz,
        'estimation': EstimationQuiz,
        'assign': AssignQuiz,
        'where': WhereQuiz,
        'who': WhoQuiz,
        'who_that': WhoThatQuiz,
        'blackjack': BlackJackQuiz,
        'sorting_ladder': SortingLadderGame,
        'clue_rush': ClueRushGame,
        'wer_weiss_mehr': WerWeissMehrGame,
        'buzzer': BuzzerGame,
        'host_points': HostPointsGame,
        'wann_war_das': WannWarDasGame,
    }
    model = type_to_model.get(game_type)
    if not model:
        from django.http import Http404
        raise Http404('Unknown game type')
    game = get_object_or_404(model, id=game_id)
    has_selected_questions = hasattr(game, 'selected_questions')
    _question_order = getattr(game, 'question_order', []) or []
    if has_selected_questions and _question_order:
        _id_set = set(game.selected_questions.values_list('id', flat=True))
        flattened_question_order = []
        for raw_item in _question_order:
            if isinstance(raw_item, (list, tuple)):
                flattened_question_order.extend(raw_item)
            else:
                flattened_question_order.append(raw_item)
        selected_ids = [i for i in flattened_question_order if i in _id_set]
        selected_ids += [i for i in _id_set if i not in set(selected_ids)]
    elif has_selected_questions:
        selected_ids = list(game.selected_questions.values_list('id', flat=True))
    else:
        selected_ids = []
    return render(request, 'admin_dashboard/manage_games.html', {
        'edit_mode': True,
        'game_id': game_id,
        'game_title': game.title,
        'game_internal_description': game.internal_description,
        'game_tutorial_enabled': getattr(game, 'tutorial_enabled', False),
        'game_tutorial_title': getattr(game, 'tutorial_title', ''),
        'game_tutorial_text': getattr(game, 'tutorial_text', ''),
        'game_tutorial_question_id': None if game_type == 'blackjack' else getattr(game, 'tutorial_question_id', None),
        'game_tutorial_set_number': getattr(game, 'tutorial_set_number', None) if game_type == 'blackjack' else None,
        'game_type': game_type,
        'game_scoring_mode': getattr(game, 'scoring_mode', ''),
        'game_points_per_correct': getattr(game, 'points_per_correct', 1),
        'game_planned_rounds': getattr(game, 'planned_rounds', None),
        'selected_ids_json': _json.dumps(selected_ids),
    })


def _normalize_tutorial_payload(data):
    tutorial_title = (data.get('tutorial_title') or '').strip()
    tutorial_text = (data.get('tutorial_text') or '').strip()
    enabled = bool(tutorial_title or tutorial_text)
    if not enabled:
        return False, '', ''
    return True, tutorial_title, tutorial_text


def _apply_tutorial_fields(game, data, fields_to_update):
    if not any(key in data for key in ('tutorial_enabled', 'tutorial_title', 'tutorial_text')):
        return
    tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)
    game.tutorial_enabled = tutorial_enabled
    game.tutorial_title = tutorial_title
    game.tutorial_text = tutorial_text
    fields_to_update.extend([
        'tutorial_enabled',
        'tutorial_title',
        'tutorial_text',
    ])


def _normalize_question_ids(raw_ids):
    normalized_ids = []
    for raw_id in raw_ids or []:
        try:
            question_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if question_id not in normalized_ids:
            normalized_ids.append(question_id)
    return normalized_ids


def _extract_tutorial_question_id(data):
    raw_values = []
    has_field = False
    if 'tutorial_question_ids' in data:
        has_field = True
        values = data.get('tutorial_question_ids') or []
        raw_values.extend(values if isinstance(values, list) else [values])
    if 'tutorial_question_id' in data:
        has_field = True
        raw_values.append(data.get('tutorial_question_id'))

    selected_ids = []
    for raw_value in raw_values:
        if raw_value in (None, '', False):
            continue
        try:
            question_id = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError('Invalid tutorial question.')
        if question_id not in selected_ids:
            selected_ids.append(question_id)

    if len(selected_ids) > 1:
        raise ValueError('Only one tutorial question can be selected per game.')
    return (selected_ids[0] if selected_ids else None), has_field


def _apply_tutorial_question_selection(game, data, selected_question_ids, fields_to_update=None):
    tutorial_question_id, has_field = _extract_tutorial_question_id(data)
    selected_ids = set(_normalize_question_ids(selected_question_ids))

    if has_field and tutorial_question_id is not None and tutorial_question_id not in selected_ids:
        raise ValueError('Tutorial question must be one of the selected questions.')

    if has_field:
        next_tutorial_id = tutorial_question_id
    else:
        current_tutorial_id = getattr(game, 'tutorial_question_id', None)
        next_tutorial_id = current_tutorial_id if current_tutorial_id in selected_ids else None

    if getattr(game, 'tutorial_question_id', None) == next_tutorial_id:
        return

    game.tutorial_question_id = next_tutorial_id
    if fields_to_update is not None:
        fields_to_update.append('tutorial_question')
    else:
        game.save(update_fields=['tutorial_question'])


def _extract_blackjack_tutorial_set_number(data):
    raw_values = []
    has_field = False
    if 'tutorial_set_numbers' in data:
        has_field = True
        values = data.get('tutorial_set_numbers') or []
        raw_values.extend(values if isinstance(values, list) else [values])
    if 'tutorial_set_number' in data:
        has_field = True
        raw_values.append(data.get('tutorial_set_number'))

    selected_set_numbers = []
    for raw_value in raw_values:
        if raw_value in (None, '', False):
            continue
        try:
            set_number = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError('Invalid tutorial set.')
        if set_number < 1:
            raise ValueError('Invalid tutorial set.')
        if set_number not in selected_set_numbers:
            selected_set_numbers.append(set_number)

    if len(selected_set_numbers) > 1:
        raise ValueError('Only one tutorial set can be selected per Black Jack quiz.')
    return (selected_set_numbers[0] if selected_set_numbers else None), has_field


def _apply_blackjack_tutorial_set_selection(quiz, data, fields_to_update=None):
    tutorial_set_number, has_field = _extract_blackjack_tutorial_set_number(data)
    configured_sets = quiz.get_explicit_question_sets(active_only=False)

    if has_field and tutorial_set_number is not None and tutorial_set_number > len(configured_sets):
        raise ValueError('Tutorial set must be one of the configured sets.')

    if has_field:
        next_tutorial_set_number = tutorial_set_number
    else:
        current_tutorial_set_number = getattr(quiz, 'tutorial_set_number', None)
        next_tutorial_set_number = (
            current_tutorial_set_number
            if current_tutorial_set_number and current_tutorial_set_number <= len(configured_sets)
            else None
        )

    if getattr(quiz, 'tutorial_set_number', None) == next_tutorial_set_number:
        return

    quiz.tutorial_set_number = next_tutorial_set_number
    if fields_to_update is not None:
        fields_to_update.append('tutorial_set_number')
    else:
        quiz.save(update_fields=['tutorial_set_number'])


@admin_required
def quiz_game_management(request):
    """Quiz game management page"""
    quizzes = Quiz.objects.all().order_by('-created_at')
    total_questions = QuizQuestion.objects.filter(is_active=True).count()
    bundles = QuizBundle.objects.filter(creator=request.user).prefetch_related('questions')

    context = {
        'quizzes': quizzes,
        'total_questions': total_questions,
        'bundles': bundles,
    }
    return render(request, 'admin_dashboard/quiz_management.html', context)


@admin_required
def get_quiz_bundles(request):
    """Return all bundles for the current user as JSON."""
    bundles = QuizBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {
            'id': b.id,
            'name': b.name,
            'question_ids': list(b.questions.values_list('id', flat=True)),
            'question_count': b.questions.count(),
        }
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_quiz_bundle(request):
    """Create a new quiz bundle."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = QuizBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        qs = QuizQuestion.objects.filter(id__in=question_ids, is_active=True)
        bundle.questions.set(qs)
    return JsonResponse({
        'success': True,
        'bundle': {
            'id': bundle.id,
            'name': bundle.name,
            'question_ids': list(bundle.questions.values_list('id', flat=True)),
            'question_count': bundle.questions.count(),
        }
    })


@admin_required
def delete_quiz_bundle(request):
    """Delete a quiz bundle."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    bundle_id = data.get('bundle_id')
    try:
        bundle = QuizBundle.objects.get(id=bundle_id, creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except QuizBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


@admin_required
def get_quiz_selected_questions(request, quiz_id: int):
    """Return questions attached to a specific quiz (selected_questions ManyToMany)."""
    try:
        quiz = get_object_or_404(Quiz, id=quiz_id)
        # Restrict to owner or superuser
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'question_text': q.question_text,
            'question_type': q.question_type,
            'time_limit': q.time_limit,
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': q.is_active,
            'is_tutorial': q.id == quiz.tutorial_question_id,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({
            'success': True,
            'questions': questions,
            'count': len(questions),
            'tutorial_question_id': quiz.tutorial_question_id,
            'scoring_mode': getattr(quiz, 'scoring_mode', None),
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_custom_quiz(request):
    """Update an existing quiz: title and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        title = data.get('title')
        question_ids = data.get('question_ids', [])

        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(Quiz, id=quiz_id)

        # Authorization: only creator or superuser can modify
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = QuizQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_assign_selected_questions(request, quiz_id: int):
    try:
        quiz = get_object_or_404(AssignQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'question_text': getattr(q, 'question_text', ''),
            'time_limit': getattr(q, 'time_limit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': q.is_active,
            'left_items': getattr(q, 'left_items', []),
            'right_items': getattr(q, 'right_items', []),
            'correct_matches': getattr(q, 'correct_matches', {}),
            'is_tutorial': q.id == quiz.tutorial_question_id,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions), 'tutorial_question_id': quiz.tutorial_question_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def update_assign_custom_quiz(request):
    """Update an existing quiz: title and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        title = data.get('title')
        question_ids = data.get('question_ids', [])

        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(AssignQuiz, id=quiz_id)

        # Authorization: only creator or superuser can modify
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = AssignQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def delete_assign_quiz(request):
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)
        quiz = get_object_or_404(AssignQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        quiz.delete()
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_estimation_selected_questions(request, quiz_id: int):
    try:
        quiz = get_object_or_404(EstimationQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'question_text': getattr(q, 'question_text', ''),
            'correct_answer': getattr(q, 'correct_answer', None),
            'points': q.get_zone_max_points(),
            'max_points': getattr(q, 'max_points', None),
            'use_manual_points': getattr(q, 'use_manual_points', False),
            'zone_count': getattr(q, 'zone_count', None),
            'points_display': q.get_zone_max_points(),
            'unit': getattr(q, 'unit', None),
            'unit_display': q.get_unit_display_text(),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': getattr(q, 'is_active', True),
            'is_tutorial': q.id == quiz.tutorial_question_id,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions), 'scoring_mode': getattr(quiz, 'scoring_mode', None), 'tutorial_question_id': quiz.tutorial_question_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def update_estimation_custom_quiz(request):
    """Update an existing quiz: title and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        title = data.get('title')
        scoring_mode = data.get('scoring_mode')
        question_ids = data.get('question_ids', [])

        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(EstimationQuiz, id=quiz_id)

        # Authorization: only creator or superuser can modify
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if scoring_mode in ('zones', 'tolerance', 'rank'):
            setattr(quiz, 'scoring_mode', scoring_mode)
            fields_to_update.append('scoring_mode')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = EstimationQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def delete_estimation_quiz(request):
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)
        quiz = get_object_or_404(EstimationQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        quiz.delete()
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_where_selected_questions(request, quiz_id: int):
    try:
        quiz = get_object_or_404(WhereQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'question_text': getattr(q, 'question_text', ''),
            'points': getattr(q, 'points', None),
            'time_limit': getattr(q, 'time_limit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': getattr(q, 'is_active', True),
            'is_tutorial': q.id == quiz.tutorial_question_id,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions), 'tutorial_question_id': quiz.tutorial_question_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def update_where_custom_quiz(request):
    """Update an existing quiz: title and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        title = data.get('title')
        question_ids = data.get('question_ids', [])

        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(WhereQuiz, id=quiz_id)

        # Authorization: only creator or superuser can modify
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        if 'scoring_mode' in data:
            quiz.scoring_mode = _normalize_where_scoring_mode(data.get('scoring_mode'))
            fields_to_update.append('scoring_mode')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = WhereQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def delete_where_quiz(request):
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)
        quiz = get_object_or_404(WhereQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        quiz.delete()
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_blackjack_selected_questions(request, quiz_id: int):
    try:
        quiz = get_object_or_404(BlackJackQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        _order = quiz.get_configured_question_ids(active_only=False)
        if _order:
            questions_by_id = BlackJackQuestion.objects.in_bulk(_order)
            questions = [{
                'id': questions_by_id[question_id].id,
                'question_text': getattr(questions_by_id[question_id], 'question_text', ''),
                'correct_answer': getattr(questions_by_id[question_id], 'correct_answer', None),
                'time_limit': getattr(questions_by_id[question_id], 'time_limit', None),
                'created_at': questions_by_id[question_id].created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'is_active': getattr(questions_by_id[question_id], 'is_active', True),
            } for question_id in _order if question_id in questions_by_id]
        else:
            qs = quiz.selected_questions.all().order_by('-created_at')
            questions = [{
                'id': q.id,
                'question_text': getattr(q, 'question_text', ''),
                'correct_answer': getattr(q, 'correct_answer', None),
                'time_limit': getattr(q, 'time_limit', None),
                'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'is_active': getattr(q, 'is_active', True),
            } for q in qs]
        explicit_sets = quiz.get_explicit_question_sets(active_only=False)
        return JsonResponse({
            'success': True,
            'questions': questions,
            'count': len(questions),
            'total_questions': quiz.total_questions,
            'scoring_mode': getattr(quiz, 'scoring_mode', 'simple'),
            'question_sets': explicit_sets,
            'set_sizes': ','.join(str(len(question_set)) for question_set in explicit_sets),
            'tutorial_set_number': quiz.tutorial_set_number,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def update_black_jack_custom_quiz(request):
    """Update an existing quiz: title and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        title = data.get('title')
        question_ids = data.get('question_ids', [])
        set_sizes = data.get('set_sizes')

        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(BlackJackQuiz, id=quiz_id)

        # Authorization: only creator or superuser can modify
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'total_questions' in data:
            quiz.total_questions = max(1, int(data.get('total_questions') or 5))
            fields_to_update.append('total_questions')
        if 'scoring_mode' in data:
            scoring_mode = (data.get('scoring_mode') or 'simple').strip()
            normalized_scoring_mode = 'rank' if scoring_mode == 'rank' else 'simple'
            if normalized_scoring_mode != quiz.scoring_mode and not quiz.can_change_scoring_mode():
                return JsonResponse({
                    'success': False,
                    'error': 'The scoring mode can only be changed before the first question is sent.',
                }, status=400)
            quiz.scoring_mode = normalized_scoring_mode
            fields_to_update.append('scoring_mode')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = BlackJackQuestion.objects.filter(id__in=question_ids)
            available_question_ids = set(qs.values_list('id', flat=True))
            ordered_question_ids = []
            for raw_id in question_ids:
                try:
                    question_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if question_id in available_question_ids and question_id not in ordered_question_ids:
                    ordered_question_ids.append(question_id)
            explicit_sets = BlackJackQuiz.build_explicit_question_sets(
                ordered_question_ids,
                default_set_size=quiz.total_questions,
                set_sizes=set_sizes,
            )
            quiz.selected_questions.set(qs)
            quiz.question_order = explicit_sets if explicit_sets else ordered_question_ids
            fields_to_update = ['question_order']
            _apply_blackjack_tutorial_set_selection(quiz, data, fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def delete_blackjack_quiz(request):
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)
        quiz = get_object_or_404(BlackJackQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        quiz.delete()
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_who_that_selected_questions(request, quiz_id: int):
    try:
        quiz = get_object_or_404(WhoThatQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'question_text': getattr(q, 'question_text', ''),
            'correct_answer': getattr(q, 'correct_answer', None),
            'points': 1,
            'time_limit': getattr(q, 'time_limit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': getattr(q, 'is_active', True),
            'is_tutorial': q.id == quiz.tutorial_question_id,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions), 'tutorial_question_id': quiz.tutorial_question_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def update_who_that_custom_quiz(request):
    """Update an existing quiz: title and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        title = data.get('title')
        question_ids = data.get('question_ids', [])

        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(WhoThatQuiz, id=quiz_id)

        # Authorization: only creator or superuser can modify
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = WhoThatQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def delete_who_that_quiz(request):
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)
        quiz = get_object_or_404(WhoThatQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        quiz.delete()
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_who_selected_questions(request, quiz_id: int):
    try:
        quiz = get_object_or_404(WhoQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'statement': getattr(q, 'statement', ''),
            'people_count': len(q.people) if q.people else 0,
            'points': getattr(q, 'points', None),
            'time_limit': getattr(q, 'time_limit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': getattr(q, 'is_active', True),
            'is_tutorial': q.id == quiz.tutorial_question_id,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions), 'tutorial_question_id': quiz.tutorial_question_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def update_who_custom_quiz(request):
    """Update an existing quiz: title and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        title = data.get('title')
        question_ids = data.get('question_ids', [])

        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(WhoQuiz, id=quiz_id)

        # Authorization: only creator or superuser can modify
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        fields_to_update = []
        if title:
            quiz.title = title
            fields_to_update.append('title')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        _apply_tutorial_fields(quiz, data, fields_to_update)
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = WhoQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            fields_to_update = ['question_order']
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True), fields_to_update)
            quiz.save(update_fields=list(dict.fromkeys(fields_to_update)))

        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def delete_who_quiz(request):
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)
        quiz = get_object_or_404(WhoQuiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
        quiz.delete()
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def delete_quiz(request):
    """Delete a quiz without deleting its questions (M2M will be removed automatically)."""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        if not quiz_id:
            return JsonResponse({'success': False, 'error': 'quiz_id is required'}, status=400)

        quiz = get_object_or_404(Quiz, id=quiz_id)
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        quiz.delete()  # This will not delete QuizQuestion instances (M2M only)
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def quiz_monitor(request, room_code):
    """Real-time quiz monitoring page"""
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(Quiz, room_code=room_code)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:quiz_management')

    quiz.ensure_clean_prestart_state(session_code=hub_session)
    
    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    # If the quiz has a predefined set of selected questions, show only those
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = QuizQuestion.objects.filter(created_by=request.user).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = QuizSession.objects.get_or_create(quiz=quiz)
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
        'current_unit_is_tutorial': is_current_unit_tutorial_question('quiz', quiz.room_code, hub_session, quiz.current_question_id),
        'question_runtime': current_snapshot('quiz', quiz.room_code, hub_session),
    }
    return render(request, 'admin_dashboard/quiz_monitor.html', context)


@admin_required
@require_POST
def create_quiz(request):
    """Create a new quiz via AJAX"""
    try:
        quiz = Quiz.objects.create(
            title="Quick Quiz",
            creator=request.user,
            status='waiting'
        )
        
        # Create associated quiz session
        QuizSession.objects.create(quiz=quiz)
        
        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


def _serialize_quiz_live_response(response):
    question = response.question
    display_answer = response.answer_text
    effective_question_type = question.get_effective_question_type()
    field_results = []
    if effective_question_type == 'multiple_choice':
        option_map = dict(question.get_options())
        display_answer = option_map.get(str(response.answer_text).upper(), response.answer_text)
    elif effective_question_type == 'short_answer':
        display_answer = question.format_short_answer_submission(response.answer_text)
        field_results = response.get_short_answer_field_results()

    is_manual_override = (
        effective_question_type == 'short_answer'
        and response.is_correct
        and not question.is_correct_answer(response.answer_text)
    )
    return {
        'answer_id': response.id,
        'participant_id': response.participant_id,
        'participant_name': response.participant.name,
        'answer_text': display_answer,
        'is_correct': response.is_correct,
        'is_manual_override': is_manual_override,
        'can_mark_correct': effective_question_type == 'short_answer' and not response.is_correct,
        'question_type': effective_question_type,
        'field_results': field_results,
        'time_taken': response.time_taken,
        'points_earned': response.points_earned,
        'total_score': response.participant.total_score,
        'question_id': response.question_id,
        'submitted_at': response.submitted_at.isoformat(),
    }


@admin_required
@require_POST
def promote_quiz_answer_correct(request, room_code):
    """Allow the host to manually promote a short-answer response to correct."""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = json.loads(request.body or '{}')
        answer_id = data.get('answer_id')
        if not answer_id:
            return JsonResponse({'success': False, 'error': 'answer_id is required'}, status=400)
        field_key = (data.get('field_key') or '').strip()
        field_is_correct = bool(data.get('is_correct', True))

        with transaction.atomic():
            answer = get_object_or_404(
                QuizAnswer.objects.select_for_update().select_related('participant', 'question'),
                id=answer_id,
                quiz=quiz,
            )

            if answer.question.get_effective_question_type() != 'short_answer':
                return JsonResponse({
                    'success': False,
                    'error': 'Only short-answer responses can be promoted manually.',
                }, status=400)

            if answer.is_correct and not field_key:
                return JsonResponse({
                    'success': False,
                    'error': 'This answer is already marked correct.',
                }, status=400)

            if field_key:
                answer.set_short_answer_field_correctness(field_key, field_is_correct)
            else:
                answer.promote_short_answer_to_correct()
            answer.participant.refresh_from_db(fields=['total_score'])
            correction_is_during_active_question = bool(
                quiz.current_question_id == answer.question_id
                and QuizSession.objects.filter(quiz=quiz, is_question_active=True).exists()
            )

        payload = {'success': True, **_serialize_quiz_live_response(answer)}

        channel_layer = get_channel_layer()
        if channel_layer is not None:
            event_payload = {key: value for key, value in payload.items() if key != 'success'}
            event_payload['visible_to_participants'] = not correction_is_during_active_question
            async_to_sync(channel_layer.group_send)(
                f'quiz_{quiz.room_code}',
                {
                    'type': 'answer_corrected',
                    **event_payload,
                }
            )

        return JsonResponse(payload)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def get_who_question_detail(request, question_id):
    """Return full details of a Who is Lying question for editing"""
    try:
        question = get_object_or_404(WhoQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = {
            'id': question.id,
            'statement': question.statement,
            'points': question.points,
            'time_limit': question.time_limit,
            'people': question.people or [],
            'explanation': question.explanation or '',
        }
        return JsonResponse({'success': True, 'question': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_who_question(request):
    """Update a Who is Lying question via AJAX (JSON body)"""
    try:
        data = json.loads(request.body)

        question_id = data.get('question_id')
        question = get_object_or_404(WhoQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        is_running_question = WhoQuiz.objects.filter(
            current_question=question,
            status='active',
        ).exists()
        if is_running_question and any(field in data for field in ('statement', 'time_limit', 'people')):
            return JsonResponse({
                'success': False,
                'error': 'This question is currently running and cannot be edited.'
            }, status=400)

        # Update fields if provided
        if 'statement' in data:
            question.statement = data.get('statement', question.statement).strip() or question.statement
        if 'points' in data:
            question.points = int(data.get('points'))
        if 'time_limit' in data:
            question.time_limit = int(data.get('time_limit'))
        if 'explanation' in data:
            question.explanation = data.get('explanation', '').strip()
        if 'people' in data:
            people = data.get('people') or []
            # Validate people structure
            if not isinstance(people, list):
                return JsonResponse({'success': False, 'error': 'Invalid people data.'}, status=400)
            for person in people:
                if not isinstance(person, dict) or 'name' not in person or 'is_lying' not in person:
                    return JsonResponse({'success': False, 'error': 'Invalid people data format.'}, status=400)
                if not str(person['name']).strip():
                    return JsonResponse({'success': False, 'error': 'All people must have names.'}, status=400)
            question.people = people

        question.save()
        return JsonResponse({'success': True})
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid numeric values provided.'}, status=400)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def create_who_custom_quiz(request):
    """Create a new 'Who is Lying?' quiz with a name and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        title = (data.get('title') or '').strip() or 'Who is Lying?'
        question_ids = data.get('question_ids') or []
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = WhoQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        if question_ids:
            qs = WhoQuestion.objects.filter(id__in=question_ids, created_by=request.user)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        WhoSession.objects.create(quiz=quiz)

        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def create_who_that_custom_quiz(request):
    """Create a new who is that quiz with a name and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        title = (data.get('title') or '').strip() or 'Who is That?'
        question_ids = data.get('question_ids') or []
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = WhoThatQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        if question_ids:
            qs = WhoThatQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        WhoThatSession.objects.create(quiz=quiz)

        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)




@admin_required
@require_POST
def create_custom_quiz(request):
    """Create a new quiz with a name and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        title = (data.get('title') or '').strip() or 'Custom Quiz'
        question_ids = data.get('question_ids') or []
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = Quiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        # Attach selected questions (only active ones the user can access)
        if question_ids:
            qs = QuizQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        # Create session
        QuizSession.objects.create(quiz=quiz)

        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


def _quiz_effective_question_type(question_type):
    return 'short_answer' if question_type == 'double_answer' else question_type


def _extract_quiz_short_answer_form_data(post_data):
    answers = [
        post_data.get('correct_answer', '').strip(),
        post_data.get('correct_answer_2', '').strip(),
        post_data.get('correct_answer_3', '').strip(),
        post_data.get('correct_answer_4', '').strip(),
    ]
    labels = [
        post_data.get('answer_label_1', post_data.get('double_answer_label_1', '')).strip(),
        post_data.get('answer_label_2', post_data.get('double_answer_label_2', '')).strip(),
        post_data.get('answer_label_3', '').strip(),
        post_data.get('answer_label_4', '').strip(),
    ]

    highest_index = 0
    for index, value in enumerate(answers, start=1):
        if value:
            highest_index = index

    if highest_index == 0:
        return None, 'Question text and correct answer are required.'
    if any(not answers[index] for index in range(highest_index)):
        return None, 'All active short-answer fields must be filled.'
    if highest_index > 1 and any(not labels[index] for index in range(highest_index)):
        return None, 'Short Answer with multiple fields requires labels for every field.'

    return {
        'correct_answer': answers[0],
        'correct_answer_2': answers[1] if highest_index >= 2 else '',
        'correct_answer_3': answers[2] if highest_index >= 3 else '',
        'correct_answer_4': answers[3] if highest_index >= 4 else '',
        'answer_label_1': labels[0] if highest_index >= 2 else '',
        'answer_label_2': labels[1] if highest_index >= 2 else '',
        'answer_label_3': labels[2] if highest_index >= 3 else '',
        'answer_label_4': labels[3] if highest_index >= 4 else '',
        'double_answer_label_1': labels[0] if highest_index >= 2 else '',
        'double_answer_label_2': labels[1] if highest_index >= 2 else '',
    }, None


@admin_required
@require_POST
def add_question(request):
    """Add a new question via AJAX"""
    try:
        question_text = request.POST.get('question_text', '').strip()
        question_type = _quiz_effective_question_type(request.POST.get('question_type', 'multiple_choice'))
        time_limit = int(request.POST.get('time_limit', 30))
        explanation = request.POST.get('explanation', '').strip()
        
        # Validate required fields
        if not question_text:
            return JsonResponse({
                'success': False,
                'error': 'Question text and correct answer are required.'
            }, status=400)

        short_answer_data = None
        if question_type == 'short_answer':
            short_answer_data, error = _extract_quiz_short_answer_form_data(request.POST)
            if error:
                return JsonResponse({'success': False, 'error': error}, status=400)
        else:
            correct_answer = request.POST.get('correct_answer', '').strip()
            if not correct_answer:
                return JsonResponse({
                    'success': False,
                    'error': 'Question text and correct answer are required.'
                }, status=400)
        
        # Create question
        question = QuizQuestion.objects.create(
            question_text=question_text,
            question_type=question_type,
            points=1,
            time_limit=time_limit,
            correct_answer=short_answer_data['correct_answer'] if short_answer_data else correct_answer,
            correct_answer_2=short_answer_data['correct_answer_2'] if short_answer_data else '',
            correct_answer_3=short_answer_data['correct_answer_3'] if short_answer_data else '',
            correct_answer_4=short_answer_data['correct_answer_4'] if short_answer_data else '',
            double_answer_label_1=short_answer_data['double_answer_label_1'] if short_answer_data else '',
            double_answer_label_2=short_answer_data['double_answer_label_2'] if short_answer_data else '',
            answer_label_1=short_answer_data['answer_label_1'] if short_answer_data else '',
            answer_label_2=short_answer_data['answer_label_2'] if short_answer_data else '',
            answer_label_3=short_answer_data['answer_label_3'] if short_answer_data else '',
            answer_label_4=short_answer_data['answer_label_4'] if short_answer_data else '',
            explanation=explanation,
            created_by=request.user
        )
        
        # Add options for multiple choice
        if question_type == 'multiple_choice':
            question.option_a = request.POST.get('option_a', '').strip()
            question.option_b = request.POST.get('option_b', '').strip()
            question.option_c = request.POST.get('option_c', '').strip()
            question.option_d = request.POST.get('option_d', '').strip()
            question.save()
        
        return JsonResponse({
            'success': True,
            'question_id': question.id
        })
    except ValueError as e:
        return JsonResponse({
            'success': False,
            'error': 'Invalid numeric values provided.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def get_quiz_question_detail(request, question_id):
    """Return full details of a quiz question for editing"""
    try:
        question = get_object_or_404(QuizQuestion, id=question_id)
        # Restrict to owner or superuser
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = {
            'id': question.id,
            'question_text': question.question_text,
            'question_type': question.get_effective_question_type(),
            'time_limit': question.time_limit,
            'option_a': question.option_a or '',
            'option_b': question.option_b or '',
            'option_c': question.option_c or '',
            'option_d': question.option_d or '',
            'correct_answer': question.correct_answer,
            'correct_answer_2': question.correct_answer_2 or '',
            'correct_answer_3': question.correct_answer_3 or '',
            'correct_answer_4': question.correct_answer_4 or '',
            'double_answer_label_1': question.double_answer_label_1 or '',
            'double_answer_label_2': question.double_answer_label_2 or '',
            'answer_label_1': question.answer_label_1 or question.double_answer_label_1 or '',
            'answer_label_2': question.answer_label_2 or question.double_answer_label_2 or '',
            'answer_label_3': question.answer_label_3 or '',
            'answer_label_4': question.answer_label_4 or '',
            'short_answer_field_count': question.get_short_answer_field_count(),
            'short_answer_fields': question.get_short_answer_fields(),
            'explanation': question.explanation or '',
            'is_active': question.is_active,
        }
        return JsonResponse({'success': True, 'question': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_quiz_question(request):
    """Update an existing quiz question via AJAX"""
    try:
        question_id = request.POST.get('question_id')
        if not question_id:
            return JsonResponse({'success': False, 'error': 'Missing question_id'}, status=400)

        # Owner restriction
        question = get_object_or_404(QuizQuestion, id=question_id, created_by=request.user)

        question_text = request.POST.get('question_text', '').strip()
        question_type = _quiz_effective_question_type(request.POST.get('question_type', question.question_type))
        time_limit = int(request.POST.get('time_limit', question.time_limit))
        explanation = request.POST.get('explanation', question.explanation or '').strip()

        if not question_text:
            return JsonResponse({'success': False, 'error': 'Question text and correct answer are required.'}, status=400)

        short_answer_data = None
        if question_type == 'short_answer':
            short_answer_data, error = _extract_quiz_short_answer_form_data(request.POST)
            if error:
                return JsonResponse({'success': False, 'error': error}, status=400)
        else:
            correct_answer = request.POST.get('correct_answer', question.correct_answer).strip()
            if not correct_answer:
                return JsonResponse({'success': False, 'error': 'Question text and correct answer are required.'}, status=400)

        question.question_text = question_text
        question.question_type = question_type
        question.time_limit = time_limit
        question.correct_answer = short_answer_data['correct_answer'] if short_answer_data else correct_answer
        question.correct_answer_2 = short_answer_data['correct_answer_2'] if short_answer_data else ''
        question.correct_answer_3 = short_answer_data['correct_answer_3'] if short_answer_data else ''
        question.correct_answer_4 = short_answer_data['correct_answer_4'] if short_answer_data else ''
        question.double_answer_label_1 = short_answer_data['double_answer_label_1'] if short_answer_data else ''
        question.double_answer_label_2 = short_answer_data['double_answer_label_2'] if short_answer_data else ''
        question.answer_label_1 = short_answer_data['answer_label_1'] if short_answer_data else ''
        question.answer_label_2 = short_answer_data['answer_label_2'] if short_answer_data else ''
        question.answer_label_3 = short_answer_data['answer_label_3'] if short_answer_data else ''
        question.answer_label_4 = short_answer_data['answer_label_4'] if short_answer_data else ''
        question.explanation = explanation

        if question_type == 'multiple_choice':
            question.option_a = request.POST.get('option_a', '').strip()
            question.option_b = request.POST.get('option_b', '').strip()
            question.option_c = request.POST.get('option_c', '').strip()
            question.option_d = request.POST.get('option_d', '').strip()
        else:
            question.option_a = ''
            question.option_b = ''
            question.option_c = ''
            question.option_d = ''

        question.save()
        return JsonResponse({'success': True})
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid numeric values provided.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def end_quiz(request):
    """End a quiz via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        
        quiz = get_object_or_404(Quiz, id=quiz_id)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)

        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()

        quiz.end_quiz('completed')

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def start_quiz(request, room_code):
    """Start a quiz"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this quiz.'
            }, status=403)

        guard_response = _guard_session_game_start(request, 'quiz', room_code)
        if guard_response:
            return guard_response
        
        quiz.start_quiz(session_code=_extract_hub_session_code(request))
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_quiz_by_room_code(request, room_code):
    """End a quiz by room code"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)

        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()

        quiz.end_quiz('completed')

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def send_question(request, room_code):
    """Send a question to quiz participants"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to send questions to this quiz.'
            }, status=403)
        
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(QuizQuestion, id=question_id)

        # Enforce selected questions if the quiz has a predefined set
        if quiz.selected_questions.exists() and not quiz.selected_questions.filter(id=question.id).exists():
            return JsonResponse({
                'success': False,
                'error': 'This question is not part of the selected set for this quiz.'
            }, status=400)
        
        # Get or create quiz session
        quiz_session, created = QuizSession.objects.get_or_create(quiz=quiz)
        
        # Send the question
        quiz_session.send_question(question)
        
        # Here you would typically send a WebSocket message to all participants
        # We'll implement this in the WebSocket consumer
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_question(request, room_code):
    """End the current question"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)
        
        # Get quiz session
        quiz_session = get_object_or_404(QuizSession, quiz=quiz)
        
        # End current question
        quiz_session.end_current_question()
        
        # Here you would send WebSocket message to all participants
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def delete_quiz_question(request):
    """Delete a quiz question via AJAX"""
    try:
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(QuizQuestion, id=question_id, created_by=request.user)
        
        # Check if question has been used in games
        if QuizAnswer.objects.filter(question=question).exists():
            # Soft delete - just deactivate
            question.is_active = False
            question.save()
            message = 'Question deactivated (it has been used in games).'
        else:
            # Hard delete
            question.delete()
            message = 'Question deleted successfully.'
        
        return JsonResponse({
            'success': True,
            'message': message
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

@admin_required
def users_management(request):
    """User management page"""
    users = User.objects.all().order_by('-date_joined')[:50]  # Latest 50 users
    
    # Get user stats
    total_users = User.objects.count()
    active_users = User.objects.filter(last_login__gte=timezone.now() - timezone.timedelta(days=30)).count()
    admin_users = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).count()
    
    context = {
        'users': users,
        'total_users': total_users,
        'active_users': active_users,
        'admin_users': admin_users,
    }
    return render(request, 'admin_dashboard/users.html', context)


@admin_required
def analytics(request):
    """Analytics page"""
    # Get quiz statistics
    quiz_stats = {
        'total_quizzes': Quiz.objects.count(),
        'active_quizzes': Quiz.objects.filter(status='active').count(),
        'completed_quizzes': Quiz.objects.filter(status='completed').count(),
        'total_participants': QuizParticipant.objects.count(),
        'total_answers': QuizAnswer.objects.count(),
        'total_questions': QuizQuestion.objects.count(),
    }
    
    # Get recent quiz activity
    recent_quizzes = Quiz.objects.filter(
        started_at__isnull=False
    ).order_by('-started_at')[:10]
    
    # Get top performing participants
    top_participants = QuizParticipant.objects.annotate(
        quiz_count=Count('quiz')
    ).order_by('-total_score')[:10]
    
    context = {
        'quiz_stats': quiz_stats,
        'recent_quizzes': recent_quizzes,
        'top_participants': top_participants,
    }
    return render(request, 'admin_dashboard/analytics.html', context)


@admin_required
def settings(request):
    """Settings page"""
    context = {}
    return render(request, 'admin_dashboard/settings.html', context)


# API endpoints for real-time data
@admin_required
def api_quiz_stats(request, room_code):
    """Get real-time quiz statistics"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)

        hub_session = _extract_hub_session_code(request)
        participants = quiz.participants.all()
        if hub_session is not None:
            participants = participants.filter(hub_session_code=hub_session)
        answers = QuizAnswer.objects.filter(quiz=quiz)
        if hub_session is not None:
            answers = answers.filter(participant__hub_session_code=hub_session)
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': answers.count(),
            'current_question_responses': 0,
            'average_score': 0,
        }
        
        if participants.exists():
            stats['average_score'] = sum(p.total_score for p in participants) / participants.count()
        
        # Current question stats
        if quiz.current_question:
            current_answers = answers.filter(question=quiz.current_question)
            stats['current_question_responses'] = current_answers.count()
            stats['correct_current_responses'] = current_answers.filter(is_correct=True).count()
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_participants(request, room_code):
    """Get current participants list"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        hub_session = _extract_hub_session_code(request)
        participants = quiz.participants.all()
        if hub_session is not None:
            participants = participants.filter(hub_session_code=hub_session)
        participants = participants.order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'is_active': participant.is_active,
                'rank': participant.get_rank(),
                'joined_at': participant.joined_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_live_responses(request, room_code):
    """Get live responses for current question"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)

        hub_session = _extract_hub_session_code(request)
        answers = QuizAnswer.objects.filter(quiz=quiz)
        if hub_session is not None:
            answers = answers.filter(participant__hub_session_code=hub_session)

        if not quiz.current_question:
            # Auto-end race condition: question may have been cleared before the
            # monitor page finished reloading. Return most recent answers from
            # this session so responses are still visible after the page reload.
            if not quiz.started_at:
                return JsonResponse({'success': True, 'responses': []})
            recent_qs = answers.filter(submitted_at__gte=quiz.started_at).select_related('participant', 'question').order_by('-submitted_at')[:20]
            responses_data = [_serialize_quiz_live_response(response) for response in recent_qs]
            return JsonResponse({'success': True, 'responses': responses_data})

        qs = answers.filter(question=quiz.current_question)
        # Filter to only answers submitted after the question was last sent (handles replayed questions)
        if quiz.question_start_time:
            qs = qs.filter(submitted_at__gte=quiz.question_start_time)
        elif quiz.started_at:
            qs = qs.filter(submitted_at__gte=quiz.started_at)
        responses = qs.select_related('participant', 'question').order_by('-submitted_at')[:20]

        # Build key→text map for multiple choice display
        option_map = {}
        if quiz.current_question.question_type == 'multiple_choice':
            option_map = dict(quiz.current_question.get_options())

        responses_data = [_serialize_quiz_live_response(response) for response in responses]
        
        return JsonResponse({
            'success': True,
            'responses': responses_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)
    


# WHERE IS THIS GAME VIEWS

from where_is_this.models import WhereQuiz, WhereQuestion, WhereParticipant, WhereAnswer, WhereSession, WhereDistanceZone

def _normalize_where_scoring_mode(value):
    return 'rank' if value == 'rank' else 'zones'


def _parse_where_zones_from_request(data):
    raw_zones = data.get('distance_zones') or data.get('zones') or ''
    if raw_zones:
        if isinstance(raw_zones, str):
            zones = json.loads(raw_zones)
        else:
            zones = raw_zones
    else:
        points = int(data.get('points', 100))
        zones = [
            {
                'min_distance_km': 0,
                'max_distance_km': float(data.get('perfect_distance', 10)),
                'points': points,
                'label': 'Zone 1',
            },
            {
                'min_distance_km': float(data.get('perfect_distance', 10)),
                'max_distance_km': float(data.get('good_distance', 100)),
                'points': int(points * 0.75),
                'label': 'Zone 2',
            },
            {
                'min_distance_km': float(data.get('good_distance', 100)),
                'max_distance_km': float(data.get('fair_distance', 500)),
                'points': int(points * 0.5),
                'label': 'Zone 3',
            },
            {
                'min_distance_km': float(data.get('fair_distance', 500)),
                'max_distance_km': float(data.get('poor_distance', 2000)),
                'points': int(points * 0.25),
                'label': 'Zone 4',
            },
        ]

    parsed = []
    for zone in zones:
        min_distance = float(zone.get('min_distance_km', zone.get('min', 0)))
        max_distance = float(zone.get('max_distance_km', zone.get('max')))
        points = int(zone.get('points', 0))
        label = (zone.get('label') or '').strip()
        if min_distance < 0:
            raise ValueError('Zone minimum distance must be >= 0.')
        if max_distance <= min_distance:
            raise ValueError('Zone maximum distance must be greater than minimum distance.')
        if points < 0:
            raise ValueError('Zone points must be >= 0.')
        parsed.append({
            'min_distance_km': min_distance,
            'max_distance_km': max_distance,
            'points': points,
            'label': label,
        })

    parsed.sort(key=lambda zone: (zone['min_distance_km'], zone['max_distance_km']))
    previous_max = None
    for zone in parsed:
        if previous_max is not None and zone['min_distance_km'] < previous_max:
            raise ValueError('Distance zones must not overlap.')
        previous_max = zone['max_distance_km']
    return parsed


def _sync_where_distance_zones(question, zones):
    question.distance_zones.all().delete()
    for zone in zones:
        WhereDistanceZone.objects.create(
            question=question,
            min_distance_km=zone['min_distance_km'],
            max_distance_km=zone['max_distance_km'],
            points=zone['points'],
            label=zone['label'],
        )


@admin_required
def where_management(request):
    """Where is this game management page"""
    # Get recent questions
    questions = WhereQuestion.objects.filter(created_by=request.user).order_by('-created_at')[:20]
    
    # Get recent quizzes
    quizzes = WhereQuiz.objects.all().order_by('-created_at')
    
    # Get game statistics
    total_questions = WhereQuestion.objects.filter(is_active=True).count()
    user_questions = WhereQuestion.objects.filter(created_by=request.user, is_active=True).count()
    
    # Get recent game sessions
    recent_games = WhereQuiz.objects.filter(status='completed').order_by('-ended_at')[:10]
    
    # Get player statistics
    total_games = WhereQuiz.objects.filter(status='completed').count()
    total_players = WhereParticipant.objects.values('name').distinct().count()
    
    # Get average accuracy
    avg_accuracy = WhereAnswer.objects.aggregate(
        avg_accuracy=Avg('accuracy_percentage')
    )['avg_accuracy'] or 0

    context = {
        'questions': questions,
        'quizzes': quizzes,
        'total_questions': total_questions,
        'user_questions': user_questions,
        'recent_games': recent_games,
        'total_games': total_games,
        'total_players': total_players,
        'average_accuracy': round(avg_accuracy, 1),
        'bundles': WhereBundle.objects.filter(creator=request.user).prefetch_related('questions'),
    }

    return render(request, 'admin_dashboard/where_management.html', context)


@admin_required
def where_monitor(request, room_code):
    """Real-time where quiz monitoring page"""
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(WhereQuiz, room_code=room_code)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:where_management')
    
    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = WhereQuestion.objects.filter(created_by=request.user).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = WhereSession.objects.get_or_create(quiz=quiz)
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
        'current_unit_is_tutorial': is_current_unit_tutorial_question('where', quiz.room_code, hub_session, quiz.current_question_id),
    }
    return render(request, 'admin_dashboard/where_monitor.html', context)


@admin_required
@require_POST
def create_where_quiz(request):
    """Create a new where quiz via AJAX"""
    try:
        quiz = WhereQuiz.objects.create(
            title="Where is this?",
            creator=request.user,
            status='waiting'
        )
        
        # Create associated quiz session
        WhereSession.objects.create(quiz=quiz)
        
        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)

@admin_required
@require_POST
def create_where_custom_quiz(request):
    """Create a new where quiz with a name and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        title = (data.get('title') or '').strip() or 'Where is this?'
        question_ids = data.get('question_ids') or []
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = WhereQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            scoring_mode=_normalize_where_scoring_mode(data.get('scoring_mode')),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        if question_ids:
            qs = WhereQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        WhereSession.objects.create(quiz=quiz)

        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def add_where_question(request):
    """Add a new where question via AJAX"""
    try:
        question_text = request.POST.get('question_text', '').strip()
        time_limit = int(request.POST.get('time_limit', 60))
        points = max(1, int(request.POST.get('points', 1)))
        perfect_distance = float(request.POST.get('perfect_distance', 10))
        good_distance = float(request.POST.get('good_distance', 100))
        fair_distance = float(request.POST.get('fair_distance', 500))
        poor_distance = float(request.POST.get('poor_distance', 2000))
        latitude = float(request.POST.get('latitude'))
        longitude = float(request.POST.get('longitude'))
        hint_text = request.POST.get('hint_text', '').strip()
        explanation = request.POST.get('explanation', '').strip()
        map_type = request.POST.get('map_type', 'world_mercator') or 'world_mercator'
        distance_zones = _parse_where_zones_from_request(request.POST)
        
        # Handle image upload
        image = request.FILES.get('image')
        
        # Validate required fields
        if not question_text:
            return JsonResponse({
                'success': False,
                'error': 'Question text is required.'
            }, status=400)
        
        if latitude is None or longitude is None:
            return JsonResponse({
                'success': False,
                'error': 'Location coordinates are required.'
            }, status=400)
        
        # Validate coordinates
        if not (-WEB_MERCATOR_MAX_LATITUDE <= latitude <= WEB_MERCATOR_MAX_LATITUDE) or not (-180 <= longitude <= 180):
            return JsonResponse({
                'success': False,
                'error': 'Invalid coordinates provided for the world map.'
            }, status=400)
        if map_type != 'world_mercator':
            return JsonResponse({
                'success': False,
                'error': 'Only the world map is supported.'
            }, status=400)
        
        # Create question
        question = WhereQuestion.objects.create(
            question_text=question_text,
            time_limit=time_limit,
            points=points,
            perfect_distance=perfect_distance,
            good_distance=good_distance,
            fair_distance=fair_distance,
            poor_distance=poor_distance,
            correct_latitude=latitude,
            correct_longitude=longitude,
            map_type=map_type,
            hint_text=hint_text if hint_text else None,
            explanation=explanation if explanation else None,
            image=image,
            created_by=request.user
        )
        _sync_where_distance_zones(question, distance_zones)
        
        return JsonResponse({
            'success': True,
            'question_id': question.id,
            'message': 'Location question added successfully!'
        })
        
    except ValueError as e:
        return JsonResponse({
            'success': False,
            'error': 'Invalid numeric values provided.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
def get_who_that_question_detail(request, question_id):
    """Return full details of a Who Is That question for editing"""
    try:
        question = get_object_or_404(WhoThatQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = {
            'id': question.id,
            'question_text': question.question_text,
            'correct_answer': question.correct_answer,
            'alternative_answers': '\n'.join(question.alternative_answers or []),
            'points': 1,
            'time_limit': question.time_limit,
            'hint_text': question.hint_text or '',
            'explanation': question.explanation or '',
            'category': question.category or '',
            'image': question.image.url if question.image else None,
        }
        return JsonResponse({'success': True, 'question': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_who_that_question(request):
    """Update a Who Is That question via AJAX; supports multipart for optional image replacement"""
    try:
        if request.content_type and request.content_type.startswith('multipart/form-data'):
            data = request.POST
            files = request.FILES
        else:
            data = json.loads(request.body)
            files = {}

        question_id = data.get('question_id')
        question = get_object_or_404(WhoThatQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        # Update fields (only if provided)
        if 'question_text' in data:
            question.question_text = data.get('question_text', question.question_text).strip() or question.question_text
        if 'correct_answer' in data:
            question.correct_answer = data.get('correct_answer', '').strip() or question.correct_answer
        if 'alternative_answers' in data:
            alt_raw = data.get('alternative_answers', '').strip()
            alt_list = []
            if alt_raw:
                for ans in alt_raw.replace('\n', ',').split(','):
                    ans = ans.strip()
                    if ans and ans not in alt_list:
                        alt_list.append(ans)
            question.alternative_answers = alt_list
        question.points = 1
        if 'time_limit' in data:
            tl = int(data.get('time_limit'))
            if 10 <= tl <= 180:
                question.time_limit = tl
        if 'hint_text' in data:
            hint_text = data.get('hint_text', '').strip()
            question.hint_text = hint_text if hint_text else None
        if 'explanation' in data:
            explanation = data.get('explanation', '').strip()
            question.explanation = explanation if explanation else None
        if 'category' in data:
            category = data.get('category', '').strip()
            question.category = category if category else None

        # Optional image replacement
        image = files.get('image') if isinstance(files, dict) else None
        if image:
            question.image = image

        question.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
@admin_required
def get_blackjack_question_detail(request, question_id):
    """Return full details of a BlackJack question for editing"""
    try:
        question = get_object_or_404(BlackJackQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = {
            'id': question.id,
            'question_text': question.question_text,
            'correct_answer': question.correct_answer,
            'time_limit': question.time_limit,
            'explanation': question.explanation or '',
        }
        return JsonResponse({'success': True, 'question': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_blackjack_question(request):
    """Update a BlackJack question via AJAX"""
    try:
        data = json.loads(request.body)

        question_id = data.get('question_id')
        question = get_object_or_404(BlackJackQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        # Update fields
        if 'question_text' in data:
            question.question_text = data.get('question_text', '').strip() or question.question_text
        if 'correct_answer' in data:
            question.correct_answer = int(data.get('correct_answer'))
        if 'time_limit' in data:
            tl = int(data.get('time_limit'))
            if 10 <= tl <= 120:
                question.time_limit = tl
        if 'explanation' in data:
            explanation = data.get('explanation', '').strip()
            question.explanation = explanation if explanation else None

        question.save()

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
@admin_required
def get_where_question_detail(request, question_id):
    """Return full details of a Where question for editing"""
    try:
        question = get_object_or_404(WhereQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = {
            'id': question.id,
            'question_text': question.question_text,
            'time_limit': question.time_limit,
            'points': question.points,
            'perfect_distance': question.perfect_distance,
            'good_distance': question.good_distance,
            'fair_distance': question.fair_distance,
            'poor_distance': question.poor_distance,
            'latitude': question.correct_latitude,
            'longitude': question.correct_longitude,
            'map_type': question.map_type,
            'distance_zones': question.get_zone_reveal_data()['zones'],
            'hint_text': question.hint_text or '',
            'explanation': question.explanation or '',
            'image': question.image.url if question.image else None,
        }
        return JsonResponse({'success': True, 'question': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def update_where_question(request):
    """Update an existing Where question via AJAX (multipart form)"""
    try:
        question_id = request.POST.get('question_id')
        if not question_id:
            return JsonResponse({'success': False, 'error': 'Missing question_id'}, status=400)

        question = get_object_or_404(WhereQuestion, id=question_id, created_by=request.user)

        # Parse fields (fallback to current values)
        question_text = (request.POST.get('question_text') or question.question_text).strip()
        time_limit = int(request.POST.get('time_limit', question.time_limit))
        points = int(request.POST.get('points', question.points))
        perfect_distance = float(request.POST.get('perfect_distance', question.perfect_distance))
        good_distance = float(request.POST.get('good_distance', question.good_distance))
        fair_distance = float(request.POST.get('fair_distance', question.fair_distance))
        poor_distance = float(request.POST.get('poor_distance', question.poor_distance))
        latitude = request.POST.get('latitude')
        longitude = request.POST.get('longitude')
        hint_text = request.POST.get('hint_text', question.hint_text or '').strip()
        explanation = request.POST.get('explanation', question.explanation or '').strip()
        map_type = request.POST.get('map_type', question.map_type) or 'world_mercator'
        distance_zones = _parse_where_zones_from_request(request.POST)

        # Basic validation
        if not question_text:
            return JsonResponse({'success': False, 'error': 'Question text is required.'}, status=400)

        # Update fields
        question.question_text = question_text
        question.time_limit = time_limit
        question.points = points
        question.perfect_distance = perfect_distance
        question.good_distance = good_distance
        question.fair_distance = fair_distance
        question.poor_distance = poor_distance
        if map_type != 'world_mercator':
            return JsonResponse({'success': False, 'error': 'Only the world map is supported.'}, status=400)
        question.map_type = map_type

        # Only overwrite coordinates if provided
        if latitude is not None and longitude is not None and latitude != '' and longitude != '':
            lat = float(latitude)
            lng = float(longitude)
            if not (-WEB_MERCATOR_MAX_LATITUDE <= lat <= WEB_MERCATOR_MAX_LATITUDE) or not (-180 <= lng <= 180):
                return JsonResponse({'success': False, 'error': 'Invalid coordinates provided for the world map.'}, status=400)
            question.correct_latitude = lat
            question.correct_longitude = lng

        question.hint_text = hint_text if hint_text else None
        question.explanation = explanation if explanation else None

        # Optional image replacement
        image = request.FILES.get('image')
        if image:
            question.image = image

        question.save()
        _sync_where_distance_zones(question, distance_zones)
        return JsonResponse({'success': True})
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid numeric values provided.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def delete_where_question(request):
    """Delete a where question via AJAX"""
    try:
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(WhereQuestion, id=question_id)
        
        # Check if question has been used in games
        if WhereAnswer.objects.filter(question=question).exists():
            # Soft delete - just deactivate
            question.is_active = False
            question.save()
            message = 'Question deactivated (it has been used in games).'
        else:
            # Hard delete
            question.delete()
            message = 'Question deleted successfully.'
        
        return JsonResponse({
            'success': True,
            'message': message
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
@require_POST
def end_where_quiz(request):
    """End a where quiz via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        
        quiz = get_object_or_404(WhereQuiz, id=quiz_id)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def start_where_quiz(request, room_code):
    """Start a where quiz"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this quiz.'
            }, status=403)

        guard_response = _guard_session_game_start(request, 'where', room_code)
        if guard_response:
            return guard_response
        
        quiz.start_quiz()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_where_quiz_by_room_code(request, room_code):
    """End a where quiz by room code"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def send_where_question(request, room_code):
    """Send a question to where quiz participants"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to send questions to this quiz.'
            }, status=403)
        
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(WhereQuestion, id=question_id, created_by=request.user)
        
        # Get or create quiz session
        quiz_session, created = WhereSession.objects.get_or_create(quiz=quiz)
        
        # Send the question
        quiz_session.send_question(question)
        
        # Here you would typically send a WebSocket message to all participants
        # We'll implement this in the WebSocket consumer
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_where_question(request, room_code):
    """End the current where question"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)
        
        # Get quiz session
        quiz_session = get_object_or_404(WhereSession, quiz=quiz)
        
        # End current question
        quiz_session.end_current_question()
        
        # Here you would send WebSocket message to all participants
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


# API endpoints for real-time data
@admin_required
def api_where_quiz_stats(request, room_code):
    """Get real-time where quiz statistics"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all()
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': WhereAnswer.objects.filter(quiz=quiz).count(),
            'current_question_responses': 0,
            'average_score': 0,
            'average_accuracy': 0,
            'average_distance': 0,
        }
        
        if participants.exists():
            stats['average_score'] = sum(p.total_score for p in participants) / participants.count()
            
        # Get all answers for this quiz
        all_answers = WhereAnswer.objects.filter(quiz=quiz)
        if all_answers.exists():
            stats['average_accuracy'] = sum(a.accuracy_percentage for a in all_answers) / all_answers.count()
            stats['average_distance'] = sum(a.distance_km for a in all_answers) / all_answers.count()
        
        # Current question stats
        if quiz.current_question:
            current_answers = WhereAnswer.objects.filter(
                quiz=quiz, 
                question=quiz.current_question
            )
            stats['current_question_responses'] = current_answers.count()
            if current_answers.exists():
                stats['current_question_avg_accuracy'] = sum(a.accuracy_percentage for a in current_answers) / current_answers.count()
                stats['current_question_avg_distance'] = sum(a.distance_km for a in current_answers) / current_answers.count()
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_where_participants(request, room_code):
    """Get current where participants list"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all().order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'is_active': participant.is_active,
                'rank': participant.get_rank(),
                'average_accuracy': participant.get_average_accuracy(),
                'joined_at': participant.joined_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_where_live_responses(request, room_code):
    """Get live responses for current where question"""
    try:
        quiz = get_object_or_404(WhereQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        if not quiz.current_question:
            return JsonResponse({
                'success': True,
                'responses': []
            })
        
        responses = WhereAnswer.objects.filter(
            quiz=quiz,
            question=quiz.current_question
        ).select_related('participant').order_by('-submitted_at')[:20]
        
        responses_data = []
        for response in responses:
            responses_data.append({
                'participant_name': response.participant.name,
                'points_earned': response.points_earned,
                'distance_km': response.distance_km,
                'formatted_distance': response.get_formatted_distance(),
                'accuracy_percentage': response.accuracy_percentage,
                'accuracy_category': response.get_accuracy_category(),
                'time_taken': response.time_taken,
                'submitted_at': response.submitted_at.isoformat(),
                'user_latitude': response.user_latitude,
                'user_longitude': response.user_longitude,
            })
        
        return JsonResponse({
            'success': True,
            'responses': responses_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def where_game_details(request, quiz_id):
    """View detailed results of a specific where quiz"""
    quiz = get_object_or_404(WhereQuiz, id=quiz_id)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:where_management')
    
    participants = quiz.participants.all().order_by('-total_score', 'name')
    answers = WhereAnswer.objects.filter(quiz=quiz).select_related('question', 'participant').order_by('submitted_at')
    
    # Calculate quiz statistics
    quiz_stats = {
        'total_participants': participants.count(),
        'total_questions': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_score': 0,
        'average_accuracy': 0,
        'average_distance': 0,
    }
    
    if participants.exists():
        quiz_stats['average_score'] = sum(p.total_score for p in participants) / participants.count()
    
    if answers.exists():
        quiz_stats['average_accuracy'] = sum(a.accuracy_percentage for a in answers) / answers.count()
        quiz_stats['average_distance'] = sum(a.distance_km for a in answers) / answers.count()
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'answers': answers,
        'quiz_stats': quiz_stats,
    }
    
    return render(request, 'admin_dashboard/where_game_details.html', context)


@admin_required
def api_where_stats(request):
    """Get where game statistics for dashboard"""
    try:
        # Basic stats
        total_questions = WhereQuestion.objects.filter(is_active=True).count()
        total_games = WhereQuiz.objects.filter(status='completed').count()
        total_players = WhereParticipant.objects.values('name').distinct().count()
        
        # Recent activity
        recent_games = WhereQuiz.objects.filter(status='completed').order_by('-ended_at')[:5]
        
        # Top scores
        top_scores = WhereParticipant.objects.filter(quiz__status='completed').order_by('-total_score')[:5]
        
        # Average stats
        avg_stats = WhereAnswer.objects.aggregate(
            avg_accuracy=Avg('accuracy_percentage'),
            avg_distance=Avg('distance_km')
        )
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_questions': total_questions,
                'total_games': total_games,
                'total_players': total_players,
                'recent_games': [
                    {
                        'id': game.id,
                        'title': game.title,
                        'room_code': game.room_code,
                        'participant_count': game.get_participant_count(),
                        'ended_at': game.ended_at.isoformat() if game.ended_at else None,
                    }
                    for game in recent_games
                ],
                'top_scores': [
                    {
                        'participant_name': participant.name,
                        'quiz_room_code': participant.quiz.room_code,
                        'total_score': participant.total_score,
                        'average_accuracy': participant.get_average_accuracy(),
                    }
                    for participant in top_scores
                ],
                'average_accuracy': round(avg_stats['avg_accuracy'] or 0, 1),
                'average_distance': round(avg_stats['avg_distance'] or 0, 1),
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
def api_where_questions(request):
    """Get paginated list of where questions"""
    try:
        page = int(request.GET.get('page', 1))
        per_page = 20
        
        questions = WhereQuestion.objects.filter(created_by=request.user)

        questions = questions.order_by('-created_at')
        
        # Paginate
        start = (page - 1) * per_page
        end = start + per_page
        paginated_questions = questions[start:end]
        
        return JsonResponse({
            'success': True,
            'questions': [
                {
                    'id': q.id,
                    'question_text': q.question_text,
                    'correct_latitude': q.correct_latitude,
                    'correct_longitude': q.correct_longitude,
                    'points': q.points,
                    'time_limit': q.time_limit,
                    'perfect_distance': q.perfect_distance,
                    'has_image': bool(q.image),
                    'is_active': q.is_active,
                    'created_at': q.created_at.isoformat(),
                    'used_count': WhereAnswer.objects.filter(question=q).count(),
                }
                for q in paginated_questions
            ],
            'total_count': questions.count(),
            'has_next': end < questions.count(),
            'page': page,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


# ASSIGN GAMES VIEWS

from Assign.models import AssignQuiz, AssignQuestion, AssignParticipant, AssignAnswer, AssignSession
from Assign.runtime import (
    ASSIGN_REVEAL_ANIMATION_MS,
    ASSIGN_REVEAL_STAGGER_MS,
    assign_reveal_counts,
    assign_reveal_ready_at,
    current_set_runtime,
)

@admin_required
def assign_management(request):
    """Assign drag & drop quiz management page"""
    quizzes = AssignQuiz.objects.all().order_by('-created_at')
    total_questions = AssignQuestion.objects.filter(is_active=True).count()
    bundles = AssignBundle.objects.filter(creator=request.user).prefetch_related('questions')

    context = {
        'quizzes': quizzes,
        'total_questions': total_questions,
        'bundles': bundles,
    }
    return render(request, 'admin_dashboard/assign_management.html', context)


@admin_required
def assign_monitor(request, room_code):
    """Real-time assign quiz monitoring page"""
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(AssignQuiz, room_code=room_code)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:assign_management')
    
    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = AssignQuestion.objects.filter(created_by=request.user).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = AssignSession.objects.get_or_create(quiz=quiz)
    
    runtime = current_set_runtime(room_code, hub_session)
    question_runtime = current_snapshot('assign', room_code, hub_session)
    runtime_is_current = bool(
        runtime
        and quiz.current_question_id
        and runtime.question_id == quiz.current_question_id
    )
    current_round_index = (
        runtime.current_round_index
        if runtime_is_current
        else quiz_session.current_round_index
    )
    current_question = runtime.question if runtime_is_current else quiz.current_question
    total_rounds_current = len(current_question.correct_matches or {}) if current_question else 0
    current_round_ends_at = (
        runtime.round_ends_at
        if (
            runtime_is_current
            and runtime.phase == runtime.PHASE_ACTIVE
            and (
                question_runtime.get('question_flow_mode') != 'manual_three_phase'
                or question_runtime.get('question_phase') == 'answering_open'
            )
        )
        else None
    )
    current_round_time_left = (
        max(
            0,
            math.ceil((current_round_ends_at - timezone.now()).total_seconds()),
        )
        if current_round_ends_at
        else 0
    )
    target_reveal_count, element_reveal_count = assign_reveal_counts(runtime)
    content_revealed_at = question_runtime.get('content_revealed_at')
    parsed_content_revealed_at = (
        parse_datetime(content_revealed_at) if content_revealed_at else None
    )
    reveal_ready_at = assign_reveal_ready_at(runtime, parsed_content_revealed_at)

    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
        'hub_session': hub_session or '',
        'current_round_index': current_round_index,
        'total_rounds_current': total_rounds_current,
        'current_round_ends_at': current_round_ends_at,
        'current_round_time_left': current_round_time_left,
        'snapshot_server_now': timezone.now(),
        'question_runtime': question_runtime,
        'assign_target_reveal_count': target_reveal_count,
        'assign_element_reveal_count': element_reveal_count,
        'assign_reveal_stagger_ms': ASSIGN_REVEAL_STAGGER_MS,
        'assign_reveal_animation_ms': ASSIGN_REVEAL_ANIMATION_MS,
        'assign_reveal_ready_at': reveal_ready_at,
        'current_unit_is_tutorial': is_current_unit_tutorial_question('assign', quiz.room_code, hub_session, quiz.current_question_id),
    }
    return render(request, 'admin_dashboard/assign_monitor.html', context)


@admin_required
@require_POST
def create_assign_quiz(request):
    """Create a new assign quiz via AJAX"""
    try:
        quiz = AssignQuiz.objects.create(
            title="Drag & Drop Quiz",
            creator=request.user,
            status='waiting'
        )
        
        # Create associated quiz session
        AssignSession.objects.create(quiz=quiz)
        
        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)

@admin_required
@require_POST
def create_assign_custom_quiz(request):
    """Create a new assign quiz with a name and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        title = (data.get('title') or '').strip() or 'Custom Quiz'
        question_ids = data.get('question_ids') or []
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = AssignQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        # Attach selected questions (only active ones the user can access)
        if question_ids:
            qs = AssignQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        # Create session
        AssignSession.objects.create(quiz=quiz)

        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def add_assign_question(request):
    """Add a new assign question via AJAX"""
    try:
        data = json.loads(request.body)
        
        question_text = data.get('question_text', '').strip()
        time_limit = int(data.get('time_limit', 60))
        left_items = data.get('left_items', [])
        right_items = data.get('right_items', [])
        correct_matches = data.get('correct_matches', {})
        explanation = data.get('explanation', '').strip()
        
        # Validate required fields (allow empty right_items and matches)
        if not question_text or not left_items:
            return JsonResponse({
                'success': False,
                'error': 'Question text and at least one left item are required.'
            }, status=400)

        # Validate matches indices if provided
        if correct_matches:
            for k, v in correct_matches.items():
                try:
                    li = int(k); ri = int(v)
                except Exception:
                    return JsonResponse({'success': False, 'error': 'Invalid match indices.'}, status=400)
                if li < 0 or li >= len(left_items):
                    return JsonResponse({'success': False, 'error': 'Left index out of range in matches.'}, status=400)
                if ri < 0 or ri >= len(right_items):
                    return JsonResponse({'success': False, 'error': 'Right index out of range in matches.'}, status=400)

        # Create question
        question = AssignQuestion.objects.create(
            question_text=question_text,
            points=1,
            time_limit=time_limit,
            left_items=left_items,
            right_items=right_items,
            correct_matches=correct_matches,
            explanation=explanation,
            created_by=request.user
        )
        
        return JsonResponse({
            'success': True,
            'question_id': question.id
        })
        
    except ValueError as e:
        return JsonResponse({
            'success': False,
            'error': 'Invalid numeric values provided.'
        }, status=400)
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def get_assign_question_detail(request, question_id):
    try:
        question = AssignQuestion.objects.get(id=question_id)
        return JsonResponse({
            'success': True,
            'question': {
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': question.time_limit,
                'left_items': question.left_items,
                'right_items': question.right_items,
                'correct_matches': question.correct_matches,
                'explanation': question.explanation
            }
        })
    except AssignQuestion.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Question not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@admin_required
@require_POST
def update_assign_question(request):
    """Update an assign question via AJAX"""
    try:


        data = json.loads(request.body)

        question_id = data.get('question_id')
        question = get_object_or_404(AssignQuestion, id=question_id)

        question_text = data.get('question_text', question.question_text).strip()
        time_limit = int(data.get('time_limit', question.time_limit))
        left_items = data.get('left_items', question.left_items)
        right_items = data.get('right_items', question.right_items)
        correct_matches = data.get('correct_matches', question.correct_matches)
        explanation = data.get('explanation', question.explanation)

        # Validate required fields (allow empty right_items and matches)
        if not question_text or not left_items:
            return JsonResponse({
                'success': False,
                'error': 'Question text and at least one left item are required.'
            }, status=400)

        # Validate matches indices if provided
        if correct_matches:
            for k, v in correct_matches.items():
                try:
                    li = int(k); ri = int(v)
                except Exception:
                    return JsonResponse({'success': False, 'error': 'Invalid match indices.'}, status=400)
                if li < 0 or li >= len(left_items):
                    return JsonResponse({'success': False, 'error': 'Left index out of range in matches.'}, status=400)
                if ri < 0 or ri >= len(right_items):
                    return JsonResponse({'success': False, 'error': 'Right index out of range in matches.'}, status=400)

        question.question_text = question_text
        question.points = 1
        question.time_limit = time_limit
        question.left_items = left_items
        question.right_items = right_items
        question.correct_matches = correct_matches
        question.explanation = explanation
        
        question.save()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)

@admin_required
@require_POST
def end_assign_quiz(request):
    """End an assign quiz via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        
        quiz = get_object_or_404(AssignQuiz, id=quiz_id)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def start_assign_quiz(request, room_code):
    """Start an assign quiz"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this quiz.'
            }, status=403)

        guard_response = _guard_session_game_start(request, 'assign', room_code)
        if guard_response:
            return guard_response
        
        quiz.start_quiz()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_assign_quiz_by_room_code(request, room_code):
    """End an assign quiz by room code"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def send_assign_question(request, room_code):
    """Send a question to assign quiz participants"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to send questions to this quiz.'
            }, status=403)
        
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(AssignQuestion, id=question_id)

        if quiz.selected_questions.exists() and not quiz.selected_questions.filter(id=question.id).exists():
            return JsonResponse({
                'success': False,
                'error': 'This question is not part of the selected set for this quiz.'
            }, status=400)
        
        # Get or create quiz session
        quiz_session, created = AssignSession.objects.get_or_create(quiz=quiz)
        
        # Send the question
        quiz_session.send_question(question)
        
        # Here you would typically send a WebSocket message to all participants
        # We'll implement this in the WebSocket consumer
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_assign_question(request, room_code):
    """End the current assign question"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)
        
        # Get quiz session
        quiz_session = get_object_or_404(AssignSession, quiz=quiz)
        
        # End current question
        quiz_session.end_current_question()
        
        # Here you would send WebSocket message to all participants
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)

@admin_required
@require_POST
def delete_assign_question(request):
    """Delete a quiz question via AJAX"""
    try:
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(AssignQuestion, id=question_id, created_by=request.user)
        
        # Check if question has been used in games
        if AssignAnswer.objects.filter(question=question).exists():
            # Soft delete - just deactivate
            question.is_active = False
            question.save()
            message = 'Question deactivated (it has been used in games).'
        else:
            # Hard delete
            question.delete()
            message = 'Question deleted successfully.'
        
        return JsonResponse({
            'success': True,
            'message': message
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

# API endpoints for real-time data
@admin_required
def api_assign_quiz_stats(request, room_code):
    """Get real-time assign quiz statistics"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all()
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': AssignAnswer.objects.filter(quiz=quiz).count(),
            'current_question_responses': 0,
            'average_score': 0,
        }
        
        if participants.exists():
            stats['average_score'] = sum(p.total_score for p in participants) / participants.count()
        
        # Current question stats
        if quiz.current_question:
            current_answers = AssignAnswer.objects.filter(
                quiz=quiz, 
                question=quiz.current_question
            )
            stats['current_question_responses'] = current_answers.count()
            stats['average_current_score'] = sum(a.points_earned for a in current_answers) / max(1, current_answers.count())
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_assign_participants(request, room_code):
    """Get current assign participants list"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all().order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'is_active': participant.is_active,
                'rank': participant.get_rank(),
                'joined_at': participant.joined_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_assign_live_responses(request, room_code):
    """Get live responses for current assign question"""
    try:
        quiz = get_object_or_404(AssignQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        if not quiz.current_question:
            return JsonResponse({
                'success': True,
                'responses': []
            })
        
        responses = AssignAnswer.objects.filter(
            quiz=quiz,
            question=quiz.current_question
        ).select_related('participant').order_by('-submitted_at')[:20]
        
        responses_data = []
        for response in responses:
            responses_data.append({
                'participant_name': response.participant.name,
                'points_earned': response.points_earned,
                'correct_matches': response.get_correct_matches_count(),
                'total_matches': response.get_total_matches_count(),
                'time_taken': response.time_taken,
                'accuracy': response.get_accuracy_percentage(),
                'submitted_at': response.submitted_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'responses': responses_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)
    


# ESTIMATION GAME VIEWS

@admin_required
@require_POST
def add_estimation_question(request):
    """Add a new estimation question via AJAX"""
    try:
        data = json.loads(request.body)

        question_text = data.get('question_text', '').strip()
        correct_answer = float(data.get('correct_answer'))
        unit = EstimationQuestion.normalize_unit_value(data.get('unit', 'number'))
        use_manual_points = data.get('use_manual_points') is True or str(data.get('use_manual_points')).lower() in ['true', '1', 'yes', 'on']
        tolerance_percentage = float(data.get('tolerance_percentage', 10.0))
        zone_count = int(data.get('zone_count', 5))
        max_points = max(1, int(data.get('max_points') or zone_count or 1))
        hint_text = data.get('hint_text', '').strip() if data.get('hint_text') is not None else ''
        explanation = data.get('explanation', '').strip() if data.get('explanation') is not None else ''
        
        # Validate required fields
        if not question_text:
            return JsonResponse({
                'success': False,
                'error': 'Question text is required.'
            }, status=400)
        
        # Validate tolerance percentage
        if not (1.0 <= tolerance_percentage <= 50.0):
            return JsonResponse({
                'success': False,
                'error': 'Tolerance percentage must be between 1% and 50%.'
            }, status=400)
        if zone_count < 1:
            return JsonResponse({
                'success': False,
                'error': 'Zone count must be at least 1.'
            }, status=400)
        
        # Create question
        question = EstimationQuestion.objects.create(
            question_text=question_text,
            correct_answer=correct_answer,
            unit=unit,
            max_points=max_points,
            use_manual_points=use_manual_points,
            tolerance_percentage=tolerance_percentage,
            zone_count=zone_count,
            hint_text=hint_text if hint_text else None,
            explanation=explanation if explanation else None,
            created_by=request.user
        )
        
        return JsonResponse({
            'success': True,
            'question_id': question.id,
            'message': 'Question added successfully!'
        })
        
    except ValueError as e:
        return JsonResponse({
            'success': False,
            'error': 'Invalid numeric values provided.'
        }, status=400)
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

@admin_required
def get_estimation_question_detail(request, question_id):
    """Return full details of an Estimation question for editing"""
    try:
        from Estimation.models import EstimationQuestion
        question = get_object_or_404(EstimationQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = {
            'id': question.id,
            'question_text': question.question_text,
            'correct_answer': question.correct_answer,
            'unit': EstimationQuestion.normalize_unit_value(question.unit),
            'max_points': question.max_points,
            'use_manual_points': question.use_manual_points,
            'points_display': question.get_zone_max_points(),
            'tolerance_percentage': question.tolerance_percentage,
            'zone_count': question.zone_count,
            'hint_text': question.hint_text or '',
            'explanation': question.explanation or '',
        }
        return JsonResponse({'success': True, 'question': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
@require_POST
def update_estimation_question(request):
    """Update an estimation question via AJAX"""
    try:
        data = json.loads(request.body)

        question_id = data.get('question_id')
        from Estimation.models import EstimationQuestion
        question = get_object_or_404(EstimationQuestion, id=question_id)
        if not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        # Update fields
        question.question_text = data.get('question_text', question.question_text).strip()
        if 'correct_answer' in data:
            question.correct_answer = float(data.get('correct_answer'))
        if 'unit' in data:
            question.unit = EstimationQuestion.normalize_unit_value(data.get('unit'))
        if 'use_manual_points' in data:
            question.use_manual_points = data.get('use_manual_points') is True or str(data.get('use_manual_points')).lower() in ['true', '1', 'yes', 'on']
        if 'tolerance_percentage' in data:
            question.tolerance_percentage = float(data.get('tolerance_percentage'))
        if 'zone_count' in data:
            question.zone_count = max(1, int(data.get('zone_count')))
        if 'max_points' in data:
            question.max_points = max(1, int(data.get('max_points') or question.zone_count or 1))
        if 'hint_text' in data:
            hint_text = data.get('hint_text', '').strip()
            question.hint_text = hint_text if hint_text else None
        if 'explanation' in data:
            explanation = data.get('explanation', '').strip()
            question.explanation = explanation if explanation else None

        question.save()

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def delete_estimation_question(request):
    """Delete an estimation question via AJAX"""
    try:
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(EstimationQuestion, id=question_id, created_by=request.user)
        
        # Check if question has been used in games
        if EstimationAnswer.objects.filter(question=question).exists():
            # Soft delete - just deactivate
            question.is_active = False
            question.save()
            message = 'Question deactivated (it has been used in games).'
        else:
            # Hard delete
            question.delete()
            message = 'Question deleted successfully.'
        
        return JsonResponse({
            'success': True,
            'message': message
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
def api_estimation_stats(request):
    """Get estimation game statistics for dashboard"""
    try:
        # Basic stats
        total_questions = EstimationQuestion.objects.filter(is_active=True).count()
        total_games = EstimationSession.objects.filter(status='completed').count()
        total_players = EstimationSession.objects.values('player_name').distinct().count()
        
        # Recent activity
        recent_games = EstimationSession.objects.filter(status='completed').order_by('-completed_at')[:5]
        
        # Top scores
        top_scores = EstimationSession.objects.filter(status='completed').order_by('-total_score')[:5]
        
        # Average stats
        avg_stats = EstimationSession.objects.filter(status='completed').aggregate(
            avg_score=Avg('total_score')
        )
        avg_accuracy = EstimationAnswer.objects.aggregate(
            avg_accuracy=Avg('accuracy_percentage')
        )['avg_accuracy'] or 0
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_questions': total_questions,
                'total_games': total_games,
                'total_players': total_players,
                'recent_games': [
                    {
                        'id': str(game.session_id),
                        'player_name': game.player_name,
                        'total_score': game.total_score,
                        'accuracy': game.get_accuracy_percentage(),
                        'completed_at': game.completed_at.isoformat() if game.completed_at else None,
                        'duration': game.get_duration_formatted(),
                    }
                    for game in recent_games
                ],
                'top_scores': [
                    {
                        'player_name': game.player_name,
                        'total_score': game.total_score,
                        'accuracy': game.get_accuracy_percentage(),
                    }
                    for game in top_scores
                ],
                'average_score': round(avg_stats['avg_score'] or 0, 1),
                'average_accuracy': round(avg_accuracy, 1),
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
def api_estimation_questions(request):
    """Get paginated list of estimation questions"""
    try:
        page = int(request.GET.get('page', 1))
        per_page = 20
        
        questions = EstimationQuestion.objects.filter(created_by=request.user)

        questions = questions.order_by('-created_at')
        
        # Paginate
        start = (page - 1) * per_page
        end = start + per_page
        paginated_questions = questions[start:end]
        
        return JsonResponse({
            'success': True,
            'questions': [
                {
                    'id': q.id,
                    'question_text': q.question_text,
                    'correct_answer': q.correct_answer,
                    'unit': q.get_unit_display_text(),
                    'unit_name': q.get_unit_display(),
                    'max_points': q.max_points,
                    'use_manual_points': q.use_manual_points,
                    'tolerance_percentage': q.tolerance_percentage,
                    'zone_count': q.zone_count,
                    'points_display': q.get_zone_max_points(),
                    'is_active': q.is_active,
                    'created_at': q.created_at.isoformat(),
                    'used_count': EstimationAnswer.objects.filter(question=q).count(),
                }
                for q in paginated_questions
            ],
            'total_count': questions.count(),
            'has_next': end < questions.count(),
            'page': page,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)





from Estimation.models import EstimationQuiz, EstimationQuestion, EstimationParticipant, EstimationAnswer, EstimationSession

@admin_required
def estimation_management(request):
    """Estimation game management page"""
    # Get recent quizzes
    quizzes = EstimationQuiz.objects.all().order_by('-created_at')

    # Get recent questions
    questions = EstimationQuestion.objects.filter(created_by=request.user).order_by('-created_at')[:20]
    
    # Get game statistics
    total_questions = EstimationQuestion.objects.filter(is_active=True).count()
    user_questions = EstimationQuestion.objects.filter(created_by=request.user, is_active=True).count()
    
    # Get recent game sessions - use EstimationQuiz and ensure sessions exist
    recent_games = EstimationQuiz.objects.filter(
        status='completed',
        session__isnull=False
    ).select_related('session').order_by('-ended_at')[:10]
    
    # Get player statistics
    total_games = EstimationQuiz.objects.filter(status='completed').count()
    total_players = EstimationParticipant.objects.values('name').distinct().count()
    
    # Get average accuracy - calculate in Python since it's a method, not a field
    all_answers = EstimationAnswer.objects.all()
    if all_answers.exists():
        total_accuracy = sum(answer.get_accuracy_percentage() for answer in all_answers)
        avg_accuracy = total_accuracy / all_answers.count()
    else:
        avg_accuracy = 0

    context = {
        'quizzes': quizzes,
        'questions': questions,
        'total_questions': total_questions,
        'user_questions': user_questions,
        'recent_games': recent_games,
        'total_games': total_games,
        'total_players': total_players,
        'average_accuracy': round(avg_accuracy, 1),
        'bundles': EstimationBundle.objects.filter(creator=request.user).prefetch_related('questions'),
        'estimation_unit_choices': EstimationQuestion.get_unit_choices_for_display(),
    }

    return render(request, 'admin_dashboard/estimation_management.html', context)


@admin_required
@require_POST
def create_estimation_quiz(request):
    """Create a new estimation quiz via AJAX"""
    try:
        data = {}
        try:
            data = json.loads(request.body or '{}')
        except Exception:
            data = {}
        scoring_mode = (data.get('scoring_mode') or 'zones').strip()
        quiz = EstimationQuiz.objects.create(
            title="Estimation Quiz",
            creator=request.user,
            status='waiting',
            scoring_mode='rank' if scoring_mode == 'rank' else ('zones' if scoring_mode == 'zones' else 'tolerance'),
        )
        
        # Create associated quiz session
        EstimationSession.objects.create(quiz=quiz)
        
        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
         return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def create_estimation_custom_quiz(request):
    """Create a new estimation quiz with a name and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        title = (data.get('title') or '').strip() or 'Estimation Quiz'
        question_ids = data.get('question_ids') or []
        scoring_mode = (data.get('scoring_mode') or 'zones').strip()
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)

        quiz = EstimationQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting',
            scoring_mode='rank' if scoring_mode == 'rank' else ('zones' if scoring_mode == 'zones' else 'tolerance')
        )

        if question_ids:
            qs = EstimationQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)
            _apply_tutorial_question_selection(quiz, data, qs.values_list('id', flat=True))

        EstimationSession.objects.create(quiz=quiz)

        return JsonResponse({'success': True, 'room_code': quiz.room_code, 'quiz_id': quiz.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@admin_required
def estimation_game_details(request, quiz_id):
    """View detailed results of a specific estimation quiz"""
    quiz = get_object_or_404(EstimationQuiz, id=quiz_id)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:estimation_management')
    
    participants = quiz.participants.all().order_by('-total_score', 'name')
    answers = EstimationAnswer.objects.filter(quiz=quiz).select_related('question', 'participant').order_by('submitted_at')
    
    # Calculate quiz statistics
    quiz_stats = {
        'total_participants': participants.count(),
        'total_questions': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_score': 0,
        'average_accuracy': 0,
    }
    
    if participants.exists():
        total_scores = [p.total_score for p in participants]
        quiz_stats['average_score'] = sum(total_scores) / len(total_scores)
    
    if answers.exists():
        total_accuracy = sum(answer.get_accuracy_percentage() for answer in answers)
        quiz_stats['average_accuracy'] = total_accuracy / answers.count()
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'answers': answers,
        'quiz_stats': quiz_stats,
    }
    
    return render(request, 'admin_dashboard/estimation_game_details.html', context)


@admin_required
def estimation_monitor(request, room_code):
    """Real-time estimation quiz monitoring page"""
    hub_session = request.GET.get('hub_session') or _get_active_hub_session_code_for_room(
        'estimation', room_code,
    )
    quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
    default_question_time_limit = 90
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:estimation_management')
    
    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = EstimationQuestion.objects.filter(created_by=request.user, is_active=True).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = EstimationSession.objects.get_or_create(quiz=quiz)
    question_runtime = current_snapshot('estimation', room_code, hub_session)
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'question_runtime': question_runtime,
        # Estimation currently uses a shared 90-second runtime default,
        # not a per-question persisted time_limit field.
        'default_question_time_limit': default_question_time_limit,
        'lobby_url': _get_lobby_url(request, room_code),
        'current_unit_is_tutorial': is_current_unit_tutorial_question('estimation', quiz.room_code, hub_session, quiz.current_question_id),
    }
    return render(request, 'admin_dashboard/estimation_monitor.html', context)


@admin_required
@require_POST
def start_estimation_quiz(request, room_code):
    """Start an estimation quiz"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this quiz.'
            }, status=403)

        guard_response = _guard_session_game_start(request, 'estimation', room_code)
        if guard_response:
            return guard_response
        
        quiz.start_quiz()
        session_code = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room(
            'estimation', room_code,
        )
        reset_question_flow(
            game_key='estimation',
            room_code=room_code,
            session_code=session_code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_estimation_quiz(request):
    """End an estimation quiz via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        
        quiz = get_object_or_404(EstimationQuiz, id=quiz_id)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_estimation_quiz_by_room_code(request, room_code):
    """End an estimation quiz by room code"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def send_estimation_question(request, room_code):
    """Send a question to estimation quiz participants"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to send questions to this quiz.'
            }, status=403)
        
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(EstimationQuestion, id=question_id, is_active=True)

        if quiz.selected_questions.exists() and not quiz.selected_questions.filter(id=question.id).exists():
            return JsonResponse({
                'success': False,
                'error': 'This question is not part of the selected set for this quiz.'
            }, status=400)
        
        session_code = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room(
            'estimation', room_code,
        )
        runtime = current_snapshot('estimation', room_code, session_code)
        action = {
            'client_action_id': data.get('client_action_id') or str(uuid.uuid4()),
            'state_revision': runtime.get('state_revision'),
            'game_id': runtime.get('game_id'),
            'question_id': question.id,
        }
        try:
            answer_duration = int(data.get('custom_time_limit') or 90)
        except (TypeError, ValueError):
            answer_duration = 90
        if answer_duration <= 0:
            answer_duration = 90

        with transaction.atomic():
            locked_quiz = EstimationQuiz.objects.select_for_update().get(pk=quiz.pk)
            quiz_session, _ = EstimationSession.objects.select_for_update().get_or_create(
                quiz=locked_quiz,
            )
            decision = present_question(
                game_key='estimation',
                room_code=room_code,
                session_code=session_code,
                action=action,
                answer_duration_seconds=answer_duration,
            )
            if decision.accepted and not decision.duplicate:
                quiz_session.prepare_question(question)
        if not decision.accepted:
            return JsonResponse({
                'success': False,
                'error': decision.message,
                'code': decision.code,
            }, status=409)
        
        # Here you would typically send a WebSocket message to all participants
        # We'll implement this in the WebSocket consumer
        
        return JsonResponse({'success': True, 'question_phase': decision.snapshot.get('question_phase')})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_estimation_question(request, room_code):
    """End the current estimation question"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)
        
        # Get quiz session
        quiz_session = get_object_or_404(EstimationSession, quiz=quiz)
        
        # End current question
        quiz_session.end_current_question()
        
        # Here you would send WebSocket message to all participants
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


# API endpoints for real-time data
@admin_required
def api_estimation_quiz_stats(request, room_code):
    """Get real-time estimation quiz statistics"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all()
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': EstimationAnswer.objects.filter(quiz=quiz).count(),
            'current_question_responses': 0,
            'average_score': 0,
            'average_accuracy': 0,
        }
        
        if participants.exists():
            total_scores = [p.total_score for p in participants]
            stats['average_score'] = sum(total_scores) / len(total_scores)
            
        # Get all answers for this quiz to calculate accuracy
        all_answers = EstimationAnswer.objects.filter(quiz=quiz)
        if all_answers.exists():
            total_accuracy = sum(answer.get_accuracy_percentage() for answer in all_answers)
            stats['average_accuracy'] = total_accuracy / all_answers.count()
        
        # Current question stats
        if quiz.current_question:
            current_answers = EstimationAnswer.objects.filter(
                quiz=quiz, 
                question=quiz.current_question
            )
            stats['current_question_responses'] = current_answers.count()
            if current_answers.exists():
                current_accuracy = sum(answer.get_accuracy_percentage() for answer in current_answers)
                stats['current_question_avg_accuracy'] = current_accuracy / current_answers.count()
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_estimation_participants(request, room_code):
    """Get current estimation participants list"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all().order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'is_active': participant.is_active,
                'rank': participant.get_rank(),
                'average_accuracy': participant.get_average_accuracy(),
                'joined_at': participant.joined_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_estimation_live_responses(request, room_code):
    """Get live responses for current estimation question"""
    try:
        quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        if not quiz.current_question:
            return JsonResponse({
                'success': True,
                'responses': []
            })
        
        responses = EstimationAnswer.objects.filter(
            quiz=quiz,
            question=quiz.current_question
        ).select_related('participant').order_by('-submitted_at')[:20]
        
        responses_data = []
        for response in responses:
            responses_data.append({
                'participant_name': response.participant.name,
                'user_answer': response.user_answer,
                'formatted_answer': response.get_formatted_user_answer(),
                'points_earned': response.points_earned,
                'accuracy_percentage': response.get_accuracy_percentage(),
                'percentage_difference': response.get_percentage_difference(),
                'difference_indicator': response.get_difference_indicator(),
                'time_taken': response.time_taken,
                'submitted_at': response.submitted_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'responses': responses_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)




from who_is_lying.models import WhoQuiz, WhoQuestion, WhoParticipant, WhoAnswer, WhoSession, get_question_timer_state

@admin_required
def who_management(request):
    """Who is lying quiz management page"""
    quizzes = WhoQuiz.objects.all().order_by('-created_at')
    total_questions = WhoQuestion.objects.filter(is_active=True).count()
    bundles = WhoBundle.objects.filter(creator=request.user).prefetch_related('questions')

    context = {
        'quizzes': quizzes,
        'total_questions': total_questions,
        'bundles': bundles,
    }
    return render(request, 'admin_dashboard/who_lying_management.html', context)


@admin_required
def who_monitor(request, room_code):
    """Real-time who is lying quiz monitoring page"""
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(WhoQuiz, room_code=room_code)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:who_management')
    
    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = WhoQuestion.objects.filter(created_by=request.user).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = WhoSession.objects.get_or_create(quiz=quiz)
    question_runtime = current_snapshot('who', room_code, hub_session)
    manual_question_flow = (
        question_runtime.get('question_flow_mode')
        == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
    )
    question_answering_open = (
        not manual_question_flow
        or question_runtime.get('question_phase')
        == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
    )
    current_question_time_left = None
    current_question_people = []
    current_person_index = 0
    current_person_name = None
    current_question_time_per_person = None
    current_question_started_at = None
    who_timer_server_now = timezone.now()
    if quiz.current_question:
        randomized_people = quiz.current_question.get_randomized_people(room_code=quiz.room_code)
        current_question_people = []
        for person in randomized_people.get('people', []):
            original_index = person.get('original_index')
            original_people = quiz.current_question.people or []
            is_lying = False
            if isinstance(original_index, int) and 0 <= original_index < len(original_people):
                is_lying = bool(original_people[original_index].get('is_lying', False))
            current_question_people.append({
                **person,
                'is_lying': is_lying,
            })
        current_question_time_per_person = max(int(quiz.current_question.time_limit or 0), 0)
        people_count = len(current_question_people)
        timer_state = get_question_timer_state(
            quiz.current_question,
            question_start_time=quiz.question_start_time,
            question_end_time=quiz_session.question_end_time,
            people_count=people_count,
            server_now=who_timer_server_now,
        )
        current_question_time_per_person = timer_state['time_per_person'] or None
        current_person_index = timer_state['current_person_index']
        current_question_time_left = timer_state['current_person_time_left']
        current_question_started_at = quiz.question_start_time
        if people_count and question_answering_open:
            current_person_name = current_question_people[current_person_index].get('name') or None
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'total_sets': available_questions.count(),
        'lobby_url': _get_lobby_url(request, room_code),
        'current_question_time_left': current_question_time_left,
        'current_question_people': current_question_people,
        'current_person_index': current_person_index,
        'current_person_name': current_person_name,
        'current_question_time_per_person': current_question_time_per_person,
        'current_question_started_at': current_question_started_at,
        'who_timer_server_now': who_timer_server_now,
        'question_runtime': question_runtime,
        'question_answering_open': question_answering_open,
        'current_unit_is_tutorial': is_current_unit_tutorial_question('who', quiz.room_code, hub_session, quiz.current_question_id),
    }
    return render(request, 'admin_dashboard/who_lying_monitor.html', context)


@admin_required
@require_POST
def create_who_quiz(request):
    """Create a new who is lying quiz via AJAX"""
    try:
        quiz = WhoQuiz.objects.create(
            title="Who is Lying?",
            creator=request.user,
            status='waiting'
        )
        
        # Create associated quiz session
        WhoSession.objects.create(quiz=quiz)
        
        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def add_who_question(request):
    """Add a new who is lying question via AJAX"""
    try:
        data = json.loads(request.body)
        
        statement = data.get('statement', '').strip()
        points = int(data.get('points', 10))
        time_limit = int(data.get('time_limit', 60))
        people = data.get('people', [])
        explanation = data.get('explanation', '').strip()
        
        # Validate required fields
        if not statement or not people:
            return JsonResponse({
                'success': False,
                'error': 'Statement and people are required.'
            }, status=400)
        
        # Validate people data
        if len(people) < 2:
            return JsonResponse({
                'success': False,
                'error': 'At least 2 people are required.'
            }, status=400)
        
        # Validate people structure
        for person in people:
            if not isinstance(person, dict) or 'name' not in person or 'is_lying' not in person:
                return JsonResponse({
                    'success': False,
                    'error': 'Invalid people data format.'
                }, status=400)
            
            if not person['name'].strip():
                return JsonResponse({
                    'success': False,
                    'error': 'All people must have names.'
                }, status=400)
        
        # Create question
        question = WhoQuestion.objects.create(
            statement=statement,
            points=points,
            time_limit=time_limit,
            people=people,
            explanation=explanation,
            created_by=request.user
        )
        
        return JsonResponse({
            'success': True,
            'question_id': question.id
        })
        
    except ValueError as e:
        return JsonResponse({
            'success': False,
            'error': 'Invalid numeric values provided.'
        }, status=400)
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)

@admin_required
@require_POST
def delete_who_question(request):
    """Delete a who question via AJAX"""
    try:
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(WhoQuestion, id=question_id)
        
        # Check if question has been used in games
        if WhoAnswer.objects.filter(question=question).exists():
            # Soft delete - just deactivate
            question.is_active = False
            question.save()
            message = 'Question deactivated (it has been used in games).'
        else:
            # Hard delete
            question.delete()
            message = 'Question deleted successfully.'
        
        return JsonResponse({
            'success': True,
            'message': message
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

@admin_required
@require_POST
def end_who_quiz(request):
    """End a who is lying quiz via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        
        quiz = get_object_or_404(WhoQuiz, id=quiz_id)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def start_who_quiz(request, room_code):
    """Start a who is lying quiz"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this quiz.'
            }, status=403)

        guard_response = _guard_session_game_start(request, 'who', room_code)
        if guard_response:
            return guard_response
        
        quiz.start_quiz()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_who_quiz_by_room_code(request, room_code):
    """End a who is lying quiz by room code"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def send_who_question(request, room_code):
    """Send a question to who is lying quiz participants"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to send questions to this quiz.'
            }, status=403)
        
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(WhoQuestion, id=question_id, created_by=request.user)
        
        # Get or create quiz session
        quiz_session, created = WhoSession.objects.get_or_create(quiz=quiz)
        
        # Send the question
        quiz_session.send_question(question)
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_who_question(request, room_code):
    """End the current who is lying question"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)
        
        # Get quiz session
        quiz_session = get_object_or_404(WhoSession, quiz=quiz)
        
        # End current question
        quiz_session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def who_game_details(request, quiz_id):
    """View detailed results of a specific who is lying quiz"""
    quiz = get_object_or_404(WhoQuiz, id=quiz_id)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:who_management')
    
    participants = quiz.participants.all().order_by('-total_score', 'name')
    answers = WhoAnswer.objects.filter(quiz=quiz).select_related('question', 'participant').order_by('submitted_at')
    
    # Calculate quiz statistics
    quiz_stats = {
        'total_participants': participants.count(),
        'total_questions': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_score': 0,
        'average_accuracy': 0,
    }
    
    if participants.exists():
        total_scores = [p.total_score for p in participants]
        quiz_stats['average_score'] = sum(total_scores) / len(total_scores)
    
    if answers.exists():
        total_accuracy = sum(answer.get_accuracy_percentage() for answer in answers)
        quiz_stats['average_accuracy'] = total_accuracy / answers.count()
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'answers': answers,
        'quiz_stats': quiz_stats,
    }
    
    return render(request, 'admin_dashboard/who_game_details.html', context)


# API endpoints for real-time data
@admin_required
def api_who_quiz_stats(request, room_code):
    """Get real-time who is lying quiz statistics"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all()
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': WhoAnswer.objects.filter(quiz=quiz).count(),
            'current_question_responses': 0,
            'average_score': 0,
            'average_accuracy': 0,
        }
        
        if participants.exists():
            total_scores = [p.total_score for p in participants]
            stats['average_score'] = sum(total_scores) / len(total_scores)
            
        # Get all answers for this quiz to calculate accuracy
        all_answers = WhoAnswer.objects.filter(quiz=quiz)
        if all_answers.exists():
            total_accuracy = sum(answer.get_accuracy_percentage() for answer in all_answers)
            stats['average_accuracy'] = total_accuracy / all_answers.count()
        
        # Current question stats
        if quiz.current_question:
            current_answers = WhoAnswer.objects.filter(
                quiz=quiz, 
                question=quiz.current_question
            )
            stats['current_question_responses'] = current_answers.count()
            if current_answers.exists():
                current_accuracy = sum(answer.get_accuracy_percentage() for answer in current_answers)
                stats['current_question_avg_accuracy'] = current_accuracy / current_answers.count()
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_who_participants(request, room_code):
    """Get current who is lying participants list"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all().order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'is_active': participant.is_active,
                'rank': participant.get_rank(),
                'average_accuracy': participant.get_average_accuracy(),
                'joined_at': participant.joined_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_who_live_responses(request, room_code):
    """Get live responses for current who is lying question"""
    try:
        quiz = get_object_or_404(WhoQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        if not quiz.current_question:
            return JsonResponse({
                'success': True,
                'responses': []
            })
        
        responses = WhoAnswer.objects.filter(
            quiz=quiz,
            question=quiz.current_question
        ).select_related('participant').order_by('-submitted_at')[:20]
        
        responses_data = []
        for response in responses:
            # Get analysis for this response
            analysis = response.get_detailed_analysis()
            selected_liars_names = response.get_selected_liars_names()
            
            responses_data.append({
                'participant_name': response.participant.name,
                'points_earned': response.points_earned,
                'correct_identifications': response.get_correct_identifications_count(),
                'total_people': response.get_total_people_count(),
                'selected_liars_names': selected_liars_names,
                'time_taken': response.time_taken,
                'accuracy': response.get_accuracy_percentage(),
                'submitted_at': response.submitted_at.isoformat(),
                'analysis': analysis
            })
        
        return JsonResponse({
            'success': True,
            'responses': responses_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_who_stats(request):
    """Get who is lying game statistics for dashboard"""
    try:
        # Basic stats
        total_questions = WhoQuestion.objects.count()
        total_games = WhoQuiz.objects.filter(status='completed').count()
        total_players = WhoParticipant.objects.values('name').distinct().count()
        
        # Recent activity
        recent_games = WhoQuiz.objects.filter(status='completed').order_by('-ended_at')[:5]
        
        # Top scores
        top_scores = WhoParticipant.objects.filter(quiz__status='completed').order_by('-total_score')[:5]
        
        # Average stats
        all_answers = WhoAnswer.objects.all()
        avg_accuracy = 0
        if all_answers.exists():
            total_accuracy = sum(answer.get_accuracy_percentage() for answer in all_answers)
            avg_accuracy = total_accuracy / all_answers.count()
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_questions': total_questions,
                'total_games': total_games,
                'total_players': total_players,
                'recent_games': [
                    {
                        'id': game.id,
                        'title': game.title,
                        'room_code': game.room_code,
                        'participant_count': game.get_participant_count(),
                        'ended_at': game.ended_at.isoformat() if game.ended_at else None,
                    }
                    for game in recent_games
                ],
                'top_scores': [
                    {
                        'participant_name': participant.name,
                        'quiz_room_code': participant.quiz.room_code,
                        'total_score': participant.total_score,
                        'average_accuracy': participant.get_average_accuracy(),
                    }
                    for participant in top_scores
                ],
                'average_accuracy': round(avg_accuracy, 1),
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
def api_who_questions(request):
    """Get paginated list of who is lying questions"""
    try:
        page = int(request.GET.get('page', 1))
        per_page = 20
        
        questions = WhoQuestion.objects.filter(created_by=request.user).order_by('-created_at')
        
        # Paginate
        start = (page - 1) * per_page
        end = start + per_page
        paginated_questions = questions[start:end]
        
        return JsonResponse({
            'success': True,
            'questions': [
                {
                    'id': q.id,
                    'statement': q.statement,
                    'points': q.points,
                    'time_limit': q.time_limit,
                    'people_count': len(q.people),
                    'liars_count': len(q.get_liars()),
                    'truth_tellers_count': len(q.get_truth_tellers()),
                    'created_at': q.created_at.isoformat(),
                    'used_count': WhoAnswer.objects.filter(question=q).count(),
                }
                for q in paginated_questions
            ],
            'total_count': questions.count(),
            'has_next': end < questions.count(),
            'page': page,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


from who_is_that.models import WhoThatQuiz, WhoThatQuestion, WhoThatParticipant, WhoThatAnswer, WhoThatSession

@admin_required
def who_that_management(request):
    """Who is that game management page"""
    # Get recent questions
    questions = WhoThatQuestion.objects.filter(created_by=request.user).order_by('-created_at')[:20]
    
    # Get recent quizzes
    quizzes = WhoThatQuiz.objects.all().order_by('-created_at')
    
    # Get game statistics
    total_questions = WhoThatQuestion.objects.filter(is_active=True).count()
    user_questions = WhoThatQuestion.objects.filter(created_by=request.user, is_active=True).count()
    
    # Get recent game sessions
    recent_games = WhoThatQuiz.objects.filter(status='completed').order_by('-ended_at')[:10]
    
    # Get player statistics
    total_games = WhoThatQuiz.objects.filter(status='completed').count()
    total_players = WhoThatParticipant.objects.values('name').distinct().count()
    
    # Get average accuracy
    all_answers = WhoThatAnswer.objects.all()
    if all_answers.exists():
        total_accuracy = sum(answer.get_accuracy_percentage() for answer in all_answers)
        avg_accuracy = total_accuracy / all_answers.count()
    else:
        avg_accuracy = 0

    context = {
        'questions': questions,
        'quizzes': quizzes,
        'total_questions': total_questions,
        'user_questions': user_questions,
        'recent_games': recent_games,
        'total_games': total_games,
        'total_players': total_players,
        'average_accuracy': round(avg_accuracy, 1),
        'bundles': WhoThatBundle.objects.filter(creator=request.user).prefetch_related('questions'),
    }

    return render(request, 'admin_dashboard/who_that_management.html', context)


@admin_required
@require_POST
def create_who_that_quiz(request):
    """Create a new who is that quiz via AJAX"""
    try:
        quiz = WhoThatQuiz.objects.create(
            title="Who is That?",
            creator=request.user,
            status='waiting'
        )
        
        # Create associated quiz session
        WhoThatSession.objects.create(quiz=quiz)
        
        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def who_that_monitor(request, room_code):
    """Real-time who is that quiz monitoring page"""
    hub_session = request.GET.get('hub_session')
    active_hub_session_code = hub_session or _get_active_hub_session_code_for_room('who_that', room_code)
    quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:who_that_management')
    
    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = WhoThatQuestion.objects.filter(created_by=request.user, is_active=True).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = WhoThatSession.objects.get_or_create(quiz=quiz)
    question_runtime = current_snapshot(
        'who_that',
        room_code,
        active_hub_session_code,
    )
    question_phase = question_runtime.get('question_phase')
    manual_question_flow = question_runtime.get('question_flow_mode') == 'manual_three_phase'
    question_answering_open = bool(
        question_runtime.get('answering_allowed')
        if manual_question_flow
        else quiz_session.is_question_active
    )
    current_question_presented = bool(
        quiz.current_question_id
        and (
            question_phase in {'content_visible', 'answering_open'}
            if manual_question_flow
            else quiz_session.is_question_active
        )
    )
    review_question = None
    raw_review_question_id = request.GET.get('review_question')
    if raw_review_question_id and not current_question_presented:
        try:
            review_question_id = int(raw_review_question_id)
        except (TypeError, ValueError):
            review_question_id = None
        if review_question_id:
            if quiz.selected_questions.filter(id=review_question_id).exists():
                review_question = quiz.selected_questions.filter(id=review_question_id).first()
            else:
                review_question = WhoThatQuestion.objects.filter(
                    id=review_question_id,
                    created_by=quiz.creator,
                ).first()

    display_question = quiz.current_question if quiz.current_question_id else review_question
    is_display_question_active = current_question_presented
    current_question_time_left = None
    if display_question and question_answering_open:
        current_question_time_left = display_question.time_limit
        if quiz_session.question_end_time:
            current_question_time_left = max(
                0,
                int((quiz_session.question_end_time - timezone.now()).total_seconds() + 0.999),
            )
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'active_hub_session_code': active_hub_session_code,
        'display_question': display_question,
        'is_display_question_active': is_display_question_active,
        'question_runtime': question_runtime,
        'question_phase': question_phase,
        'question_answering_open': question_answering_open,
        'review_question_id': review_question.id if review_question else None,
        'current_question_time_left': current_question_time_left,
        'lobby_url': _get_lobby_url(request, room_code),
        'current_unit_is_tutorial': is_current_unit_tutorial_question('who_that', quiz.room_code, active_hub_session_code, quiz.current_question_id),
    }
    return render(request, 'admin_dashboard/who_that_monitor.html', context)


@admin_required
@require_POST
def add_who_that_question(request):
    """Add a new who is that question via AJAX"""
    try:
        question_text = request.POST.get('question_text', 'Who is this person?').strip()
        correct_answer = request.POST.get('correct_answer', '').strip()
        alternative_answers = request.POST.get('alternative_answers', '').strip()
        time_limit = int(request.POST.get('time_limit', 30))
        hint_text = request.POST.get('hint_text', '').strip()
        explanation = request.POST.get('explanation', '').strip()
        category = request.POST.get('category', '').strip()
        
        # Handle image upload
        image = request.FILES.get('image')
        
        # Validate required fields
        if not correct_answer:
            return JsonResponse({
                'success': False,
                'error': 'Correct answer is required.'
            }, status=400)
        
        if not image:
            return JsonResponse({
                'success': False,
                'error': 'Image is required.'
            }, status=400)
        
        # Process alternative answers
        alt_answers_list = []
        if alternative_answers:
            # Split by comma or newline and clean up
            for answer in alternative_answers.replace('\n', ',').split(','):
                answer = answer.strip()
                if answer and answer not in alt_answers_list:
                    alt_answers_list.append(answer)
        
        # Create question
        question = WhoThatQuestion.objects.create(
            question_text=question_text,
            image=image,
            correct_answer=correct_answer,
            alternative_answers=alt_answers_list,
            points=1,
            time_limit=time_limit,
            hint_text=hint_text if hint_text else None,
            explanation=explanation if explanation else None,
            category=category if category else None,
            created_by=request.user
        )
        
        return JsonResponse({
            'success': True,
            'question_id': question.id,
            'message': 'Question added successfully!'
        })
        
    except ValueError as e:
        return JsonResponse({
            'success': False,
            'error': 'Invalid numeric values provided.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
@require_POST
def delete_who_that_question(request):
    """Delete a who is that question via AJAX"""
    try:
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(WhoThatQuestion, id=question_id, created_by=request.user)
        
        # Check if question has been used in games
        if WhoThatAnswer.objects.filter(question=question).exists():
            # Soft delete - just deactivate
            question.is_active = False
            question.save()
            message = 'Question deactivated (it has been used in games).'
        else:
            # Hard delete
            question.delete()
            message = 'Question deleted successfully.'
        
        return JsonResponse({
            'success': True,
            'message': message
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
@require_POST
def start_who_that_quiz(request, room_code):
    """Start a who is that quiz"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this quiz.'
            }, status=403)

        guard_response = _guard_session_game_start(request, 'who_that', room_code)
        if guard_response:
            return guard_response
        
        quiz.start_quiz()
        session_code = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room(
            'who_that',
            room_code,
        )
        reset_question_flow(
            game_key='who_that',
            room_code=room_code,
            session_code=session_code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_who_that_quiz(request):
    """End a who is that quiz via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        
        quiz = get_object_or_404(WhoThatQuiz, id=quiz_id)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_who_that_quiz_by_room_code(request, room_code):
    """End a who is that quiz by room code"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def send_who_that_question(request, room_code):
    """Send a question to who is that quiz participants"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to send questions to this quiz.'
            }, status=403)
        
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(WhoThatQuestion, id=question_id, is_active=True)

        if quiz.selected_questions.exists() and not quiz.selected_questions.filter(id=question.id).exists():
            return JsonResponse({
                'success': False,
                'error': 'This question is not part of the selected set for this quiz.'
            }, status=400)
        
        session_code = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room(
            'who_that',
            room_code,
        )
        snapshot = current_snapshot('who_that', room_code, session_code)
        effective_time_limit = data.get('custom_time_limit') or question.time_limit
        with transaction.atomic():
            quiz_session, created = WhoThatSession.objects.select_for_update().get_or_create(quiz=quiz)
            decision = present_question(
                game_key='who_that',
                room_code=room_code,
                session_code=session_code,
                action={
                    'question_id': question.id,
                    'game_id': str(quiz.id),
                    'state_revision': data.get('state_revision', snapshot['state_revision']),
                    'client_action_id': data.get('client_action_id') or str(uuid.uuid4()),
                },
                answer_duration_seconds=effective_time_limit,
            )
            if decision.accepted and not decision.duplicate:
                quiz_session.prepare_question(question)

        if not decision.accepted:
            return JsonResponse({
                'success': False,
                'code': decision.code,
                'error': decision.message,
                'snapshot': decision.snapshot,
            }, status=409)

        lifecycle_fields = {
            key: decision.snapshot.get(key)
            for key in (
                'state_revision',
                'server_now',
                'game_id',
                'question_phase',
                'question_presented_at',
                'question_visible_at',
                'content_revealed_at',
                'answering_started_at',
                'answering_deadline_at',
                'answering_allowed',
                'timer_running',
                'remaining_answer_time',
            )
        }
        async_to_sync(get_channel_layer().group_send)(
            f'who_that_{room_code}',
            {
                'type': 'question_prepared',
                'question': {
                    'id': question.id,
                    'points': 1,
                    'question_number': quiz_session.current_question_number,
                    'time_limit': effective_time_limit,
                },
                **lifecycle_fields,
            },
        )
        return JsonResponse({'success': True, **lifecycle_fields})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_who_that_question(request, room_code):
    """End the current who is that question"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)
        
        # Get quiz session
        quiz_session = get_object_or_404(WhoThatSession, quiz=quiz)
        
        # End current question
        quiz_session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def set_who_that_question_points(request, room_code):
    """Legacy no-op: Who is That uses fixed 1/0 scoring."""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)

        data = json.loads(request.body)
        question_id = data.get('question_id')
        question = get_object_or_404(WhoThatQuestion, id=question_id, is_active=True)

        if quiz.selected_questions.exists():
            is_allowed_question = (
                quiz.current_question_id == question.id or
                quiz.selected_questions.filter(id=question.id).exists()
            )
            if not is_allowed_question:
                return JsonResponse({
                    'success': False,
                    'error': 'This question is not part of the selected set for this quiz.'
                }, status=400)
        elif not request.user.is_superuser and question.created_by != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to edit this question.'
            }, status=403)

        if question.points != 1:
            question.points = 1
            question.save(update_fields=['points'])

        updated_scores = []
        if quiz.current_question_id == question.id:
            participant_ids = set()
            answers = WhoThatAnswer.objects.filter(
                quiz=quiz,
                question=question
            ).select_related('participant')

            for answer in answers:
                recalculated_points = question.calculate_score(answer.user_answer)
                if answer.points_earned != recalculated_points:
                    WhoThatAnswer.objects.filter(id=answer.id).update(points_earned=recalculated_points)
                participant_ids.add(answer.participant_id)

            for participant in WhoThatParticipant.objects.filter(id__in=participant_ids):
                participant.calculate_score()
                updated_scores.append({
                    'participant_id': participant.id,
                    'total_score': participant.total_score,
                })

        return JsonResponse({
            'success': True,
            'points': 1,
            'updated_scores': updated_scores,
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def who_that_game_details(request, quiz_id):
    """View detailed results of a specific who is that quiz"""
    quiz = get_object_or_404(WhoThatQuiz, id=quiz_id)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:who_that_management')
    
    participants = quiz.participants.all().order_by('-total_score', 'name')
    answers = WhoThatAnswer.objects.filter(quiz=quiz).select_related('question', 'participant').order_by('submitted_at')
    
    # Calculate quiz statistics
    quiz_stats = {
        'total_participants': participants.count(),
        'total_questions': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_score': 0,
        'average_accuracy': 0,
        'correct_answers': 0,
    }
    
    if participants.exists():
        total_scores = [p.total_score for p in participants]
        quiz_stats['average_score'] = sum(total_scores) / len(total_scores)
    
    if answers.exists():
        total_accuracy = sum(answer.get_accuracy_percentage() for answer in answers)
        quiz_stats['average_accuracy'] = total_accuracy / answers.count()
        quiz_stats['correct_answers'] = answers.filter(is_correct=True).count()
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'answers': answers,
        'quiz_stats': quiz_stats,
    }
    
    return render(request, 'admin_dashboard/who_that_game_details.html', context)


# API endpoints for real-time data
@admin_required
def api_who_that_quiz_stats(request, room_code):
    """Get real-time who is that quiz statistics"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all()
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': WhoThatAnswer.objects.filter(quiz=quiz).count(),
            'current_question_responses': 0,
            'average_score': 0,
            'average_accuracy': 0,
            'correct_answers': 0,
        }
        
        if participants.exists():
            total_scores = [p.total_score for p in participants]
            stats['average_score'] = sum(total_scores) / len(total_scores)
            
        # Get all answers for this quiz to calculate accuracy
        all_answers = WhoThatAnswer.objects.filter(quiz=quiz)
        if all_answers.exists():
            total_accuracy = sum(answer.get_accuracy_percentage() for answer in all_answers)
            stats['average_accuracy'] = total_accuracy / all_answers.count()
            stats['correct_answers'] = all_answers.filter(is_correct=True).count()
        
        # Current question stats
        if quiz.current_question:
            current_answers = WhoThatAnswer.objects.filter(
                quiz=quiz, 
                question=quiz.current_question
            )
            stats['current_question_responses'] = current_answers.count()
            if current_answers.exists():
                current_accuracy = sum(answer.get_accuracy_percentage() for answer in current_answers)
                stats['current_question_avg_accuracy'] = current_accuracy / current_answers.count()
                stats['current_question_correct'] = current_answers.filter(is_correct=True).count()
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_who_that_participants(request, room_code):
    """Get current who is that participants list"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all().order_by('-total_score', 'name')
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score,
                'questions_answered': participant.questions_answered,
                'correct_answers': participant.correct_answers,
                'is_active': participant.is_active,
                'rank': participant.get_rank(),
                'average_accuracy': participant.get_average_accuracy(),
                'accuracy_percentage': participant.get_accuracy_percentage(),
                'joined_at': participant.joined_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_who_that_live_responses(request, room_code):
    """Get live responses for current who is that question"""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
        active_hub_session_code = _extract_hub_session_code(request) or _get_active_hub_session_code_for_room('who_that', room_code)
        quiz_session = WhoThatSession.objects.filter(quiz=quiz).first()
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        def serialize_who_that_response(response):
            is_manual_override = response.is_correct and not response.question.check_answer(response.user_answer)
            return {
                'answer_id': response.id,
                'game_id': response.quiz_id,
                'question_id': response.question_id,
                'participant_id': response.participant_id,
                'participant_name': response.participant.name,
                'hub_session_code': response.participant.hub_session_code,
                'user_answer': response.user_answer,
                'formatted_answer': response.user_answer,
                'points_earned': 1 if response.is_correct else 0,
                'is_correct': response.is_correct,
                'is_manual_override': is_manual_override,
                'can_mark_correct': not response.is_correct,
                'accuracy_percentage': response.get_accuracy_percentage(),
                'match_quality': 'Manual override' if is_manual_override else response.get_match_quality(),
                'time_taken': response.time_taken,
                'submitted_at': response.submitted_at.isoformat(),
            }

        review_question = None
        raw_review_question_id = request.GET.get('review_question')
        if raw_review_question_id and not (quiz.current_question_id and quiz_session and quiz_session.is_question_active):
            try:
                review_question_id = int(raw_review_question_id)
            except (TypeError, ValueError):
                review_question_id = None
            if review_question_id:
                if quiz.selected_questions.filter(id=review_question_id).exists():
                    review_question = quiz.selected_questions.filter(id=review_question_id).first()
                else:
                    review_question = WhoThatQuestion.objects.filter(
                        id=review_question_id,
                        created_by=quiz.creator,
                    ).first()

        response_question = None
        if quiz.status == 'active' and quiz.current_question and (not quiz_session or quiz_session.is_question_active):
            response_question = quiz.current_question
        elif quiz.status in ('active', 'inactive') and review_question is not None:
            response_question = review_question

        if response_question is None:
            return JsonResponse({
                'success': True,
                'responses': []
            })
        
        responses = WhoThatAnswer.objects.filter(
            quiz=quiz,
            question=response_question
        ).select_related('participant', 'question')
        if quiz.started_at:
            responses = responses.filter(submitted_at__gte=quiz.started_at)
        if active_hub_session_code:
            responses = responses.filter(participant__hub_session_code=active_hub_session_code)
        responses = responses.order_by('-submitted_at')[:20]
        
        responses_data = [serialize_who_that_response(response) for response in responses]
        
        return JsonResponse({
            'success': True,
            'responses': responses_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def promote_who_that_answer_correct(request, room_code):
    """Allow the host to manually promote a who-is-that answer to correct."""
    try:
        quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

        data = json.loads(request.body or '{}')
        answer_id = data.get('answer_id')
        if not answer_id:
            return JsonResponse({'success': False, 'error': 'answer_id is required'}, status=400)

        with transaction.atomic():
            answer = get_object_or_404(
                WhoThatAnswer.objects.select_for_update().select_related('participant', 'question'),
                id=answer_id,
                quiz=quiz,
            )

            if answer.is_correct:
                return JsonResponse({
                    'success': False,
                    'error': 'This answer is already marked correct.',
                }, status=400)

            answer.is_correct = True
            answer.points_earned = 1
            answer.save()
            answer.participant.refresh_from_db(fields=['total_score'])

        channel_layer = get_channel_layer()
        if channel_layer is not None:
            async_to_sync(channel_layer.group_send)(
                f'who_that_{quiz.room_code}',
                {
                    'type': 'answer_corrected',
                    'participant_id': answer.participant_id,
                    'participant_name': answer.participant.name,
                    'question_id': answer.question_id,
                    'is_correct': True,
                    'points_earned': answer.points_earned,
                    'match_quality': 'Manual override',
                    'total_score': answer.participant.total_score,
                }
            )
        return JsonResponse({
            'success': True,
            'answer_id': answer.id,
            'question_id': answer.question_id,
            'participant_id': answer.participant_id,
            'participant_name': answer.participant.name,
            'user_answer': answer.user_answer,
            'formatted_answer': answer.user_answer,
            'is_correct': True,
            'is_manual_override': True,
            'can_mark_correct': False,
            'points_earned': answer.points_earned,
            'total_score': answer.participant.total_score,
            'accuracy_percentage': answer.get_accuracy_percentage(),
            'match_quality': 'Manual override',
            'time_taken': answer.time_taken,
            'submitted_at': answer.submitted_at.isoformat(),
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def api_who_that_stats(request):
    """Get who is that game statistics for dashboard"""
    try:
        # Basic stats
        total_questions = WhoThatQuestion.objects.filter(is_active=True).count()
        total_games = WhoThatQuiz.objects.filter(status='completed').count()
        total_players = WhoThatParticipant.objects.values('name').distinct().count()
        
        # Recent activity
        recent_games = WhoThatQuiz.objects.filter(status='completed').order_by('-ended_at')[:5]
        
        # Top scores
        top_scores = WhoThatParticipant.objects.filter(quiz__status='completed').order_by('-total_score')[:5]
        
        # Average stats
        all_answers = WhoThatAnswer.objects.all()
        avg_accuracy = 0
        if all_answers.exists():
            total_accuracy = sum(answer.get_accuracy_percentage() for answer in all_answers)
            avg_accuracy = total_accuracy / all_answers.count()
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_questions': total_questions,
                'total_games': total_games,
                'total_players': total_players,
                'recent_games': [
                    {
                        'id': game.id,
                        'title': game.title,
                        'room_code': game.room_code,
                        'participant_count': game.get_participant_count(),
                        'ended_at': game.ended_at.isoformat() if game.ended_at else None,
                    }
                    for game in recent_games
                ],
                'top_scores': [
                    {
                        'participant_name': participant.name,
                        'quiz_room_code': participant.quiz.room_code,
                        'total_score': participant.total_score,
                        'accuracy_percentage': participant.get_accuracy_percentage(),
                    }
                    for participant in top_scores
                ],
                'average_accuracy': round(avg_accuracy, 1),
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
def api_who_that_questions(request):
    """Get paginated list of who is that questions"""
    try:
        page = int(request.GET.get('page', 1))
        per_page = 20
        
        questions = WhoThatQuestion.objects.filter(created_by=request.user)

        questions = questions.order_by('-created_at')
        
        # Paginate
        start = (page - 1) * per_page
        end = start + per_page
        paginated_questions = questions[start:end]
        
        return JsonResponse({
            'success': True,
            'questions': [
                {
                    'id': q.id,
                    'question_text': q.question_text,
                    'correct_answer': q.correct_answer,
                    'alternative_answers': q.alternative_answers,
                    'category': q.category or 'General',
                    'points': 1,
                    'time_limit': q.time_limit,
                    'has_image': bool(q.image),
                    'is_active': q.is_active,
                    'created_at': q.created_at.isoformat(),
                    'used_count': WhoThatAnswer.objects.filter(question=q).count(),
                }
                for q in paginated_questions
            ],
            'total_count': questions.count(),
            'has_next': end < questions.count(),
            'page': page,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)
    

from black_jack_quiz.models import BlackJackQuiz, BlackJackQuestion, BlackJackParticipant, BlackJackAnswer, BlackJackSession

@admin_required
def blackjack_management(request):
    """BlackJack quiz management page"""
    # Get recent questions
    questions = BlackJackQuestion.objects.filter(created_by=request.user).order_by('-created_at')[:20]
    
    # Get recent quizzes
    quizzes = BlackJackQuiz.objects.all().order_by('-created_at')
    
    # Get game statistics
    total_questions = BlackJackQuestion.objects.filter(is_active=True).count()
    user_questions = BlackJackQuestion.objects.filter(created_by=request.user, is_active=True).count()
    
    # Get recent game sessions
    recent_games = BlackJackQuiz.objects.filter(status='completed').order_by('-ended_at')[:10]
    
    # Get player statistics
    total_games = BlackJackQuiz.objects.filter(status='completed').count()
    total_players = BlackJackParticipant.objects.values('name').distinct().count()
    
    # Calculate bust rate
    total_participants = BlackJackParticipant.objects.count()
    busted_participants = BlackJackParticipant.objects.filter(is_busted=True).count()
    bust_rate = (busted_participants / total_participants * 100) if total_participants > 0 else 0

    context = {
        'questions': questions,
        'quizzes': quizzes,
        'total_questions': total_questions,
        'user_questions': user_questions,
        'recent_games': recent_games,
        'total_games': total_games,
        'total_players': total_players,
        'bust_rate': round(bust_rate, 1),
        'bundles': BlackJackBundle.objects.filter(creator=request.user).prefetch_related('questions'),
    }

    return render(request, 'admin_dashboard/blackjack_management.html', context)


@admin_required
@require_POST
def create_blackjack_quiz(request):
    """Create a new BlackJack quiz via AJAX"""
    try:
        data = {}
        if request.body:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                data = {}
        total_questions = max(1, int(data.get('total_questions', 5) or 5))
        scoring_mode = (data.get('scoring_mode') or 'simple').strip()
        quiz = BlackJackQuiz.objects.create(
            title="BlackJack Quiz",
            creator=request.user,
            status='waiting',
            total_questions=total_questions,
            scoring_mode='rank' if scoring_mode == 'rank' else 'simple',
        )
        
        # Create associated quiz session
        BlackJackSession.objects.create(quiz=quiz)
        
        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)

@admin_required
@require_POST
def create_black_jack_custom_quiz(request):
    """Create a new BlackJack quiz with a name and selected question IDs via AJAX"""
    try:
        data = json.loads(request.body)
        title = (data.get('title') or '').strip() or 'Custom Quiz'
        question_ids = data.get('question_ids') or []
        total_questions = max(1, int(data.get('total_questions', 5) or 5))
        scoring_mode = (data.get('scoring_mode') or 'simple').strip()
        set_sizes = data.get('set_sizes')
        tutorial_enabled, tutorial_title, tutorial_text = _normalize_tutorial_payload(data)
        raw_question_ids = []
        for raw_id in question_ids:
            try:
                raw_question_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        active_questions = BlackJackQuestion.objects.filter(id__in=raw_question_ids, is_active=True)
        available_question_ids = set(active_questions.values_list('id', flat=True))
        ordered_question_ids = []
        for question_id in raw_question_ids:
            if question_id in available_question_ids and question_id not in ordered_question_ids:
                ordered_question_ids.append(question_id)
        explicit_sets = BlackJackQuiz.build_explicit_question_sets(
            ordered_question_ids,
            default_set_size=total_questions,
            set_sizes=set_sizes,
        )

        quiz = BlackJackQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            tutorial_enabled=tutorial_enabled,
            tutorial_title=tutorial_title,
            tutorial_text=tutorial_text,
            question_order=explicit_sets if explicit_sets else ordered_question_ids,
            creator=request.user,
            status='waiting',
            total_questions=total_questions,
            scoring_mode='rank' if scoring_mode == 'rank' else 'simple',
        )

        # Attach selected questions (only active ones the user can access)
        if ordered_question_ids:
            quiz.selected_questions.set(active_questions)
        _apply_blackjack_tutorial_set_selection(quiz, data)

        # Create session
        BlackJackSession.objects.create(quiz=quiz)

        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
def blackjack_monitor(request, room_code):
    """Real-time BlackJack quiz monitoring page"""
    hub_session = request.GET.get('hub_session') or _get_active_hub_session_code_for_room('blackjack', room_code)
    quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
    quiz.ensure_runtime_scoped_to_hub_session(hub_session)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:blackjack_management')
    
    participants = quiz.get_ordered_participants(session_code=hub_session)

    # Get or create quiz session
    quiz_session, created = BlackJackSession.objects.get_or_create(quiz=quiz)
    selected_set_number = quiz_session.get_normalized_selected_set_number(active_only=True)
    configured_question_ids = quiz.get_remaining_question_ids_for_next_turn(
        set_number=selected_set_number,
        active_only=True,
    )
    if quiz.has_configured_question_pool():
        available_questions_by_id = BlackJackQuestion.objects.in_bulk(configured_question_ids)
        available_questions = [
            available_questions_by_id[question_id]
            for question_id in configured_question_ids
            if question_id in available_questions_by_id
        ]
    else:
        available_questions = list(
            BlackJackQuestion.objects.filter(created_by=request.user, is_active=True).order_by('-created_at')
        )

    if quiz.current_question_id:
        current_set_number = quiz.get_set_number_for_question_id(quiz.current_question_id, active_only=False)
        current_question_in_set = quiz.get_current_question_position_in_set()
        current_set_question_count = quiz.get_set_question_count(question_id=quiz.current_question_id)
    else:
        current_set_number = selected_set_number
        current_set_question_count = quiz.get_set_question_count(set_number=selected_set_number)
        sent_in_selected_set = len(quiz_session.get_sent_question_ids_for_set(selected_set_number, active_only=True))
        current_question_in_set = min(
            sent_in_selected_set + 1,
            max(1, current_set_question_count),
        )

    next_question_in_set = current_question_in_set
    next_set_number = selected_set_number
    next_set_question_count = quiz.get_set_question_count(set_number=selected_set_number)
    total_game_questions = len(quiz.get_configured_question_ids(active_only=True)) if quiz.has_configured_question_pool() else quiz.get_total_game_questions()
    sent_question_count = 0 if quiz_session.is_waiting_fresh_start_state() else max(
        int(quiz_session.total_questions_sent or 0),
        len(quiz_session.get_asked_question_ids()),
    )
    quiz_complete = total_game_questions > 0 and sent_question_count >= total_game_questions
    explicit_question_sets = quiz.get_explicit_question_sets()
    all_set_question_ids = [
        question_id
        for question_set in explicit_question_sets
        for question_id in question_set
    ]
    set_questions_by_id = BlackJackQuestion.objects.in_bulk(all_set_question_ids)

    set_overview = []
    for set_index, question_set_ids in enumerate(explicit_question_sets, start=1):
        questions = [
            set_questions_by_id[question_id]
            for question_id in question_set_ids
            if question_id in set_questions_by_id
        ]
        set_overview.append({
            'number': set_index,
            'question_count': len(questions),
            'questions': questions,
            'is_completed': quiz_session.is_set_complete(set_index, active_only=True),
            'is_selected': set_index == selected_set_number,
            'is_active': quiz.current_question_id and current_set_number == set_index,
            'remaining_question_count': len(
                quiz_session.get_remaining_question_ids_for_set(set_index, active_only=True)
            ),
        })
    tutorial_state = get_unit_tutorial_state('blackjack', quiz.room_code, hub_session)
    question_runtime = current_snapshot('blackjack', quiz.room_code, hub_session)
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': len(participants),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'question_runtime': question_runtime,
        'lobby_url': _get_lobby_url(request, room_code),
        'can_change_scoring_mode': quiz.can_change_scoring_mode(),
        'total_game_questions': total_game_questions,
        'quiz_complete': quiz_complete,
        'current_question_in_set': current_question_in_set,
        'next_question_in_set': next_question_in_set,
        'current_set_number': current_set_number,
        'next_set_number': next_set_number,
        'total_sets': quiz.get_total_sets(),
        'current_set_question_count': current_set_question_count,
        'next_set_question_count': next_set_question_count,
        'participant_progress_question_count': current_set_question_count if quiz.current_question else next_set_question_count,
        'selected_set_number': selected_set_number,
        'selected_set_has_remaining_questions': bool(configured_question_ids),
        'blackjack_set_overview': set_overview,
        'current_unit_is_tutorial': bool(
            tutorial_state.get('current_unit_is_tutorial')
            and current_set_number == quiz.tutorial_set_number
        ),
    }
    return render(request, 'admin_dashboard/blackjack_monitor.html', context)


@admin_required
@require_POST
def select_blackjack_set(request, room_code):
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)

        quiz_session, created = BlackJackSession.objects.get_or_create(quiz=quiz)
        if quiz.current_question_id:
            return JsonResponse({
                'success': False,
                'error': 'The current question is still active. End it before switching sets.'
            }, status=400)

        data = json.loads(request.body or '{}')
        set_number = data.get('set_number')
        if quiz_session.is_waiting_fresh_start_state() and quiz_session.has_saved_progress_state():
            quiz_session.reset_for_new_run()
        quiz_session.set_selected_set_number(set_number)
        quiz_session.save(update_fields=['selected_set_number'])

        return JsonResponse({
            'success': True,
            'selected_set_number': quiz_session.selected_set_number,
            'remaining_question_count': len(
                quiz_session.get_remaining_question_ids_for_set(quiz_session.selected_set_number, active_only=True)
            ),
        })
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_blackjack_scoring_mode(request, room_code):
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)

        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to configure this quiz.'
            }, status=403)

        if not quiz.can_change_scoring_mode():
            return JsonResponse({
                'success': False,
                'error': 'The scoring mode can only be changed before the first question is sent.'
            }, status=400)

        data = json.loads(request.body or '{}')
        scoring_mode = (data.get('scoring_mode') or 'simple').strip()
        if scoring_mode not in ('simple', 'rank'):
            return JsonResponse({
                'success': False,
                'error': 'Invalid scoring mode.'
            }, status=400)

        quiz.scoring_mode = scoring_mode
        quiz.save(update_fields=['scoring_mode'])

        return JsonResponse({
            'success': True,
            'scoring_mode': quiz.scoring_mode,
            'can_change_scoring_mode': quiz.can_change_scoring_mode(),
        })
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data.'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def add_blackjack_question(request):
    """Add a new BlackJack question via AJAX"""
    try:
        data = json.loads(request.body)
        
        question_text = data.get('question_text', '').strip()
        correct_answer = int(data.get('correct_answer'))
        time_limit = int(data.get('time_limit', 30))
        explanation = data.get('explanation', '').strip()

        # Validate required fields
        if not question_text:
            return JsonResponse({
                'success': False,
                'error': 'Question text is required.'
            }, status=400)

        # Validate time limit
        if not (10 <= time_limit <= 120):
            return JsonResponse({
                'success': False,
                'error': 'Time limit must be between 10 and 120 seconds.'
            }, status=400)

        # Create question
        question = BlackJackQuestion.objects.create(
            question_text=question_text,
            correct_answer=correct_answer,
            time_limit=time_limit,
            explanation=explanation if explanation else None,
            created_by=request.user
        )
        
        return JsonResponse({
            'success': True,
            'question_id': question.id,
            'message': 'Question added successfully!'
        })
        
    except ValueError as e:
        return JsonResponse({
            'success': False,
            'error': 'Invalid numeric values provided.'
        }, status=400)
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
@require_POST
def delete_blackjack_question(request):
    """Delete a BlackJack question via AJAX"""
    try:
        data = json.loads(request.body)
        question_id = data.get('question_id')
        
        question = get_object_or_404(BlackJackQuestion, id=question_id, created_by=request.user)
        
        # Check if question has been used in games
        if BlackJackAnswer.objects.filter(question=question).exists():
            # Soft delete - just deactivate
            question.is_active = False
            question.save()
            message = 'Question deactivated (it has been used in games).'
        else:
            # Hard delete
            question.delete()
            message = 'Question deleted successfully.'
        
        return JsonResponse({
            'success': True,
            'message': message
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data.'
        }, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
@require_POST
def start_blackjack_quiz(request, room_code):
    """Start a BlackJack quiz"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to start this quiz.'
            }, status=403)

        guard_response = _guard_session_game_start(request, 'blackjack', room_code)
        if guard_response:
            return guard_response
        
        quiz.start_quiz()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_blackjack_quiz(request):
    """End a BlackJack quiz via AJAX"""
    try:
        data = json.loads(request.body)
        quiz_id = data.get('quiz_id')
        
        quiz = get_object_or_404(BlackJackQuiz, id=quiz_id)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_blackjack_quiz_by_room_code(request, room_code):
    """End a BlackJack quiz by room code"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to end this quiz.'
            }, status=403)
        
        quiz.end_quiz('completed')
        
        # End current question if active
        if hasattr(quiz, 'session'):
            quiz.session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def send_blackjack_question(request, room_code):
    """Send a question to BlackJack quiz participants"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to send questions to this quiz.'
            }, status=403)
        
        data = json.loads(request.body)
        question_id = data.get('question_id')
        selected_set_number = data.get('selected_set_number')
        
        question = get_object_or_404(BlackJackQuestion, id=question_id, is_active=True)

        send_error = quiz.get_next_question_send_error(
            question.id,
            selected_set_number=selected_set_number,
        )
        if send_error:
            return JsonResponse({
                'success': False,
                'error': send_error
            }, status=400)
        
        # Get or create quiz session
        quiz_session, created = BlackJackSession.objects.get_or_create(quiz=quiz)
        
        # Send the question
        quiz_session.send_question(question)
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
@require_POST
def end_blackjack_question(request, room_code):
    """End the current BlackJack question"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({
                'success': False,
                'error': 'You are not authorized to control this quiz.'
            }, status=403)
        
        # Get quiz session
        quiz_session = get_object_or_404(BlackJackSession, quiz=quiz)
        
        # End current question
        quiz_session.end_current_question()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def blackjack_game_details(request, quiz_id):
    """View detailed results of a specific BlackJack quiz"""
    quiz = get_object_or_404(BlackJackQuiz, id=quiz_id)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:blackjack_management')
    
    participants = quiz.get_ordered_participants()
    answers = BlackJackAnswer.objects.filter(quiz=quiz).select_related('question', 'participant').order_by('question_number', 'submitted_at')
    
    # Calculate quiz statistics
    quiz_stats = {
        'total_participants': len(participants),
        'total_questions': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_points': 0,
        'busted_participants': sum(1 for participant in participants if participant.is_busted),
        'blackjack_participants': sum(1 for participant in participants if participant.total_points == 21),
        'perfect_scores': answers.filter(points_earned=0).count(),
    }
    
    if participants:
        total_points = [p.total_points for p in participants]
        quiz_stats['average_points'] = sum(total_points) / len(total_points)
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'answers': answers,
        'quiz_stats': quiz_stats,
    }
    
    return render(request, 'admin_dashboard/blackjack_game_details.html', context)


# API endpoints for real-time data
@admin_required
def api_blackjack_quiz_stats(request, room_code):
    """Get real-time BlackJack quiz statistics"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        hub_session = request.GET.get('hub_session') or _get_active_hub_session_code_for_room('blackjack', room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all()
        if hub_session:
            participants = participants.filter(hub_session_code=hub_session)
        
        active_participants = participants.filter(is_active=True)
        current_answers = BlackJackAnswer.objects.none()
        if quiz.current_question:
            current_answers = quiz.get_current_run_answers().filter(
                question=quiz.current_question,
                participant__in=active_participants,
            )
            current_answered_participant_ids = current_answers.values_list('participant_id', flat=True).distinct()
            active_participants = active_participants.filter(
                Q(is_busted=False) | Q(id__in=current_answered_participant_ids)
            )
            current_answers = current_answers.filter(participant__in=active_participants)

        stats = {
            'participant_count': participants.count(),
            'active_participants': active_participants.count(),
            'total_answers': quiz.get_current_run_answers().filter(participant__in=participants).count(),
            'current_question_responses': 0,
            'average_points': 0,
            'busted_count': participants.filter(is_busted=True).count(),
            'blackjack_count': participants.filter(total_points=21).count(),
            'current_question_number': quiz.current_question_number,
        }
        
        if participants.exists():
            total_points = [p.total_points for p in participants]
            stats['average_points'] = sum(total_points) / len(total_points)
        
        # Current question stats
        if quiz.current_question:
            stats['current_question_responses'] = current_answers.values('participant_id').distinct().count()
            if current_answers.exists():
                avg_points = sum(answer.points_earned for answer in current_answers) / current_answers.count()
                stats['current_question_avg_points'] = avg_points
        
        return JsonResponse({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_blackjack_participants(request, room_code):
    """Get current BlackJack participants list"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.get_ordered_participants(active_only=True)
        
        participants_data = []
        for participant in participants:
            participants_data.append({
                'id': participant.id,
                'name': participant.name,
                'total_points': participant.total_points,
                'questions_answered': participant.questions_answered,
                'is_active': participant.is_active,
                'is_busted': participant.is_busted,
                'rank': participant.get_rank(),
                'status': participant.get_status(),
                'distance_from_21': participant.get_distance_from_21(),
                'joined_at': participant.joined_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'participants': participants_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_blackjack_live_responses(request, room_code):
    """Get live responses for current BlackJack question"""
    try:
        quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        if not quiz.current_question:
            return JsonResponse({
                'success': True,
                'responses': []
            })
        
        responses = BlackJackAnswer.objects.filter(
            quiz=quiz,
            question=quiz.current_question
        ).select_related('participant').order_by('-submitted_at')[:20]
        
        responses_data = []
        for response in responses:
            responses_data.append({
                'participant_name': response.participant.name,
                'user_answer': response.user_answer,
                'points_earned': response.points_earned,
                'difference': response.get_difference(),
                'difference_direction': response.get_difference_direction(),
                'total_points': response.participant.total_points,
                'is_busted': response.participant.is_busted,
                'status': response.participant.get_status(),
                'time_taken': response.time_taken,
                'question_number': response.question_number,
                'submitted_at': response.submitted_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'responses': responses_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@admin_required
def api_blackjack_stats(request):
    """Get BlackJack game statistics for dashboard"""
    try:
        # Basic stats
        total_questions = BlackJackQuestion.objects.filter(is_active=True).count()
        total_games = BlackJackQuiz.objects.filter(status='completed').count()
        total_players = BlackJackParticipant.objects.values('name').distinct().count()
        
        # Recent activity
        recent_games = BlackJackQuiz.objects.filter(status='completed').order_by('-ended_at')[:5]
        
        # Top scores (non-busted players closest to 21)
        top_scores = BlackJackParticipant.objects.filter(
            quiz__status='completed',
            is_busted=False
        ).order_by('final_score')[:5]
        
        # Calculate bust rate
        total_participants = BlackJackParticipant.objects.count()
        busted_participants = BlackJackParticipant.objects.filter(is_busted=True).count()
        bust_rate = (busted_participants / total_participants * 100) if total_participants > 0 else 0
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_questions': total_questions,
                'total_games': total_games,
                'total_players': total_players,
                'recent_games': [
                    {
                        'id': game.id,
                        'title': game.title,
                        'room_code': game.room_code,
                        'participant_count': game.get_participant_count(),
                        'ended_at': game.ended_at.isoformat() if game.ended_at else None,
                    }
                    for game in recent_games
                ],
                'top_scores': [
                    {
                        'participant_name': participant.name,
                        'quiz_room_code': participant.quiz.room_code,
                        'total_points': participant.total_points,
                        'distance_from_21': participant.get_distance_from_21(),
                        'status': participant.get_status(),
                    }
                    for participant in top_scores
                ],
                'bust_rate': round(bust_rate, 1),
                'blackjack_rate': round(
                    (BlackJackParticipant.objects.filter(total_points=21).count() / 
                     total_participants * 100) if total_participants > 0 else 0, 1
                ),
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@admin_required
def api_blackjack_questions(request):
    """Get paginated list of BlackJack questions"""
    try:
        page = int(request.GET.get('page', 1))
        per_page = 20

        questions = BlackJackQuestion.objects.filter(created_by=request.user).order_by('-created_at')

        # Paginate
        start = (page - 1) * per_page
        end = start + per_page
        paginated_questions = questions[start:end]

        return JsonResponse({
            'success': True,
            'questions': [
                {
                    'id': q.id,
                    'question_text': q.question_text,
                    'correct_answer': q.correct_answer,
                    'time_limit': q.time_limit,
                    'is_active': q.is_active,
                    'created_at': q.created_at.isoformat(),
                    'used_count': BlackJackAnswer.objects.filter(question=q).count(),
                }
                for q in paginated_questions
            ],
            'total_count': questions.count(),
            'has_next': end < questions.count(),
            'page': page,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

def get_quiz_questions(request):
    # Get page number from request
    page_number = request.GET.get('page', 1)
    
    # Get all active questions
    questions = QuizQuestion.objects.filter(is_active=True).order_by('-created_at')
    
    # Add search functionality
    search_query = request.GET.get('search', '')
    if search_query:
        questions = questions.filter(
            Q(question_text__icontains=search_query) |
            Q(question_type__icontains=search_query) |
            Q(time_limit__icontains=search_query)
        )
    
    # Paginate results (10 per page)
    paginator = Paginator(questions, 20)
    page_obj = paginator.get_page(page_number)
    
    # Prepare response data
    questions_data = []
    for question in page_obj:
        questions_data.append({
            'id': question.id,
            'question_text': question.question_text,
            'question_type': question.get_effective_question_type(),
            'time_limit': question.time_limit,
            'created_at': question.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': question.is_active,
        })
    
    return JsonResponse({
        'questions': questions_data,
        'count': paginator.count,
        'pages': paginator.num_pages,
        'current_page': page_obj.number,
    })

def get_estimation_questions(request):
    """Fetch estimation questions with pagination and search"""
    try:
        from Estimation.models import EstimationQuestion
        
        # Get page number from request
        page_number = request.GET.get('page', 1)
        
        # Get all active questions
        questions = EstimationQuestion.objects.filter(is_active=True).order_by('-created_at')
        
        # Add search functionality
        search_query = request.GET.get('search', '')
        if search_query:
            questions = questions.filter(
                Q(question_text__icontains=search_query) |
                Q(unit__icontains=search_query) |
                Q(correct_answer__icontains=search_query)
            )
        
        # Paginate results (10 per page)
        paginator = Paginator(questions, 20)
        page_obj = paginator.get_page(page_number)
        
        # Prepare response data
        questions_data = []
        for question in page_obj:
            questions_data.append({
                'id': question.id,
                'question_text': question.question_text,
                'correct_answer': question.correct_answer,
                'unit': question.unit,
                'unit_display': question.get_unit_display_text(),
                'max_points': question.max_points,
                'use_manual_points': question.use_manual_points,
                'tolerance_percentage': question.tolerance_percentage,
                'zone_count': question.zone_count,
                'points_display': question.get_zone_max_points(),
                'time_limit': "n/a", # if question.time_limit is None else question.time_limit,
                'created_at': question.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'is_active': question.is_active,
            })
        
        return JsonResponse({
            'questions': questions_data,
            'count': paginator.count,
            'pages': paginator.num_pages,
            'current_page': page_obj.number,
        })
        
    except Exception as e:
        print(e)
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

def get_assign_questions(request):
    """Fetch assign questions with pagination and search"""
    try:
        from Assign.models import AssignQuestion
        
        # Get page number from request
        page_number = request.GET.get('page', 1)
        
        # Get all active questions
        questions = AssignQuestion.objects.filter(is_active=True).order_by('-created_at')
        
        # Add search functionality
        search_query = request.GET.get('search', '')
        if search_query:
            questions = questions.filter(
                Q(question_text__icontains=search_query) |
                Q(time_limit__icontains=search_query)
            )
        
        # Paginate results (10 per page)
        paginator = Paginator(questions, 20)
        page_obj = paginator.get_page(page_number)
        
        # Prepare response data
        questions_data = []
        for question in page_obj:
            questions_data.append({
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': question.time_limit,
                'left_items_count': len(question.left_items) if question.left_items else 0,
                'right_items_count': len(question.right_items) if question.right_items else 0,
                'created_at': question.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            })
        
        return JsonResponse({
            'questions': questions_data,
            'count': paginator.count,
            'pages': paginator.num_pages,
            'current_page': page_obj.number,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

def get_who_questions(request):
    """Fetch who is lying questions with pagination and search"""
    try:
        from who_is_lying.models import WhoQuestion
        
        # Get page number from request
        page_number = request.GET.get('page', 1)
        
        # Get all active questions
        questions = WhoQuestion.objects.filter(is_active=True).order_by('-created_at')
        
        # Add search functionality
        search_query = request.GET.get('search', '')
        if search_query:
            questions = questions.filter(
                Q(statement__icontains=search_query) |
                Q(points__icontains=search_query) |
                Q(time_limit__icontains=search_query)
            )
        
        # Paginate results (10 per page)
        paginator = Paginator(questions, 20)
        page_obj = paginator.get_page(page_number)
        
        # Prepare response data
        questions_data = []
        for question in page_obj:
            questions_data.append({
                'id': question.id,
                'statement': question.statement,
                'points': question.points,
                'time_limit': question.time_limit,
                'people_count': len(question.people) if question.people else 0,
                'created_at': question.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'is_active': question.is_active,
            })
        
        return JsonResponse({
            'questions': questions_data,
            'count': paginator.count,
            'pages': paginator.num_pages,
            'current_page': page_obj.number,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

def get_where_questions(request):
    """Fetch where is this questions with pagination and search"""
    try:
        from where_is_this.models import WhereQuestion
        
        # Get page number from request
        page_number = request.GET.get('page', 1)
        
        # Get all active questions
        questions = WhereQuestion.objects.filter(is_active=True).order_by('-id')
        
        # Add search functionality
        search_query = request.GET.get('search', '')
        if search_query:
            questions = questions.filter(
                Q(question_text__icontains=search_query) |
                Q(points__icontains=search_query) |
                Q(time_limit__icontains=search_query)
            )
        
        # Paginate results (10 per page)
        paginator = Paginator(questions, 20)
        page_obj = paginator.get_page(page_number)
        
        # Prepare response data
        questions_data = []
        for question in page_obj:
            questions_data.append({
                'id': question.id,
                'question_text': question.question_text,
                'points': question.points,
                'time_limit': question.time_limit,
                'has_image': bool(question.image),
                'correct_location': f"({question.correct_latitude}, {question.correct_longitude})",
                'map_type': question.map_type,
                'distance_zones': question.get_zone_reveal_data()['zones'],
                'is_active': question.is_active,
                'created_at': question.created_at.strftime('%Y-%m-%d %H:%M:%S') if question.created_at else None,
            })
        
        return JsonResponse({
            'questions': questions_data,
            'count': paginator.count,
            'pages': paginator.num_pages,
            'current_page': page_obj.number,
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

def get_black_jack_questions(request):
    """Fetch black jack questions with pagination and search"""
    try:
        from black_jack_quiz.models import BlackJackQuestion
        
        questions = BlackJackQuestion.objects.filter(is_active=True).order_by('-created_at')

        # Search functionality
        search = request.GET.get('search', '')
        if search:
            questions = questions.filter(
                Q(question_text__icontains=search) |
                Q(correct_answer__icontains=search)
            )

        # Pagination
        paginator = Paginator(questions, 20)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)

        questions_data = []
        for question in page_obj:
            questions_data.append({
                'id': question.id,
                'question_text': question.question_text,
                'correct_answer': question.correct_answer,
                'time_limit': question.time_limit,
                'created_at': question.created_at.strftime('%Y-%m-%d %H:%M'),
                'is_active': question.is_active,
            })
        
        return JsonResponse({
            'success': True,
            'questions': questions_data,
            'count': paginator.count,
            'pages': paginator.num_pages,
            'current_page': page_obj.number,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

def get_who_that_questions(request):
    """Fetch who is that questions with pagination and search"""
    try:
        from who_is_that.models import WhoThatQuestion
        questions = WhoThatQuestion.objects.filter(is_active=True).order_by('-created_at')
        
        # Search functionality
        search = request.GET.get('search', '')
        if search:
            questions = questions.filter(
                Q(question_text__icontains=search) |
                Q(correct_answer__icontains=search) |
                Q(category__icontains=search)
            )
        
        # Pagination
        paginator = Paginator(questions, 20)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)
        
        questions_data = []
        for question in page_obj:
            questions_data.append({
                'id': question.id,
                'question_text': question.question_text,
                'correct_answer': question.correct_answer,
                'points': 1,
                'time_limit': question.time_limit,
                'has_image': bool(question.image),
                'category': question.category,
                'is_active': question.is_active,
            })
        
        return JsonResponse({
            'success': True,
            'questions': questions_data,
            'count': paginator.count,
            'pages': paginator.num_pages,
            'current_page': page_obj.number,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ===========================
# Manual Score Adjustment
# ===========================

@admin_required
@require_POST
def set_quiz_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(QuizParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_estimation_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(EstimationParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_assign_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(AssignParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_where_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(WhereParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_who_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(WhoParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_who_that_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(WhoThatParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_blackjack_participant_score(request):
    """Setzt total_points manuell und berechnet final_score neu."""
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(BlackJackParticipant, id=data['participant_id'])
        new_points = int(data['score'])
        participant.total_points = new_points
        if new_points > 21:
            participant.is_busted = True
            participant.final_score = 999
        else:
            participant.is_busted = False
            participant.final_score = abs(21 - new_points)
        participant.save(update_fields=['total_points', 'is_busted', 'final_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_points})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_clue_rush_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(ClueRushParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def set_sorting_ladder_participant_score(request):
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(SortingLadderParticipant, id=data['participant_id'])
        participant.total_score = int(data['score'])
        participant.save(update_fields=['total_score'])
        return JsonResponse({'success': True, 'new_score': participant.total_score})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


# ── Bundle views (Sorting Ladder) ────────────────────────────────────────────

@admin_required
def get_sorting_bundles(request):
    bundles = SortingBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {'id': b.id, 'name': b.name, 'question_ids': list(b.questions.values_list('id', flat=True)), 'question_count': b.questions.count()}
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_sorting_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = SortingBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        bundle.questions.set(SortingQuestion.objects.filter(id__in=question_ids, is_active=True))
    return JsonResponse({'success': True, 'bundle': {'id': bundle.id, 'name': bundle.name, 'question_ids': list(bundle.questions.values_list('id', flat=True)), 'question_count': bundle.questions.count()}})


@admin_required
def delete_sorting_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        bundle = SortingBundle.objects.get(id=data.get('bundle_id'), creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except SortingBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


# ── Bundle views (Assign) ─────────────────────────────────────────────────────

@admin_required
def get_assign_bundles(request):
    bundles = AssignBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {'id': b.id, 'name': b.name, 'question_ids': list(b.questions.values_list('id', flat=True)), 'question_count': b.questions.count()}
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_assign_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = AssignBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        bundle.questions.set(AssignQuestion.objects.filter(id__in=question_ids, is_active=True))
    return JsonResponse({'success': True, 'bundle': {'id': bundle.id, 'name': bundle.name, 'question_ids': list(bundle.questions.values_list('id', flat=True)), 'question_count': bundle.questions.count()}})


@admin_required
def delete_assign_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        bundle = AssignBundle.objects.get(id=data.get('bundle_id'), creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except AssignBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


# ── Bundle views (Estimation) ─────────────────────────────────────────────────

@admin_required
def get_estimation_bundles(request):
    bundles = EstimationBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {'id': b.id, 'name': b.name, 'question_ids': list(b.questions.values_list('id', flat=True)), 'question_count': b.questions.count()}
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_estimation_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = EstimationBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        bundle.questions.set(EstimationQuestion.objects.filter(id__in=question_ids, is_active=True))
    return JsonResponse({'success': True, 'bundle': {'id': bundle.id, 'name': bundle.name, 'question_ids': list(bundle.questions.values_list('id', flat=True)), 'question_count': bundle.questions.count()}})


@admin_required
def delete_estimation_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        bundle = EstimationBundle.objects.get(id=data.get('bundle_id'), creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except EstimationBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


# ── Bundle views (Where Is This) ──────────────────────────────────────────────

@admin_required
def get_where_bundles(request):
    bundles = WhereBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {'id': b.id, 'name': b.name, 'question_ids': list(b.questions.values_list('id', flat=True)), 'question_count': b.questions.count()}
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_where_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = WhereBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        bundle.questions.set(WhereQuestion.objects.filter(id__in=question_ids, is_active=True))
    return JsonResponse({'success': True, 'bundle': {'id': bundle.id, 'name': bundle.name, 'question_ids': list(bundle.questions.values_list('id', flat=True)), 'question_count': bundle.questions.count()}})


@admin_required
def delete_where_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        bundle = WhereBundle.objects.get(id=data.get('bundle_id'), creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except WhereBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


# ── Bundle views (Who Is Lying) ───────────────────────────────────────────────

@admin_required
def get_who_bundles(request):
    bundles = WhoBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {'id': b.id, 'name': b.name, 'question_ids': list(b.questions.values_list('id', flat=True)), 'question_count': b.questions.count()}
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_who_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = WhoBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        bundle.questions.set(WhoQuestion.objects.filter(id__in=question_ids, is_active=True))
    return JsonResponse({'success': True, 'bundle': {'id': bundle.id, 'name': bundle.name, 'question_ids': list(bundle.questions.values_list('id', flat=True)), 'question_count': bundle.questions.count()}})


@admin_required
def delete_who_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        bundle = WhoBundle.objects.get(id=data.get('bundle_id'), creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except WhoBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


# ── Bundle views (Who Is That) ────────────────────────────────────────────────

@admin_required
def get_who_that_bundles(request):
    bundles = WhoThatBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {'id': b.id, 'name': b.name, 'question_ids': list(b.questions.values_list('id', flat=True)), 'question_count': b.questions.count()}
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_who_that_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = WhoThatBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        bundle.questions.set(WhoThatQuestion.objects.filter(id__in=question_ids, is_active=True))
    return JsonResponse({'success': True, 'bundle': {'id': bundle.id, 'name': bundle.name, 'question_ids': list(bundle.questions.values_list('id', flat=True)), 'question_count': bundle.questions.count()}})


@admin_required
def delete_who_that_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        bundle = WhoThatBundle.objects.get(id=data.get('bundle_id'), creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except WhoThatBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


# ── Bundle views (Black Jack) ─────────────────────────────────────────────────

@admin_required
def get_blackjack_bundles(request):
    bundles = BlackJackBundle.objects.filter(creator=request.user).prefetch_related('questions')
    data = [
        {'id': b.id, 'name': b.name, 'question_ids': list(b.questions.values_list('id', flat=True)), 'question_count': b.questions.count()}
        for b in bundles
    ]
    return JsonResponse({'bundles': data})


@admin_required
def create_blackjack_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    name = (data.get('name') or '').strip()
    question_ids = data.get('question_ids') or []
    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    bundle = BlackJackBundle.objects.create(name=name, creator=request.user)
    if question_ids:
        bundle.questions.set(BlackJackQuestion.objects.filter(id__in=question_ids, is_active=True))
    return JsonResponse({'success': True, 'bundle': {'id': bundle.id, 'name': bundle.name, 'question_ids': list(bundle.questions.values_list('id', flat=True)), 'question_count': bundle.questions.count()}})


@admin_required
def delete_blackjack_bundle(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body)
    try:
        bundle = BlackJackBundle.objects.get(id=data.get('bundle_id'), creator=request.user)
        bundle.delete()
        return JsonResponse({'success': True})
    except BlackJackBundle.DoesNotExist:
        return JsonResponse({'error': 'Bundle not found'}, status=404)


@admin_required
@require_POST
def add_question_to_quiz_from_bank(request):
    """Add a question from the question bank to a quiz's selected_questions."""
    try:
        data = json.loads(request.body)
        game_key = data.get('game_key')
        room_code = data.get('room_code')
        question_id = data.get('question_id')
        hub_session = (data.get('hub_session') or '').strip() or None

        if not game_key or not room_code or not question_id:
            return JsonResponse({'success': False, 'error': 'Missing parameters'}, status=400)

        config = {
            'quiz':           (Quiz,             QuizQuestion),
            'sorting_ladder': (SortingLadderGame, SortingQuestion),
            'assign':         (AssignQuiz,        AssignQuestion),
            'estimation':     (EstimationQuiz,    EstimationQuestion),
            'where':          (WhereQuiz,         WhereQuestion),
            'who':            (WhoQuiz,           WhoQuestion),
            'who_that':       (WhoThatQuiz,       WhoThatQuestion),
            'blackjack':      (BlackJackQuiz,     BlackJackQuestion),
            'clue_rush':      (ClueRushGame,      ClueQuestion),
        }
        if game_key not in config:
            return JsonResponse({'success': False, 'error': 'Unknown game type'}, status=400)

        quiz_model, question_model = config[game_key]
        quiz = get_object_or_404(quiz_model, room_code=room_code)
        question = get_object_or_404(question_model, id=question_id)
        quiz.selected_questions.add(question)
        response_payload = {'success': True, 'question_id': question.id}

        if game_key == 'assign':
            question_order = [int(item) for item in (quiz.question_order or [])]
            if int(question.id) not in question_order:
                question_order.append(int(question.id))
                quiz.question_order = question_order
                quiz.save(update_fields=['question_order', 'updated_at'])

            scoreboard_questions = build_question_scoreboard(quiz, None, hub_session)
            response_payload['scoreboard_questions'] = scoreboard_questions
            channel_layer = get_channel_layer()
            if channel_layer is not None:
                async_to_sync(channel_layer.group_send)(
                    f'assign_{room_code}',
                    {
                        'type': 'scoreboard_questions_updated',
                        'scoreboard_questions': scoreboard_questions,
                    },
                )

        return JsonResponse(response_payload)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
