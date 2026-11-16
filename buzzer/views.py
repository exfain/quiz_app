from games_hub.team_names import validate_join_name
import json
from urllib.parse import urlencode
from django.urls import reverse
from games_hub import manual_game_runtime as manual

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from games_hub.views import get_post_game_results

from .models import BuzzerGame, BuzzerParticipant


def _json_body(request):
    try:
        value = json.loads(request.body or '{}')
        return value if isinstance(value, dict) else {}
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


@validate_join_name('buzzer', 'buzzer/join.html')
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
                participant, created = BuzzerParticipant.objects.get_or_create(
                    quiz=game,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': False},
                )
                capability = manual.page_token(request, participant, created=created)
                if not hub_session and not capability:
                    return JsonResponse({'success': False, 'error': 'Dieser Name ist bereits vergeben.'}, status=403)
                destination = reverse('buzzer:play', args=[room_code, participant_name])
                if hub_session:
                    destination += '?' + urlencode({'hub_session': hub_session})
                if request.content_type == 'application/json':
                    return JsonResponse({
                        'success': True,
                        'participant_id': participant.id,
                        'redirect_url': destination,
                    })
                return redirect(destination)

        if request.content_type == 'application/json':
            return JsonResponse({'success': False, 'error': error}, status=400)

    return render(request, 'buzzer/join.html', {'error': error})


@require_http_methods(["GET"])
def buzzer_play(request, room_code, participant_name):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    hub_session = (request.GET.get('hub_session') or '').strip()
    participant, created = BuzzerParticipant.objects.get_or_create(
        quiz=game,
        name=participant_name,
        hub_session_code=hub_session,
        defaults={'is_active': False},
    )
    token = manual.page_token(request, participant, created=created)
    if not hub_session and not token:
        return JsonResponse({'success': False, 'error': 'Teilnahme nicht bestaetigt.'}, status=403)
    if token and game.status == 'active' and game.active_hub_session_code == hub_session:
        participant.is_active = manual.eligible_player(game, participant)
    elif game.status != 'inactive':
        participant.is_active = False
    participant.last_activity = timezone.now()
    participant.save(update_fields=['is_active', 'last_activity', 'updated_at'])
    state = game.serialize_state(hub_session or None, participant.name)
    return render(request, 'buzzer/play.html', {
        'game': game,
        'participant': participant,
        'hub_session': hub_session,
        'state': state,
        'player_token': token,
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
    if participant_name:
        try:
            manual.identify_player(game, participant_name, hub_session, request.GET.get('player_token'))
        except manual.ActionError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    return JsonResponse(game.serialize_state(hub_session or None, participant_name), json_dumps_params={'ensure_ascii': False})


@require_POST
def buzzer_buzz(request, room_code):
    game = get_object_or_404(BuzzerGame, room_code=room_code)
    try:
        data = _json_body(request)
        code = data.get('hub_session', '')
        player = manual.identify_player(game, data.get('participant_name') or data.get('name'), code, data.get('player_token'))
        success, result = manual.perform(game, {**data, 'type': 'participant_buzz'}, code,
            lambda locked: locked.accept_buzz(player), actor=player.name)
        if success:
            manual.broadcast(game, 'buzz_accepted')
        return JsonResponse({'success': success, 'message': 'Buzz akzeptiert.' if success else str(result),
            'state': game.serialize_state(code, player.name)}, status=200 if success else 409)
    except (ValueError, TypeError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403 if getattr(exc, 'code', '') == 'unauthorized' else 409)
