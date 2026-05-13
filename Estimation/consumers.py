import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import EstimationQuiz, EstimationParticipant, EstimationQuestion, EstimationAnswer, EstimationSession
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep


class EstimationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'estimation_{self.room_code}'

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
                    'message': 'Estimation Quiz has started!'
                }
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

        await self.set_tutorial_active_db(quiz.id, False)
        # Update quiz with new question
        await self.update_quiz_question(quiz, question, custom_time_limit)
        
        # Get question data
        question_data = await self.get_question_data(question)
        max_points = await self.get_question_max_points_for_quiz(quiz.id, question.id)
        question_number = await self.get_question_number_for_quiz(quiz.id, question.id)

        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else 90
        
        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'question_text': question.question_text,
                    'unit': question_data['unit'],
                    'unit_display': question_data['unit_display'],
                    'question_number': question_number,
                    'max_points': max_points,
                    'hint_text': question.hint_text,
                    'time_limit': effective_time_limit
                }
            }
        )

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            # Get the correct answer and, if rank mode, compute rankings before clearing
            correct_answer_data = await self.get_current_question_answer(quiz)
            max_points = await self.get_current_question_max_points(quiz)
            rank_results = None
            if quiz.get_effective_scoring_mode() == 'rank':
                rank_results = await self.compute_rank_points_for_current_question(quiz.id)

            # Now clear the current question
            await self.clear_current_question(quiz.id)

            # Broadcast end of question (include rank results when applicable)
            payload = {
                'type': 'question_ended',
                'message': 'Time\'s up!',
                'correct_answer': correct_answer_data,
                'max_points': max_points,
            }
            if rank_results is not None:
                payload['rank_results'] = [
                    {
                        'participant_name': result.get('participant_name'),
                        'points_earned': result.get('points_earned'),
                        'rank_position': result.get('rank_position'),
                    }
                    for result in rank_results
                ]
            await self.channel_layer.group_send(self.room_group_name, payload)

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
            await self.end_quiz_db(quiz.id)
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
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_inactive',
                    'message': 'Quiz has been set inactive.'
                }
            )

    async def handle_participant_submit_answer(self, data):
        """Handle participant submitting their estimation answer"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        user_answer = data.get('user_answer')
        time_taken = data.get('time_taken', 0)

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name, hub_session, user_answer, time_taken
        )
        
        if answer:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'points_earned': answer['points_earned'],
                'accuracy_percentage': answer['accuracy_percentage'],
                'user_answer': answer['user_answer'],
                'percentage_difference': answer['percentage_difference']
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
                        'accuracy_percentage': answer['accuracy_percentage'],
                        'percentage_difference': answer['percentage_difference'],
                        'difference_indicator': answer['difference_indicator'],
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
            'message': event['message'],
            'correct_answer': event.get('correct_answer'),
            'rank_results': event.get('rank_results'),
            'max_points': event.get('max_points'),
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
            quiz = EstimationQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            question = quiz.current_question
            if not question:
                return None
            return {
                'id': question.id,
                'question_text': question.question_text,
                'unit': question.unit,
                'unit_display': question.get_unit_display_text(),
                'question_number': self.get_question_number_for_quiz_value(quiz, question.id),
                'max_points': question.get_max_points_for_mode(
                    quiz.get_effective_scoring_mode(),
                    self.get_participant_count_for_quiz(quiz),
                ),
                'hint_text': question.hint_text,
                'time_limit': 90,
            }
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
            quiz.tutorial_active = bool(active)
            quiz.save(update_fields=['tutorial_active'])
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            if not quiz.tutorial_enabled or not quiz.tutorial_active:
                return None
            return {
                'game_title': quiz.title,
                'tutorial_title': quiz.tutorial_title or 'Tutorial',
                'tutorial_text': quiz.tutorial_text or '',
            }
        except EstimationQuiz.DoesNotExist:
            return None

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
    def start_quiz_db(self, quiz_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            quiz.status = 'active'
            quiz.started_at = timezone.now()
            quiz.save()
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.save()
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except EstimationQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def update_quiz_question(self, quiz, question, custom_time_limit=None):
        session, _ = EstimationSession.objects.get_or_create(quiz=quiz)
        session.send_question(question)
        if custom_time_limit is not None:
            session.question_end_time = timezone.now() + timezone.timedelta(seconds=custom_time_limit)
            session.save(update_fields=['question_end_time', 'updated_at'])

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
    def compute_rank_points_for_current_question(self, quiz_id: int):
        """Pool all answers for the current question, rank by closeness, assign descending points.
        Returns list of dicts: participant_name, points_earned, rank_position, user_answer, formatted_answer, accuracy_percentage, percentage_difference, difference_indicator.
        """
        try:
            quiz = EstimationQuiz.objects.get(id=quiz_id)
            question = quiz.current_question
            if not question:
                return []

            # Gather all answers for this quiz/question
            answers = list(EstimationAnswer.objects.filter(quiz=quiz, question=question).select_related('participant', 'question'))
            if not answers:
                return []

            # Rank by absolute difference to correct answer; tie-breaker: faster time_taken wins, then earlier submitted_at
            def sort_key(ans: EstimationAnswer):
                diff = abs(ans.user_answer - question.correct_answer)
                time_val = ans.time_taken if ans.time_taken is not None else float('inf')
                return (diff, time_val, ans.submitted_at)

            answers.sort(key=sort_key)

            participant_count = max(self.get_participant_count_for_quiz(quiz), len(answers))

            results = []
            for idx, ans in enumerate(answers):
                # Descending points from participant count to 1
                points = max(1, participant_count - idx)
                # Update and save; this recalculates participant total via model's save
                ans.points_earned = points
                ans.save()

                results.append({
                    'participant_name': ans.participant.name,
                    'points_earned': ans.points_earned,
                    'rank_position': idx + 1,
                    'user_answer': ans.user_answer,
                    'formatted_answer': ans.get_formatted_user_answer(),
                    'accuracy_percentage': ans.get_accuracy_percentage(),
                    'percentage_difference': ans.get_percentage_difference(),
                    'difference_indicator': ans.get_difference_indicator(),
                    'time_taken': ans.time_taken,
                })

            return results
        except EstimationQuiz.DoesNotExist:
            return []

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
        try:
            session = quiz.session
        except EstimationSession.DoesNotExist:
            session = None

        if (
            session
            and quiz.current_question_id
            and int(question_id) == int(quiz.current_question_id)
            and session.current_question_number > 0
        ):
            return session.current_question_number

        ordered_ids = []
        seen_ids = set()
        for answer in (
            EstimationAnswer.objects
            .filter(quiz=quiz)
            .order_by('submitted_at', 'id')
            .values_list('question_id', flat=True)
        ):
            if answer in seen_ids:
                continue
            ordered_ids.append(answer)
            seen_ids.add(answer)

        if quiz.current_question_id and quiz.current_question_id not in seen_ids:
            ordered_ids.append(quiz.current_question_id)
            seen_ids.add(quiz.current_question_id)

        for raw_question_id in quiz.question_order or []:
            try:
                normalized_question_id = int(raw_question_id)
            except (TypeError, ValueError):
                continue
            if normalized_question_id not in seen_ids:
                ordered_ids.append(normalized_question_id)
                seen_ids.add(normalized_question_id)

        for selected_id in quiz.selected_questions.values_list('id', flat=True):
            if selected_id not in seen_ids:
                ordered_ids.append(selected_id)
                seen_ids.add(selected_id)

        if question_id in ordered_ids:
            return ordered_ids.index(question_id) + 1
        return 0

    def get_participant_count_for_quiz(self, quiz):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='estimation', room_code=self.room_code)
            active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
            step = active or qs.order_by('-id').first()
            session_code = step.session.code if step else None
        except Exception:
            session_code = None

        participants = quiz.participants
        if session_code:
            participants = participants.filter(hub_session_code=session_code)
        return participants.count()

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session_code, user_answer, time_taken):
        try:            # Collect final scores
            quiz = EstimationQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            
            if quiz.status != 'active':
                return None
            if not quiz.current_question:
                return None
            
            # Check if answer already exists
            existing_answer = EstimationAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question
            ).first()
            
            if existing_answer:
                return None  # Already answered
            
            # Convert user answer to float
            try:
                user_answer_float = float(user_answer)
            except (ValueError, TypeError):
                return None
            
            # Create new answer
            answer = EstimationAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
                user_answer=user_answer_float,
                time_taken=time_taken
            )
            
            return {
                'points_earned': answer.points_earned,
                'accuracy_percentage': answer.get_accuracy_percentage(),
                'user_answer': answer.user_answer,
                'formatted_answer': answer.get_formatted_user_answer(),
                'percentage_difference': answer.get_percentage_difference(),
                'difference_indicator': answer.get_difference_indicator()
            }
            
        except (EstimationQuiz.DoesNotExist, EstimationParticipant.DoesNotExist):
            return None

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
            # Filter by hub session code if available via HubGameStep
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='estimation', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
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
