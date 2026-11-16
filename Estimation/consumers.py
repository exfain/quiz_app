from games_hub.quiz_rounds import current_round
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import EstimationQuiz, EstimationParticipant, EstimationQuestion, EstimationAnswer, EstimationSession
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import (
    current_snapshot,
    finish_question_flow,
    get_runtime_state,
    open_answering,
    observe_snapshot,
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


class EstimationConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'estimation'
    authoritative_required_actions = frozenset({
        'participant_submit_answer',
        'participant_update_pending_answer',
    })

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'estimation_{self.room_code}'
        self.participant_name = None

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to estimation quiz session'
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
            
            print("Estimation Consumer: ", message_type)
            if message_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(text_data_json)
            elif message_type == 'admin_prepare_question':
                await self.handle_admin_prepare_question(text_data_json)
            elif message_type == 'admin_clear_prepared_question':
                await self.handle_admin_clear_prepared_question(text_data_json)
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
            elif message_type == 'admin_set_scoring_mode':
                await self.handle_admin_set_scoring_mode(text_data_json)
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
            elif message_type == 'participant_update_pending_answer':
                await self.handle_participant_update_pending_answer(text_data_json)
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
        quiz = await self.get_quiz()
        if quiz and quiz.status == 'active' and quiz.started_at:
            await self.channel_layer.group_send(self.room_group_name, {'type': 'quiz_started', 'message': 'Quiz is already active.', 'resume_existing': True})
            await self.hub_mirror_event('quiz_started', {'room_code': self.room_code, 'game_key': 'estimation', 'message': 'Quiz is already active.'})
            return
        if quiz and quiz.status == 'inactive' and quiz.started_at:
            activation = await database_sync_to_async(resolve_session_game_activation_for_room)(
                'estimation', self.room_code, check_only=True)
            if not activation.get('success'):
                await self.send(text_data=json.dumps({'type': 'active_game_conflict', **activation}))
                return
            await database_sync_to_async(quiz.start_quiz)(session_code=data.get('hub_session') or None)
            return
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'estimation',
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
            reset_for_fresh_start = quiz.status == 'waiting' or quiz.started_at is None
            hub_session_code = await self._get_hub_session_code_for_room()
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'estimation',
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
                'estimation',
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
                'estimation',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            await self.start_quiz_db(quiz.id, reset_for_fresh_start=reset_for_fresh_start)
            await database_sync_to_async(reset_question_flow)(
                game_key='estimation',
                room_code=self.room_code,
                session_code=hub_session_code,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            tutorial_payload = await self.activate_tutorial_runtime(quiz.id, hub_session_code, show_tutorial)
            
            start_payload = {
                'type': 'quiz_started',
                'message': 'Estimation Quiz has started!',
                'phase': 'active',
                'revealed': False,
                'current_question_id': None,
                'starts_at': None,
                'ends_at': None,
            }
            await database_sync_to_async(observe_snapshot)(
                'estimation',
                self.room_code,
                start_payload,
                hub_session_code,
            )

            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                start_payload,
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'estimation',
                'message': 'Estimation Quiz has started!'
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

        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else 90

        await self.set_tutorial_active_db(quiz.id, False)
        phase_action = dict(data)
        phase_action['question_id'] = question.id
        decision = await self.present_estimation_question(
            quiz.id,
            question.id,
            hub_session,
            phase_action,
            effective_time_limit,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question.id)
            return

        question_data = await self.get_question_data(question)
        max_points = await self.get_question_max_points_for_quiz(quiz.id, question.id)
        question_number = await self.get_question_number_for_quiz(quiz.id, question.id)
        round_state = await self.get_current_question_data(None, hub_session)
        question_payload = {
            'type': 'question_started',
            'question': {
                'id': question.id,
                'question_text': question.question_text,
                'unit': question_data['unit'],
                'unit_display': question_data['unit_display'],
                'question_number': question_number,
                'round_number': round_state.get('round_number'),
                'scoreboard': round_state.get('scoreboard'),
                'total_questions': round_state.get('total_questions'),
                'max_points': 0 if is_tutorial_round else max_points,
                'is_tutorial_round': is_tutorial_round,
                'hint_text': question.hint_text,
                'time_limit': effective_time_limit,
            },
            **self.question_lifecycle_fields(decision.snapshot),
        }
        await self.channel_layer.group_send(self.room_group_name, question_payload)

    async def handle_admin_prepare_question(self, data):
        question_id = data.get('question_id')
        quiz = await self.get_quiz()
        if not quiz or quiz.status != 'active':
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Start the quiz before selecting questions.',
                'question_id': question_id,
            }))
            return
        question = await self.get_question(question_id)
        if not question:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Frage konnte nicht ausgewaehlt werden.',
                'question_id': question_id,
            }))
            return
        if await self.quiz_has_selected_questions(quiz.id):
            if not await self.is_question_in_selected(quiz.id, question.id):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'This question is not part of the selected set for this quiz.',
                    'question_id': question_id,
                }))
                return

        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.prepare_estimation_question(question.id, hub_session, data)
        if not decision.get('accepted'):
            await self.send(text_data=json.dumps({
                'type': 'action_rejected',
                'code': decision.get('code'),
                'message': decision.get('message'),
                'question_id': question.id,
                'snapshot': decision.get('snapshot'),
            }))
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_prepared',
                'question_id': question.id,
                **self.question_lifecycle_fields(decision.get('snapshot')),
            },
        )

    async def handle_admin_clear_prepared_question(self, data):
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.clear_prepared_estimation_question(hub_session, data)
        if not decision.get('accepted'):
            await self.send(text_data=json.dumps({
                'type': 'action_rejected',
                'code': decision.get('code'),
                'message': decision.get('message'),
                'snapshot': decision.get('snapshot'),
            }))
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_preparation_cleared',
                **self.question_lifecycle_fields(decision.get('snapshot')),
            },
        )

    async def handle_admin_open_answering(self, data):
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question_id:
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
        decision = await self.open_estimation_answering(
            quiz.id,
            quiz.current_question_id,
            hub_session,
            data,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, quiz.current_question_id)
            return
        question_payload = await self.get_current_question_data()
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
        """Handle admin ending current question"""
        from .runtime import end_question
        quiz = await self.get_quiz()
        if not quiz:
            return
        code = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
        snapshot = await database_sync_to_async(current_snapshot)('estimation', self.room_code, code)
        if (snapshot.get('question_flow_mode') == 'manual_three_phase'
                and snapshot.get('question_phase') != 'answering_open'):
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Die Frage kann vor der Freigabe nicht beendet werden.'}))
            return
        event = await database_sync_to_async(end_question)(quiz.pk, code,
            question_id=data.get('question_id'), round_number=data.get('round_number'), emit=False)
        if event:
            await self.channel_layer.group_send(self.room_group_name, event)

    async def handle_admin_set_scoring_mode(self, data):
        quiz = await self.get_quiz()
        if not quiz:
            return
        scoring_mode = (data.get('scoring_mode') or '').strip()
        updated = await self.set_scoring_mode_if_waiting(quiz.id, scoring_mode)
        if not updated:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Scoring mode can only be changed before the quiz starts.'
            }))
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'scoring_mode_updated',
                'scoring_mode': scoring_mode,
            }
        )

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = (
                data.get('hub_session')
                or data.get('hub_session_code')
                or await self._get_hub_session_code_for_room()
            )
            await self.end_quiz_db(quiz.id)
            await database_sync_to_async(reset_question_flow)(
                game_key='estimation',
                room_code=self.room_code,
                session_code=hub_session,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            # Collect final scores
            final_scores = await self.get_final_scores()
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_ended',
                    'message': 'Estimation Quiz has ended. Thank you for participating!',
                    'final_scores': final_scores
                }
            )

            # Mirror to hub to auto-advance session
            # await self.hub_mirror_event('game_ended', {
            #     'room_code': self.room_code,
            #     'game_key': 'estimation'
            # })
            # Mirror to hub so hub can advance to next step or end session
            await self.hub_mirror_event('quiz_ended', {
                'room_code': self.room_code,
                'game_key': 'estimation',
                'message': 'Quiz has ended. Thank you for participating!',
                'final_scores': final_scores
            })

    async def handle_admin_set_inactive(self, data):
        """Pause the quiz without clearing its current progress."""
        quiz = await self.get_quiz()
        if quiz and quiz.status == 'active':
            await self.set_quiz_inactive_db(quiz.id)

    async def handle_participant_submit_answer(self, data):
        """Handle participant submitting their estimation answer"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session') or None
        user_answer = data.get('user_answer')
        time_taken = data.get('time_taken', 0)
        question_id = data.get('question_id')

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name, hub_session, user_answer, time_taken, question_id
        )
        
        if answer:
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted', 'question_id': answer['question_id'],
                'round_number': answer['round_number'], 'user_answer': answer['user_answer'],
                'formatted_answer': answer['formatted_answer'], 'answer_locked': True,
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'user_answer': answer['user_answer'],
                        'formatted_answer': answer['formatted_answer'],
                        'points_earned': answer['points_earned'],
                        'is_tutorial_round': answer['is_tutorial_round'],
                        'accuracy_percentage': answer['accuracy_percentage'],
                        'percentage_difference': answer['percentage_difference'],
                        'difference_indicator': answer['difference_indicator'],
                        'time_taken': answer['time_taken']
                    }
                    }
                )
        else:
            await self.send(text_data=json.dumps({'type': 'answer_rejected', 'question_id': question_id,
                'message': 'Antwort wurde nicht gespeichert. Bitte eine gültige Zahl eingeben oder erneut versuchen.'}))

    async def handle_participant_update_pending_answer(self, data):
        """Persist the latest typed estimate so it can be evaluated when the round ends."""
        saved = await self.save_pending_answer(
            data.get('participant_name'),
            data.get('hub_session'),
            data.get('user_answer', ''),
            data.get('question_id'),
        )
        await self.send(text_data=json.dumps({'type': 'pending_answer_saved' if saved else 'pending_answer_rejected',
            'question_id': data.get('question_id'), 'round_number': data.get('round_number'),
            'user_answer': data.get('user_answer', '')}))

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session') or None
        self.participant_name = participant_name
        participant = await self.get_participant_by_name(participant_name, hub_session)
        
        if participant:
            await self.mark_participant_active(participant['id'])
            quiz = await self.get_quiz()
            if quiz and quiz.status == 'inactive':
                await self.send(text_data=json.dumps({'type': 'quiz_inactive'}))
                question = await self.get_current_question_data(participant_name, hub_session)
                if question:
                    await self.send(text_data=json.dumps({'type': 'question_started', 'question': question}))
            
            # Broadcast to admin
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_joined',
                    'participant': {
                        'id': participant['id'], 'hub_session_code': hub_session,
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
                    'message': 'Quiz is already in progress',
                    'resume_existing': True,
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
                current_question_data = await self.get_current_question_data(participant_name, hub_session)
                if current_question_data:
                    await self.send(text_data=json.dumps({
                        'type': 'question_started',
                        'question': current_question_data
                    }))
                else:
                    runtime_snapshot = await database_sync_to_async(current_snapshot)(
                        'estimation', self.room_code, hub_session,
                    )
                    if runtime_snapshot.get('question_shell_prepared'):
                        await self.send(text_data=json.dumps({
                            'type': 'question_prepared',
                            'question_id': runtime_snapshot.get('prepared_question_id'),
                            **self.question_lifecycle_fields(runtime_snapshot),
                        }))
                    else:
                        event = await self.get_reveal_state(participant_name, hub_session)
                        if event:
                            await self.send(text_data=json.dumps(event))

    async def is_host(self):
        from games_hub.host_permissions import authorize_game_host
        return (await database_sync_to_async(authorize_game_host)(getattr(self, 'scope', {}).get('user'),
            'estimation', self.room_code, self._authoritative_session_code() or None)).allowed

    async def send_state(self, data):
        code = data.get('hub_session') or data.get('hub_session_code') or None
        name = data.get('participant_name') or self._authoritative_participant_name() or None
        if not name and await self.is_host():
            from .runtime import end_question
            quiz = await self.get_quiz()
            if quiz:
                event = await database_sync_to_async(end_question)(quiz.pk, code, expired_only=True, emit=False)
                if event:
                    await self.channel_layer.group_send(self.room_group_name, event)
            return
        question = await self.get_current_question_data(name, code)
        if question:
            await self.send(text_data=json.dumps({'type': 'question_started', 'question': question}))
        elif name:
            event = await self.get_reveal_state(name, code)
            if event:
                await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def get_reveal_state(self, name, code):
        from .runtime import reveal_state
        return reveal_state(self.room_code, name, code)


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
            'resume_existing': event.get('resume_existing', False),
            'message': event['message'],
            'phase': event.get('phase'),
            'revealed': event.get('revealed', False),
            'current_question_id': event.get('current_question_id'),
            'starts_at': event.get('starts_at'),
            'ends_at': event.get('ends_at'),
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

    async def question_prepared(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_prepared',
            'question_id': event.get('question_id'),
            **self.question_lifecycle_fields(event),
        }))

    async def question_preparation_cleared(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_preparation_cleared',
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
        event = dict(event)
        if not await self.is_host():
            event['evaluated_pending_answers'] = [row for row in event.get('evaluated_pending_answers', [])
                if row.get('participant_name') == self._authoritative_participant_name()
                and (row.get('hub_session_code') or '') == self._authoritative_session_code()]
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'phase': event.get('phase'),
            'revealed': event.get('revealed', True),
            'current_question_id': event.get('current_question_id'),
            'message': event['message'],
            'question_number': event.get('question_number'), 'round_number': event.get('round_number'),
            'scoreboard': event.get('scoreboard'), 'is_tutorial_round': event.get('is_tutorial_round', False),
            'correct_answer': event.get('correct_answer'),
            'rank_results': event.get('rank_results'),
            'max_points': event.get('max_points'),
            'evaluated_pending_answers': event.get('evaluated_pending_answers', []),
        }))

    async def scoring_mode_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'scoring_mode_updated',
            'scoring_mode': event.get('scoring_mode'),
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
    async def quiz_resumed(self, event):
        await self.send(text_data=json.dumps({'type': 'quiz_resumed'}))


    async def participant_answered(self, event):
        """Send participant answer to admin"""
        if not await self.is_host():
            return
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
    def get_current_question_data(self, participant_name=None, hub_session=None):
        """Return the current question with authoritative phase metadata."""
        try:
            quiz = EstimationQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None

            try:
                session = quiz.session
            except EstimationSession.DoesNotExist:
                session = None

            runtime = current_snapshot('estimation', self.room_code, hub_session)
            question_phase = runtime.get('question_phase')
            manual_flow = (
                runtime.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            prompt_details_visible = (
                not manual_flow
                or question_phase in {
                    GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
                    GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE,
                    GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN,
                }
            )
            remaining_seconds = runtime.get('remaining_answer_time')
            if not manual_flow and session and session.question_end_time:
                remaining_seconds = max(
                    0,
                    int((session.question_end_time - timezone.now()).total_seconds() + 0.999),
                )
            if remaining_seconds is None or (not manual_flow and not session):
                remaining_seconds = 90 if not manual_flow else 0
            if remaining_seconds is None:
                remaining_seconds = 0

            elapsed_seconds = 0
            if quiz.question_start_time:
                elapsed_seconds = max(0, int((timezone.now() - quiz.question_start_time).total_seconds()))

            payload = {
                'id': question.id,
                'question_text': question.question_text,
                'unit': question.unit if prompt_details_visible else '',
                'unit_display': question.get_unit_display_text() if prompt_details_visible else '',
                'question_number': self.get_question_number_for_quiz_value(quiz, question.id),
                'max_points': question.get_max_points_for_mode(
                    quiz.get_effective_scoring_mode(),
                    self.get_participant_count_for_quiz(quiz),
                ),
                'hint_text': question.hint_text if prompt_details_visible else None,
                'time_limit': remaining_seconds,
                'elapsed_seconds': elapsed_seconds,
                'starts_at': (
                    runtime.get('answering_started_at')
                    or (quiz.question_start_time.isoformat() if quiz.question_start_time else None)
                ),
                'ends_at': (
                    runtime.get('answering_deadline_at')
                    or (
                        session.question_end_time.isoformat()
                        if not manual_flow and session and session.question_end_time
                        else None
                    )
                ),
                'server_now': runtime.get('server_now'),
                'remaining_seconds': remaining_seconds,
                'has_answered': False,
                **self.question_lifecycle_fields(runtime),
            }

            from .progress import metadata
            payload.update(metadata(quiz, hub_session))

            if participant_name is not None:
                participant = quiz.participants.filter(
                    name=participant_name,
                    hub_session_code=hub_session,
                ).first()
                if participant:
                    answer = EstimationAnswer.objects.filter(
                        quiz=quiz,
                        participant=participant,
                        question=question,
                        round_number=current_round(quiz),
                    ).first()
                    if answer:
                        payload['has_answered'] = True
                        payload['existing_answer'] = {
                            'user_answer': answer.user_answer,
                            'formatted_answer': answer.get_formatted_user_answer(),
                        }
                    elif session and (
                        not manual_flow
                        or question_phase == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
                    ):
                        pending = (session.pending_answers or {}).get(str(participant.id)) or {}
                        if str(pending.get('question_id') or '') == str(question.id):
                            payload['pending_answer'] = pending.get('user_answer', '')

            return payload
        except EstimationQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_quiz(self):
        try:
            return EstimationQuiz.objects.get(room_code=self.room_code)
        except EstimationQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('estimation', self.room_code, None, quiz)
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            payload = get_tutorial_payload('estimation', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except EstimationQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            return activate_tutorial_runtime('estimation', self.room_code, hub_session_code, quiz, show_tutorial)
        except EstimationQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('estimation', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('estimation', self.room_code, hub_session_code)

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('estimation', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('estimation', self.room_code, hub_session_code)

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return EstimationQuestion.objects.get(id=question_id)
        except EstimationQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.exists()
        except EstimationQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_in_selected(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.filter(id=question_id).exists()
        except EstimationQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = EstimationQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score
            }
        except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id, reset_for_fresh_start=None):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            if reset_for_fresh_start is None:
                reset_for_fresh_start = quiz.status == 'waiting' or quiz.started_at is None
            if reset_for_fresh_start:
                quiz.status = 'waiting'
            quiz.start_quiz()
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            quiz.end_quiz()
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            from games_hub.quiz_pause import pause
            pause(quiz, 'estimation')
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def present_estimation_question(self, quiz_id, question_id, hub_session_code, action, answer_duration_seconds, at=None):
        from .runtime import present
        return present(self.room_code, quiz_id, question_id, hub_session_code, action, answer_duration_seconds, at)

    @database_sync_to_async
    @transaction.atomic
    def prepare_estimation_question(self, question_id, hub_session_code, action):
        quiz = EstimationQuiz.objects.select_for_update().get(room_code=self.room_code)
        runtime = get_runtime_state('estimation', self.room_code, hub_session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        snapshot = dict(runtime.public_snapshot or {})
        current_prepared_id = str(snapshot.get('prepared_question_id') or '')
        requested_id = str(question_id)
        if quiz.current_question_id or runtime.current_question_id or runtime.question_phase:
            return {
                'accepted': False,
                'code': 'invalid_phase',
                'message': 'Die vorherige Frage ist noch aktiv.',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        if current_prepared_id == requested_id:
            return {
                'accepted': True,
                'code': 'accepted',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        try:
            action_revision = int(action.get('state_revision'))
        except (TypeError, ValueError):
            action_revision = -1
        if action_revision < runtime.context_revision or action_revision > runtime.state_revision:
            return {
                'accepted': False,
                'code': 'stale_action',
                'message': 'Der Spielzustand hat sich geaendert.',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        supplied_game_id = str(action.get('game_id') or '')
        if supplied_game_id and supplied_game_id != runtime.game_instance_id:
            return {
                'accepted': False,
                'code': 'stale_action',
                'message': 'Die Aktion gehoert zu einer anderen Spielinstanz.',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        if not action.get('client_action_id'):
            return {
                'accepted': False,
                'code': 'invalid_action_context',
                'message': 'client_action_id fehlt.',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        snapshot.pop('question', None)
        snapshot.pop('answer_options', None)
        snapshot.pop('answer_duration_seconds', None)
        snapshot.pop('completed_question_id', None)
        snapshot['prepared_question_id'] = requested_id
        snapshot['question_shell_prepared'] = True
        snapshot['revealed'] = False
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        runtime.public_snapshot = snapshot
        runtime.save(update_fields=[
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        return {
            'accepted': True,
            'code': 'accepted',
            'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
        }

    @database_sync_to_async
    @transaction.atomic
    def clear_prepared_estimation_question(self, hub_session_code, action):
        quiz = EstimationQuiz.objects.select_for_update().get(room_code=self.room_code)
        runtime = get_runtime_state('estimation', self.room_code, hub_session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        if quiz.current_question_id or runtime.current_question_id or runtime.question_phase:
            return {
                'accepted': False,
                'code': 'invalid_phase',
                'message': 'Eine bereits gesendete Frage kann nicht zurueckgenommen werden.',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        snapshot = dict(runtime.public_snapshot or {})
        if not snapshot.get('prepared_question_id'):
            return {
                'accepted': True,
                'code': 'accepted',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        try:
            action_revision = int(action.get('state_revision'))
        except (TypeError, ValueError):
            action_revision = -1
        if action_revision < runtime.context_revision or action_revision > runtime.state_revision:
            return {
                'accepted': False,
                'code': 'stale_action',
                'message': 'Der Spielzustand hat sich geaendert.',
                'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
            }
        snapshot.pop('prepared_question_id', None)
        snapshot['question_shell_prepared'] = True
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        runtime.public_snapshot = snapshot
        runtime.save(update_fields=[
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        return {
            'accepted': True,
            'code': 'accepted',
            'snapshot': current_snapshot('estimation', self.room_code, hub_session_code),
        }

    @database_sync_to_async
    def open_estimation_answering(self, quiz_id, question_id, hub_session_code, action, at=None):
        from .runtime import open_question
        return open_question(self.room_code, quiz_id, question_id, hub_session_code, action, at)

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            if hasattr(quiz, 'session'):
                quiz.session.end_current_question()
            else:
                quiz.current_question = None
                quiz.question_start_time = None
                quiz.save()
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_current_question_max_points(self, quiz):
        if quiz.current_question:
            return quiz.current_question.get_max_points_for_mode(
                quiz.get_effective_scoring_mode(),
                self.get_participant_count_for_quiz(quiz),
            )
        return 0

    @database_sync_to_async
    def set_scoring_mode_if_waiting(self, quiz_id, scoring_mode):
        if scoring_mode not in ('zones', 'tolerance', 'rank'):
            return False
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
        except EstimationQuiz.DoesNotExist:
            return False
        if quiz.status != 'waiting':
            return False
        quiz.scoring_mode = scoring_mode
        quiz.save(update_fields=['scoring_mode'])
        return True

    @database_sync_to_async
    def get_current_question_answer(self, quiz):
        """Get the correct answer for the current question"""
        if quiz.current_question:
            scoring_mode = quiz.get_effective_scoring_mode()
            return {
                'correct_answer': quiz.current_question.correct_answer,
                'formatted_answer': quiz.current_question.get_formatted_correct_answer(),
                'unit': quiz.current_question.unit,
                'explanation': quiz.current_question.explanation,
                'scoring_mode': scoring_mode,
                'zone_scoring': quiz.current_question.get_zone_reveal_data() if scoring_mode == 'zones' else None,
            }
        return None

    @database_sync_to_async
    def compute_rank_points_for_current_question(self, quiz_id, hub_session_code=None):
        from .runtime import rank_answers
        return rank_answers(self.room_code, quiz_id, hub_session_code)

    @database_sync_to_async
    def get_question_data(self, question):
        return {
            'unit': question.unit,
            'unit_display': question.get_unit_display_text(),
            'max_points': question.get_zone_max_points(),
        }

    @database_sync_to_async
    def get_question_max_points_for_quiz(self, quiz_id, question_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            question = EstimationQuestion.objects.get(id=question_id)
            return question.get_max_points_for_mode(
                quiz.get_effective_scoring_mode(),
                self.get_participant_count_for_quiz(quiz),
            )
        except (EstimationQuiz.DoesNotExist, EstimationQuestion.DoesNotExist):
            return 0

    @database_sync_to_async
    def get_question_number_for_quiz(self, quiz_id, question_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
        except EstimationQuiz.DoesNotExist:
            return 0
        return self.get_question_number_for_quiz_value(quiz, question_id)

    def get_question_number_for_quiz_value(self, quiz, question_id):
        from .progress import question_progress
        return question_progress(quiz, self.current_session_code(), question_id)['current'] or 0

    def current_session_code(self):
        from games_hub.quiz_pause import session_code_for
        return session_code_for(EstimationQuiz.objects.get(room_code=self.room_code), 'estimation')

    def get_participant_count_for_quiz(self, quiz):
        from .progress import participant_count
        return participant_count(quiz)

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session_code, user_answer, time_taken, question_id=None):
        from .runtime import save_answer
        return save_answer(self.room_code, participant_name, hub_session_code, user_answer, time_taken, question_id)

    @database_sync_to_async
    def save_pending_answer(self, participant_name, hub_session_code, user_answer, question_id=None):
        from .runtime import save_pending
        return save_pending(self.room_code, participant_name, hub_session_code, user_answer, question_id)

    @database_sync_to_async
    def finalize_pending_answers(self, quiz_id, hub_session_code=None):
        from .runtime import finalize_pending
        return finalize_pending(self.room_code, quiz_id, hub_session_code)

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = EstimationParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except EstimationParticipant.DoesNotExist:
            pass

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = EstimationQuiz.objects.get(room_code=self.room_code)
            from .progress import scope_code
            qs = quiz.participants.filter(hub_session_code=scope_code(quiz))
            return list(qs.values('name', 'total_score'))
        except EstimationQuiz.DoesNotExist:
            return []

    # --- Hub mirroring helpers ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='estimation', room_code=self.room_code)
            active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
            step = active or qs.order_by('-id').first()
            return step.session.code if step else None
        except Exception:
            return None

    async def hub_mirror_event(self, event_type: str, payload: dict):
        session_code = await self._get_hub_session_code_for_room()
        if not session_code:
            return
        group_name = f'hub_{session_code}'
        await self.channel_layer.group_send(group_name, {
            'type': 'hub_event',
            'event': {
                'type': event_type,
                'final_scores': await self.get_final_scores(),
                **payload,
            }
        })
