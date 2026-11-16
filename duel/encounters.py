"""Read-only routing for explicit duels inside Multiplayer sessions."""
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django.views.decorators.vary import vary_on_headers
from games_hub.host_permissions import user_can_manage_hub_session
from games_hub.lobby_join import REJOIN_TOKEN_SALT
from games_hub.models import HubSession, HubParticipant
from .models import DuelGameInstance
from .match_service import get_match_snapshot
from .views import _errors


def page_url(instance, role):
    name = 'match_page' if instance.game_key == 'estimation' else instance.game_key+'_match_page'
    return reverse('duel:'+name, args=[instance.hub_session.code, instance.pk, role])


def presentation_finished(instance):
    unit = instance.units.order_by('-number').first()
    if unit is None:
        return instance.match_status == 'aborted'
    for attr in ('estimation_round_state', 'quiz_round_state', 'oral_round', 'turn_round', 'input_round', 'control_round'):
        state = getattr(unit, attr, None)
        if state is not None:
            return state.phase == 'completed'
    return False


def active_encounter(session):
    for instance in DuelGameInstance.objects.filter(hub_session=session, hub_game_step__game_format='duel').select_related('hub_session', 'hub_game_step').order_by('-created_at'):
        if instance.match_status in {'not_started', 'in_progress'} or (instance.match_status == 'completed' and not presentation_finished(instance)):
            return instance
    return None


def participant_identity(request, session):
    header = request.headers.get('Authorization', '').split()
    if len(header) != 2 or header[0].lower() != 'bearer':
        raise PermissionDenied
    try:
        identity = signing.loads(header[1], salt=REJOIN_TOKEN_SALT)
    except signing.BadSignature as exc:
        raise PermissionDenied from exc
    if (type(identity) is not dict or set(identity) != {'session_id', 'participant_id'}
            or any(type(value) is not int for value in identity.values()) or identity['session_id'] != session.pk):
        raise PermissionDenied
    return get_object_or_404(HubParticipant, session=session, pk=identity['participant_id'])


@never_cache
@vary_on_headers('Authorization', 'Cookie')
@require_GET
@_errors
def routing(request, session_code, role):
    session = get_object_or_404(HubSession, code=session_code, session_type='multiplayer')
    if role == 'host':
        if not user_can_manage_hub_session(request.user, session): raise PermissionDenied
        participant = None
    elif role == 'participant': participant = participant_identity(request, session)
    elif role == 'spectator': participant = None
    else: raise PermissionDenied
    current = active_encounter(session) if session.is_active and not session.ended_at else None
    from .tournaments import active_tournament, page_url as tournament_url
    tournament = active_tournament(session) if session.is_active and not session.ended_at else None
    if tournament:
        return JsonResponse({'url': tournament_url(tournament, role), 'spectator_url': tournament_url(tournament, 'spectator'),
            'can_start': False, 'participants': []})
    eligible = current and (role != 'participant' or participant.pk in (current.participant_a_id, current.participant_b_id))
    data = {'url': page_url(current, role) if eligible else None,
            'spectator_url': page_url(current, 'spectator') if current else None}
    if role == 'host':
        data['can_start'] = bool(session.is_active and session.started_at and not session.ended_at and session.check_in_completed)
        data['participants'] = list(session.participants.filter(is_active=True, left_permanently_at__isnull=True).exclude(check_in_excluded_by_host=True).values('id', 'nickname'))
    if role in {'host', 'spectator'}:
        latest = session.duel_instances.filter(match_status='completed', hub_game_step__game_format='duel').select_related('participant_a', 'participant_b').order_by('-created_at').first()
        if latest:
            snapshot = get_match_snapshot(latest.pk)
            data['result'] = {'draw': snapshot['draw'], 'winner': latest.participant_a.nickname if snapshot['winner_id'] == latest.participant_a_id else latest.participant_b.nickname if snapshot['winner_id'] == latest.participant_b_id else None,
                              'url': page_url(latest, role)}
    return JsonResponse(data)
