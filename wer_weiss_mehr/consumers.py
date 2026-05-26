import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep

from .models import WerWeissMehrGame, WerWeissMehrParticipant, WerWeissMehrSession
from .services import (
    apply_manual_correction,
    build_game_state,
    clear_current_set,
    end_current_round,
    finish_set,
    next_round_or_finish,
    start_set,
    start_next_round_after_review,
    store_pending_input,
    submit_answer,
)


class WerWeissMehrConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'werweissmehr_{self.room_code}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send_json({'type': 'connection_established', 'room_code': self.room_code})

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data or '{}')
        except json.JSONDecodeError:
            await self.send_json({'type': 'error', 'message': 'Invalid JSON'})
            return

        msg_type = data.get('type')
        try:
            if msg_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(data)
            elif msg_type == 'admin_set_inactive':
                await self.handle_admin_set_inactive()
            elif msg_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(data)
            elif msg_type == 'admin_start_set':
                await self.handle_admin_start_set(data)
            elif msg_type == 'admin_end_round':
                await self.handle_admin_end_round(data)
            elif msg_type == 'admin_apply_correction':
                await self.handle_admin_apply_correction(data)
            elif msg_type == 'admin_next_round':
                await self.handle_admin_next_round(data)
            elif msg_type == 'admin_finish_set':
                await self.handle_admin_finish_set(data)
            elif msg_type == 'admin_clear_set':
                await self.handle_admin_clear_set()
            elif msg_type == 'participant_join':
                await self.handle_participant_join(data)
            elif msg_type == 'participant_input_changed':
                await self.handle_participant_input_changed(data)
            elif msg_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(data)
            elif msg_type == 'get_state':
                await self.send_state(data)
            elif msg_type == 'ping':
                await self.send_json({'type': 'pong'})
        except Exception as exc:  # pylint: disable=broad-except
            await self.send_json({'type': 'error', 'message': str(exc)})

    async def send_json(self, payload):
        await self.send(text_data=json.dumps(payload, default=str))

    async def state_updated(self, event):
        await self.send_json({'type': 'state_updated', **event.get('payload', {})})

    async def quiz_started(self, event):
        await self.send_json({'type': 'quiz_started', **event})

    async def quiz_ended(self, event):
        await self.send_json({'type': 'quiz_ended', **event})

    async def handle_admin_start_quiz(self, data):
        hub_session_code = data.get('hub_session_code') or data.get('hub_session')
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'wer_weiss_mehr',
            self.room_code,
            hub_session_code,
        )
        if not lobby_ready.get('allowed', True):
            await self.send_json({
                'type': 'participants_not_in_lobby',
                'message': lobby_ready.get('message') or 'Noch nicht alle Teilnehmer sind in der Lobby.',
                'not_in_lobby_count': lobby_ready.get('not_in_lobby_count', 0),
                'participants_not_in_lobby': lobby_ready.get('participants_not_in_lobby', []),
            })
            return

        activation = await database_sync_to_async(resolve_session_game_activation_for_room)(
            'wer_weiss_mehr',
            self.room_code,
            session_code=hub_session_code,
        )
        if not activation.get('success'):
            await self.send_json({
                'type': 'active_game_conflict' if activation.get('conflict') else 'error',
                'message': activation.get('message') or activation.get('error') or 'Unable to start this game.',
                'active_game': activation.get('active_game'),
            })
            return

        await self.start_quiz_db()
        await self.send_state(data)
        await self.channel_layer.group_send(self.room_group_name, {
            'type': 'quiz_started',
            'message': 'Wer weiß mehr wurde gestartet.',
        })
        await self.broadcast_state(data)
        await self.hub_mirror_event('quiz_started', {
            'room_code': self.room_code,
            'game_key': 'wer_weiss_mehr',
        }, session_code=hub_session_code)
        await self.hub_navigate_to_game({
            'room_code': self.room_code,
            'game_key': 'wer_weiss_mehr',
        }, session_code=hub_session_code)

    async def handle_admin_set_inactive(self):
        await self.set_inactive_db()
        await self.broadcast_state({})

    async def handle_admin_end_quiz(self, data=None):
        data = data or {}
        hub_session_code = data.get('hub_session_code') or data.get('hub_session')
        final_scores = await self.end_quiz_db(hub_session_code)
        await self.channel_layer.group_send(self.room_group_name, {
            'type': 'quiz_ended',
            'message': 'Wer weiß mehr wurde beendet.',
            'final_scores': final_scores,
        })
        await self.broadcast_state(data)
        await self.hub_mirror_event('quiz_ended', {
            'room_code': self.room_code,
            'game_key': 'wer_weiss_mehr',
            'final_scores': final_scores,
        }, session_code=hub_session_code)

    async def handle_admin_start_set(self, data):
        await database_sync_to_async(start_set)(
            await self.get_quiz(),
            data.get('question_id'),
            hub_session_code=data.get('hub_session_code') or data.get('hub_session'),
            time_limit_seconds=data.get('time_limit_seconds'),
        )
        await self.broadcast_state(data)
        await self.hub_mirror_event('question_started', {
            'room_code': self.room_code,
            'game_key': 'wer_weiss_mehr',
            'question_id': data.get('question_id'),
        }, session_code=data.get('hub_session_code') or data.get('hub_session'))

    async def handle_admin_end_round(self, data=None):
        data = data or {}
        hub_session_code = data.get('hub_session_code') or data.get('hub_session')
        await database_sync_to_async(end_current_round)(await self.get_quiz())
        await self.broadcast_state(data)
        await self.hub_mirror_event('round_ended', {
            'room_code': self.room_code,
            'game_key': 'wer_weiss_mehr',
        }, session_code=hub_session_code)

    async def handle_admin_apply_correction(self, data):
        await database_sync_to_async(apply_manual_correction)(
            await self.get_quiz(),
            data.get('response_id'),
            data.get('target_answer_id'),
        )
        await self.broadcast_state(data)

    async def handle_admin_next_round(self, data=None):
        data = data or {}
        hub_session_code = data.get('hub_session_code') or data.get('hub_session')
        await database_sync_to_async(start_next_round_after_review)(await self.get_quiz())
        await self.broadcast_state(data)
        await self.hub_mirror_event('round_started', {
            'room_code': self.room_code,
            'game_key': 'wer_weiss_mehr',
        }, session_code=hub_session_code)

    async def handle_admin_finish_set(self, data=None):
        data = data or {}
        hub_session_code = data.get('hub_session_code') or data.get('hub_session')
        await database_sync_to_async(finish_set)(await self.get_quiz())
        await self.broadcast_state(data)
        await self.hub_mirror_event('question_ended', {
            'room_code': self.room_code,
            'game_key': 'wer_weiss_mehr',
        }, session_code=hub_session_code)

    async def handle_admin_clear_set(self):
        await database_sync_to_async(clear_current_set)(await self.get_quiz())
        await self.broadcast_state({})

    async def handle_participant_join(self, data):
        name = (data.get('name') or data.get('participant_name') or '').strip()
        if not name:
            await self.send_json({'type': 'error', 'message': 'Name is required.'})
            return
        await self.get_or_create_participant(name, data.get('hub_session_code') or data.get('hub_session'))
        await self.send_state(data)
        await self.broadcast_state(data)

    async def handle_participant_input_changed(self, data):
        name = (data.get('name') or data.get('participant_name') or '').strip()
        participant = await self.get_participant(name, data.get('hub_session_code') or data.get('hub_session'))
        if participant:
            await database_sync_to_async(store_pending_input)(
                await self.get_quiz(),
                participant,
                data.get('answer_text') or '',
            )

    async def handle_participant_submit_answer(self, data):
        name = (data.get('name') or data.get('participant_name') or '').strip()
        participant = await self.get_participant(name, data.get('hub_session_code') or data.get('hub_session'))
        if not participant:
            await self.send_json({'type': 'error', 'message': 'Participant not found.'})
            return
        await database_sync_to_async(submit_answer)(
            await self.get_quiz(),
            participant,
            data.get('answer_text') or '',
        )
        await self.broadcast_state(data)

    async def send_state(self, data):
        quiz = await self.get_quiz()
        state = await database_sync_to_async(build_game_state)(
            quiz,
            hub_session_code=data.get('hub_session_code') or data.get('hub_session'),
            participant_name=data.get('participant_name') or data.get('name'),
        )
        await self.send_json({'type': 'state', **state})

    async def broadcast_state(self, data):
        await self.channel_layer.group_send(self.room_group_name, {
            'type': 'state_updated',
            'payload': {
                'hub_session_code': data.get('hub_session_code') or data.get('hub_session'),
            },
        })

    @database_sync_to_async
    def get_quiz(self):
        return WerWeissMehrGame.objects.get(room_code=self.room_code)

    @database_sync_to_async
    def start_quiz_db(self):
        quiz = WerWeissMehrGame.objects.get(room_code=self.room_code)
        quiz.start_quiz()
        WerWeissMehrSession.objects.get_or_create(quiz=quiz)

    @database_sync_to_async
    def set_inactive_db(self):
        quiz = WerWeissMehrGame.objects.get(room_code=self.room_code)
        quiz.set_inactive()

    @database_sync_to_async
    def end_quiz_db(self, hub_session_code=None):
        quiz = WerWeissMehrGame.objects.get(room_code=self.room_code)
        quiz.end_quiz()
        participants = quiz.participants.all()
        if hub_session_code is not None:
            participants = participants.filter(hub_session_code=hub_session_code)
        return list(participants.order_by('-total_score', 'name').values('name', 'total_score'))

    @database_sync_to_async
    def get_or_create_participant(self, name, hub_session_code=None):
        quiz = WerWeissMehrGame.objects.get(room_code=self.room_code)
        participant, _ = WerWeissMehrParticipant.objects.get_or_create(
            quiz=quiz,
            name=name,
            hub_session_code=hub_session_code or None,
            defaults={'is_active': True},
        )
        if not participant.is_active:
            participant.is_active = True
            participant.save(update_fields=['is_active'])
        return participant

    @database_sync_to_async
    def get_participant(self, name, hub_session_code=None):
        if not name:
            return None
        return WerWeissMehrParticipant.objects.filter(
            quiz__room_code=self.room_code,
            name=name,
            hub_session_code=hub_session_code or None,
        ).first()

    @database_sync_to_async
    def _get_hub_session_code_for_room(self, requested_session_code=None):
        if requested_session_code:
            return requested_session_code
        qs = HubGameStep.objects.select_related('session').filter(game_key='wer_weiss_mehr', room_code=self.room_code)
        active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
        step = active or qs.order_by('-id').first()
        return step.session.code if step else None

    async def hub_mirror_event(self, event_type, payload, session_code=None):
        session_code = session_code or payload.get('hub_session_code')
        session_code = await self._get_hub_session_code_for_room(session_code)
        if not session_code:
            return
        await self.channel_layer.group_send(f'hub_{session_code}', {
            'type': 'hub_event',
            'event': {
                'type': event_type,
                **payload,
            },
        })

    async def hub_navigate_to_game(self, payload, session_code=None):
        session_code = session_code or payload.get('hub_session_code')
        session_code = await self._get_hub_session_code_for_room(session_code)
        if not session_code:
            return
        await self.channel_layer.group_send(f'hub_{session_code}', {
            'type': 'navigate',
            'step': {
                'index': -1,
                'order': -1,
                'game_key': payload.get('game_key'),
                'room_code': payload.get('room_code'),
                'title': payload.get('title', ''),
            },
        })
