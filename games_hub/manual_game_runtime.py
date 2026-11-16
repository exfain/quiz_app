"""Transport guards shared only by Buzzer and Host-Punktevergabe."""
from functools import wraps

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core import signing
from django.db import transaction

from .authoritative_state import _retry_sqlite_locked, validate_and_reserve_action
from .models import HubGameStep, HubParticipant


class ActionError(ValueError):
    def __init__(self, message, code='stale_action'):
        super().__init__(message)
        self.code = code


def atomic_write(fn):
    atomic = transaction.atomic(fn)
    @wraps(fn)
    def call(*args, **kwargs):
        return _retry_sqlite_locked(lambda: atomic(*args, **kwargs))
    return call


def game_key(game):
    return 'buzzer' if game._meta.app_label == 'buzzer' else 'host_points'


def session_code(game, supplied=None, *, starting=False):
    code = supplied if supplied is not None else game.active_hub_session_code
    if not code and not game.started_at and supplied is None:
        step = game._get_relevant_step()
        code = step.session.code if step else ''
    code = str(code or '')
    if code and not HubGameStep.objects.filter(game_key=game_key(game), room_code=game.room_code,
            session__code=code).exists():
        raise ActionError('Spiel und Session stimmen nicht ueberein.')
    if game.started_at and code != game.active_hub_session_code:
        old_step = game._get_relevant_step(game.active_hub_session_code) if game.active_hub_session_code else None
        old_finished = bool(game.ended_at or game.status == 'completed'
            or (old_step and old_step.session.ended_at))
        if not starting or not old_finished:
            raise ActionError('Das Spiel gehoert zu einer anderen Session.')
    return code


@atomic_write
def perform(game, data, code, operation, *, actor='__host__', starting=False):
    game = type(game).objects.select_for_update().get(pk=game.pk)
    code = session_code(game, code, starting=starting)
    if data is not None:
        state = game.serialize_state(code)
        if data.get('type') in {'admin_mark_correct', 'admin_mark_wrong'}:
            expected = data.get('buzz_id')
            if not expected or str(expected) != str(state['round'].get('current_buzz_id')):
                raise ActionError('Diese Bewertung gehoert nicht zum aktuellen Buzz.')
        decision = validate_and_reserve_action(game_key=game_key(game), room_code=game.room_code,
            session_code=code, participant_name=actor, action_type=data.get('type') or '', action=data,
            allow_inactive=starting or data.get('type') == 'admin_end_game')
        if not decision.accepted:
            raise ActionError(decision.message, decision.code)
    return operation(game)


def player_token(participant):
    return signing.dumps({'game': game_key(participant.quiz), 'quiz': participant.quiz_id,
        'participant': participant.pk, 'hub': participant.hub_session_code}, salt='manual-game.player.v1')


def valid_player_token(token, participant):
    if not isinstance(token, str) or not token:
        return False
    try:
        identity = signing.loads(token or '', salt='manual-game.player.v1')
        if identity == {'game': game_key(participant.quiz), 'quiz': participant.quiz_id,
                'participant': participant.pk, 'hub': participant.hub_session_code}:
            return True
    except (signing.BadSignature, ValueError, TypeError):
        pass
    if participant.hub_session_code:
        from .lobby_join import _token_matches_participant
        hub_player = HubParticipant.objects.filter(session__code=participant.hub_session_code,
            nickname=participant.name).first()
        return bool(hub_player and _token_matches_participant(token, hub_player))
    return False


def identify_player(game, name, code, token, *, active=True):
    code = str(code or '')
    if active:
        try:
            code = session_code(game, code)
        except ActionError as exc:
            raise ActionError(str(exc), 'unauthorized') from exc
    elif code and not HubGameStep.objects.filter(game_key=game_key(game), room_code=game.room_code,
            session__code=code).exists():
        raise ActionError('Spiel und Session stimmen nicht ueberein.', 'unauthorized')
    participant = game.participants.filter(name=name, hub_session_code=code).first()
    if not participant or not valid_player_token(token, participant):
        raise ActionError('Teilnahme nicht bestaetigt.', 'unauthorized')
    return participant


