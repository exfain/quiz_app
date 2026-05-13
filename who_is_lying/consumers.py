import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import WhoQuiz, WhoParticipant, WhoQuestion, WhoAnswer
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
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
            elif message_type == 'admin_set_inactive':
                await self.handle_admin_set_inactive(text_data_json)
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
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'who',
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
                'who',
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
            else:
                await self.set_tutorial_active_db(quiz.id, False)
            
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

        await self.set_tutorial_active_db(quiz.id, False)
        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit

        # Update quiz/session state for the whole question set.
        await self.update_quiz_question(quiz.id, question.id, effective_time_limit)
        question_number = await self.get_question_number_for_quiz(question.id)

        # Get question data for the game
        question_data = await self.get_question_data(question)
        
        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'question_number': question_number,
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
                tutorial_payload = await self.get_tutorial_payload(quiz.id)
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

    async def tutorial_start(self, event):
        await self.send(text_data=json.dumps({
            'type': 'tutorial_start',
            'game_title': event.get('game_title'),
            'tutorial_title': event.get('tutorial_title'),
            'tutorial_text': event.get('tutorial_text'),
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
    def get_current_question_data(self):
        """Return serialised question data for the currently active question, or None."""
        try:
            quiz = WhoQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            question_data = self.get_question_data_sync(question)
            return {
                'id': question.id,
                'question_number': self.get_question_number_for_quiz_value(question.id),
                'statement': question.statement,
                'time_limit': question.time_limit,
                'points': question.points,
                'people': question_data['people'],
                'total_possible_points': question_data['total_possible_points'],
            }
        except WhoQuiz.DoesNotExist:
            return None

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
            quiz.tutorial_active = bool(active)
            quiz.save(update_fields=['tutorial_active'])
        except WhoQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id):
        try:
            quiz = WhoQuiz.objects.get(id=quiz_id)
            if not quiz.tutorial_enabled or not quiz.tutorial_active:
                return None
            return {
                'game_title': quiz.title,
                'tutorial_title': quiz.tutorial_title or 'Tutorial',
                'tutorial_text': quiz.tutorial_text or '',
            }
        except WhoQuiz.DoesNotExist:
            return None

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
    def save_participant_answer(self, participant_name, hub_session_code, selected_liars, time_taken):
        try:
            quiz = WhoQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            
            if quiz.status != 'active':
                return None
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
            selected_liars_set = set(original_selected_liars)
            person_results = []
            for displayed_person in randomized_data['people']:
                original_idx = displayed_person['original_index']
                original_person = quiz.current_question.people[original_idx]
                is_actually_lying = bool(original_person.get('is_lying', False))
                was_selected = original_idx in selected_liars_set
                if was_selected and is_actually_lying:
                    points_effect = 1
                elif was_selected and not is_actually_lying:
                    points_effect = -1
                else:
                    points_effect = 0

                person_results.append({
                    'name': displayed_person['name'],
                    'is_lying': is_actually_lying,
                    'was_selected': was_selected,
                    'was_correct': is_actually_lying == was_selected,
                    'points_effect': points_effect,
                })
            
            return {
                'points_earned': answer.points_earned,
                'correct_identifications': answer.get_correct_identifications_count(),
                'total_people': answer.get_total_people_count(),
                'accuracy': answer.get_accuracy_percentage(),
                'analysis': analysis,
                'selected_liars_names': selected_liars_names,
                'person_results': person_results,
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
