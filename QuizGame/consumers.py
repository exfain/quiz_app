import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from django.core.cache import cache
from .models import Quiz, QuizParticipant, QuizQuestion, QuizAnswer
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep, HubSession


class QuizConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'quiz_{self.room_code}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to quiz session'
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
            
            print("Quiz Consumer: ", message_type)
            if message_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(text_data_json)
            elif message_type == 'tutorial_completed':
                await self.handle_tutorial_completed(text_data_json)
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
            elif message_type == 'participant_finalize_answer':
                await self.handle_participant_finalize_answer(text_data_json)
            elif message_type == 'participant_join':
                await self.handle_participant_join(text_data_json)
            elif message_type == 'admin_show_leaderboard':
                await self.handle_admin_show_leaderboard()
            elif message_type == 'admin_hide_leaderboard':
                await self.handle_admin_hide_leaderboard()
            elif message_type == 'ping':
                await self.handle_ping()
                
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))

    async def handle_admin_start_quiz(self, data):
        """Handle admin starting the quiz"""
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'quiz',
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
                'quiz',
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
            await self.start_quiz_db(quiz.id)
            tutorial_payload = None
            if quiz.tutorial_enabled and show_tutorial:
                await self.set_tutorial_active_db(quiz.id, True)
                tutorial_payload = await self.get_tutorial_payload(quiz.id)
                await self.reset_tutorial_completed(quiz.id)
            else:
                await self.set_tutorial_active_db(quiz.id, False)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'Quiz has started!'
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'quiz',
                'message': 'Quiz has started!'
            })

            if tutorial_payload:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'tutorial_start',
                        **tutorial_payload,
                    }
                )

    async def handle_tutorial_completed(self, data):
        """Handle participant completing the tutorial"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        await self.mark_tutorial_completed(participant_name, hub_session)

        quiz = await self.get_quiz()
        if not quiz:
            return

        completed, total = await self.get_tutorial_progress(quiz.id, hub_session)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'tutorial_progress',
                'completed': completed,
                'total': total,
                'all_done': completed >= total and total > 0
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
        await self.set_tutorial_active_db(quiz.id, False)
        await self.update_quiz_question(quiz, question)
        
        # Get question options
        options = await self.get_question_options(question)
        short_answer_fields = await self.get_short_answer_fields(question)
        
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
                    'question_type': question.get_effective_question_type(),
                    'options': options,
                    'short_answer_fields': short_answer_fields,
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
                    'question_type': question.get_effective_question_type(),
                    'options': options,
                    'short_answer_fields': short_answer_fields,
                    'time_limit': effective_time_limit,
                    'points': question.points,
                }
        })

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            # Fetch current question's correct answer before clearing
            correct_payload = await self.get_current_question_correct_payload()
            if correct_payload and correct_payload.get('question_id'):
                await self.mark_recently_ended_question(correct_payload['question_id'])
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

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            await self.end_quiz_db(quiz.id)
            # Fetch final scores per participant
            final_scores = await self.get_final_scores()
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_ended',
                    'message': 'Quiz has ended. Thank you for participating!',
                    'final_scores': final_scores
                }
            )

            # Mirror to hub so hub can advance to next step or end session
            await self.hub_mirror_event('quiz_ended', {
                'room_code': self.room_code,
                'game_key': 'quiz',
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
        """Handle participant submitting an answer"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session') or None  # normalize '' → None
        answer_text = data.get('answer')
        time_taken = data.get('time_taken', 0)

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name, hub_session, answer_text, time_taken
        )

        if answer:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'question_id': answer['question_id'],
                'is_correct': answer['is_correct'],
                'points_earned': answer['points_earned'],
                'total_score': answer['total_score'],
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'answer_id': answer['answer_id'],
                        'participant_name': participant_name,
                        'answer_text': answer['display_answer'],
                        'is_correct': answer['is_correct'],
                        'is_manual_override': False,
                        'can_mark_correct': answer['can_mark_correct'],
                        'question_type': answer['question_type'],
                        'points_earned': answer['points_earned'],
                        'time_taken': time_taken
                    }
                }
            )

    async def handle_participant_finalize_answer(self, data):
        """Evaluate a typed answer that was not explicitly submitted before question end."""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session') or None
        answer_text = data.get('answer')
        question_id = data.get('question_id')
        time_taken = data.get('time_taken', 0)

        answer = await self.save_participant_answer(
            participant_name,
            hub_session,
            answer_text,
            time_taken,
            question_id=question_id,
            allow_recently_ended=True,
        )

        if answer:
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'question_id': answer['question_id'],
                'is_correct': answer['is_correct'],
                'points_earned': answer['points_earned'],
                'total_score': answer['total_score'],
            }))

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'answer_text': answer['display_answer'],
                        'is_correct': answer['is_correct'],
                        'points_earned': answer['points_earned'],
                        'time_taken': time_taken
                    }
                }
            )


    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session') or None  # normalize '' → None
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
                tutorial_payload = await self.get_tutorial_payload(quiz.id)
                if tutorial_payload:
                    await self.send(text_data=json.dumps({
                        'type': 'tutorial_start',
                        **tutorial_payload,
                    }))
                # If there is an active question, send it so the participant doesn't miss it
                current_question_data = await self.get_current_question_data()
                if current_question_data:
                    await self.send(text_data=json.dumps({
                        'type': 'question_started',
                        'question': current_question_data
                    }))

    async def handle_ping(self):
        """Handle ping for keeping connection alive"""
        await self.send(text_data=json.dumps({
            'type': 'pong'
        }))

    async def handle_admin_show_leaderboard(self):
        """Host zeigt das Leaderboard für alle Teilnehmer an."""
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

    async def answer_corrected(self, event):
        await self.send(text_data=json.dumps({
            'type': 'answer_corrected',
            'participant_id': event.get('participant_id'),
            'participant_name': event.get('participant_name'),
            'question_id': event.get('question_id'),
            'is_correct': event.get('is_correct', True),
            'total_score': event.get('total_score'),
        }))

    async def tutorial_start(self, event):
        await self.send(text_data=json.dumps({
            'type': 'tutorial_start',
            'game_title': event.get('game_title'),
            'tutorial_title': event.get('tutorial_title'),
            'tutorial_text': event.get('tutorial_text'),
        }))

    async def tutorial_progress(self, event):
        await self.send(text_data=json.dumps({
            'type': 'tutorial_progress',
            'completed': event['completed'],
            'total': event['total'],
            'all_done': event['all_done']
        }))

    # Database operations
    @database_sync_to_async
    def get_current_question_data(self):
        """Return serialised question data for the currently active question, or None."""
        try:
            quiz = Quiz.objects.select_related('current_question').get(room_code=self.room_code)
            q = quiz.current_question
            if not q:
                return None
            if q.question_type == 'multiple_choice':
                options = [{'key': k, 'text': t} for k, t in q.get_options()]
            elif q.question_type == 'true_false':
                options = [{'key': 'True', 'text': 'True'}, {'key': 'False', 'text': 'False'}]
            else:
                options = []
            return {
                'id': q.id,
                'question_text': q.question_text,
                'question_type': q.get_effective_question_type(),
                'options': options,
                'short_answer_fields': q.get_public_short_answer_fields(),
                'time_limit': q.time_limit,
                'points': q.points,
            }
        except Quiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_quiz(self):
        try:
            return Quiz.objects.get(room_code=self.room_code)
        except Quiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return QuizQuestion.objects.get(id=question_id)
        except QuizQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            return quiz.selected_questions.exists()
        except Quiz.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_in_selected(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            return quiz.selected_questions.filter(id=question_id).exists()
        except Quiz.DoesNotExist:
            return False

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = Quiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score
            }
        except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def reset_tutorial_completed(self, quiz_id):
        QuizParticipant.objects.filter(quiz_id=quiz_id).update(tutorial_completed=False)

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        try:
            quiz = Quiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            participant.tutorial_completed = True
            participant.save(update_fields=['tutorial_completed'])
        except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
            pass

    @database_sync_to_async
    def get_tutorial_progress(self, quiz_id, hub_session_code):
        qs = QuizParticipant.objects.filter(quiz_id=quiz_id, is_active=True)
        if hub_session_code:
            qs = qs.filter(hub_session_code=hub_session_code)
        total = qs.count()
        completed = qs.filter(tutorial_completed=True).count()
        return completed, total

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            quiz.tutorial_active = bool(active)
            quiz.save(update_fields=['tutorial_active'])
        except Quiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            if not quiz.tutorial_enabled or not quiz.tutorial_active:
                return None
            return {
                'game_title': quiz.title,
                'tutorial_title': quiz.tutorial_title or 'Tutorial',
                'tutorial_text': quiz.tutorial_text or '',
            }
        except Quiz.DoesNotExist:
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            quiz.status = 'active'
            quiz.started_at = timezone.now()
            quiz.save()
        except Quiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.save()
        except Quiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except Quiz.DoesNotExist:
            pass

    @database_sync_to_async
    def update_quiz_question(self, quiz, question):
        quiz.current_question = question
        quiz.question_start_time = timezone.now()
        quiz.save()

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            quiz.current_question = None
            quiz.question_start_time = None
            quiz.save()
        except Quiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_question_options(self, question):
        if question.question_type == 'multiple_choice':
            return [{'key': key, 'text': text} for key, text in question.get_options()]
        elif question.question_type == 'true_false':
            return [
                {'key': 'True', 'text': 'True'},
                {'key': 'False', 'text': 'False'}
            ]
        return []

    @database_sync_to_async
    def get_short_answer_fields(self, question):
        return question.get_public_short_answer_fields()

    @database_sync_to_async
    def get_current_question_correct_payload(self):
        try:
            quiz = Quiz.objects.select_related('current_question').get(room_code=self.room_code)
            q = quiz.current_question
            if not q:
                return None
            formatted = q.get_formatted_correct_answer()
            raw = q.correct_answer
            if q.get_effective_question_type() == 'short_answer' and q.get_short_answer_field_count() > 1:
                raw = {
                    field['key']: field['correct_answer']
                    for field in q.get_short_answer_fields()
                }
            return {
                'question_id': q.id,
                'formatted_answer': formatted,
                'raw': raw,
            }
        except Quiz.DoesNotExist:
            return None

    # --- Hub mirroring helpers (Stage B) ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='quiz', room_code=self.room_code)
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
    def get_answer_progress(self, hub_session_code):
        """Return (answered_count, active_participant_count) for the current question."""
        try:
            quiz = Quiz.objects.select_related('current_question').get(room_code=self.room_code)
            if not quiz.current_question:
                return 0, 0
            participants_qs = quiz.participants.filter(is_active=True)
            if hub_session_code:
                participants_qs = participants_qs.filter(hub_session_code=hub_session_code)
            total = participants_qs.count()
            answers_qs = QuizAnswer.objects.filter(
                quiz=quiz,
                question=quiz.current_question,
                participant__in=participants_qs
            )
            # Only count answers from the current question round (handles replayed questions)
            if quiz.question_start_time:
                answers_qs = answers_qs.filter(submitted_at__gte=quiz.question_start_time)
            answered = answers_qs.count()
            return answered, total
        except Quiz.DoesNotExist:
            return 0, 0

    def _recently_ended_question_cache_key(self):
        return f'quiz_recently_ended_question:{self.room_code}'

    @database_sync_to_async
    def mark_recently_ended_question(self, question_id):
        cache.set(self._recently_ended_question_cache_key(), str(question_id), timeout=120)

    @database_sync_to_async
    def save_participant_answer(
        self,
        participant_name,
        hub_session_code,
        answer_text,
        time_taken,
        question_id=None,
        allow_recently_ended=False,
    ):
        try:
            quiz = Quiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)

            if quiz.status != 'active':
                return None

            current_question = quiz.current_question
            target_question = current_question
            question_id = str(question_id) if question_id is not None else None

            if target_question and question_id and str(target_question.id) != question_id:
                recently_ended_question_id = cache.get(self._recently_ended_question_cache_key())
                if allow_recently_ended and str(recently_ended_question_id or '') == question_id:
                    target_question = QuizQuestion.objects.filter(id=question_id).first()
                else:
                    return None

            if not target_question:
                if not (allow_recently_ended and question_id):
                    return None
                recently_ended_question_id = cache.get(self._recently_ended_question_cache_key())
                if str(recently_ended_question_id or '') != question_id:
                    return None
                target_question = QuizQuestion.objects.filter(id=question_id).first()
                if not target_question:
                    return None

            # Check if answer already exists
            existing_answer = QuizAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=target_question
            ).first()

            if existing_answer:
                # Allow re-answer if the question was resent after the previous answer
                if current_question and current_question.id == target_question.id and quiz.question_start_time and existing_answer.submitted_at < quiz.question_start_time:
                    existing_answer.delete()
                else:
                    return None  # Already answered in this round

            answer_to_store = answer_text
            display_answer = answer_text
            if target_question.get_effective_question_type() == 'short_answer':
                answer_to_store = target_question.serialize_short_answer_submission(answer_text)
                display_answer = target_question.format_short_answer_submission(answer_text)

            # Create new answer
            answer = QuizAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=target_question,
                answer_text=answer_to_store,
                time_taken=time_taken
            )

            # Resolve key → full text for multiple choice
            if target_question.question_type == 'multiple_choice':
                option_map = dict(target_question.get_options())
                display_answer = option_map.get(answer_text.upper(), answer_text)

            participant.refresh_from_db(fields=['total_score'])
            return {
                'answer_id': answer.id,
                'question_id': target_question.id,
                'is_correct': answer.is_correct,
                'points_earned': answer.points_earned,
                'display_answer': display_answer,
                'question_type': target_question.get_effective_question_type(),
                'can_mark_correct': target_question.get_effective_question_type() == 'short_answer' and not answer.is_correct,
                'total_score': participant.total_score,
            }

        except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = QuizParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except QuizParticipant.DoesNotExist:
            pass

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = Quiz.objects.get(room_code=self.room_code)
            # Filter by hub session code if available via HubGameStep
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='quiz', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
            return list(qs.values('name', 'total_score'))
        except Quiz.DoesNotExist:
            return []
