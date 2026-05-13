from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import HubGameStep, HubSession


def get_game_participant_model_map():
    from Assign.models import AssignParticipant
    from Estimation.models import EstimationParticipant
    from QuizGame.models import QuizParticipant
    from black_jack_quiz.models import BlackJackParticipant
    from clue_rush.models import ClueRushParticipant
    from sorting_ladder.models import SortingLadderParticipant
    from where_is_this.models import WhereParticipant
    from who_is_lying.models import WhoParticipant
    from who_is_that.models import WhoThatParticipant

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
        participant_model.objects.filter(
            quiz__room_code=step.room_code,
            hub_session_code=session.code,
            is_active=True,
        ).update(is_active=False)

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

    participant_model.objects.filter(
        quiz__room_code=room_code,
        hub_session_code=session_code,
        name=participant_name,
        is_active=True,
    ).update(is_active=False)

    return {
        'success': True,
        'presence': get_session_lobby_presence(session_code),
    }


def broadcast_players_recalled_to_lobby(session_code: str):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f'hub_{session_code}',
        {
            'type': 'players_recalled_to_lobby',
            'session_code': session_code,
        },
    )
