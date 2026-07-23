import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import WhereQuiz, WhereParticipant, WhereQuestion, WhereAnswer, WhereSession
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep
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


class WhereConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'where_{self.room_code}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to Where is this? quiz session'
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
            
            print("Where_is_this Consumer: ", message_type)
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
            elif message_type == 'admin_set_scoring_mode':
                await self.handle_admin_set_scoring_mode(text_data_json)
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
            else:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f'Unknown action: {message_type or "missing type"}'
                }))

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))

    async def handle_admin_start_quiz(self, data):
        """Handle admin starting the quiz"""
        hub_session_code = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'where',
            self.room_code,
            session_code=hub_session_code,
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
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'where',
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
                'where',
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
                'where',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            await self.start_quiz_db(quiz.id, reset_runtime=(quiz.status != 'inactive'))
            tutorial_payload = await self.activate_tutorial_runtime(quiz.id, hub_session_code, show_tutorial)
            
            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'Where is this? Quiz has started!',
                    'timestamp': timezone.now().isoformat(),
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'where',
                'title': quiz.title,
                'message': 'Where is this? Quiz has started!'
            }, hub_session_code)
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
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Quiz not found.',
                'question_id': question_id,
            }))
            return
        if not question_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Frage konnte nicht gestartet werden: Frage-ID fehlt.',
            }))
            return
        if quiz.status != 'active':
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Start the quiz before sending a question.',
                'question_id': question_id,
            }))
            return
            
        question = await self.get_question(question_id)
        if not question:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Frage konnte nicht gestartet werden: Frage wurde nicht gefunden.',
                'question_id': question_id,
            }))
            return

        # If quiz has a predefined set, enforce membership
        try:
            has_selected = await self.quiz_has_selected_questions(quiz.id)
            if has_selected:
                allowed = await self.is_question_in_selected(quiz.id, question.id)
                if not allowed:
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'This question is not part of the selected set for this quiz.',
                        'question_id': question_id,
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

        # Update quiz session and question progress for reload/rejoin-safe state
        await self.send_question_db(quiz.id, question.id, custom_time_limit)
        
        # Get question data
        question_data = await self.get_question_data(question)
        
        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        question_points = await self.get_question_start_points(
            quiz.id,
            question.id,
            hub_session,
            is_tutorial_round,
        )

        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'question_text': question.question_text,
                    'time_limit': effective_time_limit,
                    'points': question_points,
                    'scoring_mode': quiz.get_effective_scoring_mode(),
                    'map_type': question.map_type,
                    'is_tutorial_round': is_tutorial_round,
                    'hint_text': question.hint_text,
                    'image_url': question_data['image_url']
                }
            }
        )

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
            unit_tutorial = await self.finish_current_unit_tutorial(hub_session)
            reveal_payload = await self.finalize_current_question(quiz.id, hub_session, bool(unit_tutorial.get('is_tutorial_round')))
            await self.end_current_question_db(quiz.id)
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Question time is up!',
                    'is_tutorial_round': bool(unit_tutorial.get('is_tutorial_round')),
                    **reveal_payload,
                }
            )

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if not quiz:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Quiz not found.'}))
            return

        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        if quiz.current_question_id:
            unit_tutorial = await self.finish_current_unit_tutorial(hub_session)
            await self.finalize_current_question(
                quiz.id,
                hub_session,
                bool(unit_tutorial.get('is_tutorial_round')),
            )

        ended = await self.end_quiz_db(quiz.id)
        if not ended.get('success'):
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Quiz could not be ended.'}))
            return

        final_scores = await self.get_final_scores(hub_session)
        end_payload = {
            'type': 'quiz_ended',
            'message': 'Where is this? Quiz has ended. Thank you for participating!',
            'status': 'completed',
            'quiz_id': quiz.id,
            'room_code': self.room_code,
            'can_start_questions': False,
            'can_answer': False,
            'final_scores': final_scores
        }
        await self.send(text_data=json.dumps(end_payload))
        if not ended.get('ended'):
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            end_payload
        )
        await self.hub_mirror_event('game_ended', {
            'room_code': self.room_code,
            'game_key': 'where',
            'title': quiz.title,
            'message': 'Quiz has ended. Thank you for participating!',
            'final_scores': final_scores
        }, hub_session)

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
                'scoring_mode': 'rank' if scoring_mode == 'rank' else 'zones',
            }
        )

    async def handle_participant_submit_answer(self, data):
        """Handle participant submitting their location answer"""
        participant_name = data.get('participant_name')
        session_code = data.get('hub_session')
        x_norm = data.get('x_norm')
        y_norm = data.get('y_norm')
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        time_taken = data.get('time_taken', 0)

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name, session_code, x_norm, y_norm, latitude, longitude, time_taken
        )
        
        if answer:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'is_tutorial_round': answer['is_tutorial_round'],
                'x_norm': answer['x_norm'],
                'y_norm': answer['y_norm']
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'is_tutorial_round': answer['is_tutorial_round'],
                        'x_norm': answer['x_norm'],
                        'y_norm': answer['y_norm'],
                        'time_taken': time_taken
                    }
                }
            )

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        session_code = data.get('hub_session')
        participant = await self.get_participant_by_name(participant_name, session_code)
        
        if participant:
            await self.mark_participant_active(participant['id'])
            
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
                    session_code,
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
            'timestamp': event.get('timestamp'),
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
            'question': event['question']
        }))

    async def question_ended(self, event):
        """Send question ended message"""
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'message': event['message'],
            'is_tutorial_round': event.get('is_tutorial_round', False),
            'question': event.get('question'),
            'correct_location': event.get('correct_location'),
            'answers': event.get('answers', []),
            'scoring_mode': event.get('scoring_mode'),
            'zone_scoring': event.get('zone_scoring'),
        }))

    async def quiz_ended(self, event):
        """Send quiz ended message"""
        await self.send(text_data=json.dumps({
            'type': 'quiz_ended',
            'message': event['message'],
            'status': event.get('status', 'completed'),
            'quiz_id': event.get('quiz_id'),
            'room_code': event.get('room_code'),
            'can_start_questions': event.get('can_start_questions', False),
            'can_answer': event.get('can_answer', False),
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

    async def scoring_mode_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'scoring_mode_updated',
            'scoring_mode': event.get('scoring_mode'),
        }))

    # Database operations
    @database_sync_to_async
    def get_current_question_data(self):
        """Return serialised question data for the currently active question, or None."""
        try:
            quiz = WhereQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            return {
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': question.time_limit,
                'points': question.get_max_points_for_mode(quiz.get_effective_scoring_mode()),
                'scoring_mode': quiz.get_effective_scoring_mode(),
                'map_type': question.map_type,
                'hint_text': question.hint_text,
                'image_url': question.image.url if question.image else None,
            }
        except WhereQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_quiz(self):
        try:
            return WhereQuiz.objects.get(room_code=self.room_code)
        except WhereQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('where', self.room_code, None, quiz)
        except WhereQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            payload = get_tutorial_payload('where', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except WhereQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            return activate_tutorial_runtime('where', self.room_code, hub_session_code, quiz, show_tutorial)
        except WhereQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('where', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('where', self.room_code, hub_session_code)

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('where', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('where', self.room_code, hub_session_code)

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return WhereQuestion.objects.get(id=question_id)
        except WhereQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.exists()
        except WhereQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_in_selected(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.filter(id=question_id).exists()
        except WhereQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = WhereQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score
            }
        except (WhereQuiz.DoesNotExist, WhereParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id, reset_runtime=True):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            quiz.start_quiz(reset_runtime=reset_runtime)
        except WhereQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            if quiz.status == 'completed' and quiz.ended_at:
                return {'success': True, 'ended': False}
            if hasattr(quiz, 'session'):
                quiz.session.end_current_question()
            else:
                quiz.current_question = None
                quiz.question_start_time = None
                quiz.save(update_fields=['current_question', 'question_start_time', 'updated_at'])
            ended = quiz.end_quiz('completed')
            return {'success': True, 'ended': ended}
        except WhereQuiz.DoesNotExist:
            return {'success': False, 'ended': False}

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except WhereQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_scoring_mode_if_waiting(self, quiz_id, scoring_mode):
        if scoring_mode not in ('zones', 'rank'):
            return False
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
        except WhereQuiz.DoesNotExist:
            return False
        if quiz.status != 'waiting':
            return False
        quiz.scoring_mode = scoring_mode
        quiz.save(update_fields=['scoring_mode'])
        return True

    @database_sync_to_async
    def send_question_db(self, quiz_id, question_id, custom_time_limit=None):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            question = WhereQuestion.objects.get(id=question_id)
        except WhereQuiz.DoesNotExist:
            return
        except WhereQuestion.DoesNotExist:
            return

        quiz.tutorial_active = False
        quiz.save(update_fields=['tutorial_active'])
        session, _ = WhereSession.objects.get_or_create(quiz=quiz)

        session.send_question(question)
        if custom_time_limit is not None:
            session.question_end_time = timezone.now() + timezone.timedelta(seconds=custom_time_limit)
            session.save(update_fields=['question_end_time', 'updated_at'])

    @database_sync_to_async
    def end_current_question_db(self, quiz_id):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            session, _ = WhereSession.objects.get_or_create(quiz=quiz)
            session.end_current_question()
        except WhereQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_question_data(self, question):
        return {
            'image_url': question.image.url if question.image else None
        }

    @database_sync_to_async
    def get_participant_count_for_quiz(self, quiz_id, hub_session_code=None):
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
        except WhereQuiz.DoesNotExist:
            return 0
        return quiz.get_participant_count(hub_session_code)

    @database_sync_to_async
    def get_question_start_points(self, quiz_id, question_id, hub_session_code=None, is_tutorial_round=False):
        if is_tutorial_round:
            return 0
        try:
            quiz = WhereQuiz.objects.get(id=quiz_id)
            question = WhereQuestion.objects.get(id=question_id)
        except (WhereQuiz.DoesNotExist, WhereQuestion.DoesNotExist):
            return 0
        return question.get_max_points_for_mode(
            quiz.get_effective_scoring_mode(),
            quiz.get_participant_count(hub_session_code),
        )

    @database_sync_to_async
    def finalize_current_question(self, quiz_id, hub_session_code=None, is_tutorial_round=False):
        try:
            quiz = WhereQuiz.objects.select_related('current_question').get(id=quiz_id)
        except WhereQuiz.DoesNotExist:
            return {}

        question = quiz.current_question
        if not question:
            return {}

        answers_qs = WhereAnswer.objects.filter(quiz=quiz, question=question).select_related('participant')
        if hub_session_code:
            answers_qs = answers_qs.filter(participant__hub_session_code=hub_session_code)
        answers = list(answers_qs.order_by('distance_km', 'submitted_at', 'id'))

        scoring_mode = quiz.get_effective_scoring_mode()
        if is_tutorial_round:
            for answer in answers:
                changed_fields = []
                if answer.points_earned != 0:
                    answer.points_earned = 0
                    changed_fields.append('points_earned')
                if changed_fields:
                    changed_fields.append('updated_at')
                    answer.save(update_fields=changed_fields)
        elif scoring_mode == 'rank':
            participant_count = max(quiz.get_participant_count(hub_session_code), len(answers))
            previous_distance = None
            previous_rank = 0
            for index, answer in enumerate(answers, start=1):
                rounded_distance = round(float(answer.distance_km or 0), 6)
                if previous_distance is None or rounded_distance != previous_distance:
                    previous_rank = index
                    previous_distance = rounded_distance
                answer.rank_position = previous_rank
                answer.scoring_mode = 'rank'
                answer.points_earned = max(1, participant_count - previous_rank + 1)
                answer.zone_label = ''
                answer.save(update_fields=['rank_position', 'scoring_mode', 'points_earned', 'zone_label', 'updated_at'])
        else:
            for answer in answers:
                zone = question.get_zone_for_distance(answer.distance_km)
                answer.rank_position = None
                answer.scoring_mode = 'zones'
                answer.points_earned = int(zone['points']) if zone else 0
                answer.zone_label = zone['label'] if zone else ''
                answer.save(update_fields=['rank_position', 'scoring_mode', 'points_earned', 'zone_label', 'updated_at'])

        correct_norm = question.get_correct_norm()
        return {
            'question': {
                'id': question.id,
                'question_text': question.question_text,
                'explanation': question.explanation,
                'map_type': question.map_type,
            },
            'correct_location': {
                'latitude': question.correct_latitude,
                'longitude': question.correct_longitude,
                **(correct_norm or {}),
            },
            'scoring_mode': scoring_mode,
            'zone_scoring': question.get_zone_reveal_data() if scoring_mode == 'zones' else None,
            'answers': [
                {
                    'participant_name': answer.participant.name,
                    'participant_id': answer.participant_id,
                    'x_norm': answer.x_norm,
                    'y_norm': answer.y_norm,
                    'user_latitude': answer.user_latitude,
                    'user_longitude': answer.user_longitude,
                    'distance_km': answer.distance_km,
                    'formatted_distance': answer.get_formatted_distance(),
                    'accuracy_percentage': answer.accuracy_percentage,
                    'accuracy_category': answer.get_accuracy_category(),
                    'points_earned': answer.points_earned,
                    'rank_position': answer.rank_position,
                    'zone_label': answer.zone_label,
                    'time_taken': answer.time_taken,
                }
                for answer in answers
            ],
        }

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session_code, x_norm, y_norm, latitude, longitude, time_taken):
        try:
            quiz = WhereQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            
            if quiz.status != 'active':
                return None
            if not quiz.current_question:
                return None
            
            # Check if answer already exists
            existing_answer = WhereAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question
            ).first()
            
            if existing_answer:
                return None  # Already answered

            answer_kwargs = {
                'quiz': quiz,
                'participant': participant,
                'question': quiz.current_question,
                'time_taken': time_taken,
            }
            if x_norm is not None and y_norm is not None:
                answer_kwargs['x_norm'] = float(x_norm)
                answer_kwargs['y_norm'] = float(y_norm)
                # Model.save recalculates latitude/longitude from normalized Web-Mercator coordinates.
                answer_kwargs['user_latitude'] = 0
                answer_kwargs['user_longitude'] = 0
            else:
                answer_kwargs['user_latitude'] = float(latitude)
                answer_kwargs['user_longitude'] = float(longitude)

            # Create new answer
            answer = WhereAnswer.objects.create(**answer_kwargs)
            is_tutorial_answer = is_unit_tutorial_question(
                'where',
                self.room_code,
                hub_session_code,
                quiz.current_question_id,
            )
            if is_tutorial_answer and answer.points_earned:
                answer.points_earned = 0
                answer.save(update_fields=['points_earned', 'updated_at'])
            
            return {
                'is_tutorial_round': is_tutorial_answer,
                'x_norm': answer.x_norm,
                'y_norm': answer.y_norm,
            }
            
        except (WhereQuiz.DoesNotExist, WhereParticipant.DoesNotExist, ValueError, TypeError):
            return None

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = WhereParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except WhereParticipant.DoesNotExist:
            pass

    @database_sync_to_async
    def get_final_scores(self, session_code=None):
        try:
            quiz = WhereQuiz.objects.get(room_code=self.room_code)
            if not session_code:
                try:
                    qs = HubGameStep.objects.select_related('session').filter(game_key='where', room_code=self.room_code)
                    active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                    step = active or qs.order_by('-id').first()
                    session_code = step.session.code if step else None
                except Exception:
                    session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
            return list(qs.order_by('name').values('name', 'total_score'))
        except WhereQuiz.DoesNotExist:
            return []

    # --- Hub mirroring helpers ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='where', room_code=self.room_code)
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
