from games_hub.team_names import validate_join_name
import json
from urllib.parse import urlencode
from django.urls import reverse
from games_hub import manual_game_runtime as manual

from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from games_hub.views import get_post_game_results
from games_hub.host_permissions import authorize_game_host

from .models import HostPointsGame, HostPointsParticipant


def _json_body(request):
    try:
        value = json.loads(request.body or '{}')
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


@validate_join_name('host_points', 'host_points/join.html')
def host_points_join_view(request):
    error = None
    if request.method == 'POST':
        data = _json_body(request) if request.content_type == 'application/json' else request.POST
        room_code = (data.get('room_code') or '').strip()
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip()

        if not room_code or not participant_name:
            error = 'Room-Code und Name sind erforderlich.'
        else:
            game = HostPointsGame.objects.filter(room_code=room_code).first()
            if not game:
                error = 'Host-Punktevergabe nicht gefunden.'
            else:
                participant, created = HostPointsParticipant.objects.get_or_create(
                    quiz=game,
                    name=participant_name,
                    hub_session_code=hub_session,
                    defaults={'is_active': False},
                )
                capability = manual.page_token(request, participant, created=created)
                if not hub_session and not capability:
                    return JsonResponse({'success': False, 'error': 'Dieser Name ist bereits vergeben.'}, status=403)
                destination = reverse('host_points:play', args=[room_code, participant_name])
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

    return render(request, 'host_points/join.html', {'error': error})


@require_http_methods(["GET"])
def host_points_play(request, room_code, participant_name):
    game = get_object_or_404(HostPointsGame, room_code=room_code)
    hub_session = (request.GET.get('hub_session') or '').strip()
    participant, created = HostPointsParticipant.objects.get_or_create(
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
    return render(request, 'host_points/play.html', {
        'game': game,
        'participant': participant,
        'hub_session': hub_session,
        'state': state,
        'player_token': token,
    })


@require_http_methods(["GET"])
def host_points_result(request, room_code, participant_name):
    game = get_object_or_404(HostPointsGame, room_code=room_code)
    hub_session = (request.GET.get('hub_session') or '').strip()
    participant = game.participants.filter(name=participant_name, hub_session_code=hub_session).first()
    post_game_results = None
    if hub_session:
        from games_hub.models import HubGameStep

        step = HubGameStep.objects.filter(
            session__code=hub_session,
            game_key='host_points',
            room_code=room_code,
        ).select_related('session').first()
        if step:
            post_game_results = get_post_game_results(step.session, step, participant_name)
    return render(request, 'host_points/result.html', {
        'game': game,
        'participant': participant,
        'hub_session': hub_session,
        'post_game_results': post_game_results,
    })


@require_http_methods(["GET"])
def host_points_state(request, room_code):
    game = get_object_or_404(HostPointsGame, room_code=room_code)
    participant_name = (request.GET.get('participant_name') or request.GET.get('name') or '').strip()
    hub_session = (request.GET.get('hub_session') or '').strip()
    if participant_name:
        try:
            manual.identify_player(game, participant_name, hub_session, request.GET.get('player_token'))
        except manual.ActionError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    return JsonResponse(game.serialize_state(hub_session or None, participant_name), json_dumps_params={'ensure_ascii': False})


@require_POST
@login_required
def host_points_adjust_score(request, room_code):
    game = get_object_or_404(HostPointsGame, room_code=room_code)
    if not request.user.is_superuser and game.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    data = _json_body(request)
    authorization = authorize_game_host(
        request.user,
        'host_points',
        room_code,
        data.get('hub_session') or data.get('hub_session_code'),
    )
    if not authorization.allowed:
        return JsonResponse({
            'success': False,
            'error': authorization.message,
            'code': authorization.code,
        }, status=403)
    try:
        code = data.get('hub_session', data.get('hub_session_code', game.active_hub_session_code))
        success, result = manual.perform(game, {**data, 'type': 'admin_adjust_score'}, code,
            lambda locked: locked.adjust_score(data.get('participant_id'), data.get('delta')))
        if success:
            manual.broadcast(game, 'score_adjusted')
        return JsonResponse({'success': success, 'message': 'Punkte gespeichert.' if success else str(result),
            'state': game.serialize_state(code)}, status=200 if success else 400)
    except (ValueError, TypeError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=409)
