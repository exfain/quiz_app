import asyncio
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from . import runtime
from .identity import valid_player_token
from games_hub.host_permissions import authorize_game_host
from .models import (
    BlackJackAnswer,
    BlackJackParticipant,
    BlackJackQuestion,
    BlackJackQuiz,
    BlackJackSession,
)
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import (
    current_snapshot,
    finish_question_flow,
    open_answering,
    present_question,
    reset_question_flow,
)
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import GameRuntimeState, HubGameStep
from games_hub.tutorial_runtime import (
    activate_tutorial_runtime,
    deactivate_tutorial_runtime,
    force_close_tutorial_runtime,
    get_tutorial_payload,
    get_tutorial_start_warning,
    mark_tutorial_completed,
)
from games_hub.unit_tutorial_runtime import (
    finish_current_unit_tutorial,
    is_unit_tutorial_question,
    prepare_unit_tutorial_runtime,
    start_unit_tutorial_if_needed,
    validate_unit_tutorial_request,
)
from QuizGame.presentation import question_typewriter_timing


class BlackJackConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'blackjack'
    authoritative_required_actions = frozenset({'participant_submit_answer'})

    @staticmethod
    def _is_state_payload(payload):
        # Only build_state observes the complete public context. Partial join/answer
        # events must not alter the shared context or invalidate other players.
        return False

    @database_sync_to_async
    def get_quiz_model(self):
        return BlackJackQuiz.objects.get(room_code=self.room_code)

    @database_sync_to_async
    def is_host(self):
        if self._authoritative_participant_name():
            return False
        return authorize_game_host(getattr(self, 'scope', {}).get('user'), 'blackjack', self.room_code,
            self._authoritative_session_code() or None).allowed

    async def authorized_participant(self, data):
        name = self._authoritative_participant_name() or data.get('participant_name') or data.get('name')
        if data.get('participant_name', data.get('name', name)) != name:
            return None
        code = self._authoritative_session_code() or data.get('hub_session') or data.get('hub_session_code') or ''
        def lookup():
            return BlackJackParticipant.objects.filter(quiz__room_code=self.room_code, name=name).filter(runtime.pool_filter(code)).first()
        participant = await database_sync_to_async(lookup)()
        token = getattr(self, '_player_token', None) or data.get('player_token')
        if not participant or not valid_player_token(token, participant):
            return None
        self._player_token = token
        return participant

    async def websocket_receive(self, message):
        try:
            data = json.loads(message.get('text') or '{}')
        except (TypeError, ValueError):
            data = {}
        kind = data.get('type', '')
        if kind.startswith('participant_') or kind == 'tutorial_completed' or (kind in {'get_state', 'snapshot_request'} and not await self.is_host()):
            if not await self.authorized_participant(data):
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Teilnahme nicht bestaetigt.'}))
                return
        await super().websocket_receive(message)

    async def send_state(self, data):
        quiz = await self.get_quiz_model()
        participant = None
        if self._authoritative_participant_name() or not await self.is_host():
            participant = await self.authorized_participant(data)
            if not participant:
                return
        code = participant.hub_session_code if participant else (data.get('hub_session') or quiz.active_hub_session_code or None)
        state = await database_sync_to_async(runtime.build_state)(quiz, code, participant, host=participant is None)
        await self.send(text_data=json.dumps({'type': 'blackjack_state', **state}))

    async def state_updated(self, event):
        if self._authoritative_participant_name():
            await self.send_state({})

    async def event_matches_pool(self, event):
        if not self._authoritative_participant_name() and not await self.is_host():
            return False
        quiz = await self.get_quiz_model()
        code = self._authoritative_session_code() or quiz.active_hub_session_code
        return (event.get('hub_session_code', quiz.active_hub_session_code) or '') == (code or '')

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'blackjack_{self.room_code}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to BlackJack quiz session'
        }))

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    # Receive message from WebSocket
    async def receive(self, text_data):
        try:
            text_data_json = json.loads(text_data)
            message_type = text_data_json.get('type')
            
            print("BlackJack Consumer: ", message_type)
            if message_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(text_data_json)
            elif message_type == 'admin_send_question':
                await self.handle_admin_send_question(text_data_json)
            elif message_type == 'admin_open_answering':
                await self.handle_admin_open_answering(text_data_json)
            elif message_type == 'admin_end_question':
                await self.handle_admin_end_question(text_data_json)
            elif message_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(text_data_json)
            elif message_type == 'admin_set_inactive':
                await self.handle_admin_set_inactive(text_data_json)
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
            elif message_type == 'participant_question_timeout':
                await self.handle_participant_question_timeout(text_data_json)
            elif message_type == 'participant_join':
                await self.handle_participant_join(text_data_json)
            elif message_type == 'tutorial_completed':
                await self.handle_tutorial_completed(text_data_json)
            elif message_type == 'ping':
                await self.handle_ping()
            elif message_type == 'admin_show_leaderboard':
                await self.handle_admin_show_leaderboard()
            elif message_type == 'admin_hide_leaderboard':
                await self.handle_admin_hide_leaderboard()

        except (ValueError, TypeError) as exc:
            await self.send(text_data=json.dumps({'type': 'error', 'message': str(exc)}))
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))

    async def handle_admin_start_quiz(self, data):
        """Handle admin starting the quiz"""
        existing = await self.get_quiz_model()
        code = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
        existing_code = await database_sync_to_async(runtime.pool_code)(existing)
        if existing.started_at and existing.status in {'active', 'inactive'} and (code or '') == existing_code:
            activation = await database_sync_to_async(resolve_session_game_activation_for_room)('blackjack', self.room_code, session_code=code)
            if not activation.get('success'):
                raise ValueError(activation.get('message') or activation.get('error'))
            await database_sync_to_async(runtime.start_game)(await self.get_quiz_model(), code)
            await self.channel_layer.group_send(self.room_group_name, {'type': 'state_updated'})
            await self.send(text_data=json.dumps({'type': 'quiz_started', 'status': 'active', 'started_at': existing.started_at.isoformat(), 'hub_session_code': code, 'message': 'Spiel fortgesetzt.'}))
            return
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'blackjack',
            self.room_code,
            session_code=code,
        )
        if not lobby_ready.get('allowed', True):
            await self.send(text_data=json.dumps({
                'type': 'participants_not_in_lobby',
                'message': lobby_ready.get('message') or 'Noch nicht alle Teilnehmer sind in der Lobby.',
                'not_in_lobby_count': lobby_ready.get('not_in_lobby_count', 0),
                'participants_not_in_lobby': lobby_ready.get('participants_not_in_lobby', []),
            }))
            return
        quiz = await self.get_quiz()
        if quiz:
            show_tutorial = bool(data.get('show_tutorial', False))
            play_tutorial = bool(data.get('play_tutorial', False))
            hub_session_code = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'blackjack',
                self.room_code,
                play_tutorial,
            )
            if not unit_tutorial_validation.get('success'):
                await self.send(text_data=json.dumps({
                    'type': unit_tutorial_validation.get('type', 'error'),
                    'message': unit_tutorial_validation.get('message') or 'Tutorialset fehlt.',
                }))
                return
            activation = await database_sync_to_async(resolve_session_game_activation_for_room)(
                'blackjack',
                self.room_code,
                session_code=hub_session_code,
            )
            if not activation.get('success'):
                payload = {
                    'type': 'active_game_conflict' if activation.get('conflict') else 'error',
                    'message': activation.get('message') or activation.get('error') or 'Unable to start this game.',
                }
                if activation.get('active_game'):
                    payload['active_game'] = activation['active_game']
                await self.send(text_data=json.dumps(payload))
                return
            await database_sync_to_async(prepare_unit_tutorial_runtime)(
                'blackjack',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            await database_sync_to_async(runtime.start_game)(await self.get_quiz_model(), hub_session_code)
            started_quiz = await self.get_quiz()
            await database_sync_to_async(reset_question_flow)(
                game_key='blackjack',
                room_code=self.room_code,
                session_code=hub_session_code,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            tutorial_payload = await self.activate_tutorial_runtime(
                quiz.get('id'),
                hub_session_code,
                show_tutorial,
            )
            
            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'BlackJack Quiz has started!',
                    'status': started_quiz.get('status', 'active') if started_quiz else 'active',
                    'started_at': started_quiz.get('started_at') if started_quiz else None,
                    'timestamp': started_quiz.get('started_at') if started_quiz else None,
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'blackjack',
                'message': 'BlackJack Quiz has started!',
                'status': started_quiz.get('status', 'active') if started_quiz else 'active',
                'started_at': started_quiz.get('started_at') if started_quiz else None,
            })
            if tutorial_payload:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'tutorial_start',
                        **tutorial_payload,
                    }
                )

    async def handle_admin_send_question(self, data):
        quiz = await self.get_quiz()
        if await self.guard_tutorial_before_first_unit(data, quiz['id']):
            return
        code = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
        decision = await self.present_blackjack_question(quiz['id'], data.get('question_id'), code, data, data.get('custom_time_limit'))
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, data.get('question_id'))
            return
        await self.channel_layer.group_send(self.room_group_name, {'type': 'question_started',
            'question': decision.snapshot['question'], 'hub_session_code': code, **self.question_lifecycle_fields(decision.snapshot)})

    async def handle_admin_open_answering(self, data):
        quiz = await self.get_quiz()
        if not quiz or not quiz.get('current_question_id'):
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Es ist keine aktuelle Frage vorhanden.',
            }))
            return
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.open_blackjack_answering(
            quiz.get('id'),
            quiz.get('current_question_id'),
            hub_session,
            data,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(
                decision,
                quiz.get('current_question_id'),
            )
            return
        current_question_data = await self.get_current_question_data()
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_answering_opened',
                'question': current_question_data,
                **self.question_lifecycle_fields(decision.snapshot),
            },
        )

    async def send_question_phase_rejection(self, decision, question_id=None):
        await self.send(text_data=json.dumps({
            'type': 'action_rejected',
            'code': decision.code,
            'message': decision.message,
            'question_id': question_id,
            'snapshot': decision.snapshot,
        }))

    @staticmethod
    def question_lifecycle_fields(snapshot):
        snapshot = snapshot or {}
        return {
            key: snapshot.get(key)
            for key in (
                'state_revision',
                'server_now',
                'game_id',
                'question_flow_mode',
                'question_phase',
                'question_presented_at',
                'question_visible_at',
                'question_presentation_duration_ms',
                'question_reveal_ms_per_character',
                'content_revealed_at',
                'answering_started_at',
                'answering_deadline_at',
                'answering_allowed',
                'timer_running',
                'remaining_answer_time',
                'starts_at',
                'ends_at',
            )
        }

    async def handle_admin_end_question(self, data):
        await self.finish_current_question(data)

    async def handle_participant_question_timeout(self, data):
        # The host owns question closure; participant clocks never grade a round.
        await self.send_state(data)

    async def finish_current_question(self, data=None):
        quiz = await self.get_quiz_model()
        code = (data or {}).get('hub_session') or await self._get_hub_session_code_for_room()
        result = await database_sync_to_async(runtime.end_question)(quiz, code=code, data=data)
        await database_sync_to_async(runtime.build_state)(await self.get_quiz_model(), code)
        await self.channel_layer.group_send(self.room_group_name, result)
        if result.get('quiz_complete'):
            await self.hub_mirror_event('quiz_ended', {'game_key': 'blackjack', 'room_code': self.room_code, 'final_scores': result['final_scores']})
        return result

    async def handle_admin_end_quiz(self, data):
        quiz = await self.get_quiz_model()
        code = data.get('hub_session') or await self._get_hub_session_code_for_room()
        result = await database_sync_to_async(runtime.end_game)(quiz, code=code, data=data)
        await database_sync_to_async(runtime.build_state)(await self.get_quiz_model(), code)
        await self.channel_layer.group_send(self.room_group_name, {**result, 'hub_session_code': code})
        await self.hub_mirror_event('quiz_ended', {'game_key': 'blackjack', 'room_code': self.room_code, 'final_scores': result.get('final_scores', [])})

    async def handle_admin_set_inactive(self, data):
        quiz = await self.get_quiz_model()
        await database_sync_to_async(runtime.set_paused)(quiz, True)
        await self.channel_layer.group_send(self.room_group_name, {'type': 'quiz_inactive', 'message': 'Spiel pausiert.', 'hub_session_code': quiz.active_hub_session_code})
        await self.channel_layer.group_send(self.room_group_name, {'type': 'state_updated'})

    async def handle_participant_submit_answer(self, data):
        participant = await self.authorized_participant(data)
        if not participant:
            raise ValueError('Teilnahme nicht bestaetigt.')
        answer = await database_sync_to_async(runtime.submit)(await self.get_quiz_model(), participant, data.get('user_answer'), data=data)
        await database_sync_to_async(runtime.build_state)(await self.get_quiz_model(), participant.hub_session_code)
        await self.send(text_data=json.dumps({'type': 'answer_submitted', 'user_answer': answer.user_answer, 'answer_locked': True}))
        await self.send_state(data)
        await self.channel_layer.group_send(self.room_group_name, {'type': 'state_updated'})
        await self.channel_layer.group_send(self.room_group_name, {'type': 'participant_answered',
            'hub_session_code': participant.hub_session_code or '', 'answer': {'participant_name': participant.name,
            'user_answer': answer.user_answer, 'points_earned': answer.points_earned, 'difference': answer.points_earned}})

    async def handle_participant_join(self, data):
        participant = await self.authorized_participant(data)
        if not participant:
            raise ValueError('Teilnahme nicht bestaetigt.')
        self._authoritative_participant = participant.name
        self._authoritative_session = participant.hub_session_code or ''
        await self.mark_participant_active(participant.pk)
        await self.send_state(data)
        tutorial = await self.get_tutorial_payload(participant.quiz_id, participant.hub_session_code, participant.name)
        if tutorial:
            await self.send(text_data=json.dumps({'type': 'tutorial_start', **tutorial}))
        await self.channel_layer.group_send(self.room_group_name, {'type': 'participant_joined',
            'participant': {'id': participant.pk, 'name': participant.name,
                'total_points': participant.total_points, 'is_busted': participant.is_busted},
            'hub_session_code': participant.hub_session_code or ''})

    async def handle_tutorial_completed(self, data):
        participant_name = data.get('participant_name') or data.get('name')
        hub_session = data.get('hub_session') or data.get('hub_session_code')
        progress = await self.mark_tutorial_completed(participant_name, hub_session)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'tutorial_progress', **progress}
        )

    async def handle_admin_show_leaderboard(self):
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'show_leaderboard'}
        )

    async def show_leaderboard(self, event):
        if not await self.event_matches_pool(event):
            return
        await self.send(text_data=json.dumps({'type': 'show_leaderboard'}))

    async def handle_admin_hide_leaderboard(self):
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'hide_leaderboard'}
        )

    async def hide_leaderboard(self, event):
        if not await self.event_matches_pool(event):
            return
        await self.send(text_data=json.dumps({'type': 'hide_leaderboard'}))

    async def handle_ping(self):
        """Handle ping for keeping connection alive"""
        await self.send(text_data=json.dumps({
            'type': 'pong'
        }))

    # Event handlers for group messages
    async def quiz_started(self, event):
        if not await self.event_matches_pool(event):
            return
        """Send quiz started message"""
        await self.send(text_data=json.dumps({
            'type': 'quiz_started',
            'message': event.get('message', 'Spiel beendet.'),
            'status': event.get('status', 'active'),
            'started_at': event.get('started_at'),
            'timestamp': event.get('timestamp') or event.get('started_at'),
        }))

    async def question_started(self, event):
        if not await self.event_matches_pool(event):
            return
        """Send new question to client"""
        await self.send(text_data=json.dumps({
            'type': 'question_started',
            'question': event['question'],
            **self.question_lifecycle_fields(event),
        }))

    async def question_answering_opened(self, event):
        if not await self.event_matches_pool(event):
            return
        await self.send(text_data=json.dumps({
            'type': 'question_answering_opened',
            'question': event['question'],
            **self.question_lifecycle_fields(event),
        }))

    async def question_ending(self, event):
        if not await self.event_matches_pool(event):
            return
        await self.send(text_data=json.dumps({
            'type': 'question_ending',
            'question_id': event.get('question_id'),
        }))

    async def question_ended(self, event):
        if not await self.event_matches_pool(event):
            return
        """Send question ended message"""
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'message': event['message'],
            'correct_answer': event.get('correct_answer'),
            'quiz_complete': event.get('quiz_complete', False),
            'set_complete': event.get('set_complete', False),
            'set_number': event.get('set_number'),
            'set_results': event.get('set_results', []),
            'no_answer_bust_participants': event.get('no_answer_bust_participants', []),
            'final_scores': event.get('final_scores', []),
            'is_tutorial_round': event.get('is_tutorial_round', False),
        }))

    async def tutorial_start(self, event):
        await self.send(text_data=json.dumps({
            'type': 'tutorial_start',
            'game_title': event.get('game_title'),
            'tutorial_title': event.get('tutorial_title'),
            'tutorial_text': event.get('tutorial_text'),
            'official_participants': event.get('official_participants', []),
            'completed': event.get('completed', 0),
            'total': event.get('total', 0),
            'all_done': event.get('all_done', False),
            'participants': event.get('participants', []),
        }))

    async def tutorial_progress(self, event):
        await self.send(text_data=json.dumps({
            'type': 'tutorial_progress',
            'completed': event.get('completed', 0),
            'total': event.get('total', 0),
            'all_done': event.get('all_done', False),
            'participants': event.get('participants', []),
        }))

    async def tutorial_force_close(self, event):
        await self.send(text_data=json.dumps({'type': 'tutorial_force_close'}))

    async def guard_tutorial_before_first_unit(self, data, quiz_id):
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        warning = await self.get_tutorial_start_warning(hub_session)
        if warning and not data.get('force_tutorial_continue'):
            await self.send(text_data=json.dumps({
                'type': 'tutorial_ack_warning',
                'original_message': data,
                **warning,
            }))
            return True
        if data.get('force_tutorial_continue'):
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'tutorial_force_close'},
            )
        return False

    async def quiz_ended(self, event):
        if not await self.event_matches_pool(event):
            return
        """Send quiz ended message"""
        await self.send(text_data=json.dumps({
            'type': 'quiz_ended',
            'message': event.get('message', 'Spiel beendet.'),
            'final_scores': event.get('final_scores', [])
        }))

    async def quiz_inactive(self, event):
        if not await self.event_matches_pool(event):
            return
        await self.send(text_data=json.dumps({
            'type': 'quiz_inactive',
            'message': event.get('message', 'Quiz has been set inactive.')
        }))

    async def participant_answered(self, event):
        if await self.is_host() and await self.event_matches_pool(event):
            await self.send(text_data=json.dumps({'type': 'participant_answered', 'answer': event['answer']}))

    async def participant_joined(self, event):
        if await self.is_host() and await self.event_matches_pool(event):
            await self.send(text_data=json.dumps({'type': 'participant_joined', 'participant': event['participant']}))

    # Database operations
    @database_sync_to_async
    def get_current_question_data(self):
        quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
        return runtime.build_state(quiz).get('question')

    @database_sync_to_async
    def get_current_participant_answer(self, participant_id, question_id):
        answer = BlackJackAnswer.objects.filter(
            quiz__room_code=self.room_code,
            participant_id=participant_id,
            question_id=question_id,
        ).first()
        if not answer:
            return None
        return {
            'question_id': answer.question_id,
            'user_answer': answer.user_answer,
            'points_earned': answer.points_earned,
            'time_taken': answer.time_taken,
            'answer_locked': True,
        }

    @database_sync_to_async
    def get_quiz(self):
        try:
            quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
            return {
                'id': quiz.id,
                'room_code': quiz.room_code,
                'status': quiz.status,
                'title': quiz.title,
                'tutorial_enabled': quiz.tutorial_enabled,
                'tutorial_active': quiz.tutorial_active,
                'started_at': quiz.started_at.isoformat() if quiz.started_at else None,
                'current_question_number': quiz.current_question_number,
                'total_questions': quiz.total_questions,
                'total_game_questions': quiz.get_total_game_questions(),
                'question_in_set': quiz.get_current_question_position_in_set() or 1,
                'set_question_count': quiz.get_set_question_count(
                    question_id=quiz.current_question_id if quiz.current_question_id else None,
                    set_number=quiz.get_current_set_number(),
                ),
                'set_number': quiz.get_current_set_number(),
                'total_sets': quiz.get_total_sets(),
                'current_question_id': quiz.current_question_id,
            }
        except BlackJackQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return BlackJackQuestion.objects.get(id=question_id)
        except BlackJackQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            return quiz.has_configured_question_pool()
        except BlackJackQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_available_for_next_turn(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            return quiz.is_configured_question(question_id) and question_id in quiz.get_remaining_question_ids_for_next_turn()
        except BlackJackQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def get_next_question_send_error(self, quiz_id: int, question_id=None, selected_set_number=None):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            return quiz.get_next_question_send_error(question_id, selected_set_number=selected_set_number)
        except BlackJackQuiz.DoesNotExist:
            return 'Quiz not found.'

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_points': participant.total_points,
                'overall_points': participant.overall_points,
                'is_busted': participant.is_busted,
                'status': participant.get_status()
            }
        except (BlackJackQuiz.DoesNotExist, BlackJackParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            quiz.start_quiz()
            return {
                'status': quiz.status,
                'started_at': quiz.started_at.isoformat() if quiz.started_at else None,
            }
        except BlackJackQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def ensure_runtime_scoped_to_hub_session(self, hub_session_code=None):
        try:
            quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
            return quiz.ensure_runtime_scoped_to_hub_session(hub_session_code)
        except BlackJackQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        return runtime.end_game(BlackJackQuiz.objects.get(pk=quiz_id))

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        return runtime.set_paused(BlackJackQuiz.objects.get(pk=quiz_id), True)

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('blackjack', self.room_code, None, quiz)
        except BlackJackQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            payload = get_tutorial_payload('blackjack', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except BlackJackQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            return activate_tutorial_runtime('blackjack', self.room_code, hub_session_code, quiz, show_tutorial)
        except BlackJackQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('blackjack', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('blackjack', self.room_code, hub_session_code)

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('blackjack', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('blackjack', self.room_code, hub_session_code)

    @database_sync_to_async
    def present_blackjack_question(self, quiz_id, question_id, hub_session_code, action, answer_duration_seconds, *, is_tutorial_round=False, at=None):
        return runtime.present(BlackJackQuiz.objects.get(pk=quiz_id), question_id, hub_session_code, action, answer_duration_seconds, at=at)

    @database_sync_to_async
    def open_blackjack_answering(self, quiz_id, question_id, hub_session_code, action, at=None):
        return runtime.open_question(BlackJackQuiz.objects.get(pk=quiz_id), hub_session_code, action, at=at)

    @database_sync_to_async
    def clear_current_question(self, quiz_id, expected_question_id=None):
        quiz = BlackJackQuiz.objects.get(pk=quiz_id)
        if expected_question_id and quiz.current_question_id != expected_question_id:
            return {'already_ended': True}
        return runtime.end_question(quiz)

    @database_sync_to_async
    def get_current_question_answer(self, quiz_data):
        """Get the correct answer for the current question"""
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_data['id'])
            if quiz.current_question:
                resolved_session_code = self._get_hub_session_code_for_room_sync()
                return {
                    'question_id': quiz.current_question_id,
                    'correct_answer': quiz.current_question.correct_answer,
                    'explanation': quiz.current_question.explanation,
                    'is_tutorial_round': is_unit_tutorial_question(
                        'blackjack',
                        self.room_code,
                        resolved_session_code,
                        quiz.current_question_id,
                    ),
                }
        except BlackJackQuiz.DoesNotExist:
            pass
        return None

    @database_sync_to_async
    def get_question_timeout_state(self, question_id=None):
        try:
            quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
        except BlackJackQuiz.DoesNotExist:
            return {'active': False, 'expired': False, 'current_question_id': None, 'time_left': None}

        current_question_id = quiz.current_question_id
        if not current_question_id or quiz.status != 'active':
            return {
                'active': False,
                'expired': False,
                'current_question_id': current_question_id,
                'time_left': None,
            }

        try:
            requested_question_id = int(question_id) if question_id is not None else None
        except (TypeError, ValueError):
            requested_question_id = None

        if requested_question_id and requested_question_id != current_question_id:
            return {
                'active': False,
                'expired': False,
                'current_question_id': current_question_id,
                'time_left': None,
            }

        session = getattr(quiz, 'session', None)
        question_end_time = session.question_end_time if session else None
        if not question_end_time:
            return {
                'active': True,
                'expired': False,
                'current_question_id': current_question_id,
                'time_left': None,
            }

        now = timezone.now()
        return {
            'active': True,
            'expired': now >= question_end_time,
            'current_question_id': current_question_id,
            'time_left': max(0, int((question_end_time - now).total_seconds() + 0.999)),
        }

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session_code, user_answer, time_taken, question_id=None):
        quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
        if str(question_id) != str(quiz.current_question_id):
            return None
        participant = quiz.participants.filter(name=participant_name).filter(runtime.pool_filter(hub_session_code)).first()
        try:
            answer = runtime.submit(quiz, participant, user_answer)
        except ValueError:
            return None
        participant.refresh_from_db()
        return {'user_answer': answer.user_answer, 'points_earned': answer.points_earned, 'difference': answer.points_earned,
            'total_points': participant.total_points, 'overall_points': participant.overall_points, 'is_busted': participant.is_busted,
            'questions_answered': participant.questions_answered, 'question_number': answer.question_number,
            'set_question_count': quiz.get_set_question_count(question_id=answer.question_id), 'time_taken': answer.time_taken,
            'status': participant.get_status(), 'is_tutorial_round': runtime.current_run(quiz.session)['is_tutorial']}

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = BlackJackParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except BlackJackParticipant.DoesNotExist:
            pass

    @database_sync_to_async
    def get_final_scores(self):
        return runtime.final_scores(BlackJackQuiz.objects.get(room_code=self.room_code))

    # --- Hub mirroring helpers ---
    def _get_hub_session_code_for_room_sync(self):
        try:
            game = BlackJackQuiz.objects.get(room_code=self.room_code)
            if game.active_hub_session_code or (getattr(game, 'session', None) and game.session.set_rounds):
                return game.active_hub_session_code or None
            qs = HubGameStep.objects.select_related('session').filter(game_key='blackjack', room_code=self.room_code)
            active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
            step = active or qs.order_by('-id').first()
            return step.session.code if step else None
        except Exception:
            return None

    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        return self._get_hub_session_code_for_room_sync()

    async def hub_mirror_event(self, event_type: str, payload: dict):
        session_code = await self._get_hub_session_code_for_room()
        if not session_code:
            return
        group_name = f'hub_{session_code}'
        await self.channel_layer.group_send(group_name, {
            'type': 'hub_event',
            'event': {
                'type': event_type,
                **payload,
            }
        })
