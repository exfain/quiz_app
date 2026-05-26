import random
import string
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_http_methods, require_POST
from django.db.models import Max
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404
from django.db.models import Sum, F, Case, When, Value, IntegerField, Q
from django.db import connection, transaction
from .models import HubSession, HubParticipant, HubGameStep, GameVote
from .active_game_guard import resolve_session_game_activation
from .lobby_return_flow import (
    LOBBY_RETURN_COUNTDOWN_SECONDS,
    broadcast_lobby_return_countdown_started,
    broadcast_players_recalled_to_lobby,
    ensure_session_players_ready_for_game_start,
    get_lobby_return_countdown_state,
    mark_session_game_participants_inactive,
    mark_single_participant_inactive_for_lobby_return,
)
from .spectator import build_spectator_state
from QuizGame.models import Quiz as QuizGameModel, QuizParticipant, QuizQuestion
from sorting_ladder.models import SortingLadderGame, SortingLadderParticipant, SortingQuestion
from clue_rush.models import ClueRushGame, ClueRushParticipant
from wer_weiss_mehr.models import WerWeissMehrGame, WerWeissMehrParticipant, WerWeissMehrQuestion
from Assign.models import AssignQuiz, AssignParticipant, AssignQuestion
from Estimation.models import EstimationQuiz, EstimationParticipant, EstimationQuestion
from where_is_this.models import WhereQuiz, WhereParticipant, WhereQuestion
from who_is_lying.models import WhoQuiz, WhoParticipant, WhoQuestion
from who_is_that.models import WhoThatQuiz, WhoThatParticipant, WhoThatQuestion
from black_jack_quiz.models import BlackJackQuiz, BlackJackParticipant, BlackJackQuestion


def gen_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


@require_http_methods(["GET", "POST"])
def join_session(request):
    """Participants enter a session code + nickname to join a hub session."""
    error = None
    if request.method == 'POST':
        code = (request.POST.get('code') or '').strip().upper()
        nickname = (request.POST.get('nickname') or '').strip()
        if not code:
            error = 'Bitte einen Session-Code eingeben.'
        elif not nickname:
            error = 'Bitte einen Namen eingeben.'
        else:
            try:
                session = HubSession.objects.get(code=code)
                if session.ended_at:
                    error = 'Diese Session ist bereits beendet.'
                else:
                    from urllib.parse import urlencode
                    return redirect(
                        f"/hub/lobby/{session.code}/?{urlencode({'nickname': nickname})}"
                    )
            except HubSession.DoesNotExist:
                error = 'Session nicht gefunden. Bitte Code prüfen.'
    return render(request, 'hub/join_session.html', {'error': error})


@login_required
@require_http_methods(["GET", "POST"])
def create_session(request):
    if request.method == 'POST':
        name = request.POST.get('name') or ''
        code = request.POST.get('code') or gen_code()

        if HubSession.objects.filter(code=code).exists():
            return render(request, 'hub/create_session.html', {
                'error': 'A session with this code already exists. Please choose a different code.',
                **_get_game_instances(),
            })

        # games_order: JSON array of {"game_key": "...", "room_code": "..."} objects
        try:
            games_ordered = json.loads(request.POST.get('games_order', '[]'))
            if not isinstance(games_ordered, list):
                games_ordered = []
        except json.JSONDecodeError:
            games_ordered = []

        session = HubSession.objects.create(code=code, name=name, is_active=False)

        GAME_MODEL_MAP = {
            'quiz':           QuizGameModel,
            'estimation':     EstimationQuiz,
            'assign':         AssignQuiz,
            'where':          WhereQuiz,
            'who':            WhoQuiz,
            'who_that':       WhoThatQuiz,
            'blackjack':      BlackJackQuiz,
            'clue_rush':      ClueRushGame,
            'sorting_ladder': SortingLadderGame,
            'wer_weiss_mehr': WerWeissMehrGame,
        }

        for order, entry in enumerate(games_ordered):
            game_key = entry.get('game_key', '')
            room_code = entry.get('room_code', '')
            title = entry.get('title', game_key.replace('_', ' ').title())
            if not game_key or not room_code:
                continue
            # Reset the game instance to waiting so it can be played fresh
            model = GAME_MODEL_MAP.get(game_key)
            if model:
                try:
                    model.objects.filter(room_code=room_code).update(status='waiting')
                except Exception:
                    pass
            HubGameStep.objects.create(
                session=session,
                order=order,
                game_key=game_key,
                room_code=room_code,
                title=title,
            )

        return redirect('admin_dashboard:sessions_overview')

    return render(request, 'hub/create_session.html', _get_game_instances())


