import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from games_hub.views import get_post_game_results

from .models import WannWarDasGame, WannWarDasParticipant


def _json_body(request):
    try:
        return json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return {}


def join_view(request):
    error = None
    if request.method == 'POST':
        data = _json_body(request) if request.content_type == 'application/json' else request.POST
        room_code = (data.get('room_code') or '').strip()
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip()
        game = WannWarDasGame.objects.filter(room_code=room_code).first()
        if not game:
            error = 'Spiel nicht gefunden.'
        elif not participant_name:
            error = 'Name ist erforderlich.'
        else:
            participant, _ = WannWarDasParticipant.objects.get_or_create(
                quiz=game,
                name=participant_name,
                hub_session_code=hub_session,
                defaults={'is_active': True},
            )
            if request.content_type == 'application/json':
                return JsonResponse({'success': True, 'participant_id': participant.id})
            return redirect('wann_war_das:play', room_code=room_code, participant_name=participant_name)
        if request.content_type == 'application/json':
            return JsonResponse({'success': False, 'error': error}, status=400)
    return render(request, 'wann_war_das/join.html', {'error': error})


def play_view(request, room_code, participant_name):
    game = get_object_or_404(WannWarDasGame, room_code=room_code)
    hub_session = (request.GET.get('hub_session') or '').strip()
    participant, _ = WannWarDasParticipant.objects.get_or_create(
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
    return render(request, 'wann_war_das/play.html', {
        'game': game,
        'participant': participant,
        'hub_session': hub_session,
        'state': game.serialize_state(hub_session, participant.name),
    })


def result_view(request, room_code, participant_name):
    game = get_object_or_404(WannWarDasGame, room_code=room_code)
    hub_session = (request.GET.get('hub_session') or '').strip()
    participant = game.participants.filter(name=participant_name, hub_session_code=hub_session).first()
    post_game_results = None
    if hub_session:
        from games_hub.models import HubGameStep

        step = HubGameStep.objects.filter(
            session__code=hub_session,
            game_key='wann_war_das',
            room_code=room_code,
        ).select_related('session').first()
        if step:
            post_game_results = get_post_game_results(step.session, step, participant_name)
    return render(request, 'wann_war_das/result.html', {
        'game': game,
        'participant': participant,
        'hub_session': hub_session,
        'post_game_results': post_game_results,
    })


def state_view(request, room_code):
    game = get_object_or_404(WannWarDasGame, room_code=room_code)
    participant_name = (request.GET.get('participant_name') or request.GET.get('name') or '').strip()
    hub_session = (request.GET.get('hub_session') or '').strip()
    return JsonResponse(game.serialize_state(hub_session, participant_name), json_dumps_params={'ensure_ascii': False})


@require_POST
def submit_answer(request, room_code, participant_name):
    game = get_object_or_404(WannWarDasGame, room_code=room_code)
    data = _json_body(request)
    hub_session = (data.get('hub_session') or request.GET.get('hub_session') or '').strip()
    participant = game.participants.filter(name=participant_name, hub_session_code=hub_session).first()
    answer, error = game.submit_answer(participant, data.get('answer'))
    if not answer:
        return JsonResponse({'success': False, 'error': error or 'Antwort konnte nicht gespeichert werden.'}, status=400)
    return JsonResponse({
        'success': True,
        'answer': answer.to_dict(reveal=False),
        'state': game.serialize_state(hub_session, participant_name),
    }, json_dumps_params={'ensure_ascii': False})
