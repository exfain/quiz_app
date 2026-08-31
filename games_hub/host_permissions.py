from dataclasses import dataclass

from .models import HubGameStep, HubSession


@dataclass(frozen=True)
class HostAuthorization:
    allowed: bool
    code: str = ''
    message: str = ''


def _is_authenticated(user):
    return bool(user and getattr(user, 'is_authenticated', False))


def _game_for_room(game_key, room_code):
    model = HubSession._get_game_model_map().get(str(game_key or ''))
    if not model or not room_code:
        return None
    return model.objects.filter(room_code=str(room_code)).first()


def user_can_manage_hub_session(user, session):
    """Use the persisted owner, with a safe fallback for pre-owner sessions."""
    if not _is_authenticated(user) or not session:
        return False
    if getattr(user, 'is_superuser', False):
        return True
    if session.creator_id:
        return session.creator_id == user.pk

    steps = list(session.steps.exclude(room_code=''))
    if not steps:
        return False
    for step in steps:
        game = _game_for_room(step.game_key, step.room_code)
        if not game or getattr(game, 'creator_id', None) != user.pk:
            return False
    return True


def authorize_game_host(user, game_key, room_code, session_code=None):
    if not _is_authenticated(user):
        return HostAuthorization(False, 'unauthorized', 'Authentication required.')

    game = _game_for_room(game_key, room_code)
    if not game:
        return HostAuthorization(False, 'invalid_action_context', 'Spielinstanz nicht gefunden.')
    if not getattr(user, 'is_superuser', False) and getattr(game, 'creator_id', None) != user.pk:
        return HostAuthorization(False, 'unauthorized', 'Keine Berechtigung fuer diese Spielinstanz.')

    if session_code:
        step = (
            HubGameStep.objects.select_related('session')
            .filter(
                session__code=str(session_code),
                game_key=str(game_key),
                room_code=str(room_code),
            )
            .first()
        )
        if not step:
            return HostAuthorization(False, 'stale_action', 'Spiel und Session stimmen nicht ueberein.')
        if not user_can_manage_hub_session(user, step.session):
            return HostAuthorization(False, 'unauthorized', 'Keine Berechtigung fuer diese Session.')

    return HostAuthorization(True)