def page_token(request, participant, *, created=False):
    """Hub pages use the existing lobby credential; standalone joins own a session cookie."""
    key = f'manual-player:{game_key(participant.quiz)}:{participant.pk}'
    owner = request.user.is_authenticated and (request.user.is_superuser or participant.quiz.creator_id == request.user.pk)
    if created and not participant.hub_session_code:
        request.session[key] = True
    if owner or request.session.get(key):
        return player_token(participant)
    return ''


def eligible_player(game, participant):
    if not participant.hub_session_code:
        return True
    step = game._get_relevant_step(participant.hub_session_code)
    return bool(step and step.participant_snapshots.filter(participant__nickname=participant.name,
        active_player=True, included_in_scoring=True).exists())


def action_errors(fn):
    @wraps(fn)
    async def call(self, data):
        import json
        try:
            return await fn(self, data)
        except ActionError as exc:
            await self.send(text_data=json.dumps({'type': 'action_rejected', 'code': exc.code, 'message': str(exc)}))
            await self.send_state(data)
    return call


def broadcast(game, event_type='state_updated', **extra):
    game.refresh_from_db()
    key = game_key(game)
    state = game.serialize_state(game.active_hub_session_code)
    event = {'type': key + '_state', 'event_type': event_type, **state, **extra}
    layer = get_channel_layer()
    if layer:
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(f'{key}_{game.room_code}', event))


@atomic_write
def set_status(game, status):
    game = type(game).objects.select_for_update().get(pk=game.pk)
    if game.status == status:
        return False
    game.status = status
    game.save(update_fields=['status', 'updated_at'])
    broadcast(game)
    return True


class ManualGameConsumerMixin:
    """Authenticate before the common consumer captures a participant identity."""
    @staticmethod
    def _is_state_payload(payload):
        return False  # serialize_state observes the complete, canonical snapshot.

    def _is_guarded_action(self, message_type, data):
        if message_type == 'participant_buzz':
            return False  # perform reserves and mutates atomically instead.
        return super()._is_guarded_action(message_type, data)

    async def websocket_receive(self, message):
        import json
        from channels.db import database_sync_to_async
        from .host_permissions import authorize_game_host
        try:
            data = json.loads(message.get('text') or '{}')
            if not isinstance(data, dict):
                raise ValueError('Ungueltige Nachricht.')
            kind = str(data.get('type') or '')
            if kind.startswith('participant_') or kind == 'tutorial_completed' or (
                    kind in {'get_state', 'snapshot_request'} and (data.get('participant_name') or self._authoritative_participant_name())):
                await self.player_identity(data, active=kind in {'participant_buzz', 'tutorial_completed'})
            elif kind in {'get_state', 'snapshot_request'} or kind.startswith('admin_'):
                quiz = await self.get_game()
                code = data.get('hub_session', data.get('hub_session_code', quiz.active_hub_session_code))
                authorization = await database_sync_to_async(authorize_game_host)(
                    self.scope.get('user'), self.authoritative_game_key, self.room_code, code)
                if not authorization.allowed:
                    raise ActionError(authorization.message, authorization.code)
                if getattr(self, '_manual_session', None) is not None and str(code or '') != self._manual_session:
                    raise ActionError('Die Verbindung gehoert zu einer anderen Session.')
                self._manual_session = str(code or '')
            await super().websocket_receive(message)
        except (ValueError, TypeError) as exc:
            await self.send(text_data=json.dumps({'type': 'action_rejected', 'code': getattr(exc, 'code', 'invalid_action_context'), 'message': str(exc)}))

    async def player_identity(self, data, *, active=False):
        from channels.db import database_sync_to_async
        name = data.get('participant_name') or data.get('name') or self._authoritative_participant_name()
        bound = self._authoritative_participant_name()
        code = data.get('hub_session', data.get('hub_session_code', getattr(self, '_manual_session', None)))
        if bound and name != bound:
            raise ActionError('Die Verbindung gehoert zu einem anderen Teilnehmer.', 'unauthorized')
        token = data.get('player_token') or getattr(self, '_manual_player_token', '')
        participant = await database_sync_to_async(identify_player)(await self.get_game(), name, code, token, active=active)
        self._manual_player_token = token
        self._manual_session = participant.hub_session_code
        self._authoritative_participant = participant.name
        self._authoritative_session = participant.hub_session_code
        return participant

    async def event_in_session(self, event):
        return (getattr(self, '_manual_session', None) is not None
            and event.get('hub_session_code') == self._manual_session)
