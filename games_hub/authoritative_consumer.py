from __future__ import annotations

import json

from channels.db import database_sync_to_async

from .host_permissions import authorize_game_host
from .authoritative_state import (
    current_snapshot,
    disconnect_socket_connection,
    observe_snapshot,
    register_socket_connection,
    touch_socket_connection,
    validate_and_reserve_action,
)


NON_FINAL_PARTICIPANT_MESSAGES = {
    'participant_join',
    'participant_check_round',
    'participant_question_timeout',
    'get_state',
    'snapshot_request',
    'ping',
    'heartbeat',
}


class AuthoritativeGameConsumerMixin:
    """Adds durable revisions, action guards and socket presence compatibly."""

    authoritative_game_key = ''
    authoritative_scope_kind = 'game'
    authoritative_required_actions = frozenset()

    async def websocket_receive(self, message):
        data = None
        if message.get('text'):
            try:
                data = json.loads(message['text'])
            except (TypeError, json.JSONDecodeError):
                data = None
        if isinstance(data, dict):
            message_type = data.get('type')
            if str(message_type or '').startswith('admin_'):
                authorization = await database_sync_to_async(authorize_game_host)(
                    self.scope.get('user'),
                    self.authoritative_game_key,
                    self._authoritative_room_code(),
                    data.get('hub_session') or data.get('hub_session_code'),
                )
                if not authorization.allowed:
                    await self.send(text_data=json.dumps({
                        'type': 'action_rejected',
                        'code': authorization.code,
                        'message': authorization.message,
                    }))
                    return
            guarded_action = self._is_guarded_action(message_type, data)
            if not guarded_action:
                self._capture_identity(data)
            if message_type in {'heartbeat', 'ping'}:
                await database_sync_to_async(touch_socket_connection)(self.channel_name)
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'server_now': await self._server_now(),
                }))
                return
            if (
                message_type == 'snapshot_request'
                and self.authoritative_game_key
            ):
                await self._send_authoritative_snapshot(data)
                return
            if guarded_action:
                if not self._authoritative_participant_name():
                    await self.send(text_data=json.dumps({
                        'type': 'action_rejected',
                        'code': 'invalid_action_context',
                        'message': 'Die Socket-Identitaet wurde noch nicht bestaetigt.',
                    }))
                    return
                decision = await database_sync_to_async(validate_and_reserve_action)(
                    game_key=self.authoritative_game_key,
                    room_code=self._authoritative_room_code(),
                    session_code=self._authoritative_session_code(),
                    participant_name=self._authoritative_participant_name(),
                    action_type=message_type,
                    action=data,
                )
                if not decision.accepted:
                    snapshot = await database_sync_to_async(current_snapshot)(
                        self.authoritative_game_key,
                        self._authoritative_room_code(),
                        self._authoritative_session_code(),
                    )
                    await self.send(text_data=json.dumps({
                        'type': 'action_rejected',
                        'code': decision.code,
                        'message': decision.message,
                        'snapshot': snapshot,
                    }))
                    await self._send_authoritative_snapshot(data)
                    return
                if decision.received_at:
                    data['server_received_at'] = decision.received_at
                if decision.time_taken is not None:
                    data['time_taken'] = decision.time_taken
                participant_name = self._authoritative_participant_name()
                session_code = self._authoritative_session_code()
                if participant_name:
                    data['participant_name'] = participant_name
                    data['name'] = participant_name
                    data['nickname'] = participant_name
                if session_code:
                    data['hub_session'] = session_code
                    data['hub_session_code'] = session_code
                message = dict(message)
                message['text'] = json.dumps(data)

        await super().websocket_receive(message)

        if isinstance(data, dict) and data.get('type') in {'join', 'participant_join'}:
            await self._register_presence()

    async def websocket_disconnect(self, message):
        try:
            await super().websocket_disconnect(message)
        finally:
            await database_sync_to_async(disconnect_socket_connection)(self.channel_name)

    async def send(self, text_data=None, bytes_data=None, close=False):
        if text_data and self.authoritative_game_key:
            try:
                payload = json.loads(text_data)
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                if self._is_state_payload(payload):
                    standard = await database_sync_to_async(observe_snapshot)(
                        self.authoritative_game_key,
                        self._authoritative_room_code(),
                        payload,
                        self._authoritative_session_code(),
                    )
                else:
                    standard = await database_sync_to_async(current_snapshot)(
                        self.authoritative_game_key,
                        self._authoritative_room_code(),
                        self._authoritative_session_code(),
                    )
                for field in (
                    'state_revision',
                    'server_now',
                    'game_id',
                    'session_id',
                    'game_key',
                    'room_code',
                    'phase',
                    'current_question_id',
                    'current_round_id',
                    'current_set_id',
                    'starts_at',
                    'ends_at',
                    'question_flow_mode',
                    'question_phase',
                    'question_presented_at',
                    'question_visible_at',
                    'content_revealed_at',
                    'answering_started_at',
                    'answering_deadline_at',
                    'answering_allowed',
                    'timer_running',
                    'remaining_answer_time',
                ):
                    if standard.get(field) is not None or field in {
                        'question_phase',
                        'question_presented_at',
                        'question_visible_at',
                        'content_revealed_at',
                        'answering_started_at',
                        'answering_deadline_at',
                        'remaining_answer_time',
                    }:
                        payload[field] = standard[field]
                text_data = json.dumps(payload)
        await super().send(text_data=text_data, bytes_data=bytes_data, close=close)

    def _capture_identity(self, data):
        session_code = data.get('hub_session') or data.get('hub_session_code')
        participant_name = (
            data.get('participant_name')
            or data.get('name')
            or data.get('nickname')
        )
        if session_code and not getattr(self, '_authoritative_session', None):
            self._authoritative_session = str(session_code)
        if participant_name and not getattr(self, '_authoritative_participant', None):
            self._authoritative_participant = str(participant_name).strip()

    async def _register_presence(self):
        session_code = self._authoritative_session_code()
        participant_name = self._authoritative_participant_name()
        if not session_code or not participant_name:
            return
        await database_sync_to_async(register_socket_connection)(
            channel_name=self.channel_name,
            session_code=session_code,
            participant_name=participant_name,
            scope_kind=self.authoritative_scope_kind,
            game_key=self.authoritative_game_key,
            room_code=self._authoritative_room_code(),
        )

    async def _send_authoritative_snapshot(self, request_data=None):
        request_data = dict(request_data or {})
        request_data.setdefault('participant_name', self._authoritative_participant_name())
        request_data.setdefault('hub_session', self._authoritative_session_code())
        if hasattr(self, 'send_state'):
            await self.send_state(request_data)
            return
        if request_data.get('participant_name') and hasattr(self, 'handle_participant_join'):
            await self.handle_participant_join(request_data)

    def _authoritative_session_code(self):
        return (
            getattr(self, '_authoritative_session', None)
            or getattr(self, 'session_code', None)
            or ''
        )

    def _authoritative_participant_name(self):
        return (
            getattr(self, '_authoritative_participant', None)
            or getattr(self, 'hub_participant_name', None)
            or ''
        )

    def _authoritative_room_code(self):
        return str(
            getattr(self, 'room_code', None)
            or getattr(self, 'session_code', None)
            or ''
        )

    def _is_guarded_action(self, message_type, data):
        if not self.authoritative_game_key or not message_type:
            return False
        if message_type in self.authoritative_required_actions:
            return True
        if message_type in NON_FINAL_PARTICIPANT_MESSAGES:
            return False
        if not (
            message_type.startswith('participant_')
            or message_type in {'tutorial_completed', 'ready', 'vote'}
        ):
            return False
        # Legacy and host clients remain compatible until they load the shared
        # browser protocol, which always supplies a client action id.
        return bool(data.get('client_action_id'))

    @staticmethod
    def _is_state_payload(payload):
        message_type = str(payload.get('type') or '')
        return bool(
            payload.get('question')
            or payload.get('game')
            or payload.get('timer')
            or payload.get('phase')
            or payload.get('question_state')
            or payload.get('event_type')
            or message_type.endswith(('_state', '_started', '_ended', '_revealed'))
            or message_type in {
                'state',
                'question_started',
                'question_ended',
                'round_started',
                'round_ended',
                'quiz_started',
                'quiz_ended',
                'answer_submitted',
            }
        )

    @database_sync_to_async
    def _server_now(self):
        from django.utils import timezone

        return timezone.now().isoformat()