def _get_game_instances():
    """Return all game instances grouped by type for the session creation wizard."""
    GAME_TYPES = [
        ('quiz',           QuizGameModel,      'Quick Quiz',      'help-circle'),
        ('estimation',     EstimationQuiz,     'Estimation',      'bar-chart-2'),
        ('assign',         AssignQuiz,         'Assign',          'list-checks'),
        ('where',          WhereQuiz,          'Where Is This?',  'map-pin'),
        ('who',            WhoQuiz,            'Who Is Lying?',   'user-x'),
        ('who_that',       WhoThatQuiz,        'Who Is That?',    'users'),
        ('blackjack',      BlackJackQuiz,      'Black Jack',      'spade'),
        ('clue_rush',      ClueRushGame,       'Clue Rush',       'zap'),
        ('sorting_ladder', SortingLadderGame,  'Sorting Ladder',  'list-ordered'),
        ('wer_weiss_mehr', WerWeissMehrGame,   'Wer weiß mehr?', 'layers'),
    ]
    games = []
    for game_key, model, label, icon in GAME_TYPES:
        qs = model.objects.all().order_by('-created_at') if hasattr(model, 'created_at') else model.objects.all()
        for obj in qs:
            try:
                q_count = obj.selected_questions.count()
            except Exception:
                q_count = 0
            games.append({
                'game_key': game_key,
                'label': label,
                'icon': icon,
                'title': getattr(obj, 'title', getattr(obj, 'name', str(obj))),
                'room_code': obj.room_code,
                'question_count': q_count,
                'status': getattr(obj, 'status', ''),
            })
    return {'all_game_instances': games}


def get_game_participant_data(session, game_model, participant_model, room_code, game_key):
    """Helper function to get participant data for a specific game"""
    try:
        game = game_model.objects.get(room_code=room_code)
        # Only include participants who joined during this session's window
        participants_qs = participant_model.objects.filter(quiz=game, hub_session_code=session.code).select_related('quiz')
        if session.started_at:
            participants_qs = participants_qs.filter(joined_at__gte=session.started_at)
        if session.ended_at:
            participants_qs = participants_qs.filter(joined_at__lte=session.ended_at)
        participants = participants_qs
        
        data = {}
        for p in participants:
            # Some games (e.g., BlackJack) track total_points instead of total_score
            score_value = getattr(p, 'total_score', None)
            if score_value is None:
                score_value = getattr(p, 'total_points', 0)
            accuracy_fn = getattr(p, 'get_average_accuracy', None)
            accuracy_value = accuracy_fn() if callable(accuracy_fn) else 0
            data[p.name] = {
                'score': score_value,
                'accuracy': accuracy_value
            }
        return data
    except game_model.DoesNotExist:
        return {}

