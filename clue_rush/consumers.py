import json
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import ClueRushGame, ClueRushParticipant, ClueQuestion, ClueAnswer
from games_hub.models import HubGameStep, HubSession
try:
    from rapidfuzz import fuzz
except Exception:
    fuzz = None


class ClueRushGameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'cluerush_{self.room_code}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to clue rush session'
        }))

    async def clue_started(self, event):
        """Send started clue to clients"""
        await self.send(text_data=json.dumps({
            'type': 'clue_started',
            'clue': event['clue']
        }))

    async def clue_sequence_completed(self, event):
        await self.send(text_data=json.dumps({
            'type': 'clue_sequence_completed',
            'message': event.get('message', 'All clues sent')
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
            
            print("ClueRushGame Consumer: ", message_type)
            if message_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(text_data_json)
            elif message_type == 'admin_send_question':
                await self.handle_admin_send_question(text_data_json)
            elif message_type == 'admin_end_question':
                await self.handle_admin_end_question(text_data_json)
            elif message_type == 'admin_send_clue':
                await self.handle_admin_send_clue(text_data_json)
            elif message_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(text_data_json)
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
            elif message_type == 'participant_join':
                await self.handle_participant_join(text_data_json)
            elif message_type == 'admin_accept_close_answer':
                await self.handle_admin_accept_close_answer(text_data_json)
            elif message_type == 'admin_change_points':
                await self.handle_admin_change_points(text_data_json)
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
        if quiz:
            await self.start_quiz_db(quiz.id)
            
            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'ClueRushGame has started!'
                }
            )

    async def handle_admin_send_question(self, data):
        """Handle admin sending a new question"""
        question_id = data.get('question_id')
        # Optional per-send override for time limit (seconds)
        try:
            custom_time_limit = int(data.get('custom_time_limit')) if data.get('custom_time_limit') is not None else None
            if custom_time_limit is not None and custom_time_limit <= 0:
                custom_time_limit = None
        except (TypeError, ValueError):
            custom_time_limit = None
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

        # Update quiz with new question
        await self.update_quiz_question(quiz, question)
        
        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit

        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'question_text': question.question_text,
                    'time_limit': effective_time_limit,
                    'points': question.points,
                }
            }
        )

        # Mirror to hub (Stage B): allow centralized listeners to react to question start
        await self.hub_mirror_event('question_started', {
            'room_code': self.room_code,
            'question': {
                'id': question.id,
                'question_text': question.question_text,
                'time_limit': effective_time_limit,
                'points': question.points,
            }
        })

        # Start automatic clue sending for this question
        try:
            if hasattr(self, 'auto_clue_task') and self.auto_clue_task and not self.auto_clue_task.done():
                self.auto_clue_task.cancel()
        except Exception:
            pass
        self.auto_clue_task = asyncio.create_task(self._auto_send_clues())

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            # Stop automatic clue sending if running
            try:
                if hasattr(self, 'auto_clue_task') and self.auto_clue_task and not self.auto_clue_task.done():
                    self.auto_clue_task.cancel()
            except Exception:
                pass
            # Build correct answer payload before clearing the current question
            correct_payload = await self.get_current_question_correct_payload()
            await self.clear_current_question(quiz.id)
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Question time is up!',
                    'correct_answer': correct_payload
                }
            )

            # Mirror to hub (Stage B)
            await self.hub_mirror_event('question_ended', {
                'room_code': self.room_code,
                'message': 'Question time is up!',
                'correct_answer': correct_payload
            })

    async def handle_admin_send_clue(self, data):
        """Handle admin requesting to send the next clue for the current question."""
        # Avoid direct ORM attribute access in async context
        has_question = await self._has_current_question()
        if not has_question:
            return
        # Advance to next clue in DB, retrieve details
        next_clue = await self.advance_next_clue()
        if not next_clue:
            # No more clues to send; optionally notify
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'clue_sequence_completed',
                    'message': 'All clues have been sent.'
                }
            )
            return
        # Broadcast clue start to all clients
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'clue_started',
                'clue': next_clue,
            }
        )

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            # Stop automatic clue sending if running
            try:
                if hasattr(self, 'auto_clue_task') and self.auto_clue_task and not self.auto_clue_task.done():
                    self.auto_clue_task.cancel()
            except Exception:
                pass
            await self.end_quiz_db(quiz.id)
            # Fetch final scores per participant
            final_scores = await self.get_final_scores()
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_ended',
                    'message': 'ClueRushGame has ended. Thank you for participating!',
                    'final_scores': final_scores
                }
            )

            # Mirror to hub so hub can advance to next step or end session
            await self.hub_mirror_event('quiz_ended', {
                'room_code': self.room_code,
                'game_key': 'clue_rush',
                'message': 'ClueRushGame has ended. Thank you for participating!',
                'final_scores': final_scores
            })

    async def handle_participant_submit_answer(self, data):
        """Handle participant submitting an answer"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        answer_text = data.get('answer')
        time_taken = data.get('time_taken', 0)

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name, hub_session, answer_text, time_taken
        )
        
        if answer:
            # Send confirmation to participant
            progress_history = await self.get_participant_score_history(participant_name, hub_session)
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'is_correct': answer['is_correct'],
                'points_earned': answer['points_earned'],
                'is_close': answer.get('is_close', False),
                'progress_history': progress_history,
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'answer_text': answer_text,
                        'is_correct': answer['is_correct'],
                        'points_earned': answer['points_earned'],
                        'is_close': answer.get('is_close', False),
                        'time_taken': time_taken
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
                        'total_score': participant['total_score']
                    }
                }
            )

            # If quiz is already active, send quiz_started directly to this participant
            quiz = await self.get_quiz()
            progress_history = await self.get_participant_score_history(participant_name, hub_session)
            await self.send(text_data=json.dumps({
                'type': 'progress_history',
                'history': progress_history,
            }))
            if quiz and quiz.status == 'active':
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Quiz is already in progress'
                }))
                snapshot = await self.get_rejoin_snapshot(participant_name, hub_session)
                if snapshot.get('question'):
                    await self.send(text_data=json.dumps({
                        'type': 'question_started',
                        'question': snapshot['question']
                    }))
                for clue in snapshot.get('revealed_clues', []):
                    await self.send(text_data=json.dumps({
                        'type': 'clue_started',
                        'clue': clue
                    }))
                if snapshot.get('participant_answer'):
                    await self.send(text_data=json.dumps({
                        'type': 'participant_rehydrated',
                        'answer': snapshot['participant_answer']
                    }))

    async def handle_admin_accept_close_answer(self, data):
        """Admin approves a close answer to award points as correct."""
        participant_name = data.get('participant_name')
        result = await self.approve_close_answer_db(participant_name)
        if result:
            # Acknowledge to the admin client
            await self.send(text_data=json.dumps({
                'type': 'close_answer_approved',
                'participant_name': result['participant_name'],
                'points_earned': result['points_earned'],
            }))
            # Optionally notify all admins in the room
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': result['participant_name'],
                        'answer_text': result.get('answer_text', ''),
                        'is_correct': True,
                        'change_points': True,
                        'points_earned': result['points_earned'],
                        'time_taken': result.get('time_taken')
                    }
                }
            )

    async def handle_admin_change_points(self, data):
        participant_name = data.get('participant_name')
        raw_points = data.get('points')
        try:
            points = int(raw_points)
        except (TypeError, ValueError):
            return
        if points < 0:
            return

        result = await self.change_points_db(participant_name, points)
        if not result:
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'participant_answered',
                'answer': {
                    'participant_name': result['participant_name'],
                    'answer_text': result.get('answer_text', ''),
                    'is_correct': result.get('is_correct', False),
                    'change_points': True, #Comment out to avoid changing points again
                    'points_earned': result['points_earned'],
                    'time_taken': result.get('time_taken'),
                },
            },
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
            'correct_answer': event.get('correct_answer')
        }))

    async def quiz_ended(self, event):
        """Send quiz ended message"""
        await self.send(text_data=json.dumps({
            'type': 'quiz_ended',
            'message': event['message'],
            'final_scores': event.get('final_scores', [])
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
    def get_quiz(self):
        try:
            return ClueRushGame.objects.get(room_code=self.room_code)
        except ClueRushGame.DoesNotExist:
            return None

    @database_sync_to_async
    def approve_close_answer_db(self, participant_name: str):
        """Mark an existing close answer as correct and award points."""
        try:
            quiz = ClueRushGame.objects.select_related('session', 'current_question').get(room_code=self.room_code)
            # Determine hub session for this room
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='clue_rush', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            # Resolve participant within this room's session if possible
            if session_code:
                participant = quiz.participants.get(name=participant_name, hub_session_code=session_code)
            else:
                participant = quiz.participants.get(name=participant_name)

            if not quiz.current_question:
                return None

            answer = ClueAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question
            ).first()
            if not answer:
                return None

            # If already correct, no action
            if answer.is_correct:
                return {
                    'participant_name': participant.name,
                    'points_earned': answer.points_earned,
                    'answer_text': answer.answer_text,
                    'time_taken': answer.time_taken,
                }

            # Compute points like model.save() would on initial create
            total_clues = quiz.current_question.clues.count()
            current_clue_number = quiz.session.current_clue_number if hasattr(quiz, 'session') and quiz.session else 0
            base_points = quiz.current_question.points
            awarded = base_points + (total_clues - current_clue_number + 1)

            answer.is_correct = True
            answer.points_earned = awarded
            answer.save()

            return {
                'participant_name': participant.name,
                'points_earned': answer.points_earned,
                'answer_text': answer.answer_text,
                'time_taken': answer.time_taken,
            }
        except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def change_points_db(self, participant_name: str, new_points: int):
        try:
            quiz = ClueRushGame.objects.select_related('session', 'current_question').get(room_code=self.room_code)

            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='clue_rush', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            if session_code:
                participant = quiz.participants.get(name=participant_name, hub_session_code=session_code)
            else:
                participant = quiz.participants.get(name=participant_name)

            if not quiz.current_question:
                return None

            answer = ClueAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
            ).first()
            if not answer:
                return None

            answer.points_earned = new_points
            answer.save()

            return {
                'participant_name': participant.name,
                'points_earned': answer.points_earned,
                'answer_text': answer.answer_text,
                'time_taken': answer.time_taken,
                'is_correct': answer.is_correct,
            }
        except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
            return None

    async def _auto_send_clues(self):
        """Automatically send clues sequentially using each clue's duration."""
        while True:
            # Check we still have an active question
            if not await self._has_current_question():
                break
            clue = await self.advance_next_clue()
            if not clue:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'clue_sequence_completed',
                        'message': 'All clues have been sent.'
                    }
                )
                break
            # Broadcast clue
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'clue_started',
                    'clue': clue,
                }
            )
            # No next-clue timer after the last clue.
            if not clue.get('has_next_clue', False):
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'clue_sequence_completed',
                        'message': 'All clues have been sent.'
                    }
                )
                break
            try:
                await asyncio.sleep(max(0, int(clue.get('duration', 0))))
            except asyncio.CancelledError:
                break

    @database_sync_to_async
    def _has_current_question(self) -> bool:
        try:
            quiz = ClueRushGame.objects.only('id', 'current_question').get(room_code=self.room_code)
            return bool(quiz.current_question_id)
        except ClueRushGame.DoesNotExist:
            return False

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return ClueQuestion.objects.get(id=question_id)
        except ClueQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            return quiz.selected_questions.exists()
        except ClueRushGame.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_in_selected(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            return quiz.selected_questions.filter(id=question_id).exists()
        except ClueRushGame.DoesNotExist:
            return False

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = ClueRushGame.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score
            }
        except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            quiz.status = 'active'
            quiz.started_at = timezone.now()
            quiz.save()
        except ClueRushGame.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.save()
        except ClueRushGame.DoesNotExist:
            pass

    @database_sync_to_async
    def update_quiz_question(self, quiz, question):
        quiz.current_question = question
        quiz.question_start_time = timezone.now()
        # Reset clue tracking for the new question
        try:
            session = quiz.session
            if session:
                session.current_clue_number = 0
                session.is_clue_active = False
                session.clue_end_time = None
                session.save()
        except Exception:
            pass
        quiz.current_clue = None
        quiz.clue_start_time = None
        quiz.save()

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            quiz.current_question = None
            quiz.question_start_time = None
            # Reset current clue state as well
            quiz.current_clue = None
            quiz.clue_start_time = None
            # Also reset session clue number for cleanliness
            try:
                session = quiz.session
                if session:
                    session.current_clue_number = 0
                    session.is_clue_active = False
                    session.clue_end_time = None
                    session.save()
            except Exception:
                pass
            quiz.save()
        except ClueRushGame.DoesNotExist:
            pass


    @database_sync_to_async
    def advance_next_clue(self):
        """Advance the session to the next clue for the current question and return clue info dict.
        Returns None if there is no next clue.
        """
        try:
            quiz = ClueRushGame.objects.select_related('session', 'current_question').get(room_code=self.room_code)
            if not quiz.current_question:
                return None
            clues_qs = quiz.current_question.clues.order_by('order')
            # Use current_clue as primary source to avoid off-by-one issues
            # when clue ordering starts at 0 or when session counters get stale.
            if quiz.current_clue_id:
                current_order = quiz.current_clue.order
                next_obj = clues_qs.filter(order__gt=current_order).first()
            else:
                # No clue has been sent yet for this question: always start with the first clue
                next_obj = clues_qs.first()
            if not next_obj:
                return None
            # Update DB state
            quiz.current_clue = next_obj
            quiz.clue_start_time = timezone.now()
            if hasattr(quiz, 'session') and quiz.session:
                session = quiz.session
                session.current_clue_number = next_obj.order
                session.is_clue_active = True
                session.clue_end_time = timezone.now() + timezone.timedelta(seconds=next_obj.duration)
                session.save()
            quiz.save()
            has_next_clue = clues_qs.filter(order__gt=next_obj.order).exists()
            return {
                'id': next_obj.id,
                'order': next_obj.order,
                'clue_text': next_obj.clue_text,
                'duration': next_obj.duration,
                'has_next_clue': has_next_clue,
            }
        except ClueRushGame.DoesNotExist:
            return None

    # --- Hub mirroring helpers (Stage B) ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='clue_rush', room_code=self.room_code)
            # Prefer an active session if available
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

    @database_sync_to_async
    def save_participant_answer(self, participant_name,hub_session_code, answer_text, time_taken):
        try:
            quiz = ClueRushGame.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            
            if not quiz.current_question:
                return None
            
            # Check if answer already exists
            existing_answer = ClueAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question
            ).first()
            
            if existing_answer:
                return None  # Already answered
            
            # Create new answer
            answer = ClueAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
                answer_text=answer_text,
                time_taken=time_taken
            )
            # Compute closeness using rapidfuzz if available (only when not exactly correct)
            is_close = False
            try:
                if fuzz is not None and not answer.is_correct:
                    correct = (quiz.current_question.answer or '')
                    a = ' '.join((answer_text or '').strip().lower().split())
                    b = ' '.join((correct or '').strip().lower().split())
                    similarity = fuzz.ratio(a, b)
                    # Threshold can be tuned; start with 80
                    is_close = similarity >= 80
            except Exception:
                is_close = False
            
            return {
                'is_correct': answer.is_correct,
                'points_earned': answer.points_earned,
                'is_close': is_close
            }
            
        except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = ClueRushParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except ClueRushParticipant.DoesNotExist:
            pass

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = ClueRushGame.objects.get(room_code=self.room_code)
            # Filter by hub session code if available via HubGameStep
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='clue_rush', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
            return list(qs.values('name', 'total_score'))
        except ClueRushGame.DoesNotExist:
            return []

    @database_sync_to_async
    def get_current_question_correct_payload(self):
        try:
            quiz = ClueRushGame.objects.select_related('current_question').get(room_code=self.room_code)
            q = quiz.current_question
            if not q:
                return None
            formatted = (q.answer or '').strip()
            return {
                'question_id': q.id,
                'formatted_answer': formatted,
                'raw': q.answer,
            }
        except ClueRushGame.DoesNotExist:
            return None

    @database_sync_to_async
    def get_rejoin_snapshot(self, participant_name, hub_session):
        """Build a server-authoritative snapshot for participant rejoin/reconnect."""
        try:
            quiz = ClueRushGame.objects.select_related('current_question', 'current_clue').get(room_code=self.room_code)
        except ClueRushGame.DoesNotExist:
            return {}

        question_payload = None
        revealed_clues = []
        participant_answer = None

        current_question = quiz.current_question
        if current_question:
            question_payload = {
                'id': current_question.id,
                'question_text': current_question.question_text,
                'time_limit': current_question.time_limit,
                'points': current_question.points,
            }

            clues_qs = list(current_question.clues.order_by('order'))
            current_clue_order = None
            if quiz.current_clue_id:
                current_clue_order = quiz.current_clue.order
            else:
                try:
                    session = quiz.session
                    current_clue_order = session.current_clue_number if session else None
                except Exception:
                    current_clue_order = None

            if current_clue_order is not None:
                for clue in clues_qs:
                    if clue.order <= current_clue_order:
                        revealed_clues.append({
                            'id': clue.id,
                            'order': clue.order,
                            'clue_text': clue.clue_text,
                            'duration': clue.duration,
                            'has_next_clue': any(c.order > clue.order for c in clues_qs),
                        })

            try:
                participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
                answer = ClueAnswer.objects.filter(
                    quiz=quiz,
                    participant=participant,
                    question=current_question
                ).first()
                if answer:
                    participant_answer = {
                        'answer_text': answer.answer_text,
                        'is_correct': answer.is_correct,
                        'points_earned': answer.points_earned,
                        'submitted_at': answer.submitted_at.isoformat() if answer.submitted_at else None,
                    }
            except ClueRushParticipant.DoesNotExist:
                participant_answer = None

        return {
            'question': question_payload,
            'revealed_clues': revealed_clues,
            'participant_answer': participant_answer,
        }

    @database_sync_to_async
    def get_participant_score_history(self, participant_name, hub_session_code):
        try:
            quiz = ClueRushGame.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
        except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
            return []

        answers = list(
            ClueAnswer.objects
            .filter(quiz=quiz, participant=participant)
            .select_related('question')
            .order_by('submitted_at', 'id')
        )

        history = []
        for idx, answer in enumerate(answers, start=1):
            max_points = answer.question.clues.count()
            achieved_points = 0
            if answer.is_correct and max_points > 0:
                derived = answer.points_earned - answer.question.points
                achieved_points = max(0, min(derived, max_points))

            history.append({
                'question_number': idx,
                'achieved_points': achieved_points,
                'max_points': max_points,
            })
        return history
