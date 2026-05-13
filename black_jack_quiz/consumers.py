import asyncio
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import models
from django.utils import timezone
from .models import BlackJackQuiz, BlackJackParticipant, BlackJackQuestion, BlackJackAnswer
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep


class BlackJackConsumer(AsyncWebsocketConsumer):
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
            elif message_type == 'admin_end_question':
                await self.handle_admin_end_question(text_data_json)
            elif message_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(text_data_json)
            elif message_type == 'admin_set_inactive':
                await self.handle_admin_set_inactive(text_data_json)
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
            elif message_type == 'participant_join':
                await self.handle_participant_join(text_data_json)
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
            show_tutorial = bool(data.get('show_tutorial', True))
            await self.start_quiz_db(quiz.get('id'))
            tutorial_payload = None
            if quiz.get('tutorial_enabled') and show_tutorial:
                await self.set_tutorial_active_db(quiz.get('id'), True)
                tutorial_payload = await self.get_tutorial_payload(quiz.get('id'))
            else:
                await self.set_tutorial_active_db(quiz.get('id'), False)
            
            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'BlackJack Quiz has started!'
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'blackjack',
                'message': 'BlackJack Quiz has started!'
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

        send_error = await self.get_next_question_send_error(quiz.get('id'), question.id, selected_set_number)
        if send_error:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': send_error
            }))
            return

        await self.set_tutorial_active_db(quiz.get('id'), False)
        # Update quiz with new question
        await self.update_quiz_question(quiz, question)
        current_question_data = await self.get_current_question_data()
        if not current_question_data:
            return
        
        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        current_question_data['time_limit'] = effective_time_limit
        
        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': current_question_data
            }
        )

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
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
            
            transition = await self.clear_current_question(quiz.get('id'))
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
                    'final_scores': final_scores,
                }
            )

            if quiz_complete:
                await self.hub_mirror_event('quiz_ended', {
                    'room_code': self.room_code,
                    'game_key': 'blackjack',
                    'message': 'Quiz has ended. Thank you for participating!',
                    'final_scores': final_scores,
                })

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            await self.end_quiz_db(quiz.get('id'))
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
                        'status': answer_result['status'],
                        'time_taken': time_taken,
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
                    'message': 'Quiz is already in progress'
                }))
                tutorial_payload = await self.get_tutorial_payload(quiz.get('id'))
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

    async def question_started(self, event):
        """Send new question to client"""
        await self.send(text_data=json.dumps({
            'type': 'question_started',
            'question': event['question']
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
            'final_scores': event.get('final_scores', []),
        }))

    async def tutorial_start(self, event):
        await self.send(text_data=json.dumps({
            'type': 'tutorial_start',
            'game_title': event.get('game_title'),
            'tutorial_title': event.get('tutorial_title'),
            'tutorial_text': event.get('tutorial_text'),
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

    # Database operations
    @database_sync_to_async
    def get_current_question_data(self):
        """Return serialised question data for the currently active question, or None."""
        try:
            quiz = BlackJackQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            return {
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': question.time_limit,
                'question_number': quiz.current_question_number,
                'question_in_set': quiz.get_question_number_in_set(question_id=question.id),
                'set_question_count': quiz.get_set_question_count(question_id=question.id),
                'set_number': quiz.get_current_set_number(),
                'total_sets': quiz.get_total_sets(),
            }
        except BlackJackQuiz.DoesNotExist:
            return None

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
                'current_question_number': quiz.current_question_number,
                'total_questions': quiz.total_questions,
                'total_game_questions': quiz.get_total_game_questions(),
                'question_in_set': quiz.get_question_number_in_set(
                    question_id=quiz.current_question_id if quiz.current_question_id else None
                ) or 1,
                'set_question_count': quiz.get_set_question_count(
                    question_id=quiz.current_question_id if quiz.current_question_id else None,
                    set_number=quiz.get_current_set_number(),
                ),
                'set_number': quiz.get_current_set_number(),
                'total_sets': quiz.get_total_sets(),
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
        except BlackJackQuiz.DoesNotExist:
            pass

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
            quiz.tutorial_active = bool(active)
            quiz.save(update_fields=['tutorial_active'])
        except BlackJackQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            if not quiz.tutorial_enabled or not quiz.tutorial_active:
                return None
            return {
                'game_title': quiz.title,
                'tutorial_title': quiz.tutorial_title or 'Tutorial',
                'tutorial_text': quiz.tutorial_text or '',
            }
        except BlackJackQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def update_quiz_question(self, quiz_data, question):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_data['id'])
            quiz.current_question = question
            quiz.question_start_time = timezone.now()
            quiz.current_question_number += 1
            quiz.save()
            
            # Update session
            if hasattr(quiz, 'session'):
                quiz.session.send_question(question)
        except BlackJackQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = BlackJackQuiz.objects.get(id=quiz_id)
            quiz.current_question = None
            quiz.question_start_time = None
            quiz.save()
            
            # End current question in session
            if hasattr(quiz, 'session'):
                return quiz.session.end_current_question()
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
                return {
                    'question_id': quiz.current_question_id,
                    'correct_answer': quiz.current_question.correct_answer,
                    'explanation': quiz.current_question.explanation
                }
        except BlackJackQuiz.DoesNotExist:
            pass
        return None

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session_code, user_answer, time_taken, question_id=None):
        try:
            quiz = BlackJackQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            
            if quiz.status not in {'active', 'completed'}:
                return None
            if participant.is_busted:
                return None

            target_question = None
            normalized_question_id = None
            try:
                normalized_question_id = int(question_id) if question_id is not None else None
            except (TypeError, ValueError):
                normalized_question_id = None

            if quiz.current_question:
                if normalized_question_id is not None and normalized_question_id != quiz.current_question_id:
                    return None
                target_question = quiz.current_question
            elif normalized_question_id is not None and hasattr(quiz, 'session') and not quiz.session.is_question_active:
                asked_question_ids = quiz.session.get_asked_question_ids()
                if asked_question_ids and asked_question_ids[-1] == normalized_question_id:
                    target_question = BlackJackQuestion.objects.filter(id=normalized_question_id, is_active=True).first()

            if not target_question:
                return None
            
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
            
            # Create new answer
            answer = BlackJackAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=target_question,
                user_answer=user_answer_int,
                time_taken=time_taken,
                question_number=quiz.current_question_number
            )
            
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
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='blackjack', room_code=self.room_code)
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
                **payload,
            }
        })
