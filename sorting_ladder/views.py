from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
import json

from .models import SortingLadderGame, SortingLadderParticipant
from games_hub.unit_tutorial_runtime import is_current_unit_tutorial_question


def join_view(request):
    """Combined view for Sorting Ladder join page (GET) and join action (POST)."""
    if request.method == 'GET':
        return render(request, 'sorting_ladder/join.html')

    # POST: join game
    try:
        data = json.loads(request.body or '{}')
        participant_name = (data.get('participant_name') or '').strip()
        room_code = (data.get('room_code') or '').strip()
        hub_session = (data.get('hub_session') or '').strip() or None

        if not participant_name or not room_code:
            return JsonResponse({'success': False, 'error': 'Name and room code are required.'})

        if len(participant_name) > 50:
            return JsonResponse({'success': False, 'error': 'Name must be 50 characters or less.'})

        if len(room_code) != 4 or not room_code.isdigit():
            return JsonResponse({'success': False, 'error': 'Room code must be exactly 4 digits.'})

        try:
            quiz = SortingLadderGame.objects.get(room_code=room_code)
        except SortingLadderGame.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Game not found. Please check the room code.'})

        # Only allow joining waiting or active games
        if quiz.status not in ['waiting', 'active', 'inactive']:
            return JsonResponse({'success': False, 'error': 'This game is no longer accepting participants.'})

        # Participant limit (optionally scoped by hub_session)
        if hub_session:
            current_count = quiz.participants.filter(hub_session_code=hub_session).count()
        else:
            current_count = quiz.participants.count()
        if current_count >= quiz.max_participants:
            return JsonResponse({'success': False, 'error': 'This game is full. Maximum participants reached.'})

        # Enforce unique name per game (and per hub session if provided)
        name_qs = quiz.participants.filter(name__iexact=participant_name)
        if hub_session:
            name_qs = name_qs.filter(hub_session_code=hub_session)
        if name_qs.exists():
            return JsonResponse({'success': False, 'error': 'This name is already taken in this game.'})

        if hub_session:
            participant, created = SortingLadderParticipant.objects.get_or_create(
                quiz=quiz,
                name=participant_name,
                hub_session_code=hub_session,
                defaults={'is_active': True},
            )
        else:
            participant, created = SortingLadderParticipant.objects.get_or_create(
                quiz=quiz,
                name=participant_name,
                defaults={'is_active': True},
            )

        if not created:
            participant.is_active = True
            if hub_session and not participant.hub_session_code:
                participant.hub_session_code = hub_session
            participant.save()

        return JsonResponse({'success': True, 'participant_id': participant.id, 'game_status': quiz.status})

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'})
    except Exception:
        return JsonResponse({'success': False, 'error': 'An error occurred. Please try again.'})


def check_room_code(request, room_code):
    """Check if room code is valid and return game info."""
    try:
        quiz = SortingLadderGame.objects.get(room_code=room_code)
        if quiz.status not in ['waiting', 'active', 'inactive']:
            return JsonResponse({'success': False, 'error': 'This game is no longer accepting participants.'})
        return JsonResponse({
            'success': True,
            'quiz': {
                'id': quiz.id,
                'title': quiz.title,
                'room_code': quiz.room_code,
                'status': quiz.get_status_display(),
                'participant_count': quiz.participants.count(),
                'max_participants': quiz.max_participants,
            },
        })
    except SortingLadderGame.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Invalid room code. Please check and try again.'})


def play(request, room_code, participant_name):
    """Sorting Ladder play page for participants."""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)
        participant = get_object_or_404(
            SortingLadderParticipant,
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code,
        )

        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()

        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.participants.filter(hub_session_code=session_code, is_active=True).count(),
            'current_unit_is_tutorial': is_current_unit_tutorial_question('sorting_ladder', quiz.room_code, session_code, quiz.current_question_id),
        }
        return render(request, 'sorting_ladder/play.html', context)
    except (SortingLadderGame.DoesNotExist, SortingLadderParticipant.DoesNotExist):
        return redirect('sorting_ladder:join')


def get_game_status(request, room_code, participant_name):
    """Get current Sorting Ladder status for a participant."""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)
        participant = get_object_or_404(
            SortingLadderParticipant,
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code,
        )

        participant.last_activity = timezone.now()
        participant.save()

        status_data = {
            'game_status': quiz.status,
            'rounds_survived': participant.rounds_survived,
            'is_eliminated': participant.is_eliminated,
        }

        return JsonResponse({'success': True, **status_data})

    except (SortingLadderGame.DoesNotExist, SortingLadderParticipant.DoesNotExist):
        return JsonResponse({'success': False, 'error': 'Participant not found.'})


def leave_game(request, room_code, participant_name):
    """Mark a participant as having left the Sorting Ladder game."""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)
        participant = get_object_or_404(
            SortingLadderParticipant,
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code,
        )
        participant.is_active = False
        participant.save()
        return JsonResponse({'success': True})
    except (SortingLadderGame.DoesNotExist, SortingLadderParticipant.DoesNotExist):
        return JsonResponse({'success': False, 'error': 'Participant not found.'})


def api_participants(request, room_code):
    """Public API: get active participants and their rounds survived."""
    try:
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)
        participants = quiz.participants.filter(is_active=True).order_by('-rounds_survived', 'name')
        data = [
            {
                'name': p.name,
                'rounds_survived': p.rounds_survived,
                'is_eliminated': p.is_eliminated,
            }
            for p in participants
        ]
        return JsonResponse({'success': True, 'participants': data, 'count': len(data)})
    except SortingLadderGame.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Game not found.'})


def api_leaderboard(request, room_code):
    """Public API: simple leaderboard ordered by rounds survived."""
    try:
        quiz = get_object_or_404(SortingLadderGame, room_code=room_code)
        participants = quiz.participants.all().order_by('-rounds_survived', 'name')[:10]
        data = [
            {
                'rank': idx,
                'name': p.name,
                'rounds_survived': p.rounds_survived,
                'is_eliminated': p.is_eliminated,
            }
            for idx, p in enumerate(participants, 1)
        ]
        return JsonResponse({'success': True, 'leaderboard': data})
    except SortingLadderGame.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Game not found.'})
