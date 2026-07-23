from __future__ import annotations

import math
import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.utils import timezone

from .models import HubGameStep, HubSession


LOBBY_RETURN_COUNTDOWN_SECONDS = 10
logger = logging.getLogger(__name__)


def _get_lobby_return_countdown_cache_key(session_code: str) -> str:
    return f'hub:lobby_return_countdown:{session_code}'


def start_lobby_return_countdown(session_code: str, duration_seconds: int = LOBBY_RETURN_COUNTDOWN_SECONDS) -> dict:
    safe_duration = max(1, int(duration_seconds or LOBBY_RETURN_COUNTDOWN_SECONDS))
    ends_at = timezone.now() + timedelta(seconds=safe_duration)
    payload = {
        'session_code': session_code,
        'duration_seconds': safe_duration,
        'ends_at': ends_at.isoformat(),
    }
    cache.set(
        _get_lobby_return_countdown_cache_key(session_code),
        payload,
        timeout=safe_duration + 30,
    )
    return payload


def clear_lobby_return_countdown(session_code: str):
    cache.delete(_get_lobby_return_countdown_cache_key(session_code))


def get_lobby_return_countdown_state(session_code: str) -> dict:
    payload = cache.get(_get_lobby_return_countdown_cache_key(session_code))
    if not payload:
        return {
            'active': False,
            'session_code': session_code,
            'duration_seconds': LOBBY_RETURN_COUNTDOWN_SECONDS,
            'remaining_seconds': 0,
            'ends_at': None,
            'server_now': timezone.now().isoformat(),
        }

    try:
        ends_at = timezone.datetime.fromisoformat(payload['ends_at'])
    except Exception:
        clear_lobby_return_countdown(session_code)
        return {
            'active': False,
            'session_code': session_code,
            'duration_seconds': LOBBY_RETURN_COUNTDOWN_SECONDS,
            'remaining_seconds': 0,
            'ends_at': None,
            'server_now': timezone.now().isoformat(),
        }

    if timezone.is_naive(ends_at):
        ends_at = timezone.make_aware(ends_at, timezone.get_current_timezone())

    now = timezone.now()
    remaining_seconds = max(0, math.ceil((ends_at - now).total_seconds()))
    if remaining_seconds <= 0:
        clear_lobby_return_countdown(session_code)
        return {
            'active': False,
            'session_code': session_code,
            'duration_seconds': int(payload.get('duration_seconds') or LOBBY_RETURN_COUNTDOWN_SECONDS),
            'remaining_seconds': 0,
            'ends_at': ends_at.isoformat(),
            'server_now': now.isoformat(),
        }

    return {
        'active': True,
        'session_code': session_code,
        'duration_seconds': int(payload.get('duration_seconds') or LOBBY_RETURN_COUNTDOWN_SECONDS),
        'remaining_seconds': remaining_seconds,
        'ends_at': ends_at.isoformat(),
        'server_now': now.isoformat(),
    }


def get_game_participant_model_map():
    from Assign.models import AssignParticipant
    from Estimation.models import EstimationParticipant
    from QuizGame.models import QuizParticipant
    from black_jack_quiz.models import BlackJackParticipant
    from clue_rush.models import ClueRushParticipant
    from sorting_ladder.models import SortingLadderParticipant
    from wer_weiss_mehr.models import WerWeissMehrParticipant
    from where_is_this.models import WhereParticipant
    from who_is_lying.models import WhoParticipant
    from who_is_that.models import WhoThatParticipant
    from buzzer.models import BuzzerParticipant
    from host_points.models import HostPointsParticipant
    from wann_war_das.models import WannWarDasParticipant

    return {
        'quiz': QuizParticipant,
        'assign': AssignParticipant,
        'estimation': EstimationParticipant,
        'where': WhereParticipant,
        'who': WhoParticipant,
        'who_that': WhoThatParticipant,
        'blackjack': BlackJackParticipant,
        'clue_rush': ClueRushParticipant,
        'sorting_ladder': SortingLadderParticipant,
        'wer_weiss_mehr': WerWeissMehrParticipant,
        'buzzer': BuzzerParticipant,
        'host_points': HostPointsParticipant,
        'wann_war_das': WannWarDasParticipant,
    }


def get_relevant_step_for_room(game_key: str, room_code: str) -> HubGameStep | None:
    qs = HubGameStep.objects.select_related('session').filter(game_key=game_key, room_code=room_code)
    active = qs.filter(session__is_active=True, session__ended_at__isnull=True).order_by('-id').first()
    if active:
        return active
    available = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
    return available or qs.order_by('-id').first()


