import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from games_hub.views import get_post_game_results

from .models import BuzzerGame, BuzzerParticipant


def _json_body(request):
    try:
        return json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return {}


def _participant_name_from_request(request):
    data = _json_body(request)
    return (
        data.get('participant_name')
        or data.get('name')
        or request.POST.get('participant_name')
        or request.POST.get('name')
        or ''
    ).strip()


def buzzer_join_view(request):
    error = None
    if request.method == 'POST':
        data = _json_body(request) if request.content_type == 'application/json' else request.POST
        room_code = (data.get('room_code') or '').strip()
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip()

        if not room_code or not participant_name:
            error = 'Room-Code und Name sind erforderlich.'
        else:
            game = BuzzerGame.objects.filter(room_code=room_code).first()
            if not game:
                error = 'Buzzer-Spiel nicht gefunden.'
            else:
                participant, _ = BuzzerParticipant.objects.get_or_create(
                    quiz=game,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': False},
                )
                if request.content_type == 'application/json':
                    return JsonResponse({
                        'success': True,
                        'participant_id': participant.id,
                        'redirect_url': f'/buzzer/play/{room_code}/{participant_name}/',
                    })
                return redirect('buzzer:play', room_code=room_code, participant_name=participant_name)

        if request.content_type == 'application/json':
            return JsonResponse({'success': False, 'error': error}, status=400)

    return render(request, 'buzzer/join.html', {'error': error})


@require_http_methods(["GET"])
def buzzer_play(request, room_code, participant_name):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    hub_session = (request.GET.get('hub_session') or '').strip()
    participant, _ = BuzzerParticipant.objects.get_or_create(
        quiz=game,
        name=participant_name,
        hub_session_code=hub_session,
        defaults={'is_active': False},
    )
    participant.is_active = bool(
        game.status == 'active'
        and (
            not game.active_hub_session_code
            or game.active_hub_session_code == hub_session
        )
    )
    participant.last_activity = timezone.now()
    participant.save(update_fields=['is_active', 'last_activity', 'updated_at'])
    state = game.serialize_state(hub_session or None, participant.name)
    return render(request, 'buzzer/play.html', {
        'game': game,
        'participant': participant,
        'hub_session': hub_session,
        'state': state,
    })


@require_http_methods(["GET"])
def buzzer_result(request, room_code, participant_name):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    hub_session = (request.GET.get('hub_session') or '').strip()
    participant = game.participants.filter(name=participant_name, hub_session_code=hub_session).first()
    post_game_results = None
    if hub_session:
        from games_hub.models import HubGameStep, HubSession

        step = HubGameStep.objects.filter(
            session__code=hub_session,
            game_key='buzzer',
            room_code=room_code,
        ).select_related('session').first()
        if step:
            post_game_results = get_post_game_results(step.session, step, participant_name)
    return render(request, 'buzzer/result.html', {
        'game': game,
        'participant': participant,
        'hub_session': hub_session,
        'post_game_results': post_game_results,
    })


@require_http_methods(["GET"])
def buzzer_state(request, room_code):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    participant_name = (request.GET.get('participant_name') or request.GET.get('name') or '').strip()
    hub_session = (request.GET.get('hub_session') or '').strip()
    return JsonResponse(game.serialize_state(hub_session or None, participant_name), json_dumps_params={'ensure_ascii': False})


@require_POST
def buzzer_buzz(request, room_code):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    participant_name = _participant_name_from_request(request)
    hub_session = (_json_body(request).get('hub_session') or request.POST.get('hub_session') or '').strip()
    participant = game.participants.filter(name=participant_name, hub_session_code=hub_session).first()
    success, result = game.accept_buzz(participant)
    status = 200 if success else 400
    return JsonResponse({
        'success': success,
        'message': result if isinstance(result, str) else 'Buzz akzeptiert.',
        'state': game.serialize_state(hub_session or None, participant_name),
    }, status=status, json_dumps_params={'ensure_ascii': False})