def get_leaderboard_data(session):
    """Generate leaderboard data for a session"""

    try:
        # Get all game steps for the session
        steps = session.steps.all().order_by('order')
        
        # Initialize response data
        games = []
        participants_data = {}
        instance_meta = {}
        
        # Map of game keys to their models and participant models
        # Keys must match HubGameStep.game_key values used throughout the app
        GAME_MODELS = {
            'quiz': (QuizGameModel, QuizParticipant, 'Quiz Game'),
            'clue_rush': (ClueRushGame, ClueRushParticipant, 'Clue Rush Game'),
            'estimation': (EstimationQuiz, EstimationParticipant, 'Estimation'),
            'assign': (AssignQuiz, AssignParticipant, 'Assign'),
            'who': (WhoQuiz, WhoParticipant, 'Who is Lying?'),
            'who_that': (WhoThatQuiz, WhoThatParticipant, 'Who is That?'),
            'where': (WhereQuiz, WhereParticipant, 'Where is This?'),
            'blackjack': (BlackJackQuiz, BlackJackParticipant, 'Black Jack'),
            'sorting_ladder': (SortingLadderGame, SortingLadderParticipant, 'Sorting Ladder'),
            'wer_weiss_mehr': (WerWeissMehrGame, WerWeissMehrParticipant, 'Wer weiß mehr?'),
        }
        
        # Process each game step
        for step in steps:
            game_key = step.game_key
            if game_key not in [g['key'] for g in games]:
                games.append({
                    'key': game_key,
                    'name': next((g[2] for k, g in GAME_MODELS.items() if k == game_key), game_key.title())
                })

            # Get participant data for this specific game instance (room)
            if game_key in GAME_MODELS and GAME_MODELS[game_key][1] is not None:
                game_model, participant_model, _ = GAME_MODELS[game_key]
                game_data = get_game_participant_data(session, game_model, participant_model, step.room_code, game_key)

                # Use a per-instance key so multiple steps of same type don't overwrite
                instance_key = f"{game_key}:{step.room_code}"

                # Record instance display metadata (title and type)
                type_name = next((g[2] for k, g in GAME_MODELS.items() if k == game_key), game_key.title())
                # Prefer the actual quiz object's title; fallback to step.title if present
                game_obj = None
                try:
                    game_obj = game_model.objects.only('title').get(room_code=step.room_code)
                except Exception:
                    game_obj = None
                game_title = (getattr(game_obj, 'title', None) or getattr(step, 'title', '') or '')
                # Points awarded to the winner of this step: position 1 → 1 pt, position 2 → 2 pts, …
                hub_points = step.order + 1
                instance_meta[instance_key] = {
                    'title': game_title,
                    'type': type_name,
                    'hub_points': hub_points,
                }

                # Determine winner score for this game
                max_score = max((d['score'] for d in game_data.values()), default=0)

                # Update participants data
                for name, data in game_data.items():
                    if name not in participants_data:
                        participants_data[name] = {
                            'name': name,
                            'total_score': 0,
                            'weighted_score': 0,
                            'games_played': 0,
                            'game_scores': {},
                            'game_accuracies': {}
                        }

                    # Count every game instance played
                    participants_data[name]['games_played'] += 1

                    # Store scores per instance to avoid overwriting when multiple steps exist
                    participants_data[name]['game_scores'][instance_key] = data['score']
                    participants_data[name]['game_accuracies'][instance_key] = data['accuracy']
                    participants_data[name]['total_score'] += data['score']

                    # Winner earns hub_points; ties share; 0 if no one scored
                    if max_score > 0 and data['score'] == max_score:
                        participants_data[name]['weighted_score'] += hub_points
        
        # Apply score adjustments from HubParticipant
        hub_participants = {
            hp.nickname: hp
            for hp in HubParticipant.objects.filter(session=session)
        }
        for name, pdata in participants_data.items():
            hp = hub_participants.get(name)
            pdata['hub_participant_id'] = hp.id if hp else None
            pdata['score_adjustment'] = hp.score_adjustment if hp else 0
            pdata['total_score'] += hp.score_adjustment if hp else 0

        # Convert to list and sort by weighted score
        participants = sorted(participants_data.values(), key=lambda x: x['weighted_score'], reverse=True)
    except Exception as e:
        print("Error getting leaderboard data:", e)
    return {
        'games': games,
        'participants': participants,
        'instances': instance_meta,
    }

@login_required
@require_POST
def set_hub_participant_score(request):
    """Manually set the score adjustment for a HubParticipant."""
    try:
        data = json.loads(request.body)
        participant = get_object_or_404(HubParticipant, id=data['participant_id'])
        participant.score_adjustment = int(data['score'])
        participant.save(update_fields=['score_adjustment'])
        return JsonResponse({'success': True, 'new_score': participant.score_adjustment})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


