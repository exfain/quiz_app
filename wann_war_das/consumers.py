import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep
from games_hub.tutorial_runtime import (
    activate_tutorial_runtime,
    force_close_tutorial_runtime,
    get_tutorial_payload,
    get_tutorial_start_warning,
    mark_tutorial_completed,
)
from games_hub.unit_tutorial_runtime import (
    finish_current_unit_tutorial,
    prepare_unit_tutorial_runtime,
    start_unit_tutorial_if_needed,
    validate_unit_tutorial_request,
)

from .models import WannWarDasGame, WannWarDasParticipant, WannWarDasQuestion


class WannWarDasConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'wann_war_das_{self.room_code}'
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
        elif message_type == 'admin_start_question':
            await self.handle_admin_start_question(data)
        elif message_type == 'admin_end_question':
            await self.handle_admin_end_question(data)
        elif message_type == 'admin_end_game':
            await self.handle_admin_end_game(data)
        elif message_type == 'participant_submit_answer':
            await self.handle_participant_submit_answer(data)
        elif message_type == 'participant_join':
            await self.handle_participant_join(data)
        elif message_type == 'tutorial_completed':
            await self.handle_tutorial_completed(data)
        elif message_type == 'get_state':
            await self.send_state(data)

    async def handle_admin_start_game(self, data):
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self.get_hub_session_code()
        )
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'wann_war_das',
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

        game = await self.get_game()
        if not game:
            return
        play_tutorial = bool(data.get('play_tutorial'))
        validation = await database_sync_to_async(validate_unit_tutorial_request)(
            'wann_war_das',
            self.room_code,
            play_tutorial,
        )
        if not validation.get('success'):
            await self.send(text_data=json.dumps({
                'type': validation.get('type', 'error'),
                'message': validation.get('message') or 'Tutorialfrage fehlt.',
            }))
            return

        activation = await database_sync_to_async(resolve_session_game_activation_for_room)(
            'wann_war_das',
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

        await database_sync_to_async(prepare_unit_tutorial_runtime)(
            'wann_war_das',
            self.room_code,
            hub_session,
            play_tutorial,
            validate=False,
        )
        await database_sync_to_async(game.start_quiz)(hub_session)
        tutorial_payload = await database_sync_to_async(activate_tutorial_runtime)(
            'wann_war_das',
            self.room_code,
            hub_session,
            game,
            bool(data.get('show_tutorial')),
        )
        await self.broadcast_state({'type': 'game_started'}, hub_session)
        await self.hub_mirror_event('quiz_started', {
            'game_key': 'wann_war_das',
            'room_code': self.room_code,
            'title': game.title,
            'message': 'Wann war das? gestartet.',
        }, hub_session)
        if tutorial_payload:
            await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_start', **tutorial_payload})

    async def handle_admin_start_question(self, data):
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
                'message': 'Starte zuerst das Spiel, bevor du eine Frage startest.',
            }))
            return
        if not bool(data.get('force_start')):
            warning = await database_sync_to_async(get_tutorial_start_warning)('wann_war_das', self.room_code, hub_session)
            if warning:
                await self.send(text_data=json.dumps({'type': 'tutorial_ack_warning', **warning}))
                return
        else:
            await database_sync_to_async(force_close_tutorial_runtime)('wann_war_das', self.room_code, hub_session, game)

        unit_tutorial = await database_sync_to_async(start_unit_tutorial_if_needed)(
            'wann_war_das',
            self.room_code,
            hub_session,
        )
        is_tutorial_round = bool(unit_tutorial.get('is_tutorial_round'))
        question_id = unit_tutorial.get('tutorial_question_id') if is_tutorial_round else data.get('question_id')
        question = await self.get_question(question_id)
        if not question:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Frage nicht gefunden.'}))
            return
        if not is_tutorial_round and await self.is_configured_tutorial_question(game.id, question.id):
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Die Tutorialfrage kann nicht als gewertete Frage gestartet werden.',
            }))
            return
        allowed = await self.question_is_selected(game.id, question.id)
        if not allowed:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Diese Frage gehoert nicht zum Spiel.'}))
            return
        started = await database_sync_to_async(game.start_question)(question, hub_session, is_tutorial_round)
        if not started:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Die Frage konnte fuer diesen Spieldurchlauf nicht gestartet werden.',
            }))
            return
        await self.broadcast_state({'type': 'question_started'}, hub_session)

    async def handle_participant_submit_answer(self, data):
        game = await self.get_game()
        if not game:
            return
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code() or '').strip()
        participant = await self.get_participant(participant_name, hub_session)
        result = await database_sync_to_async(game.submit_answer)(participant, data.get('answer'))
        answer, error = result
        if not answer:
            await self.send(text_data=json.dumps({'type': 'answer_rejected', 'message': error or 'Antwort abgelehnt.'}))
            return
        await self.send(text_data=json.dumps({
            'type': 'answer_submitted',
            'answer': answer.to_dict(reveal=False),
        }))
        await self.broadcast_state({'type': 'participant_answered'}, hub_session)

    async def handle_admin_end_question(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        await database_sync_to_async(finish_current_unit_tutorial)('wann_war_das', self.room_code, hub_session)
        await database_sync_to_async(game.reveal_current_question)()
        await self.broadcast_state({'type': 'question_ended'}, hub_session)

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
            'game_key': 'wann_war_das',
            'room_code': self.room_code,
            'title': game.title,
        }, hub_session)

    async def handle_participant_join(self, data):
        participant_name = (data.get('participant_name') or data.get('name') or '').strip()
        hub_session = (data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code() or '').strip()
        if participant_name:
            await self.get_or_create_participant(participant_name, hub_session)
            tutorial_payload = await database_sync_to_async(get_tutorial_payload)(
                'wann_war_das',
                self.room_code,
                hub_session,
                participant_name,
            )
            if tutorial_payload:
                await self.send(text_data=json.dumps({'type': 'tutorial_start', **tutorial_payload}))
        await self.send_state(data)

    async def handle_tutorial_completed(self, data):
        participant_name = data.get('participant_name') or data.get('name')
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        progress = await database_sync_to_async(mark_tutorial_completed)(
            'wann_war_das',
            self.room_code,
            hub_session,
            participant_name,
        )
        await self.channel_layer.group_send(self.room_group_name, {'type': 'tutorial_progress', **progress})

    async def send_state(self, data):
        game = await self.get_game()
        if not game:
            return
        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self.get_hub_session_code()
        participant_name = data.get('participant_name') or data.get('name')
        state = await database_sync_to_async(game.serialize_state)(hub_session, participant_name)
        await self.send(text_data=json.dumps({'type': 'wann_war_das_state', **state}))

    async def broadcast_state(self, extra=None, hub_session=None):
        game = await self.get_game()
        if not game:
            return
        state = await database_sync_to_async(game.serialize_state)(hub_session)
        extra = extra or {}
        event = {'type': 'wann_war_das_state', 'event_type': extra.get('type'), **state}
        event.update({key: value for key, value in extra.items() if key != 'type'})
        await self.channel_layer.group_send(self.room_group_name, event)

    async def wann_war_das_state(self, event):
        await self.send(text_data=json.dumps(event))

    async def tutorial_start(self, event):
        await self.send(text_data=json.dumps(event))

    async def tutorial_progress(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def get_game(self):
        return WannWarDasGame.objects.filter(room_code=self.room_code).first()

    @database_sync_to_async
    def get_question(self, question_id):
        if not question_id:
            return None
        return WannWarDasQuestion.objects.filter(id=question_id, is_active=True).first()

    @database_sync_to_async
    def question_is_selected(self, game_id, question_id):
        game = WannWarDasGame.objects.get(id=game_id)
        return not game.selected_questions.exists() or game.selected_questions.filter(id=question_id).exists()

    @database_sync_to_async
    def is_configured_tutorial_question(self, game_id, question_id):
        game = WannWarDasGame.objects.get(id=game_id)
        return bool(game.tutorial_question_id and str(game.tutorial_question_id) == str(question_id))

    @database_sync_to_async
    def get_participant(self, name, hub_session):
        if not name:
            return None
        return WannWarDasParticipant.objects.filter(
            quiz__room_code=self.room_code,
            name=name,
            hub_session_code=hub_session or '',
        ).first()

    @database_sync_to_async
    def get_or_create_participant(self, name, hub_session):
        game = WannWarDasGame.objects.get(room_code=self.room_code)
        participant, _ = WannWarDasParticipant.objects.get_or_create(
            quiz=game,
            name=name,
            hub_session_code=hub_session or '',
            defaults={'is_active': True},
        )
        return participant

    @database_sync_to_async
    def get_hub_session_code(self):
        step = (
            HubGameStep.objects.select_related('session')
            .filter(game_key='wann_war_das', room_code=self.room_code, session__ended_at__isnull=True)
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
