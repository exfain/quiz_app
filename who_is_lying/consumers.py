import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import WhoQuiz, WhoParticipant, WhoQuestion, WhoAnswer
from games_hub.models import HubGameStep


class WhoConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'who_{self.room_code}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to Who is Lying quiz session'
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
            
            print("Who_is_lying Consumer: ", message_type)
            if message_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(text_data_json)
            elif message_type == 'admin_send_question':
                await self.handle_admin_send_question(text_data_json)
            elif message_type == 'admin_end_question':
                await self.handle_admin_end_question(text_data_json)
            elif message_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(text_data_json)
            elif message_type == 'admin_set_time_per_person':
                await self.handle_admin_set_time_per_person(text_data_json)
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
        quiz = await self.get_quiz()
        if quiz:
            await self.start_quiz_db(quiz.id)
            
            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'Who is Lying? Quiz has started!'
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

        # Update quiz with new question
        await self.update_quiz_question(quiz, question)
        
        # Get question data for the game
        question_data = await self.get_question_data(question)

        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        
        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'statement': question.statement,
                    'time_limit': effective_time_limit,
                    'points': question.points,
                    'people': question_data['people'],
                    'total_possible_points': question_data['total_possible_points']
                }
            }
        )

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            await self.clear_current_question(quiz.id)
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Question time is up!'
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

    async def handle_admin_set_time_per_person(self, data):
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

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name, hub_session, selected_liars, time_taken
        )
        
        if answer:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'points_earned': answer['points_earned'],
                'correct_identifications': answer['correct_identifications'],
                'total_people': answer['total_people'],
                'accuracy': answer['accuracy'],
                'analysis': answer['analysis']
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'points_earned': answer['points_earned'],
                        'correct_identifications': answer['correct_identifications'],
                        'total_people': answer['total_people'],
                        'selected_liars_names': answer['selected_liars_names'],
                        'time_taken': time_taken,
                        'accuracy': answer['accuracy']
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
            if quiz and quiz.status == 'active':
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Quiz is already in progress'
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

    async def question_ended(self, event):
        """Send question ended message"""
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
    def get_quiz(self):
        try:
            return WhoQuiz.objects.get(room_code=self.room_code)
        except WhoQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return WhoQuestion.objects.get(id=question_id)
        except WhoQuestion.DoesNotExist:
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
    def start_quiz_db(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            quiz.status = 'active'
            quiz.started_at = timezone.now()
            quiz.save()
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.save()
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def update_quiz_question(self, quiz, question):
        quiz.current_question = question
        quiz.question_start_time = timezone.now()
        quiz.save()

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            quiz.current_question = None
            quiz.question_start_time = None
            quiz.save()
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_question_data(self, question):
        # Get randomized people with room code for consistent shuffling
        randomized = question.get_randomized_people(room_code=self.room_code)
        
        return {
            'people': randomized['people'],
            'total_possible_points': question.get_total_possible_points()
        }

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session_code, selected_liars, time_taken):
        try:
            quiz = WhoQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            
            if not quiz.current_question:
                return None
            
            # Check if answer already exists
            existing_answer = WhoAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question
            ).first()
            
            if existing_answer:
                return None  # Already answered
            
            # Get the same randomized data using the same room code
            randomized_data = quiz.current_question.get_randomized_people(room_code=self.room_code)
            position_to_original = randomized_data['position_to_original']
            
            # Convert selected liars from shuffled positions to original positions
            original_selected_liars = []
            for shuffled_pos in selected_liars:
                original_idx = position_to_original.get(int(shuffled_pos))
                if original_idx is not None:
                    original_selected_liars.append(original_idx)
            
            # Create new answer with original indices
            answer = WhoAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
                selected_liars=original_selected_liars,
                time_taken=time_taken
            )
            
            # Get detailed analysis
            analysis = answer.get_detailed_analysis()
            selected_liars_names = answer.get_selected_liars_names()
            
            return {
                'points_earned': answer.points_earned,
                'correct_identifications': answer.get_correct_identifications_count(),
                'total_people': answer.get_total_people_count(),
                'accuracy': answer.get_accuracy_percentage(),
                'analysis': analysis,
                'selected_liars_names': selected_liars_names
            }
            
        except (WhoQuiz.DoesNotExist, WhoParticipant.DoesNotExist):
            return None

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
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='who', room_code=self.room_code)
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
