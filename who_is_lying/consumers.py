import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import (
    WhoQuiz,
    WhoParticipant,
    WhoQuestion,
    WhoAnswer,
    WhoSession,
    get_question_timer_state,
    remember_recently_ended_question,
    clear_recently_ended_question,
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


logger = logging.getLogger(__name__)


class WhoConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'who'
    authoritative_required_actions = frozenset({'participant_submit_answer'})

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'who_{self.room_code}'
        self.who_participant_id = None

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()
        logger.info(
            'Who game socket connected',
            extra={
                'who_room_code': self.room_code,
                'who_channel_name': self.channel_name,
                'game_connected_at': timezone.now().isoformat(),
            },
        )

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to Who is Lying quiz session'
        }))

    async def disconnect(self, close_code):
        logger.info(
            'Who game socket disconnected',
            extra={
                'who_room_code': self.room_code,
                'who_participant_id': self.who_participant_id,
                'who_channel_name': self.channel_name,
                'game_disconnected_at': timezone.now().isoformat(),
                'close_code': close_code,
            },
        )
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
            
            print("Who_is_lying Consumer: ", message_type)
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
            elif message_type == 'admin_set_time_per_person':
                await self.handle_admin_set_time_per_person(text_data_json)
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
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

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))

    async def handle_admin_start_quiz(self, data):
        """Handle admin starting the quiz"""
        requested_hub_session = str(
            data.get('hub_session') or data.get('hub_session_code') or ''
        ).strip()
        hub_session_code = await self._get_hub_session_code_for_room(
            requested_hub_session or None
        )
        if requested_hub_session and not hub_session_code:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'This Who is Lying game is not part of the requested hub session.',
            }))
            return
        quiz = await self.get_quiz()
        if not quiz:
            return
        if quiz.status == 'active' and quiz.started_at:
            await self.send(text_data=json.dumps({
                'type': 'quiz_started',
                'message': 'Who is Lying? Quiz has already started.',
            }))
            return
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'who',
            self.room_code,
            session_code=hub_session_code,
        )
        if not lobby_ready.get('allowed', True):
            logger.warning(
                'Who game start rejected by lobby presence guard',
                extra={
                    'who_room_code': self.room_code,
                    'not_in_lobby_count': lobby_ready.get('not_in_lobby_count', 0),
                    'participants_not_in_lobby': [
                        entry.get('name')
                        for entry in lobby_ready.get('participants_not_in_lobby', [])
                    ],
                },
            )
            await self.send(text_data=json.dumps({
                'type': 'participants_not_in_lobby',
                'message': lobby_ready.get('message') or 'Noch nicht alle Teilnehmer sind in der Lobby.',
                'not_in_lobby_count': lobby_ready.get('not_in_lobby_count', 0),
                'participants_not_in_lobby': lobby_ready.get('participants_not_in_lobby', []),
            }))
            return
        if quiz:
            show_tutorial = bool(data.get('show_tutorial', False))
            play_tutorial = bool(data.get('play_tutorial', False))
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'who',
                self.room_code,
                play_tutorial,
            )
            if not unit_tutorial_validation.get('success'):
                await self.send(text_data=json.dumps({
                    'type': unit_tutorial_validation.get('type', 'error'),
                    'message': unit_tutorial_validation.get('message') or 'Tutorialfrage fehlt.',
                }))
                return
            activation = await database_sync_to_async(resolve_session_game_activation_for_room)(
                'who',
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
                'who',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            started = await self.start_quiz_db(
                quiz.id,
                quiz.status,
                quiz.started_at,
            )
            if not started:
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Who is Lying? Quiz has already started.',
                }))
                return
            await database_sync_to_async(reset_question_flow)(
                game_key='who',
                room_code=self.room_code,
                session_code=hub_session_code,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            tutorial_payload = await self.activate_tutorial_runtime(quiz.id, hub_session_code, show_tutorial)
            
            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'Who is Lying? Quiz has started!'
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'who',
                'message': 'Who is Lying? Quiz has started!'
            }, session_code=hub_session_code)
            if tutorial_payload:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'tutorial_start',
                        **tutorial_payload,
                    }
                )

    async def handle_admin_send_question(self, data):
        """Handle admin sending a new question"""
        # Optional per-send override for time limit (seconds)
        try:
            custom_time_limit = int(data.get('custom_time_limit')) if data.get('custom_time_limit') is not None else None
            if custom_time_limit is not None and custom_time_limit <= 0:
                custom_time_limit = None
        except (TypeError, ValueError):
            custom_time_limit = None

        question_id = data.get('question_id')
        quiz = await self.get_quiz()
        
        if not quiz:
            return
            
        question = await self.get_question(question_id)
        if not question:
            return

        # If quiz has a predefined set, enforce membership
        try:
            has_selected = await self.quiz_has_selected_questions(quiz.id)
            if has_selected:
                allowed = await self.is_question_in_selected(quiz.id, question.id)
                if not allowed:
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'This question is not part of the selected set for this quiz.'
                    }))
                    return
        except Exception:
            pass

        if await self.guard_tutorial_before_first_unit(data, quiz.id):
            return

        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        unit_tutorial = await self.start_unit_tutorial_if_needed(hub_session)
        is_tutorial_round = bool(unit_tutorial.get('is_tutorial_round'))
        if is_tutorial_round and str(unit_tutorial.get('tutorial_question_id') or '') != str(question.id):
            question = await self.get_question(unit_tutorial.get('tutorial_question_id'))
            if not question:
                return

        await self.set_tutorial_active_db(quiz.id, False)
        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit

        phase_action = dict(data)
        phase_action['question_id'] = question.id
        decision = await self.present_who_question(
            quiz.id,
            question.id,
            hub_session,
            phase_action,
            effective_time_limit,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question.id)
            return
        question_payload = await self.get_current_question_data(hub_session)
        if not question_payload:
            return
        question_payload['is_tutorial_round'] = is_tutorial_round
        question_payload['points'] = 0 if is_tutorial_round else question_payload.get('points')
        question_payload['total_possible_points'] = 0 if is_tutorial_round else question_payload.get('total_possible_points')
        
        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': question_payload,
                **self.question_lifecycle_fields(decision.snapshot),
            }
        )

    async def handle_admin_open_answering(self, data):
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Es ist kein aktuelles Set vorhanden.',
            }))
            return
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.open_who_answering(
            quiz.id,
            quiz.current_question_id,
            hub_session,
            data,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, quiz.current_question_id)
            return
        question_payload = await self.get_current_question_data(hub_session)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_answering_opened',
                'question': question_payload,
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
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
            phase_snapshot = await database_sync_to_async(current_snapshot)(
                'who', self.room_code, hub_session,
            )
            if (
                phase_snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
                and phase_snapshot.get('question_phase')
                != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
            ):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Das Set kann vor dem Start nicht beendet werden.',
                }))
                return
            await self.remember_recently_ended_current_question(quiz.id)
            unit_tutorial = await self.finish_current_unit_tutorial(hub_session)
            await database_sync_to_async(finish_question_flow)(
                game_key='who',
                room_code=self.room_code,
                session_code=hub_session,
                question_id=quiz.current_question_id,
            )
            await self.clear_current_question(quiz.id)
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Question time is up!',
                    'is_tutorial_round': bool(unit_tutorial.get('is_tutorial_round')),
                }
            )

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            await self.end_quiz_db(quiz.id)
            # Collect final scores
            final_scores = await self.get_final_scores()
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_ended',
                    'message': 'Who is Lying? Quiz has ended. Thank you for participating!',
                    'final_scores': final_scores
                }
            )

            # Mirror to hub to auto-advance session
            # await self.hub_mirror_event('game_ended', {
            #     'room_code': self.room_code,
            #     'game_key': 'who'
            # })
            # Mirror to hub so hub can advance to next step or end session
            await self.hub_mirror_event('quiz_ended', {
                'room_code': self.room_code,
                'game_key': 'who',
                'message': 'Quiz has ended. Thank you for participating!',
                'final_scores': final_scores
            })

    async def handle_admin_set_inactive(self, data):
        """Pause the quiz without clearing its current progress."""
        quiz = await self.get_quiz()
        if quiz and quiz.status == 'active':
            await self.set_quiz_inactive_db(quiz.id)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_inactive',
                    'message': 'Quiz has been set inactive.'
                }
            )

    async def handle_admin_set_time_per_person(self, data):
        quiz = await self.get_quiz()
        if not quiz or quiz.current_question_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Time per person cannot be changed during a running question.'
            }))
            return
        try:
            seconds = int(data.get('time_per_person') or 0)
        except (TypeError, ValueError):
            return
        if seconds <= 0:
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'time_per_person_updated',
                'time_per_person': seconds,
            }
        )

    async def handle_participant_submit_answer(self, data):
        """Handle participant submitting their answer"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        selected_liars = data.get('selected_liars', [])
        time_taken = data.get('time_taken', 0)
        question_id = data.get('question_id')

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name, hub_session, selected_liars, time_taken, question_id
        )
        
        if answer:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'question_id': answer['question_id'],
                'question_number': answer['question_number'],
                'points_earned': answer['points_earned'],
                'max_points': answer['max_points'],
                'progress_history': answer['progress_history'],
                'is_tutorial_round': answer['is_tutorial_round'],
                'correct_identifications': answer['correct_identifications'],
                'total_people': answer['total_people'],
                'accuracy': answer['accuracy'],
                'analysis': answer['analysis'],
                'person_results': answer['person_results'],
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'points_earned': answer['points_earned'],
                        'is_tutorial_round': answer['is_tutorial_round'],
                        'correct_identifications': answer['correct_identifications'],
                        'total_people': answer['total_people'],
                        'selected_liars_names': answer['selected_liars_names'],
                        'time_taken': answer['time_taken'],
                        'accuracy': answer['accuracy']
                    }
                }
            )
        else:
            await self.send(text_data=json.dumps({
                'type': 'answer_rejected',
                'message': 'Answer was not accepted.',
                'question_id': question_id,
            }))

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        participant = await self.get_participant_by_name(participant_name, hub_session)
        
        if participant:
            self.who_participant_id = participant['id']
            await self.mark_participant_active(participant['id'])
            logger.info(
                'Who participant game presence activated',
                extra={
                    'who_room_code': self.room_code,
                    'who_participant_id': participant['id'],
                    'hub_session_code': hub_session,
                    'game_connected_at': timezone.now().isoformat(),
                },
            )
            
            # Broadcast to admin
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_joined',
                    'participant': {
                        'name': participant['name'],
                        'total_score': participant['total_score']
                    }
                }
            )

            # If quiz is already active, send quiz_started directly to this participant
            quiz = await self.get_quiz()
            if quiz and quiz.status == 'active':
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Quiz is already in progress'
                }))
                tutorial_payload = await self.get_tutorial_payload(
                    quiz.id,
                    hub_session,
                    participant_name=participant_name,
                )
                if tutorial_payload:
                    await self.send(text_data=json.dumps({
                        'type': 'tutorial_start',
                        **tutorial_payload,
                    }))
                current_question_data = await self.get_current_question_data(hub_session)
                if current_question_data:
                    current_phase = current_question_data.get('question_phase')
                    await self.send(text_data=json.dumps({
                        'type': (
                            'question_answering_opened'
                            if current_phase == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
                            else 'question_started'
                        ),
                        'question': current_question_data,
                        **self.question_lifecycle_fields(current_question_data),
                    }))
                    existing_answer = await self.get_current_participant_answer(
                        participant['id'],
                        current_question_data['id'],
                    )
                    if existing_answer:
                        await self.send(text_data=json.dumps({
                            'type': 'answer_submitted',
                            'message': 'Answer already submitted',
                            **existing_answer,
                        }))
                else:
                    reveal_data = await self.get_recent_participant_reveal(
                        participant_name,
                        hub_session,
                    )
                    if reveal_data:
                        await self.send(text_data=json.dumps({
                            'type': 'set_revealed',
                            'phase': 'revealed',
                            **reveal_data,
                        }))

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
        await self.send(text_data=json.dumps({'type': 'show_leaderboard'}))

    async def handle_admin_hide_leaderboard(self):
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'hide_leaderboard'}
        )

    async def hide_leaderboard(self, event):
        await self.send(text_data=json.dumps({'type': 'hide_leaderboard'}))

    async def handle_ping(self):
        """Handle ping for keeping connection alive"""
        await self.send(text_data=json.dumps({
            'type': 'pong'
        }))

    # Event handlers for group messages
    async def quiz_started(self, event):
        """Send quiz started message"""
        await self.send(text_data=json.dumps({
            'type': 'quiz_started',
            'message': event['message']
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

    async def question_started(self, event):
        """Send new question to client"""
        await self.send(text_data=json.dumps({
            'type': 'question_started',
            'question': event['question'],
            **self.question_lifecycle_fields(event),
        }))

    async def question_answering_opened(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_answering_opened',
            'question': event['question'],
            **self.question_lifecycle_fields(event),
        }))

    async def question_ended(self, event):
        """Send question ended message"""
        reveal_data = await self.get_recent_participant_reveal(
            self._authoritative_participant_name(),
            self._authoritative_session_code(),
        )
        if reveal_data:
            await self.send(text_data=json.dumps({
                'type': 'set_revealed',
                'phase': 'revealed',
                **reveal_data,
            }))
            return
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'message': event['message']
        }))

    async def quiz_ended(self, event):
        """Send quiz ended message"""
        await self.send(text_data=json.dumps({
            'type': 'quiz_ended',
            'message': event['message'],
            'final_scores': event.get('final_scores', [])
        }))

    async def quiz_inactive(self, event):
        await self.send(text_data=json.dumps({
            'type': 'quiz_inactive',
            'message': event.get('message', 'Quiz has been set inactive.')
        }))

    async def participant_answered(self, event):
        """Send participant answer to admin"""
        await self.send(text_data=json.dumps({
            'type': 'participant_answered',
            'answer': event['answer']
        }))

    async def participant_joined(self, event):
        """Send new participant info to admin"""
        await self.send(text_data=json.dumps({
            'type': 'participant_joined',
            'participant': event['participant']
        }))

    async def time_per_person_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'time_per_person_updated',
            'time_per_person': event.get('time_per_person'),
        }))

    # Database operations
    @database_sync_to_async
    def get_current_question_data(self, hub_session=None):
        """Return serialised question data for the currently active question, or None."""
        try:
            quiz = WhoQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            question_data = self.get_question_data_sync(question)
            try:
                session = quiz.session
            except Exception:
                session = None
            server_now = timezone.now()
            runtime = current_snapshot('who', self.room_code, hub_session)
            manual_flow = (
                runtime.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            answering_open = (
                not manual_flow
                or runtime.get('question_phase')
                == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
            )
            timer_state = get_question_timer_state(
                question,
                question_start_time=quiz.question_start_time,
                question_end_time=session.question_end_time if session else None,
                people_count=len(question_data['people']),
                server_now=server_now,
            )
            prepared_duration = runtime.get('answer_duration_seconds')
            if not quiz.question_start_time and question_data['people']:
                try:
                    prepared_time_per_person = int(
                        round(float(prepared_duration) / len(question_data['people']))
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    prepared_time_per_person = 0
                if prepared_time_per_person > 0:
                    timer_state['time_per_person'] = prepared_time_per_person
                    timer_state['current_person_time_left'] = prepared_time_per_person
            exposed_people = question_data['people'] if answering_open else []
            return {
                'id': question.id,
                'question_number': self.get_question_number_for_quiz_value(question.id),
                'total_sets': max(
                    len(quiz.question_order or []),
                    quiz.selected_questions.count(),
                    int(session.total_questions_sent or 0) if session else 0,
                ),
                'statement': question.statement,
                'time_limit': timer_state['time_per_person'],
                'time_per_person': timer_state['time_per_person'],
                'points': question.points,
                'people': exposed_people,
                'total_possible_points': question_data['total_possible_points'],
                'current_person_index': timer_state['current_person_index'],
                'current_person_time_left': timer_state['current_person_time_left'],
                'question_started_at': quiz.question_start_time.isoformat() if quiz.question_start_time else None,
                'question_end_time': session.question_end_time.isoformat() if session and session.question_end_time else None,
                'starts_at': quiz.question_start_time.isoformat() if quiz.question_start_time else None,
                'ends_at': session.question_end_time.isoformat() if session and session.question_end_time else None,
                'server_now': server_now.isoformat(),
                **self.question_lifecycle_fields(runtime),
            }
        except WhoQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_current_participant_answer(self, participant_id, question_id):
        answer = WhoAnswer.objects.select_related(
            'quiz',
            'participant',
            'question',
        ).filter(
            quiz__room_code=self.room_code,
            participant_id=participant_id,
            question_id=question_id,
        ).first()
        if not answer:
            return None
        score_progress = self._build_participant_score_progress(
            answer.quiz,
            answer.participant,
            answer.participant.hub_session_code,
            answer.question,
        )
        return {
            'question_id': answer.question_id,
            'selected_liars': answer.selected_liars,
            'points_earned': answer.points_earned,
            'time_taken': answer.time_taken,
            'answer_locked': True,
            **score_progress,
        }

    @database_sync_to_async
    def get_recent_participant_reveal(self, participant_name, hub_session_code):
        if not participant_name or not hub_session_code:
            return None
        quiz = (
            WhoQuiz.objects
            .select_related('session__recently_ended_question')
            .filter(room_code=self.room_code)
            .first()
        )
        if not quiz:
            return None
        try:
            session = quiz.session
        except WhoSession.DoesNotExist:
            return None
        question = session.recently_ended_question
        if not question:
            return None
        participant = WhoParticipant.objects.filter(
            quiz=quiz,
            name=participant_name,
            hub_session_code=hub_session_code,
        ).first()
        if not participant:
            return None
        answer = WhoAnswer.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
        ).first()
        selected_liars = set(answer.selected_liars or []) if answer else set()
        randomized_data = question.get_randomized_people(room_code=self.room_code)
        person_results = []
        for displayed_person in randomized_data['people']:
            original_index = displayed_person['original_index']
            original_person = question.people[original_index]
            is_lying = bool(original_person.get('is_lying', False))
            was_selected = original_index in selected_liars
            person_results.append({
                'name': displayed_person['name'],
                'is_lying': is_lying,
                'was_selected': was_selected,
                'was_correct': is_lying == was_selected,
                'points_effect': 1 if is_lying and was_selected else -1 if was_selected else 0,
            })
        if answer:
            analysis = answer.get_detailed_analysis()
            correct_identifications = answer.get_correct_identifications_count()
            accuracy = answer.get_accuracy_percentage()
            points_earned = answer.points_earned
            selected_liars_names = answer.get_selected_liars_names()
            time_taken = answer.time_taken
        else:
            analysis = {
                'correct_liars': [],
                'missed_liars': [
                    person.get('name', '')
                    for person in question.people
                    if person.get('is_lying', False)
                ],
                'false_accusations': [],
                'correct_truth_tellers': [
                    person.get('name', '')
                    for person in question.people
                    if not person.get('is_lying', False)
                ],
            }
            correct_identifications = len(analysis['correct_truth_tellers'])
            accuracy = round(
                (correct_identifications / len(question.people)) * 100,
                1,
            ) if question.people else 0
            points_earned = 0
            selected_liars_names = []
            time_taken = None
        score_progress = self._build_participant_score_progress(
            quiz,
            participant,
            hub_session_code,
            question,
        )
        return {
            'question_id': question.id,
            'question_number': session.current_question_number,
            'statement': question.statement,
            'points_earned': points_earned,
            'correct_identifications': correct_identifications,
            'total_people': len(question.people or []),
            'accuracy': accuracy,
            'analysis': analysis,
            'selected_liars_names': selected_liars_names,
            'time_taken': time_taken,
            'person_results': person_results,
            'answer_locked': bool(answer),
            **score_progress,
        }

    def _build_participant_score_progress(self, quiz, participant, hub_session_code, question):
        from .views import _build_participant_progress_history

        progress_history = _build_participant_progress_history(
            quiz,
            participant,
            hub_session_code,
        )
        current_progress = next(
            (
                entry
                for entry in progress_history
                if int(entry['question_id']) == int(question.id)
            ),
            None,
        )
        return {
            'question_number': (
                current_progress['question_number']
                if current_progress
                else self.get_question_number_for_quiz_value(question.id)
            ),
            'max_points': question.get_total_possible_points(),
            'progress_history': progress_history,
        }

    @database_sync_to_async
    def get_quiz(self):
        try:
            return WhoQuiz.objects.get(room_code=self.room_code)
        except WhoQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('who', self.room_code, None, quiz)
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            payload = get_tutorial_payload('who', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except WhoQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            return activate_tutorial_runtime('who', self.room_code, hub_session_code, quiz, show_tutorial)
        except WhoQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('who', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('who', self.room_code, hub_session_code)

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('who', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('who', self.room_code, hub_session_code)

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return WhoQuestion.objects.get(id=question_id)
        except WhoQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def get_question_number_for_quiz(self, question_id):
        return self.get_question_number_for_quiz_value(question_id)

    def get_question_number_for_quiz_value(self, question_id):
        try:
            quiz = WhoQuiz.objects.prefetch_related('selected_questions').get(room_code=self.room_code)
        except WhoQuiz.DoesNotExist:
            return None

        try:
            session = quiz.session
        except Exception:
            session = None
        if (
            session
            and quiz.current_question_id
            and int(question_id) == int(quiz.current_question_id)
            and session.current_question_number > 0
        ):
            return session.current_question_number

        selected_question_ids = list(quiz.selected_questions.values_list('id', flat=True))
        answered_question_ids = []
        seen_ids = set()
        for answered_question_id in (
            WhoAnswer.objects
            .filter(quiz=quiz)
            .order_by('submitted_at', 'id')
            .values_list('question_id', flat=True)
        ):
            if answered_question_id in seen_ids:
                continue
            answered_question_ids.append(answered_question_id)
            seen_ids.add(answered_question_id)

        ordered_question_ids = list(answered_question_ids)

        if quiz.current_question_id and quiz.current_question_id not in seen_ids:
            ordered_question_ids.append(quiz.current_question_id)
            seen_ids.add(quiz.current_question_id)

        for raw_question_id in quiz.question_order or []:
            try:
                ordered_question_id = int(raw_question_id)
            except (TypeError, ValueError):
                continue
            if ordered_question_id not in seen_ids:
                ordered_question_ids.append(ordered_question_id)
                seen_ids.add(ordered_question_id)

        for candidate_id in selected_question_ids:
            if candidate_id not in seen_ids:
                ordered_question_ids.append(candidate_id)
                seen_ids.add(candidate_id)

        for index, ordered_question_id in enumerate(ordered_question_ids, start=1):
            if ordered_question_id == question_id:
                return index
        return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.exists()
        except WhoQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_in_selected(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.filter(id=question_id).exists()
        except WhoQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = WhoQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score
            }
        except (WhoQuiz.DoesNotExist, WhoParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id, previous_status, previous_started_at):
        with transaction.atomic():
            try:
                quiz = WhoQuiz.objects.select_for_update().get(id=quiz_id)
            except WhoQuiz.DoesNotExist:
                return False
            if quiz.status == 'active' and quiz.started_at:
                if previous_status == 'active' or quiz.started_at != previous_started_at:
                    return False
            quiz.status = 'active'
            quiz.started_at = timezone.now()
            quiz.save()
            clear_recently_ended_question(quiz.room_code)
            return True

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.save()
            clear_recently_ended_question(quiz.room_code)
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def update_quiz_question(self, quiz_id, question_id, time_per_person):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            question = WhoQuestion.objects.get(id=question_id)
        except (WhoQuiz.DoesNotExist, WhoQuestion.DoesNotExist):
            return

        try:
            session = quiz.session
        except Exception:
            from .models import WhoSession
            session, _ = WhoSession.objects.get_or_create(quiz=quiz)
        session.send_question(question, time_per_person=time_per_person)
        clear_recently_ended_question(quiz.room_code)

    @database_sync_to_async
    @transaction.atomic
    def present_who_question(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
        time_per_person,
        at=None,
    ):
        quiz = WhoQuiz.objects.select_for_update().get(id=quiz_id)
        question = WhoQuestion.objects.get(id=question_id)
        session, _ = WhoSession.objects.select_for_update().get_or_create(quiz=quiz)
        decision = present_question(
            game_key='who',
            room_code=self.room_code,
            session_code=hub_session_code,
            action=action,
            answer_duration_seconds=question.get_total_duration_seconds(time_per_person),
            at=at,
        )
        if decision.accepted and not decision.duplicate:
            session.prepare_question(question)
            clear_recently_ended_question(quiz.room_code)
        return decision

    @database_sync_to_async
    @transaction.atomic
    def open_who_answering(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
        at=None,
    ):
        quiz = (
            WhoQuiz.objects.select_for_update()
            .select_related('current_question')
            .get(id=quiz_id)
        )
        session = WhoSession.objects.select_for_update().get(quiz=quiz)
        if quiz.current_question_id != question_id:
            return open_answering(
                game_key='who',
                room_code=self.room_code,
                session_code=hub_session_code,
                action=action,
            )
        opened_at = at or timezone.now()
        decision = open_answering(
            game_key='who',
            room_code=self.room_code,
            session_code=hub_session_code,
            action=action,
            at=opened_at,
        )
        if decision.accepted and not decision.duplicate:
            started_at = parse_datetime(decision.snapshot.get('answering_started_at') or '')
            deadline = parse_datetime(decision.snapshot.get('answering_deadline_at') or '')
            if not started_at or not deadline:
                raise ValueError('Authoritative answering timestamps are missing.')
            session.open_answering(
                quiz.current_question,
                started_at=started_at,
                answer_duration_seconds=(deadline - started_at).total_seconds(),
            )
        return decision

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            if hasattr(quiz, 'session'):
                quiz.session.end_current_question()
            else:
                quiz.current_question = None
                quiz.question_start_time = None
                quiz.save()
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_question_data(self, question):
        return self.get_question_data_sync(question)

    def get_question_data_sync(self, question):
        # Get randomized people with room code for consistent shuffling
        randomized = question.get_randomized_people(room_code=self.room_code)
        
        return {
            'people': randomized['people'],
            'total_possible_points': question.get_total_possible_points()
        }

    @database_sync_to_async
    @transaction.atomic
    def save_participant_answer(self, participant_name, hub_session_code, selected_liars, time_taken, question_id=None):
        try:
            quiz = (
                WhoQuiz.objects.select_for_update()
                .select_related('current_question')
                .get(room_code=self.room_code)
            )
            participant = WhoParticipant.objects.select_for_update().get(
                quiz=quiz,
                name=participant_name,
                hub_session_code=hub_session_code,
            )
            
            if quiz.status != 'active':
                return None

            if (
                not quiz.current_question_id
                or question_id is None
                or str(question_id) != str(quiz.current_question_id)
            ):
                return None
            target_question = quiz.current_question
            session = WhoSession.objects.select_for_update().filter(quiz=quiz).first()
            received_at = timezone.now()
            if (
                not session
                or not session.is_question_active
                or (
                    session.question_end_time
                    and received_at >= session.question_end_time
                )
            ):
                return None
            
            # Check if answer already exists
            existing_answer = WhoAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=target_question
            ).first()
            
            if existing_answer:
                return None  # Already answered
            
            # Get the same randomized data using the same room code
            randomized_data = target_question.get_randomized_people(room_code=self.room_code)
            position_to_original = randomized_data['position_to_original']
            
            # Convert selected liars from shuffled positions to original positions
            original_selected_liars = []
            for shuffled_pos in selected_liars:
                original_idx = position_to_original.get(int(shuffled_pos))
                if original_idx is not None:
                    original_selected_liars.append(original_idx)
            
            server_time_taken = (
                max(0.0, (received_at - quiz.question_start_time).total_seconds())
                if quiz.question_start_time
                else 0.0
            )

            # Create new answer with original indices
            answer, created = WhoAnswer.objects.get_or_create(
                quiz=quiz,
                participant=participant,
                question=target_question,
                defaults={
                    'selected_liars': original_selected_liars,
                    'time_taken': server_time_taken,
                },
            )
            if not created:
                return None
            is_tutorial_answer = is_unit_tutorial_question(
                'who',
                self.room_code,
                hub_session_code,
                target_question.id,
            )
            if is_tutorial_answer and answer.points_earned:
                answer.points_earned = 0
                answer.save(update_fields=['points_earned', 'updated_at'])
            
            # Get detailed analysis
            analysis = answer.get_detailed_analysis()
            selected_liars_names = answer.get_selected_liars_names()
            selected_liars_set = set(original_selected_liars)
            person_results = []
            for displayed_person in randomized_data['people']:
                original_idx = displayed_person['original_index']
                original_person = target_question.people[original_idx]
                is_actually_lying = bool(original_person.get('is_lying', False))
                was_selected = original_idx in selected_liars_set
                if was_selected and is_actually_lying:
                    points_effect = 1
                elif was_selected and not is_actually_lying:
                    points_effect = -1
                else:
                    points_effect = 0
                if is_tutorial_answer:
                    points_effect = 0

                person_results.append({
                    'name': displayed_person['name'],
                    'is_lying': is_actually_lying,
                    'was_selected': was_selected,
                    'was_correct': is_actually_lying == was_selected,
                    'points_effect': points_effect,
                })
            
            return {
                'question_id': target_question.id,
                **self._build_participant_score_progress(
                    quiz,
                    participant,
                    hub_session_code,
                    target_question,
                ),
                'points_earned': answer.points_earned,
                'is_tutorial_round': is_tutorial_answer,
                'correct_identifications': answer.get_correct_identifications_count(),
                'total_people': answer.get_total_people_count(),
                'accuracy': answer.get_accuracy_percentage(),
                'analysis': analysis,
                'selected_liars_names': selected_liars_names,
                'time_taken': answer.time_taken,
                'person_results': person_results,
            }
            
        except (WhoQuiz.DoesNotExist, WhoParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def remember_recently_ended_current_question(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
        except WhoQuiz.DoesNotExist:
            return

        if quiz.current_question_id:
            remember_recently_ended_question(quiz.room_code, quiz.current_question_id)

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = WhoParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except WhoParticipant.DoesNotExist:
            pass

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = WhoQuiz.objects.get(room_code=self.room_code)
            # Filter by hub session code if available via HubGameStep
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='who', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
            return list(qs.values('name', 'total_score'))
        except WhoQuiz.DoesNotExist:
            return []

    # --- Hub mirroring helpers ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self, requested_session_code=None):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='who', room_code=self.room_code)
            if requested_session_code:
                step = qs.filter(session__code=requested_session_code).first()
                return step.session.code if step else None
            active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
            step = active or qs.order_by('-id').first()
            return step.session.code if step else None
        except Exception:
            return None

    async def hub_mirror_event(self, event_type: str, payload: dict, session_code=None):
        session_code = session_code or await self._get_hub_session_code_for_room()
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
