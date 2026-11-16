import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import WhoThatQuiz, WhoThatParticipant, WhoThatQuestion, WhoThatAnswer, WhoThatSession
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import (
    current_snapshot,
    finish_question_flow,
    present_question,
    question_content_is_visible,
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


class WhoThatConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'who_that'
    authoritative_required_actions = frozenset({
        'participant_submit_answer',
        'participant_update_pending_answer',
    })

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'who_that_{self.room_code}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to Who is That quiz session'
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

            print("Who_is_that Consumer: ", message_type)
            if message_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(text_data_json)
            elif message_type == 'admin_send_question':
                await self.handle_admin_send_question(text_data_json)
            elif message_type == 'admin_end_question':
                await self.handle_admin_end_question(text_data_json)
            elif message_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(text_data_json)
            elif message_type == 'admin_set_inactive':
                await self.handle_admin_set_inactive(text_data_json)
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
            await self.send(text_data=json.dumps({'type': 'quiz_started', 'message': 'Quiz is already active.'}))
            return
        if quiz and quiz.status == 'inactive' and quiz.started_at:
            activation = await database_sync_to_async(resolve_session_game_activation_for_room)(
                'who_that', self.room_code, check_only=True)
            if not activation.get('success'):
                await self.send(text_data=json.dumps({'type': 'active_game_conflict', **activation}))
                return
            await database_sync_to_async(quiz.start_quiz)(session_code=data.get('hub_session') or None)
            return
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'who_that',
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
            if quiz.status == 'active':
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Who is That Quiz is already active.',
                }))
                return
            show_tutorial = bool(data.get('show_tutorial', False))
            play_tutorial = bool(data.get('play_tutorial', False))
            hub_session_code = await self._get_hub_session_code_for_room()
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'who_that',
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
                'who_that',
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
                'who_that',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            await self.start_quiz_db(quiz.id)
            await database_sync_to_async(reset_question_flow)(
                game_key='who_that',
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
                    'message': 'Who is That Quiz has started!'
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'who_that',
                'message': 'Who is That Quiz has started!'
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
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Spiel nicht gefunden.'}))
            return

        question = await self.get_question(question_id)
        if not question:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Frage nicht gefunden.'}))
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
        decision = await self.present_who_that_question(
            quiz.id,
            question.id,
            hub_session,
            phase_action,
            effective_time_limit,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question.id)
            return
        question_number = await self.get_current_question_number(quiz.id)
        lifecycle = self.question_lifecycle_fields(decision.snapshot)

        # Announce only safe metadata. Clients request the full state at the
        # authoritative visibility timestamp.
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_prepared',
                'question': {
                    'id': question.id,
                    'points': 0 if is_tutorial_round else 1,
                    'is_tutorial_round': is_tutorial_round,
                    'question_number': question_number,
                    'time_limit': effective_time_limit,
                },
                **lifecycle,
            }
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
                'answer_duration_seconds',
            )
        }

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        from .runtime import end_question
        quiz = await self.get_quiz()
        if quiz:
            await database_sync_to_async(end_question)(quiz.id,
                data.get('hub_session') or await self._get_hub_session_code_for_room(),
                question_id=data.get('question_id'), round_number=data.get('round_number'))

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
                    'message': 'Who is That Quiz has ended. Thank you for participating!',
                    'final_scores': final_scores
                }
            )

            # Mirror to hub to auto-advance session
            # await self.hub_mirror_event('game_ended', {
            #     'room_code': self.room_code,
            #     'game_key': 'who_that'
            # })
            # Mirror to hub so hub can advance to next step or end session
            await self.hub_mirror_event('quiz_ended', {
                'room_code': self.room_code,
                'game_key': 'who_that',
                'message': 'Quiz has ended. Thank you for participating!',
                'final_scores': final_scores
            })

    async def handle_admin_set_inactive(self, data):
        """Pause the quiz without clearing its current progress."""
        quiz = await self.get_quiz()
        if quiz and quiz.status == 'active':
            await self.set_quiz_inactive_db(quiz.id)

    async def handle_participant_submit_answer(self, data):
        """Handle participant submitting their answer"""
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
                'user_answer': answer['user_answer'], 'answer_locked': True,
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'answer_id': answer['answer_id'],
                        'game_id': answer['game_id'],
                        'question_id': answer['question_id'],
                        'participant_id': answer['participant_id'],
                        'participant_name': participant_name,
                        'hub_session_code': answer['hub_session_code'],
                        'user_answer': answer['user_answer'],
                        'is_correct': answer['is_correct'],
                        'is_manual_override': False,
                        'can_mark_correct': answer['can_mark_correct'],
                        'points_earned': answer['points_earned'],
                        'is_tutorial_round': answer['is_tutorial_round'],
                        'accuracy_percentage': answer['accuracy_percentage'],
                        'match_quality': answer['match_quality'],
                        'time_taken': answer['time_taken'],
                        'submitted_at': answer['submitted_at'],
                    }
                }
            )
        else:
            await self.send(text_data=json.dumps({
                'type': 'answer_rejected', 'message': 'Antwort wurde nicht gespeichert. Bitte erneut versuchen.',
                'question_id': question_id,
            }))

    async def handle_participant_update_pending_answer(self, data):
        """Persist the latest typed answer so it can be evaluated at question end."""
        saved = await self.save_pending_answer(
            data.get('participant_name'),
            data.get('hub_session'),
            data.get('user_answer', ''),
            data.get('question_id'),
        )
        await self.send(text_data=json.dumps({
            'type': 'pending_answer_saved' if saved else 'pending_answer_rejected',
            'question_id': data.get('question_id'), 'user_answer': (data.get('user_answer') or '').strip() if isinstance(data.get('user_answer'), str) else '',
        }))

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session') or None
        participant = await self.get_participant_by_name(participant_name, hub_session)

        if participant:
            await self.mark_participant_active(participant['id'])

            # Broadcast to admin
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_joined',
                    'participant': {
                        'id': participant['id'],
                        'hub_session_code': hub_session,
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
                await self.send_current_question_state(
                    hub_session,
                    participant_id=participant['id'],
                )

    async def send_state(self, data):
        """Return the current phase-safe state for host or participant reloads."""
        participant_id = None
        participant_name = data.get('participant_name')
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        if participant_name:
            participant = await self.get_participant_by_name(
                participant_name,
                hub_session,
            )
            participant_id = participant['id'] if participant else None
        await self.send_current_question_state(
            hub_session,
            participant_id=participant_id,
        )

    async def send_current_question_state(self, hub_session, participant_id=None):
        host = await self.is_host()
        if host:
            from .runtime import end_question
            quiz = await self.get_quiz()
            if quiz:
                await database_sync_to_async(end_question)(quiz.id, hub_session, expired_only=True)
        state = await self.get_current_question_state(hub_session)
        if not state:
            if not host:
                await self.send_reveal_state(hub_session, participant_id)
            return
        event_type = (
            'question_started'
            if state['content_visible']
            else 'question_prepared'
        )
        await self.send(text_data=json.dumps({
            'type': event_type,
            'question': state['question'],
            **state['lifecycle'],
        }))
        if participant_id and state['content_visible']:
            existing_answer = await self.get_current_participant_answer(
                participant_id,
                state['question']['id'],
            )
            if existing_answer:
                await self.send(text_data=json.dumps({
                    'type': 'answer_submitted',
                    'message': 'Answer already submitted',
                    **existing_answer,
                }))
            else:
                draft = await self.get_pending_answer(participant_id, state['question']['id'])
                if draft is not None:
                    await self.send(text_data=json.dumps({'type': 'pending_answer_restored',
                        'question_id': state['question']['id'], 'user_answer': draft}))

    async def is_host(self):
        from games_hub.host_permissions import authorize_game_host
        decision = await database_sync_to_async(authorize_game_host)(
            getattr(self, 'scope', {}).get('user'), 'who_that', self.room_code,
            self._authoritative_session_code() or None)
        return decision.allowed

    async def send_reveal_state(self, hub_session, participant_id):
        event = await self.get_reveal_state(hub_session, participant_id)
        if event:
            await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def get_reveal_state(self, hub_session, participant_id):
        from .runtime import reveal_state
        return reveal_state(self.room_code, hub_session, participant_id)

    @database_sync_to_async
    def get_pending_answer(self, participant_id, question_id):
        session = WhoThatSession.objects.filter(quiz__room_code=self.room_code).first()
        entry = (session.pending_answers or {}).get(str(participant_id), {}) if session else {}
        if str(entry.get('question_id')) == str(question_id):
            return entry.get('user_answer', '')
        return None

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

    async def question_prepared(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_prepared',
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
            'message': event['message'],
            'question_id': event.get('question_id'),
            'round_number': event.get('round_number'),
            'question_number': event.get('question_number'),
            'is_tutorial_round': event.get('is_tutorial_round', False),
            'correct_answer': event.get('correct_answer'),
            'evaluated_pending_answers': event.get('evaluated_pending_answers', []),
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

    async def answer_corrected(self, event):
        """Broadcast a manual host correction to all clients."""
        if not await self.is_host():
            quiz = await self.get_quiz()
            if quiz and quiz.current_question_id == event.get('question_id'):
                from games_hub.quiz_rounds import current_round
                active_round = await database_sync_to_async(current_round)(quiz)
                if not event.get('round_number') or active_round == event.get('round_number'):
                    return
            if (event.get('participant_name') != self._authoritative_participant_name()
                    or (event.get('hub_session_code') or '') != self._authoritative_session_code()):
                return
        await self.send(text_data=json.dumps({
            'type': 'answer_corrected',
            'participant_id': event.get('participant_id'),
            'participant_name': event.get('participant_name'),
            'question_id': event.get('question_id'),
            'round_number': event.get('round_number'),
            'is_correct': event.get('is_correct', True),
            'points_earned': event.get('points_earned', 0),
            'match_quality': event.get('match_quality', 'Manual override'),
            'total_score': event.get('total_score', 0),
        }))

    async def participant_joined(self, event):
        """Send new participant info to admin"""
        await self.send(text_data=json.dumps({
            'type': 'participant_joined',
            'participant': event['participant']
        }))

    # Database operations
    @staticmethod
    def round_metadata(quiz, hub_session_code=None):
        from games_hub.quiz_rounds import progress, current_round, scoreboard
        from games_hub.unit_tutorial_runtime import is_current_unit_tutorial_question
        state = progress(quiz, 'who_that', hub_session_code, question_id=quiz.current_question_id)
        tutorial = is_current_unit_tutorial_question('who_that', quiz.room_code, hub_session_code, quiz.current_question_id)
        return {'question_number': state['current'], 'total_questions': state['total'], 'points': 0 if tutorial else 1,
            'paused': quiz.status == 'inactive',
            'round_number': current_round(quiz), 'scoreboard': scoreboard(quiz, 'who_that', hub_session_code),
            'is_tutorial_round': is_current_unit_tutorial_question('who_that', quiz.room_code, hub_session_code, quiz.current_question_id)}

    @database_sync_to_async
    def get_current_question_state(self, hub_session_code=None):
        """Return phase-safe state without exposing content before visibility."""
        try:
            quiz = WhoThatQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            session = getattr(quiz, 'session', None)
            runtime = current_snapshot(
                'who_that',
                self.room_code,
                hub_session_code,
            )
            now = timezone.now()
            manual_flow = (
                runtime.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            content_visible = (
                question_content_is_visible(runtime, at=now)
                if manual_flow
                else True
            )
            duration = runtime.get('answer_duration_seconds') or question.time_limit
            safe_question = {
                'id': question.id,
                'points': 1,
                **self.round_metadata(quiz, hub_session_code),
                'time_limit': duration,
            }
            if content_visible:
                safe_question.update({
                    'question_text': question.question_text,
                    'image_url': question.image.url if question.image else None,
                    'hint_text': question.hint_text,
                    'category': question.category,
                })
            safe_question.update(self.question_lifecycle_fields(runtime))
            if manual_flow:
                safe_question['time_left'] = runtime.get('remaining_answer_time')
                safe_question['question_end_time'] = runtime.get('answering_deadline_at')
            else:
                safe_question['time_left'] = (
                    max(0, int((session.question_end_time - now).total_seconds() + 0.999))
                    if session and session.question_end_time
                    else duration
                )
                safe_question['question_end_time'] = (
                    session.question_end_time.isoformat()
                    if session and session.question_end_time
                    else None
                )
            safe_question['server_now'] = now.isoformat()
            return {
                'content_visible': content_visible,
                'question': safe_question,
                'lifecycle': self.question_lifecycle_fields(runtime),
            }
        except WhoThatQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_current_question_data(self, hub_session_code=None):
        """Backward-compatible full payload for an already visible question."""
        try:
            quiz = WhoThatQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            session = getattr(quiz, 'session', None)
            runtime = current_snapshot('who_that', self.room_code, hub_session_code)
            manual_flow = (
                runtime.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            if manual_flow and not question_content_is_visible(runtime):
                return None
            duration = runtime.get('answer_duration_seconds') or question.time_limit
            now = timezone.now()
            time_left = runtime.get('remaining_answer_time')
            question_end_time = runtime.get('answering_deadline_at')
            starts_at = runtime.get('answering_started_at')
            if not manual_flow:
                question_end_time = (
                    session.question_end_time.isoformat()
                    if session and session.question_end_time
                    else None
                )
                starts_at = quiz.question_start_time.isoformat() if quiz.question_start_time else None
                time_left = (
                    max(0, int((session.question_end_time - now).total_seconds() + 0.999))
                    if session and session.question_end_time
                    else duration
                )
            return {
                'id': question.id,
                'question_text': question.question_text,
                'image_url': question.image.url if question.image else None,
                'points': 1,
                **self.round_metadata(quiz, hub_session_code),
                'time_limit': duration,
                'time_left': time_left,
                'question_end_time': question_end_time,
                'starts_at': starts_at,
                'ends_at': question_end_time,
                'server_now': runtime.get('server_now'),
                'hint_text': question.hint_text,
                'category': question.category,
                **self.question_lifecycle_fields(runtime),
            }
        except WhoThatQuiz.DoesNotExist:
            return None

    def current_round_number(self):
        from games_hub.quiz_rounds import current_round
        return current_round(WhoThatQuiz.objects.get(room_code=self.room_code))

    @database_sync_to_async
    def get_current_participant_answer(self, participant_id, question_id):
        answer = WhoThatAnswer.objects.filter(
            quiz__room_code=self.room_code,
            participant_id=participant_id,
            question_id=question_id,
            round_number=self.current_round_number(),
        ).first()
        if not answer:
            return None
        return {
            'question_id': answer.question_id,
            'user_answer': answer.user_answer,
            'time_taken': answer.time_taken,
            'answer_locked': True,
        }

    @database_sync_to_async
    def get_quiz(self):
        try:
            return WhoThatQuiz.objects.get(room_code=self.room_code)
        except WhoThatQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('who_that', self.room_code, None, quiz)
        except WhoThatQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            payload = get_tutorial_payload('who_that', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except WhoThatQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            return activate_tutorial_runtime('who_that', self.room_code, hub_session_code, quiz, show_tutorial)
        except WhoThatQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('who_that', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('who_that', self.room_code, hub_session_code)

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('who_that', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('who_that', self.room_code, hub_session_code)

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return WhoThatQuestion.objects.get(id=question_id)
        except WhoThatQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.exists()
        except WhoThatQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_in_selected(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.filter(id=question_id).exists()
        except WhoThatQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = WhoThatQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score
            }
        except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            quiz.start_quiz()
        except WhoThatQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            quiz.end_quiz()
        except WhoThatQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            from games_hub.quiz_pause import pause
            pause(quiz, 'who_that')
        except WhoThatQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_current_question_number(self, quiz_id):
        from games_hub.quiz_rounds import progress
        from games_hub.quiz_pause import session_code_for
        quiz = WhoThatQuiz.objects.get(pk=quiz_id)
        return progress(quiz, 'who_that', session_code_for(quiz, 'who_that'), question_id=quiz.current_question_id)['current']

    @database_sync_to_async
    def present_who_that_question(self, quiz_id, question_id, hub_session_code, action, answer_duration_seconds, at=None):
        from .runtime import present
        return present(self.room_code, quiz_id, question_id, hub_session_code, action, answer_duration_seconds, at)

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            session, _ = WhoThatSession.objects.get_or_create(quiz=quiz)
            session.end_current_question()
        except WhoThatQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_current_question_answer(self, quiz):
        """Get the correct answer for the current question"""
        if quiz.current_question:
            return {
                'correct_answer': quiz.current_question.correct_answer,
                'alternative_answers': quiz.current_question.alternative_answers,
                'explanation': quiz.current_question.explanation,
                'image_url': quiz.current_question.image.url if quiz.current_question.image else None
            }
        return None

    @database_sync_to_async
    def get_question_data(self, question):
        return {
            'image_url': question.image.url if question.image else None,
            'points': 1
        }

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session_code, user_answer, time_taken, question_id=None):
        from .runtime import save_answer
        return save_answer(self.room_code, participant_name, hub_session_code, user_answer, time_taken, question_id)

    @database_sync_to_async
    def save_pending_answer(self, participant_name, hub_session_code, user_answer, question_id=None):
        from .runtime import save_pending
        return save_pending(self.room_code, participant_name, hub_session_code, user_answer, question_id)

    @database_sync_to_async
    def finalize_pending_answers(self, quiz_id):
        from .runtime import finalize_pending
        return finalize_pending(self.room_code, quiz_id)

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = WhoThatParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except WhoThatParticipant.DoesNotExist:
            pass

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = WhoThatQuiz.objects.get(room_code=self.room_code)
            # Filter by hub session code if available via HubGameStep
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='who_that', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
            return list(qs.values('name', 'total_score'))
        except WhoThatQuiz.DoesNotExist:
            return []

        # --- Hub mirroring helpers (Stage B) ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='who_that', room_code=self.room_code)
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
        # HubConsumer expects group messages of type 'hub_event' with 'event' payload
        await self.channel_layer.group_send(group_name, {
            'type': 'hub_event',
            'event': {
                'type': event_type,
                **payload,
            }
        })