def get_session_lobby_presence(session_code: str) -> dict:
    session = HubSession.objects.prefetch_related('steps', 'participants').get(code=session_code)
    participant_names = list(
        session.participants.order_by('joined_at').values_list('nickname', flat=True)
    )

    active_game_map: dict[str, list[dict[str, str]]] = {}
    participant_model_map = get_game_participant_model_map()

    for step in session.steps.exclude(room_code='').order_by('order'):
        participant_model = participant_model_map.get(step.game_key)
        if not participant_model:
            continue

        active_names = list(
            participant_model.objects.filter(
                quiz__room_code=step.room_code,
                hub_session_code=session.code,
                is_active=True,
            ).values_list('name', flat=True)
        )
        if not active_names:
            continue

        for name in active_names:
            active_game_map.setdefault(name, []).append({
                'game_key': step.game_key,
                'room_code': step.room_code,
                'title': step.title or step.get_game_key_display(),
            })

    participants_not_in_lobby = [
        {
            'name': name,
            'games': active_game_map.get(name, []),
        }
        for name in participant_names
        if name in active_game_map
    ]
    participants_in_lobby = [
        {'name': name}
        for name in participant_names
        if name not in active_game_map
    ]

    return {
        'session_code': session.code,
        'total_participants': len(participant_names),
        'all_in_lobby': len(participants_not_in_lobby) == 0,
        'in_lobby_count': len(participants_in_lobby),
        'not_in_lobby_count': len(participants_not_in_lobby),
        'participants_in_lobby': participants_in_lobby,
        'participants_not_in_lobby': participants_not_in_lobby,
    }


def ensure_session_players_ready_for_game_start(session_code: str) -> dict:
    presence = get_session_lobby_presence(session_code)
    if presence['all_in_lobby']:
        return {
            'allowed': True,
            **presence,
        }

    return {
        'allowed': False,
        'message': 'Noch nicht alle Teilnehmer sind in der Lobby.',
        **presence,
    }


def ensure_session_players_ready_for_game_start_for_room(
    game_key: str,
    room_code: str,
    session_code: str | None = None,
) -> dict:
    if session_code:
        return ensure_session_players_ready_for_game_start(session_code)
    step = get_relevant_step_for_room(game_key, room_code)
    if not step:
        return {'allowed': True, 'all_in_lobby': True, 'total_participants': 0, 'not_in_lobby_count': 0}
    return ensure_session_players_ready_for_game_start(step.session.code)


def mark_session_game_participants_inactive(session_code: str) -> dict:
    session = HubSession.objects.prefetch_related('steps').get(code=session_code)
    participant_model_map = get_game_participant_model_map()

    for step in session.steps.exclude(room_code='').order_by('order'):
        participant_model = participant_model_map.get(step.game_key)
        if not participant_model:
            continue
        updated_count = participant_model.objects.filter(
            quiz__room_code=step.room_code,
            hub_session_code=session.code,
            is_active=True,
        ).update(is_active=False)
        if updated_count:
            logger.info(
                'Session game participants returned to lobby',
                extra={
                    'hub_session_code': session.code,
                    'game_key': step.game_key,
                    'room_code': step.room_code,
                    'participants_updated': updated_count,
                    'cleanup_at': timezone.now().isoformat(),
                },
            )

    return get_session_lobby_presence(session.code)


def mark_single_participant_inactive_for_lobby_return(
    session_code: str,
    game_key: str,
    room_code: str,
    participant_name: str,
) -> dict:
    step = HubGameStep.objects.select_related('session').filter(
        session__code=session_code,
        game_key=game_key,
        room_code=room_code,
    ).first()
    if not step:
        return {'success': False, 'error': 'Spiel der Session nicht gefunden.'}

    participant_model = get_game_participant_model_map().get(game_key)
    if not participant_model:
        return {'success': False, 'error': 'Unbekannter Spieltyp.'}

    updated_count = participant_model.objects.filter(
        quiz__room_code=room_code,
        hub_session_code=session_code,
        name=participant_name,
        is_active=True,
    ).update(is_active=False)
    logger.info(
        'Single game participant returned to lobby',
        extra={
            'hub_session_code': session_code,
            'game_key': game_key,
            'room_code': room_code,
            'participant_name': participant_name,
            'participants_updated': updated_count,
            'cleanup_at': timezone.now().isoformat(),
        },
    )

    return {
        'success': True,
        'presence': get_session_lobby_presence(session_code),
    }


def broadcast_players_recalled_to_lobby(session_code: str):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    clear_lobby_return_countdown(session_code)

    async_to_sync(channel_layer.group_send)(
        f'hub_{session_code}',
        {
            'type': 'players_recalled_to_lobby',
            'session_code': session_code,
        },
    )


def broadcast_lobby_return_countdown_started(
    session_code: str,
    duration_seconds: int = LOBBY_RETURN_COUNTDOWN_SECONDS,
):
    channel_layer = get_channel_layer()
    countdown_state = start_lobby_return_countdown(session_code, duration_seconds=duration_seconds)
    if not channel_layer:
        return countdown_state

    async_to_sync(channel_layer.group_send)(
        f'hub_{session_code}',
        {
            'type': 'lobby_return_countdown_started',
            'session_code': session_code,
            'duration_seconds': countdown_state['duration_seconds'],
            'ends_at': countdown_state['ends_at'],
            'server_now': timezone.now().isoformat(),
        },
    )
    return countdown_state
