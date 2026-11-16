import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.core.exceptions import ValidationError

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.host_permissions import authorize_game_host
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.authoritative_state import register_socket_connection, disconnect_socket_connection
from .identity import valid_player_token
from .models import WhereQuiz, WhereParticipant, WhereQuestion
from . import runtime as r


class WhereConsumer(AuthoritativeGameConsumerMixin, AsyncJsonWebsocketConsumer):
    authoritative_game_key = 'where'
    authoritative_required_actions = frozenset({'participant_submit_answer'})

    @staticmethod
    def _is_state_payload(payload):
        # runtime.state already observes the public DTO, never the host solution.
        return False

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'where_{self.room_code}'
        self.code, self.player_id, self.host = '', None, False
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send_json({'type': 'connection_established'})

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        await database_sync_to_async(disconnect_socket_connection)(self.channel_name)

    async def receive_json(self, data):
        if not isinstance(data, dict):
            return
        try:
            kind = data.get('type')
            if kind in {'participant_join', 'host_join'}:
                await self.join(data, kind == 'host_join')
            elif kind in {'get_state', 'snapshot_request'}:
                await self.send_state()
            elif kind in {'ping', 'heartbeat'}:
                await self.send_json({'type': 'pong'})
            elif kind in {'participant_submit_answer', 'participant_input_changed'}:
                if not self.player_id:
                    raise r.ActionError('Teilnahme nicht bestätigt.')
                point = await self.save(data, kind == 'participant_submit_answer')
                await self.send_json({'type': 'answer_submitted' if kind == 'participant_submit_answer' else 'input_saved',
                                     'question_id': data.get('question_id'), 'round_id': data.get('round_id'), **point})
                await self.send_state()
            elif str(kind).startswith('admin_'):
                await self.action(data)
                if kind == 'admin_start_quiz':
                    await self.send_json({'type': 'host_start_confirmed', 'start_request_id': data.get('start_request_id')})
                await self.send_state()
            else:
                raise r.ActionError('Unbekannte Aktion.')
        except (ValidationError, ValueError, TypeError, KeyError, WhereQuiz.DoesNotExist, WhereQuestion.DoesNotExist) as exc:
            await self.send_json({'type': 'error', 'message': str(exc)})

    async def join(self, data, host):
        code = str(data.get('hub_session') or '')
        if host:
            authorization = await database_sync_to_async(authorize_game_host)(self.scope.get('user'), 'where', self.room_code, code)
            if not authorization.allowed:
                raise r.ActionError(authorization.message)
            self.host, self.code = True, code
        else:
            participant = await self.player(data, code)
            if not participant or not valid_player_token(data.get('player_token'), participant):
                raise r.ActionError('Teilnahme nicht bestätigt.')
            if self.player_id and self.player_id != participant.pk:
                raise r.ActionError('Die Socket-Identität kann nicht gewechselt werden.')
            self.player_id, self.code = participant.pk, code
            self._authoritative_participant, self._authoritative_session = participant.name, code
            if code:
                await database_sync_to_async(register_socket_connection)(channel_name=self.channel_name, session_code=code,
                    participant_name=participant.name, scope_kind='game', game_key='where', room_code=self.room_code)
        await self.send_state()

    @database_sync_to_async
    def player(self, data, code):
        return WhereParticipant.objects.filter(quiz__room_code=self.room_code,
            name=data.get('participant_name'), **r.pool(code)).first()

    @database_sync_to_async
    def save(self, data, confirm):
        participant = WhereParticipant.objects.select_related('quiz').get(pk=self.player_id)
        return r.save_input(participant.quiz, participant, data, confirm=confirm)

    @database_sync_to_async
    def action(self, data):
        code = str(data.get('hub_session') or self.code or '')
        auth = authorize_game_host(self.scope.get('user'), 'where', self.room_code, code)
        if not self.host or code != self.code or not auth.allowed:
            raise r.ActionError('Keine Hostberechtigung für diese Session.')
        game = WhereQuiz.objects.get(room_code=self.room_code)
        kind = data['type']
        if kind == 'admin_start_quiz':
            already_started = game.status == 'active' and bool(game.started_at)
            if game.status not in {'active', 'inactive'} or not game.started_at:
                presence = ensure_session_players_ready_for_game_start_for_room('where', self.room_code, code)
                if not presence.get('allowed', True):
                    raise r.ActionError(presence['message'])
            decision = resolve_session_game_activation_for_room('where', self.room_code, session_code=code)
            if not decision.get('success'):
                raise r.ActionError(decision.get('error') or 'Ein anderes Spiel ist noch aktiv.')
            r.start(game, code)
            # Use the established hub event to route the confirmed lobby teams.
            from asgiref.sync import async_to_sync
            if not already_started:
                async_to_sync(self.channel_layer.group_send)(f'hub_{code}', {'type': 'hub_event', 'event': {'type': 'quiz_started',
                    'game_key': 'where', 'room_code': game.room_code, 'title': game.title}})
        elif kind == 'admin_send_question':
            question = WhereQuestion.objects.get(pk=data['question_id'], created_by=game.creator, is_active=True)
            r.send_question(game, question, time_limit=data.get('custom_time_limit'))
        elif kind == 'admin_end_question':
            self.check_round(game, data)
            r.reveal(game)
        elif kind == 'admin_end_quiz':
            self.check_round(game, data)
            r.end_game(game)
        elif kind == 'admin_set_inactive':
            r.set_paused(game, True)
        else:
            raise r.ActionError('Unbekannte Hostaktion.')

    @staticmethod
    def check_round(game, data):
        session = game.session
        if game.current_question_id and (str(data.get('question_id')) != str(game.current_question_id)
                or str(data.get('round_id')) != str(session.current_question_number)):
            raise r.ActionError('Die Hostaktion gehört zu einer früheren Frage.')

    @database_sync_to_async
    def get_state(self):
        if not self.host and not self.player_id:
            return None
        game = WhereQuiz.objects.get(room_code=self.room_code)
        participant = WhereParticipant.objects.get(pk=self.player_id) if self.player_id else None
        return r.state(game, self.code, participant, host=self.host)

    async def send_state(self, data=None):
        state = await self.get_state()
        if state:
            await self.send_json(state)

    async def where_updated(self, event):
        await self.send_state()
