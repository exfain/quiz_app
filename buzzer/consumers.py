import json

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep
from games_hub.tutorial_runtime import (
    activate_tutorial_runtime,
    force_close_tutorial_runtime,
    get_tutorial_payload,
    get_tutorial_progress,
    get_tutorial_start_warning,
    mark_tutorial_completed,
)

from .models import BuzzerGame, BuzzerParticipant


class BuzzerConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'buzzer_{self.room_code}'
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
        if message_type == 'admin_start_game':
            await self.handle_admin_start_game(data)
        elif message_type == 'tutorial_completed':
            await self.handle_tutorial_completed(data)
        elif message_type == 'admin_start_round':
            await self.handle_admin_start_round(data)
        elif message_type == 'admin_open_buzzer':
            await self.handle_admin_open_buzzer(data)
        elif message_type == 'participant_buzz':
            await self.handle_participant_buzz(data)
        elif message_type == 'admin_mark_correct':
            await self.handle_admin_mark_correct(data)
        elif message_type == 'admin_mark_wrong':
            await self.handle_admin_mark_wrong(data)
        elif message_type == 'admin_end_round':
            await self.handle_admin_end_round(data)
        elif message_type == 'admin_end_game':
            await self.handle_admin_end_game(data)
        elif message_type == 'participant_join':
            await self.handle_participant_join(data)
        elif message_type == 'get_state':
            await self.send_state(data)

    async def handle_admin_start_game(self, data):
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self.get_hub_session_code()
        )
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'buzzer',
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
            'buzzer',
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

        game = await self.get_game()
        if not game:
            return
        await database_sync_to_async(game.start_quiz)(hub_session)
        tutorial_payload = await database_sync_to_async(activate_tutorial_runtime)(
            'buzzer',
            self.room_code,
            hub_session,
            game,
            bool(data.get('show_tutorial')),
        )
        await self.broadcast_state({'type': 'game_started'}, hub_session)
        await self.hub_mirror_event('quiz_started', {
            'game_key': 'buzzer',
            'room_code': self.room_code,
            'title': game.title,
            'message': 'Buzzer gestartet.',
        }, hub_session)
        if tutorial_payload:
            await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_start', **tutorial_payload})

    async def handle_tutorial_completed(self, data):
        participant_name = data.get('participant_name') or data.get('name')
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        progress = await database_sync_to_async(mark_tutorial_completed)(
            'buzzer',
            self.room_code,
            hub_session,
            participant_name,
        )
        await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_progress', **progress})

    async def handle_admin_start_round(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        if (
            game.status != 'active'
            or (
                hub_session
                and game.active_hub_session_code
                and hub_session != game.active_hub_session_code
            )
        ):
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Starte zuerst das Spiel, bevor du eine Runde startest.',
            }))
            return
        if not bool(data.get('force_start')):
            warning = await database_sync_to_async(get_tutorial_start_warning)('buzzer', self.room_code, hub_session)
            if warning:
                await self.send(text_data=json.dumps({'type': 'tutorial_ack_warning', **warning}))
                return
        else:
            await database_sync_to_async(force_close_tutorial_runtime)('buzzer', self.room_code, hub_session, game)

        round_obj = await database_sync_to_async(game.start_round)(hub_session)
        if not round_obj:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Die Runde konnte in diesem Zustand nicht gestartet werden.',
            }))
            return
        await self.broadcast_state({'type': 'round_started'}, hub_session)

    async def handle_admin_open_buzzer(self, data):
        game = await self.get_game()
        if game:
            opened = await database_sync_to_async(game.open_buzzer)()
            if not opened:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Der Buzzer kann in diesem Zustand nicht freigegeben werden.',
                }))
                return
            await self.broadcast_state(
                {'type': 'buzzer_opened'},
                data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
            )

    async def handle_participant_buzz(self, data):
        game = await self.get_game()
        if not game:
            return
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code() or '').strip()
        participant = await self.get_participant(participant_name, hub_session)
        success, result = await database_sync_to_async(game.accept_buzz)(participant)
        if not success:
            await self.send(text_data=json.dumps({'type': 'buzz_rejected', 'message': result}))
            return
        await self.broadcast_state({'type': 'buzz_accepted', 'participant_name': participant_name}, hub_session)

    async def handle_admin_mark_correct(self, data):
        game = await self.get_game()
        if game:
            marked = await database_sync_to_async(game.mark_current_correct)()
            if not marked:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Es gibt keinen Buzz zum Bewerten.'}))
                return
            await self.broadcast_state(
                {'type': 'answer_marked_correct'},
                data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
            )

    async def handle_admin_mark_wrong(self, data):
        game = await self.get_game()
        if game:
            marked = await database_sync_to_async(game.mark_current_wrong)()
            if not marked:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Es gibt keinen Buzz zum Bewerten.'}))
                return
            await self.broadcast_state(
                {'type': 'answer_marked_wrong'},
                data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
            )

    async def handle_admin_end_round(self, data):
        game = await self.get_game()
        if game:
            ended = await database_sync_to_async(game.end_current_round)()
            if not ended:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Es gibt keine aktive Runde.'}))
                return
            await self.broadcast_state(
                {'type': 'round_ended'},
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
            'game_key': 'buzzer',
            'room_code': self.room_code,
            'title': game.title,
        }, hub_session)

    async def handle_participant_join(self, data):
        game = await self.get_game()
        if not game:
            return
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code() or '').strip()
        if participant_name:
            await self.get_or_create_participant(participant_name, hub_session)
            tutorial_payload = await database_sync_to_async(get_tutorial_payload)(
                'buzzer',
                self.room_code,
                hub_session,
                participant_name,
            )
            if tutorial_payload:
                await self.send(text_data=json.dumps({'type': 'tutorial_start', **tutorial_payload}))
        await self.send_state(data)

    async def send_state(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        participant_name = data.get('participant_name') or data.get('name')
        state = await database_sync_to_async(game.serialize_state)(hub_session, participant_name)
        await self.send(text_data=json.dumps({'type': 'buzzer_state', **state}))

    async def broadcast_state(self, extra=None, hub_session=None):
        game = await self.get_game()
        if not game:
            return
        state = await database_sync_to_async(game.serialize_state)(hub_session)
        extra = extra or {}
        event = {'type': 'buzzer_state', 'event_type': extra.get('type'), **state}
        event.update({key: value for key, value in extra.items() if key != 'type'})
        await self.channel_layer.group_send(
            self.room_group_name,
            event,
        )

    async def buzzer_state(self, event):
        await self.send(text_data=json.dumps(event))

    async def tutorial_start(self, event):
        await self.send(text_data=json.dumps(event))

    async def tutorial_progress(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def get_game(self):
        return BuzzerGame.objects.filter(room_code=self.room_code).first()

    @database_sync_to_async
    def get_participant(self, name, hub_session):
        if not name:
            return None
        return BuzzerParticipant.objects.filter(
            quiz__room_code=self.room_code,
            name=name,
            hub_session_code=hub_session or '',
        ).first()

    @database_sync_to_async
    def get_or_create_participant(self, name, hub_session):
        game = BuzzerGame.objects.get(room_code=self.room_code)
        participant, _ = BuzzerParticipant.objects.get_or_create(
            quiz=game,
            name=name,
            hub_session_code=hub_session or '',
            defaults={'is_active': False},
        )
        return participant

    @database_sync_to_async
    def get_hub_session_code(self):
        step = (
            HubGameStep.objects.select_related('session')
            .filter(game_key='buzzer', room_code=self.room_code, session__ended_at__isnull=True)
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
