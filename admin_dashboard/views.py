from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db.models import Count, Q, Avg
from django.utils import timezone
from django.contrib import messages
import json
from QuizGame.models import Quiz, QuizQuestion, QuizParticipant, QuizAnswer, QuizSession, QuizBundle
from sorting_ladder.models import SortingLadderGame, SortingLadderParticipant, SortingQuestion, SortingItem, SortingLadderSession, SortingBundle
from Assign.models import AssignQuiz, AssignQuestion, AssignParticipant, AssignBundle
from Estimation.models import EstimationQuiz, EstimationQuestion, EstimationParticipant, EstimationBundle
from where_is_this.models import WhereQuiz, WhereQuestion, WhereParticipant, WhereBundle
from black_jack_quiz.models import BlackJackQuiz, BlackJackQuestion, BlackJackParticipant, BlackJackBundle
from clue_rush.models import ClueRushGame, ClueRushParticipant, ClueQuestion, Clue, ClueAnswer, ClueRushSession
from who_is_that.models import WhoThatQuiz, WhoThatQuestion, WhoThatParticipant, WhoThatBundle
from who_is_lying.models import WhoQuiz, WhoQuestion, WhoParticipant, WhoBundle
from games_hub.models import HubSession, HubParticipant, HubGameStep
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
        HubSession.objects.filter(code=session_code).delete()
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

        # Generate a unique new code
        def _gen():
            return ''.join(random.choices(_string.ascii_uppercase + _string.digits, k=6))
        new_code = _gen()
        while HubSession.objects.filter(code=new_code).exists():
            new_code = _gen()

        new_session = HubSession.objects.create(
            code=new_code,
            name=original.name,
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
                        new_game.selected_questions.set(orig_game.selected_questions.all())
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
    if not is_admin(request.user):
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

        quiz = ClueRushGame.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
        )

        if question_ids:
            qs = ClueQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)

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
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        if isinstance(data.get('question_ids'), list):
            qs = ClueQuestion.objects.filter(id__in=data['question_ids'], is_active=True)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in data['question_ids']]
            quiz.save(update_fields=['question_order'])

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
    quiz = get_object_or_404(ClueRushGame, room_code=room_code)

    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:clue_rush_management')

    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('-total_score', 'name')
    # If the quiz has a predefined set of selected questions, show only those
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = ClueQuestion.objects.filter(created_by=request.user).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = ClueRushSession.objects.get_or_create(quiz=quiz)

    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
    }
    return render(request, 'admin_dashboard/clue_rush_monitor.html', context)


