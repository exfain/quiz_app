import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import WhoThatQuiz, WhoThatParticipant, WhoThatQuestion, WhoThatAnswer, WhoThatSession
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


class WhoThatConsumer(AsyncWebsocketConsumer):
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
        question_timing = await self.activate_question(quiz.id, question.id, effective_time_limit)

        # Get question data
        question_data = await self.get_question_data(question)

        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'question_text': question.question_text,
                    'image_url': question_data['image_url'],
                    'points': 0 if is_tutorial_round else 1,
                    'is_tutorial_round': is_tutorial_round,
                    'question_number': question_timing.get('question_number', 0),
                    'time_limit': effective_time_limit,
                    'time_left': question_timing['time_left'],
                    'question_end_time': question_timing['question_end_time'],
                    'hint_text': question.hint_text,
                    'category': question.category
                }
            }
        )

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            # Get the correct answer before clearing the question
            correct_answer_data = await self.get_current_question_answer(quiz)
            evaluated_pending_answers = await self.finalize_pending_answers(quiz.id)
            hub_session = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
            unit_tutorial = await self.finish_current_unit_tutorial(hub_session)

            await self.clear_current_question(quiz.id)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Time\'s up!',
                    'correct_answer': correct_answer_data,
                    'evaluated_pending_answers': evaluated_pending_answers,
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
        answer = await self.save_participant_answer(
            participant_name, hub_session, user_answer, time_taken, question_id
        )

        if answer:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'is_correct': answer['is_correct'],
                'points_earned': answer['points_earned'],
                'is_tutorial_round': answer['is_tutorial_round'],
                'accuracy_percentage': answer['accuracy_percentage'],
                'match_quality': answer['match_quality']
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

    async def handle_participant_update_pending_answer(self, data):
        """Persist the latest typed answer so it can be evaluated at question end."""
        await self.save_pending_answer(
            data.get('participant_name'),
            data.get('hub_session'),
            data.get('user_answer', ''),
            data.get('question_id'),
        )

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        participant = await self.get_participant_by_name(participant_name, hub_session)

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
            'question': event['question']
        }))

    async def question_ended(self, event):
        """Send question ended message"""
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'message': event['message'],
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

    async def participant_answered(self, event):
        """Send participant answer to admin"""
        await self.send(text_data=json.dumps({
            'type': 'participant_answered',
            'answer': event['answer']
        }))

    async def answer_corrected(self, event):
        """Broadcast a manual host correction to all clients."""
        await self.send(text_data=json.dumps({
            'type': 'answer_corrected',
            'participant_id': event.get('participant_id'),
            'participant_name': event.get('participant_name'),
            'question_id': event.get('question_id'),
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
    @database_sync_to_async
    def get_current_question_data(self):
        """Return serialised question data for the currently active question, or None."""
        try:
            quiz = WhoThatQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            session = getattr(quiz, 'session', None)
            time_left = question.time_limit
            question_end_time = None
            if session and session.question_end_time:
                question_end_time = session.question_end_time.isoformat()
                time_left = max(
                    0,
                    int((session.question_end_time - timezone.now()).total_seconds() + 0.999),
                )
            return {
                'id': question.id,
                'question_text': question.question_text,
                'image_url': question.image.url if question.image else None,
                'points': 1,
                'question_number': session.current_question_number if session else 0,
                'time_limit': question.time_limit,
                'time_left': time_left,
                'question_end_time': question_end_time,
                'hint_text': question.hint_text,
                'category': question.category,
            }
        except WhoThatQuiz.DoesNotExist:
            return None

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
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.question_start_time = None
            quiz.save(update_fields=['status', 'ended_at', 'current_question', 'question_start_time'])
            if hasattr(quiz, 'session'):
                quiz.session.is_question_active = False
                quiz.session.question_end_time = None
                quiz.session.pending_answers = {}
                quiz.session.save(update_fields=['is_question_active', 'question_end_time', 'pending_answers', 'updated_at'])
        except WhoThatQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = WhoThatQuiz.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except WhoThatQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def activate_question(self, quiz_id, question_id, effective_time_limit):
        quiz = WhoThatQuiz.objects.get(id=quiz_id)
        question = WhoThatQuestion.objects.get(id=question_id)
        session, _ = WhoThatSession.objects.get_or_create(quiz=quiz)
        session.send_question(question, effective_time_limit)
        return {
            'question_number': session.current_question_number,
            'question_end_time': session.question_end_time.isoformat() if session.question_end_time else None,
            'time_left': max(
                0,
                int((session.question_end_time - timezone.now()).total_seconds() + 0.999),
            ) if session.question_end_time else effective_time_limit,
        }

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
        try:
            quiz = WhoThatQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)

            if quiz.status != 'active':
                return None
            if not quiz.current_question:
                return None
            if question_id and str(quiz.current_question_id) != str(question_id):
                return None

            # Check if answer already exists
            existing_answer = WhoThatAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question
            ).first()

            if existing_answer:
                return None  # Already answered

            # Create new answer
            answer = WhoThatAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
                user_answer=user_answer,
                time_taken=time_taken
            )
            is_tutorial_answer = is_unit_tutorial_question(
                'who_that',
                self.room_code,
                hub_session_code,
                quiz.current_question_id,
            )
            if is_tutorial_answer and answer.points_earned:
                answer.points_earned = 0
                answer.save(update_fields=['points_earned', 'updated_at'])

            # Record stats in session
            if hasattr(quiz, 'session'):
                quiz.session.record_answer(answer.is_correct, time_taken)
                pending_answers = dict(quiz.session.pending_answers or {})
                pending_answers.pop(str(participant.id), None)
                quiz.session.pending_answers = pending_answers
                quiz.session.save(update_fields=['pending_answers', 'updated_at'])

            return {
                'answer_id': answer.id,
                'game_id': quiz.id,
                'question_id': answer.question_id,
                'participant_id': participant.id,
                'hub_session_code': participant.hub_session_code,
                'user_answer': answer.user_answer,
                'is_correct': answer.is_correct,
                'can_mark_correct': not is_tutorial_answer and not answer.is_correct,
                'points_earned': answer.points_earned,
                'is_tutorial_round': is_tutorial_answer,
                'accuracy_percentage': answer.get_accuracy_percentage(),
                'match_quality': answer.get_match_quality(),
                'time_taken': answer.time_taken,
                'submitted_at': answer.submitted_at.isoformat(),
            }

        except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def save_pending_answer(self, participant_name, hub_session_code, user_answer, question_id=None):
        try:
            quiz = WhoThatQuiz.objects.get(room_code=self.room_code)
            if quiz.status != 'active':
                return False
            if not quiz.current_question:
                return False
            if question_id and str(quiz.current_question_id) != str(question_id):
                return False

            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            session, _ = WhoThatSession.objects.get_or_create(quiz=quiz)
            pending_answers = dict(session.pending_answers or {})
            entry_key = str(participant.id)
            cleaned_answer = (user_answer or '').strip()

            if cleaned_answer:
                pending_answers[entry_key] = {
                    'question_id': quiz.current_question_id,
                    'user_answer': cleaned_answer,
                    'updated_at': timezone.now().isoformat(),
                    'participant_name': participant.name,
                    'hub_session_code': participant.hub_session_code,
                }
            else:
                pending_answers.pop(entry_key, None)

            session.pending_answers = pending_answers
            session.save(update_fields=['pending_answers', 'updated_at'])
            return True
        except (WhoThatQuiz.DoesNotExist, WhoThatParticipant.DoesNotExist):
            return False

    @database_sync_to_async
    def finalize_pending_answers(self, quiz_id):
        try:
            quiz = WhoThatQuiz.objects.select_related('current_question', 'session').get(id=quiz_id)
        except WhoThatQuiz.DoesNotExist:
            return []

        question = quiz.current_question
        session = getattr(quiz, 'session', None)
        if not question or not session:
            return []

        pending_answers = dict(session.pending_answers or {})
        finalized = []
        changed = False

        for participant_id, pending in list(pending_answers.items()):
            if str(pending.get('question_id')) != str(question.id):
                continue

            user_answer = (pending.get('user_answer') or '').strip()
            if not user_answer:
                pending_answers.pop(participant_id, None)
                changed = True
                continue

            try:
                participant = quiz.participants.get(id=int(participant_id))
            except (WhoThatParticipant.DoesNotExist, ValueError, TypeError):
                pending_answers.pop(participant_id, None)
                changed = True
                continue

            existing_answer = WhoThatAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).first()

            if existing_answer:
                pending_answers.pop(participant_id, None)
                changed = True
                continue

            updated_at = parse_datetime(pending.get('updated_at') or '')
            if updated_at and timezone.is_naive(updated_at):
                updated_at = timezone.make_aware(updated_at, timezone.get_current_timezone())

            time_taken = 0
            if quiz.question_start_time and updated_at:
                time_taken = max(0, (updated_at - quiz.question_start_time).total_seconds())

            answer = WhoThatAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=question,
                user_answer=user_answer,
                time_taken=time_taken,
            )

            session.record_answer(answer.is_correct, time_taken)

            finalized.append({
                'participant_name': participant.name,
                'hub_session_code': participant.hub_session_code,
                'is_correct': answer.is_correct,
                'points_earned': answer.points_earned,
                'accuracy_percentage': answer.get_accuracy_percentage(),
                'match_quality': answer.get_match_quality(),
            })

            pending_answers.pop(participant_id, None)
            changed = True

        if changed:
            session.pending_answers = pending_answers
            session.save(update_fields=['pending_answers', 'updated_at'])

        return finalized

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
