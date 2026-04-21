from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
import json
from .models import ClueRushGame, ClueRushParticipant


def join_view(request):
    """Combined view for Clue Rush join page (GET) and join action (POST)."""
    if request.method == 'GET':
        return render(request, 'clue_rush/join.html')

    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            print("data ", data)
            participant_name = data.get('participant_name', '').strip()
            room_code = data.get('room_code', '').strip()
            hub_session = (data.get('hub_session') or '').strip() or None

            if not participant_name or not room_code:
                return JsonResponse({'success': False, 'error': 'Name and room code are required.'})

            if len(participant_name) > 50:
                return JsonResponse({'success': False, 'error': 'Name must be 50 characters or less.'})

            if len(room_code) != 4 or not room_code.isdigit():
                return JsonResponse({'success': False, 'error': 'Room code must be exactly 4 digits.'})

            try:
                quiz = ClueRushGame.objects.get(room_code=room_code)
            except ClueRushGame.DoesNotExist:
                return JsonResponse({'success': False, 'error': 'Game not found. Please check the room code.'})

            if quiz.status not in ['waiting', 'active', 'inactive']:
                return JsonResponse({'success': False, 'error': 'This quiz is no longer accepting participants.'})

            if hub_session:
                current_count = quiz.participants.filter(hub_session_code=hub_session).count()
            else:
                current_count = quiz.participants.count()
            if current_count >= quiz.max_participants:
                return JsonResponse({'success': False, 'error': 'This quiz is full. Maximum participants reached.'})

            name_qs = quiz.participants.filter(name__iexact=participant_name)
            if hub_session:
                name_qs = name_qs.filter(hub_session_code=hub_session)
            if name_qs.exists():
                return JsonResponse({'success': False, 'error': 'This name is already taken in this quiz.'})

            if hub_session:
                participant, created = ClueRushParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': True}
                )
            else:
                participant, created = ClueRushParticipant.objects.get_or_create(
                    quiz=quiz,
                    name=participant_name,
                    defaults={'is_active': True}
                )

            if not created:
                participant.is_active = True
                if hub_session and not participant.hub_session_code:
                    participant.hub_session_code = hub_session
                participant.save()

            return JsonResponse({'success': True, 'participant_id': participant.id, 'game_status': quiz.status})

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid request format.'})
        except Exception as e:
            print(e)
            return JsonResponse({'success': False, 'error': 'An error occurred. Please try again.'})


def check_room_code(request, room_code):
    """Check if room code is valid and return quiz info."""
    try:
        quiz = ClueRushGame.objects.get(room_code=room_code)
        if quiz.status not in ['waiting', 'active', 'inactive']:
            return JsonResponse({'success': False, 'error': 'This quiz is no longer accepting participants.'})
        return JsonResponse({
            'success': True,
            'quiz': {
                'id': quiz.id,
                'title': quiz.title,
                'room_code': quiz.room_code,
                'status': quiz.get_status_display(),
                'participant_count': quiz.participants.count(),
                'max_participants': quiz.max_participants,
            }
        })
    except ClueRushGame.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Invalid room code. Please check and try again.'})


def play(request, room_code, participant_name):
    """Clue Rush play page for participants."""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        participant = get_object_or_404(
            ClueRushParticipant,
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code
        )

        participant.is_active = True
        participant.last_activity = timezone.now()
        participant.save()

        context = {
            'quiz': quiz,
            'participant': participant,
            'hub_session': session_code,
            'participant_count': quiz.participants.filter(hub_session_code=session_code, is_active=True).count()
        }
        return render(request, 'clue_rush/play.html', context)
    except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
        return redirect('clue_rush:join')


def result(request, room_code, participant_name):
    try:
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        participant = get_object_or_404(ClueRushParticipant, quiz=quiz, name=participant_name)
        context = {
            'quiz': quiz,
            'participant': participant,
        }
        return render(request, 'clue_rush/result.html', context)
    except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
        return redirect('clue_rush:join')


@require_POST
@csrf_exempt
def submit_guess(request, room_code, participant_name):
    """Submit a single guess. Points follow: total_clues - current_round + 1 when correct."""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        participant = get_object_or_404(
            ClueRushParticipant,
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code
        )

        if quiz.status != 'active':
            return JsonResponse({'success': False, 'error': 'No active quiz.'})

        if participant.has_guessed:
            return JsonResponse({'success': False, 'error': 'You have already submitted your guess.'})

        data = json.loads(request.body)
        guess_text = data.get('guess', '').strip()
        if not guess_text:
            return JsonResponse({'success': False, 'error': 'Guess cannot be empty.'})

        participant.has_guessed = True
        participant.guess_text = guess_text
        correct = guess_text.lower() == quiz.answer_text.strip().lower()
        participant.guess_correct = correct
        points = 0
        if correct:
            points = max(0, quiz.clues.count() - quiz.current_round + 1)
            participant.points_earned = points
            participant.total_score += points
        participant.last_activity = timezone.now()
        participant.save()

        return JsonResponse({'success': True, 'correct': correct, 'points_earned': points})

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'})
    except Exception:
        return JsonResponse({'success': False, 'error': 'An error occurred while submitting your guess.'})


def get_game_status(request, room_code, participant_name):
    """Get current quiz status for participant."""
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        participant = get_object_or_404(
            ClueRushParticipant,
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code
        )

        participant.last_activity = timezone.now()
        participant.save()

        status_data = {
            'game_status': quiz.status,
            'current_round': quiz.current_round,
            'total_clues': quiz.clues.count(),
            'participant_score': participant.total_score,
            'has_guessed': participant.has_guessed,
        }

        return JsonResponse({'success': True, **status_data})

    except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
        return JsonResponse({'success': False, 'error': 'Participant not found.'})


def leave_game(request, room_code, participant_name):
    try:
        session_code = request.GET.get('hub_session')
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        participant = get_object_or_404(
            ClueRushParticipant,
            quiz=quiz,
            name=participant_name,
            hub_session_code=session_code
        )
        participant.is_active = False
        participant.save()
        return JsonResponse({'success': True})
    except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
        return JsonResponse({'success': False, 'error': 'Participant not found.'})


def api_participants(request, room_code):
    try:
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        participants = quiz.participants.filter(is_active=True).order_by('-total_score', 'name')
        data = [
            {
                'name': p.name,
                'total_score': p.total_score,
                'has_guessed': p.has_guessed,
                'points_earned': p.points_earned,
            }
            for p in participants
        ]
        return JsonResponse({'success': True, 'participants': data, 'count': len(data)})
    except ClueRushGame.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Game not found.'})


def api_leaderboard(request, room_code):
    try:
        quiz = get_object_or_404(ClueRushGame, room_code=room_code)
        participants = quiz.participants.all().order_by('-total_score', 'name')[:10]
        data = [
            {
                'rank': idx,
                'name': p.name,
                'total_score': p.total_score,
                'has_guessed': p.has_guessed,
                'points_earned': p.points_earned,
            }
            for idx, p in enumerate(participants, 1)
        ]
        return JsonResponse({'success': True, 'leaderboard': data})
    except ClueRushGame.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Game not found.'})