def session_leaderboard_api(request, session_code):
    """API endpoint to get leaderboard data for a session"""
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        session = HubSession.objects.get(code=session_code)
        data = get_leaderboard_data(session)
        return JsonResponse(data)
    except HubSession.DoesNotExist:
        return JsonResponse({'error': 'Session not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

def lobby(request, session_code: str):
    session = get_object_or_404(HubSession, code=session_code)
    participants = HubParticipant.objects.filter(session=session)
    
    return render(request, 'hub/lobby.html', {
        'session_code': session_code,
        'session_name': session.name or session_code,
        'participants': list(participants.values('id', 'nickname'))
    })

def session_leaderboard(request, session_code: str):
    """Display the final leaderboard for a session."""
    session = get_object_or_404(HubSession, code=session_code)
    leaderboard_data = get_leaderboard_data(session)
    
    # Convert the data to a JSON string for the template
    leaderboard_json = json.dumps(leaderboard_data['participants'], default=str)
    instance_meta_json = json.dumps(leaderboard_data.get('instances', {}), default=str)
    
    #Check if user is admin
    is_admin = False
    if request.user.is_superuser or request.user.is_staff:
        is_admin = True
    
    return render(request, 'hub/leaderboard.html', {
        'session': session,
        'leaderboard_data': leaderboard_json,  # Pass as JSON string
        'participants': leaderboard_data['participants'],    # Also pass as Python object for template loops
        'instance_meta_json': instance_meta_json,
        'is_admin': is_admin
    })

def monitor(request, session_code: str):
    session = get_object_or_404(HubSession, code=session_code)
    steps = list(session.steps.all())

    # Annotate each step with the current status of its game instance
    _game_model_map = {
        'quiz':           QuizGameModel,
        'sorting_ladder': SortingLadderGame,
        'clue_rush':      ClueRushGame,
        'assign':         AssignQuiz,
        'estimation':     EstimationQuiz,
        'where':          WhereQuiz,
        'who':            WhoQuiz,
        'who_that':       WhoThatQuiz,
        'blackjack':      BlackJackQuiz,
        'wer_weiss_mehr': WerWeissMehrGame,
    }
    for step in steps:
        model = _game_model_map.get(step.game_key)
        if model and step.room_code:
            game = model.objects.filter(room_code=step.room_code).first()
            step.game_status = getattr(game, 'status', None) if game else None
        else:
            step.game_status = None

    next_planned_step = next(
        (step for step in steps if getattr(step, 'game_status', None) != 'completed'),
        None,
    )

    # Gather waiting games grouped by type for the current user
    user = request.user
    waiting_games = {
        'quiz': QuizGameModel.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'clue_rush': ClueRushGame.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'assign': AssignQuiz.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'estimation': EstimationQuiz.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'where': WhereQuiz.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'who': WhoQuiz.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'who_that': WhoThatQuiz.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'blackjack': BlackJackQuiz.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'sorting_ladder': SortingLadderGame.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
        'wer_weiss_mehr': WerWeissMehrGame.objects.filter(status='waiting', creator=user).values('title', 'room_code'),
    }

    # All lobby participants for the right column
    session_players = list(
        session.participants.order_by('joined_at').values('id', 'nickname', 'score_adjustment')
    )
    from .utils import get_server_ip
    ip = get_server_ip()
    return render(request, 'hub/monitor.html', {
        'session': session,
        'steps': steps,
        'next_planned_step_id': next_planned_step.id if next_planned_step else None,
        'ip': ip,
        'waiting_games': waiting_games,
        'session_players': session_players,
    })


def spectate_session(request, session_code: str):
    """Read-only spectator view for a hub session. No participant registration."""
    session = get_object_or_404(HubSession, code=session_code)
    return render(request, 'hub/spectate.html', {
        'session': session,
        'session_code': session_code,
    })


def spectate_session_state(request, session_code: str):
    """Return read-only spectator state. This endpoint never joins a participant."""
    session = get_object_or_404(HubSession, code=session_code)
    return JsonResponse(
        build_spectator_state(session),
        json_dumps_params={'ensure_ascii': False},
    )


@login_required
@require_POST
def add_step_to_session(request, session_code):
    """Add a new game step to a running session."""
    session = get_object_or_404(HubSession, code=session_code)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    game_key = data.get('game_key', '').strip()
    title = data.get('title', '').strip()
    question_ids = data.get('question_ids', [])
    # Support single game_id or list of game_ids
    _raw_ids = data.get('game_ids') or ([data['game_id']] if data.get('game_id') else [])
    game_ids = [int(i) for i in _raw_ids if i]

    valid_keys = [choice[0] for choice in HubGameStep.GAME_CHOICES]
    if game_key not in valid_keys:
        return JsonResponse({'error': 'Invalid game type'}, status=400)

    _game_model_map = {
        'quiz': QuizGameModel, 'assign': AssignQuiz, 'estimation': EstimationQuiz,
        'where': WhereQuiz, 'who': WhoQuiz, 'who_that': WhoThatQuiz,
        'blackjack': BlackJackQuiz, 'sorting_ladder': SortingLadderGame, 'clue_rush': ClueRushGame,
        'wer_weiss_mehr': WerWeissMehrGame,
    }

    if game_ids:
        # Add one step per selected game instance
        game_model = _game_model_map.get(game_key)
        if not game_model:
            return JsonResponse({'error': 'Unknown game type'}, status=400)
        created_steps = []
        for gid in game_ids:
            try:
                game_instance = game_model.objects.get(id=gid)
            except game_model.DoesNotExist:
                continue
            # Reset game state so it can be played again
            game_instance.status = 'waiting'
            game_instance.synced = False
            for attr in ('started_at', 'ended_at', 'question_start_time'):
                if hasattr(game_instance, attr):
                    setattr(game_instance, attr, None)
            if hasattr(game_instance, 'current_question'):
                game_instance.current_question = None
            if hasattr(game_instance, 'current_question_number'):
                game_instance.current_question_number = 0
            game_instance.save()
            max_order = session.steps.aggregate(Max('order'))['order__max']
            next_order = 0 if max_order is None else max_order + 1
            step = HubGameStep.objects.create(
                session=session,
                order=next_order,
                game_key=game_key,
                room_code=game_instance.room_code,
                title=game_instance.title,
            )
            created_steps.append({
                'id': step.id,
                'order': step.order,
                'game_key': step.game_key,
                'game_key_display': step.get_game_key_display(),
                'room_code': step.room_code,
                'title': step.title,
            })
        return JsonResponse({'success': True, 'steps': created_steps})
    else:
        max_order = session.steps.aggregate(Max('order'))['order__max']
        next_order = 0 if max_order is None else max_order + 1
        quiz_title = title or f"{session.name or session.code} - {game_key.replace('_', ' ').title()} {next_order + 1}"
        room_code = auto_create_game_quiz(game_key, request.user, quiz_title)
        if not room_code:
            return JsonResponse({'error': 'Game could not be created'}, status=500)
        if question_ids:
            _assign_questions_to_quiz(game_key, room_code, question_ids)
        step = HubGameStep.objects.create(
            session=session,
            order=next_order,
            game_key=game_key,
            room_code=room_code,
            title=quiz_title,
        )
        return JsonResponse({
            'success': True,
            'steps': [{
                'id': step.id,
                'order': step.order,
                'game_key': step.game_key,
                'game_key_display': step.get_game_key_display(),
                'room_code': step.room_code,
                'title': step.title,
            }]
        })


def _assign_questions_to_quiz(game_key: str, room_code: str, question_ids: list):
    """Assign selected question IDs to a newly created quiz via selected_questions."""
    from QuizGame.models import QuizQuestion
    from Assign.models import AssignQuestion
    from Estimation.models import EstimationQuestion
    from where_is_this.models import WhereQuestion
    from who_is_lying.models import WhoQuestion
    from who_is_that.models import WhoThatQuestion
    from black_jack_quiz.models import BlackJackQuestion
    from sorting_ladder.models import SortingQuestion
    from clue_rush.models import ClueQuestion
    from wer_weiss_mehr.models import WerWeissMehrQuestion

    quiz_model_map = {
        'quiz':           (QuizGameModel,      QuizQuestion),
        'assign':         (AssignQuiz,         AssignQuestion),
        'estimation':     (EstimationQuiz,     EstimationQuestion),
        'where':          (WhereQuiz,          WhereQuestion),
        'who':            (WhoQuiz,            WhoQuestion),
        'who_that':       (WhoThatQuiz,        WhoThatQuestion),
        'blackjack':      (BlackJackQuiz,      BlackJackQuestion),
        'sorting_ladder': (SortingLadderGame,  SortingQuestion),
        'clue_rush':      (ClueRushGame,       ClueQuestion),
        'wer_weiss_mehr': (WerWeissMehrGame,   WerWeissMehrQuestion),
    }
    entry = quiz_model_map.get(game_key)
    if not entry:
        return
    quiz_model, question_model = entry
    try:
        quiz = quiz_model.objects.get(room_code=room_code)
        questions = question_model.objects.filter(id__in=question_ids)
        quiz.selected_questions.set(questions)
    except Exception:
        pass


def auto_create_game_quiz(game_key: str, user, title: str):
    """Create a quiz instance for the given game and return its room_code."""
    try:
        if game_key == 'quiz':
            obj = QuizGameModel.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'clue_rush':
            obj = ClueRushGame.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'assign':
            obj = AssignQuiz.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'estimation':
            obj = EstimationQuiz.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'where':
            obj = WhereQuiz.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'who':
            obj = WhoQuiz.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'who_that':
            obj = WhoThatQuiz.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'blackjack':
            obj = BlackJackQuiz.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'sorting_ladder':
            obj = SortingLadderGame.objects.create(creator=user, title=title)
            return obj.room_code
        if game_key == 'wer_weiss_mehr':
            obj = WerWeissMehrGame.objects.create(creator=user, title=title)
            return obj.room_code
    except Exception:
        return None
    return None


@login_required
def get_available_questions(request, game_key):
    """Return available questions for a given game type."""
    from QuizGame.models import QuizQuestion
    from Assign.models import AssignQuestion
    from Estimation.models import EstimationQuestion
    from where_is_this.models import WhereQuestion
    from who_is_lying.models import WhoQuestion
    from who_is_that.models import WhoThatQuestion
    from black_jack_quiz.models import BlackJackQuestion
    from sorting_ladder.models import SortingQuestion
    from clue_rush.models import ClueQuestion

    config = {
        'quiz':           (QuizQuestion,       'question_text'),
        'assign':         (AssignQuestion,      'question_text'),
        'estimation':     (EstimationQuestion,  'question_text'),
        'where':          (WhereQuestion,       'question_text'),
        'who':            (WhoQuestion,         'statement'),
        'who_that':       (WhoThatQuestion,     'question_text'),
        'blackjack':      (BlackJackQuestion,   'question_text'),
        'sorting_ladder': (SortingQuestion,     'question_text'),
        'clue_rush':      (ClueQuestion,        'question_text'),
        'wer_weiss_mehr': (WerWeissMehrQuestion, 'question_text'),
    }
    entry = config.get(game_key)
    if not entry:
        return JsonResponse({'error': 'Unknown game type'}, status=400)

    model, text_field = entry
    questions = [
        {'id': q.id, 'text': getattr(q, text_field, '')}
        for q in model.objects.all().order_by('id')
    ]
    return JsonResponse({'questions': questions})


@login_required
def get_game_instances(request, game_key):
    """Return existing pre-configured game instances for a given game type."""
    model_map = {
        'quiz': QuizGameModel, 'assign': AssignQuiz, 'estimation': EstimationQuiz,
        'where': WhereQuiz, 'who': WhoQuiz, 'who_that': WhoThatQuiz,
        'blackjack': BlackJackQuiz, 'sorting_ladder': SortingLadderGame, 'clue_rush': ClueRushGame,
        'wer_weiss_mehr': WerWeissMehrGame,
    }
    model = model_map.get(game_key)
    if not model:
        return JsonResponse({'error': 'Unknown game type'}, status=400)
    instances = model.objects.all().order_by('-created_at')
    return JsonResponse({
        'instances': [
            {
                'id': g.id,
                'title': g.title,
                'internal_description': getattr(g, 'internal_description', ''),
                'room_code': g.room_code,
                'q_count': g.selected_questions.count(),
            }
            for g in instances
        ]
    })


@login_required
@require_POST
def reorder_steps(request, session_code):
    """Reorder game steps. Body: {order: [step_id, step_id, ...]} (list of step PKs in new order)."""
    try:
        data = json.loads(request.body)
        step_ids = data.get('order', [])
        if not isinstance(step_ids, list):
            return JsonResponse({'success': False, 'error': 'Order must be a list.'}, status=400)

        try:
            step_ids = [int(step_id) for step_id in step_ids]
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': 'Order contains invalid step ids.'}, status=400)

        session = get_object_or_404(HubSession, code=session_code)
        with transaction.atomic():
            steps = list(session.steps.select_for_update().order_by('order'))
            known_ids = {step.id for step in steps}

            if len(step_ids) != len(steps) or len(set(step_ids)) != len(step_ids) or set(step_ids) != known_ids:
                return JsonResponse({'success': False, 'error': 'Order does not match the session steps.'}, status=400)

            max_order = max((step.order for step in steps), default=-1)
            offset = max_order + len(steps) + 1
            session.steps.update(order=F('order') + offset)

            order_map = {step_id: new_order for new_order, step_id in enumerate(step_ids)}
            for step in steps:
                step.order = order_map[step.id]
                step.save(update_fields=['order'])

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@login_required
@require_POST
def activate_session_game(request, session_code):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    game_key = (data.get('game_key') or '').strip()
    room_code = (data.get('room_code') or '').strip()
    action = (data.get('action') or '').strip() or None
    check_only = bool(data.get('check_only'))

    if not game_key or not room_code:
        return JsonResponse({'success': False, 'error': 'Missing game_key or room_code'}, status=400)

    result = resolve_session_game_activation(
        session_code,
        game_key,
        room_code,
        action=action,
        check_only=check_only,
    )
    status = 409 if result.get('conflict') else 200
    return JsonResponse(result, status=status)


@login_required
@require_POST
def delete_step(request, session_code, step_id):
    """Delete a game step from a session and renumber remaining steps."""
    session = get_object_or_404(HubSession, code=session_code)
    step = get_object_or_404(HubGameStep, id=step_id, session=session)
    step.delete()
    # Renumber remaining steps
    for new_order, s in enumerate(session.steps.order_by('order')):
        if s.order != new_order:
            s.order = new_order
            s.save(update_fields=['order'])
    return JsonResponse({'success': True})


@require_POST
def submit_vote(request, session_code):
    """Participant submits a vote for the next game."""
    try:
        data = json.loads(request.body)
        nickname = data.get('nickname', '').strip()
        step_order = data.get('step_order')
        if not nickname or step_order is None:
            return JsonResponse({'success': False, 'error': 'Missing nickname or step_order'}, status=400)

        session = get_object_or_404(HubSession, code=session_code)
        step = get_object_or_404(HubGameStep, session=session, order=step_order)

        vote, created = GameVote.objects.update_or_create(
            session=session,
            participant_nickname=nickname,
            defaults={'step': step},
        )

        votes = _get_vote_counts(session)
        return JsonResponse({'success': True, 'created': created, 'votes': votes})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


def get_votes(request, session_code):
    """Return current vote counts for a session."""
    session = get_object_or_404(HubSession, code=session_code)
    votes = _get_vote_counts(session)
    return JsonResponse({'votes': votes})


@login_required
def session_lobby_presence_api(request, session_code):
    if request.method != 'GET':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    session = get_object_or_404(HubSession, code=session_code)
    presence = ensure_session_players_ready_for_game_start(session.code)
    return JsonResponse({'success': True, **presence})


@login_required
def session_recall_countdown_state_api(request, session_code):
    if request.method != 'GET':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    get_object_or_404(HubSession, code=session_code)
    return JsonResponse({
        'success': True,
        **get_lobby_return_countdown_state(session_code),
    })


@login_required
@require_POST
def start_recall_countdown(request, session_code):
    get_object_or_404(HubSession, code=session_code)
    countdown_state = broadcast_lobby_return_countdown_started(
        session_code,
        duration_seconds=LOBBY_RETURN_COUNTDOWN_SECONDS,
    )
    return JsonResponse({
        'success': True,
        **get_lobby_return_countdown_state(session_code),
        'duration_seconds': countdown_state['duration_seconds'],
        'ends_at': countdown_state['ends_at'],
    })


@login_required
@require_POST
def recall_session_participants_to_lobby(request, session_code):
    session = get_object_or_404(HubSession, code=session_code)
    presence = mark_session_game_participants_inactive(session.code)
    broadcast_players_recalled_to_lobby(session.code)
    return JsonResponse({'success': True, **presence})


@require_POST
def participant_return_to_lobby(request, session_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    game_key = (data.get('game_key') or '').strip()
    room_code = (data.get('room_code') or '').strip()
    participant_name = (data.get('participant_name') or '').strip()

    if not game_key or not room_code or not participant_name:
        return JsonResponse({'success': False, 'error': 'Missing participant return data.'}, status=400)

    result = mark_single_participant_inactive_for_lobby_return(
        session_code=session_code,
        game_key=game_key,
        room_code=room_code,
        participant_name=participant_name,
    )
    status = 200 if result.get('success') else 400
    return JsonResponse(result, status=status)


def _get_vote_counts(session):
    """Helper: return list of {step_order, game_key, title, count} sorted by count desc."""
    from django.db.models import Count
    steps = session.steps.all()
    vote_qs = GameVote.objects.filter(session=session).values('step_id').annotate(count=Count('id'))
    vote_map = {v['step_id']: v['count'] for v in vote_qs}
    result = []
    for step in steps:
        result.append({
            'step_order': step.order,
            'game_key': step.game_key,
            'title': step.title,
            'count': vote_map.get(step.id, 0),
        })
    result.sort(key=lambda x: x['step_order'])
    return result
