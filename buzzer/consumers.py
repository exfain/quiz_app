import json

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from django.utils import timezone

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import validate_and_reserve_action
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

from games_hub.manual_game_runtime import ManualGameConsumerMixin, perform, session_code as checked_session, ActionError, action_errors

from .models import BuzzerGame, BuzzerParticipant


class BuzzerConsumer(ManualGameConsumerMixin, AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'buzzer'
    authoritative_required_actions = frozenset({'participant_buzz'})
    authoritative_admin_actions = frozenset({
        'admin_start_game',
        'admin_start_round',
        'admin_open_buzzer',
        'admin_mark_correct',
        'admin_mark_wrong',
        'admin_end_round',
        'admin_end_game',
        'admin_set_inactive',
    })

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
        elif message_type == 'admin_set_inactive':
            await self.handle_admin_set_inactive(data)
        elif message_type == 'participant_join':
            await self.handle_participant_join(data)
        elif message_type == 'get_state':
            await self.send_state(data)

    @action_errors
    async def handle_admin_start_game(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        same_run = game.started_at and game.active_hub_session_code == (hub_session or '')
        if game.status in {'completed', 'cancelled'} and game.active_hub_session_code == (hub_session or ''):
            raise ActionError('Dieses Spiel ist bereits beendet.', 'invalid_phase')
        if same_run and game.status == 'active':
            await self.send_state(data)
            return
        if not (same_run and game.status == 'inactive'):
            readiness = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
                'buzzer', self.room_code, session_code=hub_session)
            if not readiness.get('allowed', True):
                await self.send(text_data=json.dumps({'type': 'participants_not_in_lobby', **readiness}))
                return
        def start(locked):
            activation = resolve_session_game_activation_for_room('buzzer', self.room_code, session_code=hub_session)
            if not activation.get('success'):
                raise ActionError(activation.get('message') or activation.get('error') or 'Spielstart nicht moeglich.')
            locked.refresh_from_db()
            return locked.start_quiz(hub_session)
        await database_sync_to_async(perform)(game, data, hub_session, start, starting=True)
        game = await self.get_game()
        tutorial = None
        if not same_run:
            tutorial = await database_sync_to_async(activate_tutorial_runtime)(
                'buzzer', self.room_code, hub_session, game, bool(data.get('show_tutorial')))
        await self.broadcast_state({'type': 'game_started'}, hub_session)
        await self.hub_mirror_event('quiz_started', {'game_key': 'buzzer', 'room_code': self.room_code,
            'title': game.title}, hub_session)
        if tutorial:
            await self.channel_layer.group_send(self.room_group_name,
                {'type': 'tutorial_start', 'hub_session_code': hub_session or '', **tutorial})

    async def handle_tutorial_completed(self, data):
        participant_name = data.get('participant_name') or data.get('name')
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        progress = await database_sync_to_async(mark_tutorial_completed)(
            'buzzer',
            self.room_code,
            hub_session,
            participant_name,
        )
        await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_progress', 'hub_session_code': hub_session or '', **progress})

    @action_errors
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
        def start_round(locked):
            if data.get('force_start'):
                force_close_tutorial_runtime('buzzer', self.room_code, hub_session, locked)
            return locked.start_round(hub_session)
        round_obj = await self.apply_action(data, start_round)
        if not round_obj:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Die Runde konnte in diesem Zustand nicht gestartet werden.',
            }))
            return
        if data.get('force_start'):
            await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_force_close', 'hub_session_code': hub_session or ''})
        await self.broadcast_state({'type': 'round_started'}, hub_session)

    @action_errors
    async def handle_admin_open_buzzer(self, data):
        game = await self.get_game()
        if game:
            opened = await self.apply_action(data, lambda locked: locked.open_buzzer())
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
        participant = await self.player_identity(data)
        participant_name, hub_session = participant.name, participant.hub_session_code
        success, result = await database_sync_to_async(perform)(game, data, hub_session,
            lambda locked: locked.accept_buzz(participant), actor=participant.name)
        if not success:
            await self.send(text_data=json.dumps({'type': 'buzz_rejected', 'message': result}))
            return
        await self.broadcast_state({'type': 'buzz_accepted', 'participant_name': participant_name}, hub_session)

    @action_errors
    async def handle_admin_mark_correct(self, data):
        game = await self.get_game()
        if game:
            marked = await self.apply_action(data, lambda locked: locked.mark_current_correct())
            if not marked:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Es gibt keinen Buzz zum Bewerten.'}))
                return
            await self.broadcast_state(
                {'type': 'answer_marked_correct'},
                data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
            )

    @action_errors
    async def handle_admin_mark_wrong(self, data):
        game = await self.get_game()
        if game:
            marked = await self.apply_action(data, lambda locked: locked.mark_current_wrong())
            if not marked:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Es gibt keinen Buzz zum Bewerten.'}))
                return
            await self.broadcast_state(
                {'type': 'answer_marked_wrong'},
                data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
            )

    @action_errors
    async def handle_admin_end_round(self, data):
        game = await self.get_game()
        if game:
            ended = await self.apply_action(data, lambda locked: locked.end_current_round())
            if not ended:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Es gibt keine aktive Runde.'}))
                return
            await self.broadcast_state(
                {'type': 'round_ended'},
                data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code(),
            )

    @action_errors
    async def handle_admin_end_game(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        ended = await self.apply_action(data, lambda locked: locked.end_quiz())
        if not ended:
            await self.send_state(data)
            return
        await self.broadcast_state({'type': 'game_ended'}, hub_session)
        await self.hub_mirror_event('game_ended', {
            'game_key': 'buzzer',
            'room_code': self.room_code,
            'title': game.title,
        }, hub_session)

    @action_errors
    async def handle_admin_set_inactive(self, data):
        changed = await self.apply_action(data, lambda locked: type(locked).objects.filter(pk=locked.pk, status='active').update(status='inactive', updated_at=timezone.now()) == 1)
        if not changed:
            await self.send_state(data)
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        await self.broadcast_state({'type': 'game_inactive'}, hub_session)

    async def handle_participant_join(self, data):
        game = await self.get_game()
        if not game:
            return
        participant = await self.player_identity(data)
        participant_name, hub_session = participant.name, participant.hub_session_code
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
        )
        if decision.accepted:
            return True
        await self.send(text_data=json.dumps({
            'type': 'action_rejected',
            'code': decision.code,
            'message': decision.message,
        }))
        await self.send_state({'hub_session': hub_session})
        return False

    async def send_state(self, data):
        game = await self.get_game()
        if not game:
            return
        participant_name = self._authoritative_participant_name() or data.get('participant_name') or data.get('name')
        if participant_name:
            participant = await self.player_identity(data)
            code = participant.hub_session_code
        else:
            code = data.get('hub_session', data.get('hub_session_code', game.active_hub_session_code))
            self._manual_session = str(code or '')
        state = await database_sync_to_async(game.serialize_state)(code, participant_name)
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
        if not await self.event_in_session(event):
            return
        await self.send(text_data=json.dumps(event))

    async def tutorial_start(self, event):
        if not await self.event_in_session(event):
            return
        await self.send(text_data=json.dumps(event))

    async def tutorial_progress(self, event):
        if not await self.event_in_session(event):
            return
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def get_game(self):
        return BuzzerGame.objects.filter(room_code=self.room_code).first()

    @database_sync_to_async
    def set_game_inactive(self):
        return BuzzerGame.objects.filter(
            room_code=self.room_code,
            status='active',
        ).update(status='inactive', updated_at=timezone.now()) == 1

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
        if game.status == 'active' and participant.hub_session_code == game.active_hub_session_code:
            eligible = not participant.hub_session_code or game._get_relevant_step(participant.hub_session_code).participant_snapshots.filter(participant__nickname=name, active_player=True, included_in_scoring=True).exists()
            if eligible and not participant.is_active:
                participant.is_active = True
                participant.save(update_fields=['is_active', 'updated_at'])
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

    async def tutorial_force_close(self, event):
        if await self.event_in_session(event):
            await self.send(text_data=json.dumps(event))

    async def apply_action(self, data, operation):
        game = await self.get_game()
        code = data.get('hub_session', data.get('hub_session_code', game.active_hub_session_code))
        return await database_sync_to_async(perform)(game, data, code, operation)