@admin_required
def sorting_ladder_monitor(request, room_code):
    """Admin monitor for a live Sorting Ladder game session."""
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(SortingLadderGame, room_code=room_code)

    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:sorting_ladder_management')

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

    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'session': session,
        'lobby_url': _get_lobby_url(request, room_code),
        'hub_session': hub_session or '',
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
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions)})
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

        quiz = SortingLadderGame.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in topic_ids],
            creator=request.user,
            status='waiting',
        )

        if isinstance(topic_ids, list) and topic_ids:
            qs = SortingQuestion.objects.filter(id__in=topic_ids, is_active=True)
            # selected_questions is a ManyToMany to SortingQuestion on the game model
            quiz.selected_questions.set(qs)

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
        } for t in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            topics.sort(key=lambda t: _omap.get(t['id'], len(_order)))
        return JsonResponse({'success': True, 'topics': topics, 'count': len(topics)})
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
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        if isinstance(data.get('topic_ids'), list):
            qs = SortingQuestion.objects.filter(id__in=data['topic_ids'], is_active=True)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in data['topic_ids']]
            quiz.save(update_fields=['question_order'])

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

    def _add_games(queryset, game_type, game_type_display, monitor_url_name, end_url_name, score_field='total_score'):
        for game in queryset:
            try:
                participant_count = game.participants.filter(is_active=True).count()
            except Exception:
                participant_count = 0
            hub_session_code = room_code_to_hub_session.get(game.room_code)
            active_games.append({
                'title': game.title,
                'room_code': game.room_code,
                'game_type': game_type,
                'game_type_display': game_type_display,
                'monitor_url_name': monitor_url_name,
                'end_url_name': end_url_name,
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
    _add_games(ClueRushGame.objects.filter(status='active'), 'clue_rush', 'Clue Rush', 'admin_dashboard:clue_rush_monitor', None)
    _add_games(SortingLadderGame.objects.filter(status='active'), 'sorting_ladder', 'Sorting Ladder', 'admin_dashboard:sorting_ladder_monitor', 'admin_dashboard:end_sorting_ladder_game_by_room_code')

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
                'status': game.status,
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

    all_games.sort(key=lambda g: g['created_at'], reverse=True)
    return render(request, 'admin_dashboard/games_overview.html', {'all_games': all_games})


@admin_required
def create_game(request):
    """New game creation form."""
    return render(request, 'admin_dashboard/manage_games.html')


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
    }
    model = type_to_model.get(game_type)
    if not model:
        from django.http import Http404
        raise Http404('Unknown game type')
    game = get_object_or_404(model, id=game_id)
    _question_order = getattr(game, 'question_order', []) or []
    if _question_order:
        _id_set = set(game.selected_questions.values_list('id', flat=True))
        selected_ids = [i for i in _question_order if i in _id_set]
        selected_ids += [i for i in _id_set if i not in set(selected_ids)]
    else:
        selected_ids = list(game.selected_questions.values_list('id', flat=True))
    return render(request, 'admin_dashboard/manage_games.html', {
        'edit_mode': True,
        'game_id': game_id,
        'game_title': game.title,
        'game_internal_description': game.internal_description,
        'game_type': game_type,
        'selected_ids_json': _json.dumps(selected_ids),
    })


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
            'points': q.points,
            'time_limit': q.time_limit,
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': q.is_active,
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions)})
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
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = QuizQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            quiz.save(update_fields=['question_order'])

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
            'points': getattr(q, 'points', None),
            'time_limit': getattr(q, 'time_limit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': q.is_active,
            'left_items': getattr(q, 'left_items', []),
            'right_items': getattr(q, 'right_items', []),
            'correct_matches': getattr(q, 'correct_matches', {}),
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions)})
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
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = AssignQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            quiz.save(update_fields=['question_order'])

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
            'points': getattr(q, 'max_points', None),
            'unit': getattr(q, 'unit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': getattr(q, 'is_active', True),
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions), 'scoring_mode': getattr(quiz, 'scoring_mode', None)})
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
        if scoring_mode in ('tolerance', 'rank'):
            setattr(quiz, 'scoring_mode', scoring_mode)
            fields_to_update.append('scoring_mode')
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = EstimationQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            quiz.save(update_fields=['question_order'])

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
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions)})
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
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = WhereQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            quiz.save(update_fields=['question_order'])

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
        qs = quiz.selected_questions.all().order_by('-created_at')
        questions = [{
            'id': q.id,
            'question_text': getattr(q, 'question_text', ''),
            'correct_answer': getattr(q, 'correct_answer', None),
            'time_limit': getattr(q, 'time_limit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': getattr(q, 'is_active', True),
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions)})
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
        if 'internal_description' in data:
            quiz.internal_description = (data['internal_description'] or '').strip()
            fields_to_update.append('internal_description')
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = BlackJackQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            quiz.save(update_fields=['question_order'])

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
            'points': getattr(q, 'points', None),
            'time_limit': getattr(q, 'time_limit', None),
            'created_at': q.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_active': getattr(q, 'is_active', True),
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions)})
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
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = WhoThatQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            quiz.save(update_fields=['question_order'])

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
        } for q in qs]
        _order = quiz.question_order or []
        if _order:
            _omap = {i: pos for pos, i in enumerate(_order)}
            questions.sort(key=lambda q: _omap.get(q['id'], len(_order)))
        return JsonResponse({'success': True, 'questions': questions, 'count': len(questions)})
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
        if fields_to_update:
            quiz.save(update_fields=fields_to_update)

        # Update selected questions
        if isinstance(question_ids, list):
            qs = WhoQuestion.objects.filter(id__in=question_ids)
            quiz.selected_questions.set(qs)
            quiz.question_order = [int(i) for i in question_ids]
            quiz.save(update_fields=['question_order'])

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

        quiz = WhoQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        if question_ids:
            qs = WhoQuestion.objects.filter(id__in=question_ids, created_by=request.user)
            quiz.selected_questions.set(qs)

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

        quiz = WhoThatQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        if question_ids:
            qs = WhoThatQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)

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

        quiz = Quiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        # Attach selected questions (only active ones the user can access)
        if question_ids:
            qs = QuizQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)

        # Create session
        QuizSession.objects.create(quiz=quiz)

        return JsonResponse({
            'success': True,
            'room_code': quiz.room_code,
            'quiz_id': quiz.id
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@admin_required
@require_POST
def add_question(request):
    """Add a new question via AJAX"""
    try:
        question_text = request.POST.get('question_text', '').strip()
        question_type = request.POST.get('question_type', 'multiple_choice')
        points = int(request.POST.get('points', 10))
        time_limit = int(request.POST.get('time_limit', 30))
        correct_answer = request.POST.get('correct_answer', '').strip()
        explanation = request.POST.get('explanation', '').strip()
        
        # Validate required fields
        if not question_text or not correct_answer:
            return JsonResponse({
                'success': False,
                'error': 'Question text and correct answer are required.'
            }, status=400)
        
        # Create question
        question = QuizQuestion.objects.create(
            question_text=question_text,
            question_type=question_type,
            points=points,
            time_limit=time_limit,
            correct_answer=correct_answer,
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
            'question_type': question.question_type,
            'points': question.points,
            'time_limit': question.time_limit,
            'option_a': question.option_a or '',
            'option_b': question.option_b or '',
            'option_c': question.option_c or '',
            'option_d': question.option_d or '',
            'correct_answer': question.correct_answer,
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
        question_type = request.POST.get('question_type', question.question_type)
        points = int(request.POST.get('points', question.points))
        time_limit = int(request.POST.get('time_limit', question.time_limit))
        correct_answer = request.POST.get('correct_answer', question.correct_answer).strip()
        explanation = request.POST.get('explanation', question.explanation or '').strip()

        if not question_text or not correct_answer:
            return JsonResponse({'success': False, 'error': 'Question text and correct answer are required.'}, status=400)

        question.question_text = question_text
        question.question_type = question_type
        question.points = points
        question.time_limit = time_limit
        question.correct_answer = correct_answer
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
        
        quiz.start_quiz()
        
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
        
        participants = quiz.participants.all()
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': QuizAnswer.objects.filter(quiz=quiz).count(),
            'current_question_responses': 0,
            'average_score': 0,
        }
        
        if participants.exists():
            stats['average_score'] = sum(p.total_score for p in participants) / participants.count()
        
        # Current question stats
        if quiz.current_question:
            current_answers = QuizAnswer.objects.filter(
                quiz=quiz, 
                question=quiz.current_question
            )
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
def api_live_responses(request, room_code):
    """Get live responses for current question"""
    try:
        quiz = get_object_or_404(Quiz, room_code=room_code)
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        if not quiz.current_question:
            # Auto-end race condition: question may have been cleared before the
            # monitor page finished reloading. Return most recent answers from
            # this session so responses are still visible after the page reload.
            if not quiz.started_at:
                return JsonResponse({'success': True, 'responses': []})
            recent_qs = QuizAnswer.objects.filter(
                quiz=quiz,
                submitted_at__gte=quiz.started_at
            ).select_related('participant', 'question').order_by('-submitted_at')[:20]
            responses_data = []
            for response in recent_qs:
                display_answer = response.answer_text
                if response.question.question_type == 'multiple_choice':
                    opt_map = dict(response.question.get_options())
                    display_answer = opt_map.get(response.answer_text.upper(), response.answer_text)
                responses_data.append({
                    'participant_name': response.participant.name,
                    'answer_text': display_answer,
                    'is_correct': response.is_correct,
                    'time_taken': response.time_taken,
                    'points_earned': response.points_earned,
                    'submitted_at': response.submitted_at.isoformat(),
                })
            return JsonResponse({'success': True, 'responses': responses_data})

        qs = QuizAnswer.objects.filter(
            quiz=quiz,
            question=quiz.current_question,
        )
        # Filter to only answers submitted after the question was last sent (handles replayed questions)
        if quiz.question_start_time:
            qs = qs.filter(submitted_at__gte=quiz.question_start_time)
        elif quiz.started_at:
            qs = qs.filter(submitted_at__gte=quiz.started_at)
        responses = qs.select_related('participant').order_by('-submitted_at')[:20]

        # Build key→text map for multiple choice display
        option_map = {}
        if quiz.current_question.question_type == 'multiple_choice':
            option_map = dict(quiz.current_question.get_options())

        responses_data = []
        for response in responses:
            display_answer = option_map.get(response.answer_text.upper(), response.answer_text) \
                if option_map else response.answer_text
            responses_data.append({
                'participant_name': response.participant.name,
                'answer_text': display_answer,
                'is_correct': response.is_correct,
                'time_taken': response.time_taken,
                'points_earned': response.points_earned,
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
    


# WHERE IS THIS GAME VIEWS

from where_is_this.models import WhereQuiz, WhereQuestion, WhereParticipant, WhereAnswer, WhereSession

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
    
    # Get difficulty breakdown with default values
    difficulty_stats = WhereQuestion.objects.filter(is_active=True).values('difficulty').annotate(
        count=Count('id')
    )
    
    # Initialize with default values to avoid KeyError
    difficulty_counts = {
        'easy': 0,
        'medium': 0,
        'hard': 0
    }
    
    # Update with actual counts
    for stat in difficulty_stats:
        if stat['difficulty'] in difficulty_counts:
            difficulty_counts[stat['difficulty']] = stat['count']
    
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
        'difficulty_counts': difficulty_counts,
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

        quiz = WhereQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        if question_ids:
            qs = WhereQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)

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
        difficulty = request.POST.get('difficulty', 'medium')
        time_limit = int(request.POST.get('time_limit', 60))
        points = int(request.POST.get('points', 100))
        perfect_distance = float(request.POST.get('perfect_distance', 10))
        good_distance = float(request.POST.get('good_distance', 100))
        fair_distance = float(request.POST.get('fair_distance', 500))
        poor_distance = float(request.POST.get('poor_distance', 2000))
        latitude = float(request.POST.get('latitude'))
        longitude = float(request.POST.get('longitude'))
        hint_text = request.POST.get('hint_text', '').strip()
        explanation = request.POST.get('explanation', '').strip()
        
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
        
        # Validate difficulty
        if difficulty not in ['easy', 'medium', 'hard']:
            return JsonResponse({
                'success': False,
                'error': 'Invalid difficulty level.'
            }, status=400)
        
        # Validate coordinates
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return JsonResponse({
                'success': False,
                'error': 'Invalid coordinates provided.'
            }, status=400)
        
        # Create question
        question = WhereQuestion.objects.create(
            question_text=question_text,
            difficulty=difficulty,
            time_limit=time_limit,
            points=points,
            perfect_distance=perfect_distance,
            good_distance=good_distance,
            fair_distance=fair_distance,
            poor_distance=poor_distance,
            correct_latitude=latitude,
            correct_longitude=longitude,
            hint_text=hint_text if hint_text else None,
            explanation=explanation if explanation else None,
            image=image,
            created_by=request.user
        )
        
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
            'difficulty': question.difficulty,
            'points': question.points,
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
        if 'difficulty' in data:
            diff = data.get('difficulty')
            if diff in ['easy', 'medium', 'hard']:
                question.difficulty = diff
        if 'points' in data:
            question.points = int(data.get('points'))
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
            'difficulty': question.difficulty,
            'time_limit': question.time_limit,
            'points': question.points,
            'perfect_distance': question.perfect_distance,
            'good_distance': question.good_distance,
            'fair_distance': question.fair_distance,
            'poor_distance': question.poor_distance,
            'latitude': question.correct_latitude,
            'longitude': question.correct_longitude,
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
        difficulty = request.POST.get('difficulty', question.difficulty)
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

        # Basic validation
        if not question_text:
            return JsonResponse({'success': False, 'error': 'Question text is required.'}, status=400)

        if difficulty not in ['easy', 'medium', 'hard']:
            return JsonResponse({'success': False, 'error': 'Invalid difficulty level.'}, status=400)

        # Update fields
        question.question_text = question_text
        question.difficulty = difficulty
        question.time_limit = time_limit
        question.points = points
        question.perfect_distance = perfect_distance
        question.good_distance = good_distance
        question.fair_distance = fair_distance
        question.poor_distance = poor_distance

        # Only overwrite coordinates if provided
        if latitude is not None and longitude is not None and latitude != '' and longitude != '':
            lat = float(latitude)
            lng = float(longitude)
            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                return JsonResponse({'success': False, 'error': 'Invalid coordinates provided.'}, status=400)
            question.correct_latitude = lat
            question.correct_longitude = lng

        question.hint_text = hint_text if hint_text else None
        question.explanation = explanation if explanation else None

        # Optional image replacement
        image = request.FILES.get('image')
        if image:
            question.image = image

        question.save()
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
        
        # Difficulty breakdown
        difficulty_stats = WhereQuiz.objects.filter(status='completed').annotate(
            avg_score=Avg('participants__total_score'),
            avg_accuracy=Avg('where_answers__accuracy_percentage')
        ).values('avg_score', 'avg_accuracy')
        
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
        difficulty = request.GET.get('difficulty', 'all')
        page = int(request.GET.get('page', 1))
        per_page = 20
        
        questions = WhereQuestion.objects.filter(created_by=request.user)
        
        if difficulty != 'all':
            questions = questions.filter(difficulty=difficulty)
        
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
                    'difficulty': q.difficulty,
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
    
    current_round_index = quiz_session.current_round_index
    total_rounds_current = len(quiz.current_question.left_items) if quiz.current_question and quiz.current_question.left_items else 0

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

        quiz = AssignQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        # Attach selected questions (only active ones the user can access)
        if question_ids:
            qs = AssignQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)

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
        points = int(data.get('points', 10))
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

        if len(left_items) <= len(right_items):
            return JsonResponse({
                'success': False,
                'error': f'Die linke Seite muss mindestens ein Item mehr haben als die rechte (links: {len(left_items)}, rechts: {len(right_items)}).'
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
            points=points,
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
                'points': question.points,
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
        points = int(data.get('points', question.points))
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

        if len(left_items) <= len(right_items):
            return JsonResponse({
                'success': False,
                'error': f'Die linke Seite muss mindestens ein Item mehr haben als die rechte (links: {len(left_items)}, rechts: {len(right_items)}).'
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
        question.points = points
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
        unit = data.get('unit', 'number')
        difficulty = data.get('difficulty', 'medium')
        max_points = int(data.get('max_points', 100))
        tolerance_percentage = float(data.get('tolerance_percentage', 10.0))
        hint_text = data.get('hint_text', '').strip() if data.get('hint_text') is not None else ''
        explanation = data.get('explanation', '').strip() if data.get('explanation') is not None else ''
        
        # Validate required fields
        if not question_text:
            return JsonResponse({
                'success': False,
                'error': 'Question text is required.'
            }, status=400)
        
        # Validate difficulty
        if difficulty not in ['easy', 'medium', 'hard']:
            return JsonResponse({
                'success': False,
                'error': 'Invalid difficulty level.'
            }, status=400)
        
        # Validate tolerance percentage
        if not (1.0 <= tolerance_percentage <= 50.0):
            return JsonResponse({
                'success': False,
                'error': 'Tolerance percentage must be between 1% and 50%.'
            }, status=400)
        
        # Create question
        question = EstimationQuestion.objects.create(
            question_text=question_text,
            correct_answer=correct_answer,
            unit=unit,
            difficulty=difficulty,
            max_points=max_points,
            tolerance_percentage=tolerance_percentage,
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
            'unit': question.unit,
            'difficulty': question.difficulty,
            'max_points': question.max_points,
            'tolerance_percentage': question.tolerance_percentage,
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
            question.unit = data.get('unit')
        if 'difficulty' in data:
            question.difficulty = data.get('difficulty')
        if 'max_points' in data:
            question.max_points = int(data.get('max_points'))
        if 'tolerance_percentage' in data:
            question.tolerance_percentage = float(data.get('tolerance_percentage'))
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
        
        # Difficulty breakdown
        difficulty_stats = EstimationSession.objects.filter(status='completed').values('difficulty').annotate(
            count=Count('id'),
            avg_score=Avg('total_score'),
            avg_accuracy=Avg('answers__accuracy_percentage')
        )
        
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
                'difficulty_breakdown': list(difficulty_stats),
                'recent_games': [
                    {
                        'id': str(game.session_id),
                        'player_name': game.player_name,
                        'difficulty': game.difficulty,
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
                        'difficulty': game.difficulty,
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
        difficulty = request.GET.get('difficulty', 'all')
        page = int(request.GET.get('page', 1))
        per_page = 20
        
        questions = EstimationQuestion.objects.filter(created_by=request.user)
        
        if difficulty != 'all':
            questions = questions.filter(difficulty=difficulty)
        
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
                    'difficulty': q.difficulty,
                    'max_points': q.max_points,
                    'tolerance_percentage': q.tolerance_percentage,
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
    
    # Get difficulty breakdown with default values
    difficulty_stats = EstimationQuestion.objects.filter(is_active=True).values('difficulty').annotate(
        count=Count('id')
    )
    
    # Initialize with default values to avoid KeyError
    difficulty_counts = {
        'easy': 0,
        'medium': 0,
        'hard': 0
    }
    
    # Update with actual counts
    for stat in difficulty_stats:
        if stat['difficulty'] in difficulty_counts:
            difficulty_counts[stat['difficulty']] = stat['count']
    
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
        'difficulty_counts': difficulty_counts,
        'total_games': total_games,
        'total_players': total_players,
        'average_accuracy': round(avg_accuracy, 1),
        'bundles': EstimationBundle.objects.filter(creator=request.user).prefetch_related('questions'),
    }

    return render(request, 'admin_dashboard/estimation_management.html', context)


@admin_required
@require_POST
def create_estimation_quiz(request):
    """Create a new estimation quiz via AJAX"""
    try:
        quiz = EstimationQuiz.objects.create(
            title="Estimation Quiz",
            creator=request.user,
            status='waiting'
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
        scoring_mode = (data.get('scoring_mode') or 'tolerance').strip()

        quiz = EstimationQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting',
            scoring_mode='rank' if scoring_mode == 'rank' else 'tolerance'
        )

        if question_ids:
            qs = EstimationQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)

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
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
    
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
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
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
        
        quiz.start_quiz()
        
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
        
        # Get or create quiz session
        quiz_session, created = EstimationSession.objects.get_or_create(quiz=quiz)
        
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




from who_is_lying.models import WhoQuiz, WhoQuestion, WhoParticipant, WhoAnswer, WhoSession

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
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
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
    
    # Get difficulty breakdown with default values
    difficulty_stats = WhoThatQuestion.objects.filter(is_active=True).values('difficulty').annotate(
        count=Count('id')
    )
    
    # Initialize with default values to avoid KeyError
    difficulty_counts = {
        'easy': 0,
        'medium': 0,
        'hard': 0
    }
    
    # Update with actual counts
    for stat in difficulty_stats:
        if stat['difficulty'] in difficulty_counts:
            difficulty_counts[stat['difficulty']] = stat['count']
    
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
        'difficulty_counts': difficulty_counts,
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
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
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
        difficulty = request.POST.get('difficulty', 'medium')
        points = int(request.POST.get('points', 100))
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
        
        # Validate difficulty
        if difficulty not in ['easy', 'medium', 'hard']:
            return JsonResponse({
                'success': False,
                'error': 'Invalid difficulty level.'
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
            difficulty=difficulty,
            points=points,
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
        
        quiz.start_quiz()
        
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
        
        # Get or create quiz session
        quiz_session, created = WhoThatSession.objects.get_or_create(quiz=quiz)
        
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
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        if not quiz.current_question:
            return JsonResponse({
                'success': True,
                'responses': []
            })
        
        responses = WhoThatAnswer.objects.filter(
            quiz=quiz,
            question=quiz.current_question
        ).select_related('participant').order_by('-submitted_at')[:20]
        
        responses_data = []
        for response in responses:
            responses_data.append({
                'participant_name': response.participant.name,
                'user_answer': response.user_answer,
                'points_earned': response.points_earned,
                'is_correct': response.is_correct,
                'accuracy_percentage': response.get_accuracy_percentage(),
                'match_quality': response.get_match_quality(),
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
        difficulty = request.GET.get('difficulty', 'all')
        page = int(request.GET.get('page', 1))
        per_page = 20
        
        questions = WhoThatQuestion.objects.filter(created_by=request.user)
        
        if difficulty != 'all':
            questions = questions.filter(difficulty=difficulty)
        
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
                    'difficulty': q.difficulty,
                    'category': q.category or 'General',
                    'points': q.points,
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
        quiz = BlackJackQuiz.objects.create(
            title="BlackJack Quiz",
            creator=request.user,
            status='waiting'
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

        quiz = BlackJackQuiz.objects.create(
            title=title,
            internal_description=(data.get('internal_description') or '').strip(),
            question_order=[int(i) for i in question_ids],
            creator=request.user,
            status='waiting'
        )

        # Attach selected questions (only active ones the user can access)
        if question_ids:
            qs = BlackJackQuestion.objects.filter(id__in=question_ids, is_active=True)
            quiz.selected_questions.set(qs)

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
    hub_session = request.GET.get('hub_session')
    quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
    
    # Ensure the logged-in user is the creator or is admin
    if not request.user.is_superuser and quiz.creator != request.user:
        return redirect('admin_dashboard:blackjack_management')
    
    participants = quiz.participants.all().filter(hub_session_code=hub_session).order_by('final_score', 'name')  # Lower score is better
    if quiz.selected_questions.exists():
        available_questions = quiz.selected_questions.all().order_by('-created_at')
    else:
        available_questions = BlackJackQuestion.objects.filter(created_by=request.user, is_active=True).order_by('-created_at')
    
    # Get or create quiz session
    quiz_session, created = BlackJackSession.objects.get_or_create(quiz=quiz)
    
    context = {
        'quiz': quiz,
        'participants': participants,
        'participant_count': participants.count(),
        'available_questions': available_questions,
        'quiz_session': quiz_session,
        'lobby_url': _get_lobby_url(request, room_code),
    }
    return render(request, 'admin_dashboard/blackjack_monitor.html', context)


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
        
        question = get_object_or_404(BlackJackQuestion, id=question_id, is_active=True)

        if quiz.selected_questions.exists() and not quiz.selected_questions.filter(id=question.id).exists():
            return JsonResponse({
                'success': False,
                'error': 'This question is not part of the selected set for this quiz.'
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
    
    # Order participants by final_score (lower is better), then by name
    participants = quiz.participants.all().order_by('final_score', 'name')
    answers = BlackJackAnswer.objects.filter(quiz=quiz).select_related('question', 'participant').order_by('question_number', 'submitted_at')
    
    # Calculate quiz statistics
    quiz_stats = {
        'total_participants': participants.count(),
        'total_questions': quiz.session.total_questions_sent if hasattr(quiz, 'session') else 0,
        'total_answers': answers.count(),
        'average_points': 0,
        'busted_participants': participants.filter(is_busted=True).count(),
        'blackjack_participants': participants.filter(total_points=21).count(),
        'perfect_scores': answers.filter(points_earned=0).count(),
    }
    
    if participants.exists():
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
        
        # Ensure the logged-in user is the creator or is admin
        if not request.user.is_superuser and quiz.creator != request.user:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        participants = quiz.participants.all()
        
        stats = {
            'participant_count': participants.count(),
            'active_participants': participants.filter(is_active=True).count(),
            'total_answers': BlackJackAnswer.objects.filter(quiz=quiz).count(),
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
            current_answers = BlackJackAnswer.objects.filter(
                quiz=quiz, 
                question=quiz.current_question
            )
            stats['current_question_responses'] = current_answers.count()
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
        
        participants = quiz.participants.all().order_by('final_score', 'name')
        
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
            'question_type': question.question_type,
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
                Q(difficulty__icontains=search_query) |
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
                'difficulty': question.difficulty,
                'max_points': question.max_points,
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
                Q(difficulty__icontains=search_query) |
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
                'difficulty': question.difficulty,
                'points': question.points,
                'time_limit': question.time_limit,
                'has_image': bool(question.image),
                'correct_location': f"({question.correct_latitude}, {question.correct_longitude})",
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
                Q(difficulty__icontains=search) |
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
                'difficulty': question.difficulty,
                'points': question.points,
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

        return JsonResponse({'success': True, 'question_id': question.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
