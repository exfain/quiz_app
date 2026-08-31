import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import validate_and_reserve_action
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep
from games_hub.tutorial_runtime import (
    activate_tutorial_runtime,
    get_tutorial_payload,
    get_tutorial_progress,
    mark_tutorial_completed,
)

from .models import HostPointsGame


class HostPointsConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'host_points'
    authoritative_admin_actions = frozenset({
        'admin_start_game',
        'admin_adjust_score',
        'admin_next_round',
        'admin_end_game',
        'admin_set_inactive',
    })

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'host_points_{self.room_code}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send(text_data=json.dumps({'type': 'connection_established'}))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Invalid JSON'}))
            return

        message_type = data.get('type')
        if (
            message_type in self.authoritative_admin_actions
            and not await self.reserve_admin_action(data)
        ):
            return
        if message_type == 'admin_start_game':
            await self.handle_admin_start_game(data)
        elif message_type == 'tutorial_completed':
            await self.handle_tutorial_completed(data)
        elif message_type == 'admin_adjust_score':
            await self.handle_admin_adjust_score(data)
        elif message_type == 'admin_next_round':
            await self.handle_admin_next_round(data)
        elif message_type == 'admin_end_game':
            await self.handle_admin_end_game(data)
        elif message_type == 'admin_set_inactive':
            await self.handle_admin_set_inactive(data)
        elif message_type == 'participant_join':
            await self.handle_participant_join(data)
        elif message_type == 'get_state':
            await self.send_state(data)

    async def handle_admin_start_game(self, data):
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        game = await self.get_game()
        if not game:
            return
        if (
            game.status == 'active'
            and game.started_at is not None
            and game.current_round_number >= 1
            and (
                not hub_session
                or game.active_hub_session_code == hub_session
            )
        ):
            await self.send_state(data)
            return

        previous_started_at = game.started_at
        previous_round_number = game.current_round_number
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'host_points',
            self.room_code,
            session_code=hub_session,
        )
        if not lobby_ready.get('allowed', True):
            await self.send(text_data=json.dumps({
                'type': 'participants_not_in_lobby',
                'message': lobby_ready.get('message') or 'Noch nicht alle Teilnehmer sind in der Lobby.',
                'not_in_lobby_count': lobby_ready.get('not_in_lobby_count', 0),
                'participants_not_in_lobby': lobby_ready.get('participants_not_in_lobby', []),
            }))
            return

        activation = await database_sync_to_async(resolve_session_game_activation_for_room)(
            'host_points',
            self.room_code,
            session_code=hub_session,
        )
        if not activation.get('success'):
            await self.send(text_data=json.dumps({
                'type': 'active_game_conflict' if activation.get('conflict') else 'error',
                'message': activation.get('message') or activation.get('error') or 'Spiel konnte nicht gestartet werden.',
                'active_game': activation.get('active_game'),
            }))
            return

        started = await database_sync_to_async(game.start_quiz)(
            hub_session,
            allow_reactivation=True,
            expected_previous_started_at=previous_started_at,
            expected_previous_round=previous_round_number,
        )
        if not started:
            await self.send_state(data)
            return
        tutorial_payload = await database_sync_to_async(activate_tutorial_runtime)(
            'host_points',
            self.room_code,
            hub_session,
            game,
            bool(data.get('show_tutorial')),
        )
        await self.broadcast_state({'type': 'game_started'}, hub_session)
        await self.hub_mirror_event('quiz_started', {
            'game_key': 'host_points',
            'room_code': self.room_code,
            'title': game.title,
            'message': 'Host-Punktevergabe gestartet.',
        }, hub_session)
        if tutorial_payload:
            await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_start', **tutorial_payload})

    async def handle_tutorial_completed(self, data):
        participant_name = data.get('participant_name') or data.get('name')
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        progress = await database_sync_to_async(mark_tutorial_completed)(
            'host_points',
            self.room_code,
            hub_session,
            participant_name,
        )
        await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_progress', **progress})

    async def handle_admin_adjust_score(self, data):
        game = await self.get_game()
        if not game:
            return
        success, result = await database_sync_to_async(game.adjust_score)(
            data.get('participant_id'),
            data.get('delta'),
        )
        if not success:
            await self.send(text_data=json.dumps({'type': 'error', 'message': result}))
            return
        await self.broadcast_state(
            {'type': 'score_adjusted', 'participant_name': getattr(result, 'name', '')},
            data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
        )

    async def handle_admin_next_round(self, data):
        game = await self.get_game()
        if not game:
            return
        moved = await database_sync_to_async(game.next_round)(
            data.get('round_id') or data.get('expected_round'),
        )
        if not moved:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Die Runde kann in diesem Zustand nicht erhöht werden.'}))
            return
        await self.broadcast_state(
            {'type': 'round_advanced'},
            data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
        )

    async def handle_admin_end_game(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        ended = await database_sync_to_async(game.end_quiz)()
        if not ended:
            await self.send_state(data)
            return
        await self.broadcast_state({'type': 'game_ended'}, hub_session)
        await self.hub_mirror_event('game_ended', {
            'game_key': 'host_points',
            'room_code': self.room_code,
            'title': game.title,
        }, hub_session)

    async def handle_admin_set_inactive(self, data):
        changed = await self.set_game_inactive()
        if not changed:
            await self.send_state(data)
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        await self.broadcast_state({'type': 'game_inactive'}, hub_session)

    async def handle_participant_join(self, data):
        game = await self.get_game()
        if not game:
            return
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code() or '').strip()
        if participant_name:
            await self.get_or_create_participant(participant_name, hub_session)
            tutorial_payload = await database_sync_to_async(get_tutorial_payload)(
                'host_points',
                self.room_code,
                hub_session,
                participant_name,
            )
            if tutorial_payload:
                await self.send(text_data=json.dumps({'type': 'tutorial_start', **tutorial_payload}))
        await self.send_state(data)

    async def reserve_admin_action(self, data):
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self.get_hub_session_code()
        )
        decision = await database_sync_to_async(validate_and_reserve_action)(
            game_key=self.authoritative_game_key,
            room_code=self.room_code,
            session_code=hub_session,
            participant_name='__host__',
            action_type=data.get('type') or '',
            action=data,
            allow_inactive=data.get('type') == 'admin_start_game',
        )
        if decision.accepted:
            return True
        await self.send(text_data=json.dumps({
            'type': 'action_rejected',
            'code': decision.code,
            'message': decision.message,
        }))
        await self.send_state({
            'hub_session': hub_session,
        })
        return False

    async def send_state(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        participant_name = data.get('participant_name') or data.get('name')
        state = await database_sync_to_async(game.serialize_state)(hub_session, participant_name)
        await self.send(text_data=json.dumps({'type': 'host_points_state', **state}))

    async def broadcast_state(self, extra=None, hub_session=None):
        game = await self.get_game()
        if not game:
            return
        state = await database_sync_to_async(game.serialize_state)(hub_session)
        extra = extra or {}
        event = {'type': 'host_points_state', 'event_type': extra.get('type'), **state}
        event.update({key: value for key, value in extra.items() if key != 'type'})
        await self.channel_layer.group_send(self.room_group_name, event)

    async def host_points_state(self, event):
        await self.send(text_data=json.dumps(event))

    async def tutorial_start(self, event):
        await self.send(text_data=json.dumps(event))

    async def tutorial_progress(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def get_game(self):
        return HostPointsGame.objects.filter(room_code=self.room_code).first()

    @database_sync_to_async
    def set_game_inactive(self):
        return HostPointsGame.objects.filter(
            room_code=self.room_code,
            status='active',
        ).update(status='inactive', updated_at=timezone.now()) == 1

    @database_sync_to_async
    def get_or_create_participant(self, name, hub_session):
        game = HostPointsGame.objects.get(room_code=self.room_code)
        participant, _ = game.participants.get_or_create(
            name=name,
            hub_session_code=hub_session or '',
            defaults={'is_active': False},
        )
        return participant

    @database_sync_to_async
    def get_hub_session_code(self):
        step = (
            HubGameStep.objects.select_related('session')
            .filter(game_key='host_points', room_code=self.room_code, session__ended_at__isnull=True)
            .order_by('-id')
            .first()
        )
        return step.session.code if step else None

    async def hub_mirror_event(self, event_type, payload, session_code=None):
        session_code = session_code or await self.get_hub_session_code()
        if not session_code:
            return
        await self.channel_layer.group_send(
            f'hub_{session_code}',
            {'type': 'hub_event', 'event': {'type': event_type, **payload}},
        )
