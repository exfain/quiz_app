import asyncio
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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


class BlackJackConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'blackjack'
    authoritative_required_actions = frozenset({'participant_submit_answer'})

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

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))

    async def handle_admin_start_quiz(self, data):
        """Handle admin starting the quiz"""
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'blackjack',
            self.room_code,
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
            hub_session_code = await self._get_hub_session_code_for_room()
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
            started_quiz = await self.start_quiz_db(quiz.get('id'))
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
        """Handle admin sending a new question"""
        # Optional per-send override for time limit (seconds)
        try:
            custom_time_limit = int(data.get('custom_time_limit')) if data.get('custom_time_limit') is not None else None
            if custom_time_limit is not None and custom_time_limit <= 0:
                custom_time_limit = None
        except (TypeError, ValueError):
            custom_time_limit = None

        question_id = data.get('question_id')
        selected_set_number = data.get('selected_set_number')
        quiz = await self.get_quiz()
        
        if not quiz:
            return
            
        question = await self.get_question(question_id)
        if not question:
            return

        if await self.guard_tutorial_before_first_unit(data, quiz.get('id')):
            return

        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
        unit_tutorial = await self.start_unit_tutorial_if_needed(hub_session)
        is_tutorial_round = bool(unit_tutorial.get('is_tutorial_round'))
        if is_tutorial_round and str(unit_tutorial.get('tutorial_question_id') or '') != str(question.id):
            question = await self.get_question(unit_tutorial.get('tutorial_question_id'))
            if not question:
                return

        if not is_tutorial_round:
            send_error = await self.get_next_question_send_error(quiz.get('id'), question.id, selected_set_number)
            if send_error:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': send_error
                }))
                return

        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        await self.set_tutorial_active_db(quiz.get('id'), False)
        phase_action = dict(data)
        phase_action['question_id'] = question.id
        decision = await self.present_blackjack_question(
            quiz.get('id'),
            question.id,
            hub_session,
            phase_action,
            effective_time_limit,
            is_tutorial_round=is_tutorial_round,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question.id)
            return
        current_question_data = await self.get_current_question_data()
        if not current_question_data:
            return
        current_question_data['time_limit'] = effective_time_limit
        current_question_data['is_tutorial_round'] = is_tutorial_round
        
        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': current_question_data,
                **self.question_lifecycle_fields(decision.snapshot),
            }
        )

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
        await self.finish_current_question()

    async def handle_participant_question_timeout(self, data):
        """Let a participant trigger the authoritative server timeout path."""
        timeout_state = await self.get_question_timeout_state(data.get('question_id'))
        if not timeout_state.get('expired'):
            await self.send(text_data=json.dumps({
                'type': 'question_timeout_sync',
                'active': timeout_state.get('active', False),
                'expired': timeout_state.get('expired', False),
                'current_question_id': timeout_state.get('current_question_id'),
                'time_left': timeout_state.get('time_left'),
            }))
            return

        await self.finish_current_question()

    async def finish_current_question(self):
        """End the active question and broadcast the same transition to every client."""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = await self._get_hub_session_code_for_room()
            phase_snapshot = await database_sync_to_async(current_snapshot)(
                'blackjack', self.room_code, hub_session,
            )
            if (
                phase_snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
                and phase_snapshot.get('question_phase')
                != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
            ):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Die Frage kann vor der Freigabe nicht beendet werden.',
                }))
                return None
            # Get the correct answer before clearing the question
            correct_answer_data = await self.get_current_question_answer(quiz)
            ending_question_id = correct_answer_data.get('question_id') if correct_answer_data else None

            if ending_question_id:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'question_ending',
                        'question_id': ending_question_id,
                    }
                )
                await asyncio.sleep(0.5)
            
            transition = await self.clear_current_question(quiz.get('id'), expected_question_id=ending_question_id)
            if transition.get('already_ended'):
                return transition
            if ending_question_id:
                await database_sync_to_async(finish_question_flow)(
                    game_key='blackjack',
                    room_code=self.room_code,
                    session_code=hub_session,
                    question_id=ending_question_id,
                )
            quiz_complete = transition.get('quiz_complete', False)
            final_scores = await self.get_final_scores() if quiz_complete else []
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Time\'s up!',
                    'correct_answer': correct_answer_data,
                    'quiz_complete': quiz_complete,
                    'set_complete': transition.get('set_complete', False),
                    'set_number': transition.get('set_number'),
                    'set_results': transition.get('participants', []),
                    'no_answer_bust_participants': transition.get('no_answer_bust_participants', []),
                    'final_scores': final_scores,
                    'is_tutorial_round': bool(transition.get('is_tutorial_round')),
                }
            )

            if quiz_complete:
                await self.hub_mirror_event('quiz_ended', {
                    'room_code': self.room_code,
                    'game_key': 'blackjack',
                    'message': 'Quiz has ended. Thank you for participating!',
                    'final_scores': final_scores,
                })
            return transition

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            await self.end_quiz_db(quiz.get('id'))
            hub_session = (
                data.get('hub_session')
                or data.get('hub_session_code')
                or await self._get_hub_session_code_for_room()
            )
            await database_sync_to_async(reset_question_flow)(
                game_key='blackjack',
                room_code=self.room_code,
                session_code=hub_session,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            # Collect final totals (BlackJack uses total_points)
            final_scores = await self.get_final_scores()
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_ended',
                    'message': 'BlackJack Quiz has ended. Thank you for playing!',
                    'final_scores': final_scores
                }
            )

            # Mirror to hub to auto-advance session
            # await self.hub_mirror_event('game_ended', {
            #     'room_code': self.room_code,
            #     'game_key': 'blackjack'
            # })
            # Mirror to hub so hub can advance to next step or end session
            await self.hub_mirror_event('quiz_ended', {
                'room_code': self.room_code,
                'game_key': 'blackjack',
                'message': 'Quiz has ended. Thank you for participating!',
                'final_scores': final_scores
            })

    async def handle_admin_set_inactive(self, data):
        """Pause the quiz without clearing its current progress."""
        quiz = await self.get_quiz()
        if quiz and quiz.get('status') == 'active':
            await self.set_quiz_inactive_db(quiz.get('id'))
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_inactive',
                    'message': 'Quiz has been set inactive.'
                }
            )

    async def handle_participant_submit_answer(self, data):
        """Handle participant submitting their answer"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        user_answer = data.get('user_answer')
        time_taken = data.get('time_taken', 0)
        question_id = data.get('question_id')

        # Save the answer
        answer_result = await self.save_participant_answer(
            participant_name,hub_session, user_answer, time_taken, question_id=question_id
        )
        
        if answer_result:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'points_earned': answer_result['points_earned'],
                'user_answer': answer_result['user_answer'],
                'difference': answer_result['difference'],
                'total_points': answer_result['total_points'],
                'overall_points': answer_result['overall_points'],
                'is_busted': answer_result['is_busted'],
                'is_tutorial_round': answer_result.get('is_tutorial_round', False),
                'questions_remaining': max(0, answer_result['set_question_count'] - answer_result['questions_answered'])
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'user_answer': answer_result['user_answer'],
                        'points_earned': answer_result['points_earned'],
                        'difference': answer_result['difference'],
                        'total_points': answer_result['total_points'],
                        'is_busted': answer_result['is_busted'],
                        'is_tutorial_round': answer_result.get('is_tutorial_round', False),
                        'status': answer_result['status'],
                        'time_taken': answer_result['time_taken'],
                        'question_number': answer_result['question_number']
                    }
                }
            )

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        participant = await self.get_participant_by_name(participant_name, hub_session)

        if participant:
            await self.ensure_runtime_scoped_to_hub_session(hub_session)
            await self.mark_participant_active(participant['id'])
            
            # Broadcast to admin
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_joined',
                    'participant': {
                        'name': participant['name'],
                        'total_points': participant['total_points'],
                        'is_busted': participant['is_busted'],
                        'status': participant['status']
                    }
                }
            )

            # If quiz is already active, send quiz_started directly to this participant
            quiz = await self.get_quiz()
            if quiz and quiz.get('status') == 'active':
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Quiz is already in progress',
                    'status': quiz.get('status'),
                    'started_at': quiz.get('started_at'),
                    'timestamp': quiz.get('started_at'),
                }))
                tutorial_payload = await self.get_tutorial_payload(
                    quiz.get('id'),
                    hub_session,
                    participant_name=participant_name,
                )
                if tutorial_payload:
                    await self.send(text_data=json.dumps({
                        'type': 'tutorial_start',
                        **tutorial_payload,
                    }))
                current_question_data = await self.get_current_question_data()
                if current_question_data:
                    await self.send(text_data=json.dumps({
                        'type': 'question_started',
                        'question': current_question_data
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
            'message': event['message'],
            'status': event.get('status', 'active'),
            'started_at': event.get('started_at'),
            'timestamp': event.get('timestamp') or event.get('started_at'),
        }))

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

    async def question_ending(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_ending',
            'question_id': event.get('question_id'),
        }))

    async def question_ended(self, event):
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

    # Database operations
    @database_sync_to_async
    def get_current_question_data(self):
        """Return serialised question data for the currently active question, or None."""
        try:
            quiz = BlackJackQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            session = getattr(quiz, 'session', None)
            hub_session = self._get_hub_session_code_for_room_sync()
            runtime = current_snapshot('blackjack', self.room_code, hub_session)
            is_tutorial_round = is_unit_tutorial_question('blackjack', self.room_code, hub_session, question.id)
            payload = {
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': int(runtime.get('answer_duration_seconds') or question.time_limit),
                'question_end_time': session.question_end_time.isoformat() if session and session.question_end_time else None,
                'starts_at': (
                    quiz.question_start_time.isoformat()
                    if quiz.question_start_time
                    else None
                ),
                'ends_at': (
                    session.question_end_time.isoformat()
                    if session and session.question_end_time
                    else None
                ),
                'server_now': timezone.now().isoformat(),
                'question_number': quiz.current_question_number,
                'question_in_set': quiz.get_current_question_position_in_set(),
                'set_question_count': quiz.get_set_question_count(question_id=question.id),
                'set_number': quiz.get_current_set_number(),
                'total_sets': quiz.get_total_sets(),
                'is_tutorial_round': is_tutorial_round,
            }
            payload.update(self.question_lifecycle_fields(runtime))
            return payload
        except BlackJackQuiz.DoesNotExist:
            return None

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
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.save()
        except BlackJackQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except BlackJackQuiz.DoesNotExist:
            pass

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
    @transaction.atomic
    def present_blackjack_question(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
        answer_duration_seconds,
        *,
        is_tutorial_round=False,
        at=None,
    ):
        quiz = BlackJackQuiz.objects.select_for_update().get(id=quiz_id)
        question = BlackJackQuestion.objects.get(id=question_id)
        session, _ = BlackJackSession.objects.select_for_update().get_or_create(quiz=quiz)
        decision = present_question(
            game_key='blackjack',
            room_code=self.room_code,
            session_code=hub_session_code,
            action=action,
            answer_duration_seconds=answer_duration_seconds,
            at=at,
        )
        if decision.accepted and not decision.duplicate:
            session.prepare_question(
                question,
                track_progress=not is_tutorial_round,
            )
        return decision

    @database_sync_to_async
    @transaction.atomic
    def open_blackjack_answering(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
        at=None,
    ):
        quiz = (
            BlackJackQuiz.objects.select_for_update()
            .select_related('current_question')
            .get(id=quiz_id)
        )
        session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
        if quiz.current_question_id != question_id:
            return open_answering(
                game_key='blackjack',
                room_code=self.room_code,
                session_code=hub_session_code,
                action=action,
            )
        opened_at = at or timezone.now()
        decision = open_answering(
            game_key='blackjack',
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
    def clear_current_question(self, quiz_id, expected_question_id=None):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            if expected_question_id and quiz.current_question_id != expected_question_id:
                return {
                    'already_ended': True,
                    'set_complete': False,
                    'set_number': quiz.get_current_set_number(),
                    'quiz_complete': quiz.is_quiz_complete(),
                }
            resolved_session_code = self._get_hub_session_code_for_room_sync()
            if quiz.current_question_id and is_unit_tutorial_question(
                'blackjack',
                self.room_code,
                resolved_session_code,
                quiz.current_question_id,
            ):
                try:
                    session = quiz.session
                except Exception:
                    session = None
                quiz.current_question = None
                quiz.question_start_time = None
                quiz.save(update_fields=['current_question', 'question_start_time'])
                if session:
                    session.is_question_active = False
                    session.question_end_time = None
                    session.total_responses_current_question = 0
                    session.average_points_current_question = 0
                    session.save(update_fields=[
                        'is_question_active',
                        'question_end_time',
                        'total_responses_current_question',
                        'average_points_current_question',
                    ])
                unit_tutorial = finish_current_unit_tutorial('blackjack', self.room_code, resolved_session_code)
                return {
                    'set_complete': False,
                    'set_number': quiz.get_current_set_number(),
                    'quiz_complete': False,
                    'participants': [],
                    'no_answer_bust_participants': [],
                    'is_tutorial_round': bool(unit_tutorial.get('is_tutorial_round')),
                }
            if hasattr(quiz, 'session'):
                return quiz.session.end_current_question()

            quiz.current_question = None
            quiz.question_start_time = None
            quiz.save()
            return {
                'set_complete': False,
                'set_number': quiz.get_current_set_number(),
                'quiz_complete': quiz.is_quiz_complete(),
            }
        except BlackJackQuiz.DoesNotExist:
            return {
                'set_complete': False,
                'set_number': None,
                'quiz_complete': False,
            }

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
    @transaction.atomic
    def save_participant_answer(self, participant_name, hub_session_code, user_answer, time_taken, question_id=None):
        try:
            quiz = (
                BlackJackQuiz.objects.select_for_update()
                .select_related('current_question')
                .get(room_code=self.room_code)
            )
            participant = BlackJackParticipant.objects.select_for_update().get(
                quiz=quiz,
                name=participant_name,
                hub_session_code=hub_session_code,
            )
            
            if quiz.status != 'active':
                return None
            if participant.is_busted:
                return None

            normalized_question_id = None
            try:
                normalized_question_id = int(question_id) if question_id is not None else None
            except (TypeError, ValueError):
                normalized_question_id = None

            if (
                not quiz.current_question
                or normalized_question_id is None
                or normalized_question_id != quiz.current_question_id
            ):
                return None
            target_question = quiz.current_question
            session = BlackJackSession.objects.select_for_update().filter(quiz=quiz).first()
            received_at = timezone.now()
            if (
                not session
                or not session.is_question_active
                or not quiz.question_start_time
                or received_at < quiz.question_start_time
                or (
                    session.question_end_time
                    and received_at >= session.question_end_time
                )
            ):
                return None
            is_tutorial_answer = is_unit_tutorial_question(
                'blackjack',
                self.room_code,
                hub_session_code,
                target_question.id,
            )
            
            # Check if answer already exists
            existing_answer = BlackJackAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=target_question
            ).first()
            
            if existing_answer:
                return None  # Already answered
            
            # Convert user answer to integer
            try:
                user_answer_int = int(user_answer)
            except (ValueError, TypeError):
                return None
            
            previous_participant_state = {
                'total_points': participant.total_points,
                'overall_points': participant.overall_points,
                'questions_answered': participant.questions_answered,
                'is_busted': participant.is_busted,
                'final_score': participant.final_score,
            }
            server_time_taken = (
                max(0.0, (received_at - quiz.question_start_time).total_seconds())
                if quiz.question_start_time
                else 0.0
            )

            # Create new answer
            answer, created = BlackJackAnswer.objects.get_or_create(
                quiz=quiz,
                participant=participant,
                question=target_question,
                defaults={
                    'user_answer': user_answer_int,
                    'time_taken': server_time_taken,
                    'question_number': quiz.current_question_number,
                },
            )
            if not created:
                return None
            if is_tutorial_answer:
                if answer.points_earned:
                    answer.points_earned = 0
                    answer.save(update_fields=['points_earned'])
                for field, value in previous_participant_state.items():
                    setattr(participant, field, value)
                participant.save(update_fields=list(previous_participant_state.keys()))
            
            # Refresh participant data after score calculation
            participant.refresh_from_db()
            
            return {
                'points_earned': answer.points_earned,
                'user_answer': answer.user_answer,
                'difference': answer.get_difference(),
                'total_points': participant.total_points,
                'overall_points': participant.overall_points,
                'is_busted': participant.is_busted,
                'status': participant.get_status(),
                'questions_answered': participant.questions_answered,
                'question_number': answer.question_number,
                'set_question_count': quiz.get_set_question_count(question_number=quiz.current_question_number),
                'is_tutorial_round': is_tutorial_answer,
                'time_taken': answer.time_taken,
            }
            
        except (BlackJackQuiz.DoesNotExist, BlackJackParticipant.DoesNotExist):
            return None

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
        try:
            quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
            # Filter by hub session code if available via HubGameStep
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='blackjack', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
            return list(qs.order_by('-overall_points', 'name').values(
                'name',
                'total_points',
                'overall_points',
                total_score=models.F('overall_points'),
            ))
        except BlackJackQuiz.DoesNotExist:
            return []

    # --- Hub mirroring helpers ---
    def _get_hub_session_code_for_room_sync(self):
        try:
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
