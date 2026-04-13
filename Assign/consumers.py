import asyncio
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import AssignQuiz, AssignParticipant, AssignQuestion, AssignAnswer
from games_hub.models import HubGameStep


class AssignConsumer(AsyncWebsocketConsumer):
    # Tracks which channels have submitted for a given (room_code, round_index)
    _round_submissions: dict[tuple, set] = {}
    # Prevents duplicate auto-advance/auto-end triggers
    _auto_advancing: set = set()
    # Tracks participant channels per room (nur Teilnehmer, nicht Admins)
    _participant_channels: dict[str, set] = {}
    # Maps channel_name → participant_name (für Live-Response-Anzeige)
    _channel_participants: dict[str, str] = {}
    # Effektives Zeit-Limit pro Raum (kann vom gespeicherten Wert abweichen)
    _effective_time_limits: dict[str, int] = {}
    # Tracks eliminated participant channels per room
    _eliminated_participants: dict[str, set] = {}

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'assign_{self.room_code}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to assign quiz session'
        }))

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
        # Remove this channel from any round-submission tracking
        for key in list(self.__class__._round_submissions):
            self.__class__._round_submissions[key].discard(self.channel_name)
        # Teilnehmer-Channel entfernen
        if self.room_code in self.__class__._participant_channels:
            self.__class__._participant_channels[self.room_code].discard(self.channel_name)
        # Teilnehmer-Name-Mapping entfernen
        self.__class__._channel_participants.pop(self.channel_name, None)
        # Eliminated-Tracking bereinigen
        if self.room_code in self.__class__._eliminated_participants:
            self.__class__._eliminated_participants[self.room_code].discard(self.channel_name)

    # Receive message from WebSocket
    async def receive(self, text_data):
        try:
            text_data_json = json.loads(text_data)
            message_type = text_data_json.get('type')
            
            print("Assign Consumer: ", message_type)
            if message_type == 'admin_start_quiz':
                await self.handle_admin_start_quiz(text_data_json)
            elif message_type == 'admin_send_question':
                await self.handle_admin_send_question(text_data_json)
            elif message_type == 'admin_end_question':
                await self.handle_admin_end_question(text_data_json)
            elif message_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(text_data_json)
            elif message_type == 'admin_next_round':
                await self.handle_admin_next_round(text_data_json)
            elif message_type == 'admin_show_solution':
                await self.handle_admin_show_solution(text_data_json)
            elif message_type == 'participant_check_round':
                await self.handle_participant_check_round(text_data_json)
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
                    'message': 'Drag & Drop Quiz has started!'
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

        # Reset submission tracking for this room
        for key in list(self.__class__._round_submissions):
            if key[0] == self.room_code:
                del self.__class__._round_submissions[key]
        self.__class__._auto_advancing.discard(self.room_code)
        # Eliminierte Teilnehmer für neue Frage zurücksetzen
        self.__class__._eliminated_participants[self.room_code] = set()

        # Get question data for drag-drop
        question_data = await self.get_question_data(question)

        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        # Für alle Folgerunden merken
        self.__class__._effective_time_limits[self.room_code] = effective_time_limit

        # Runden-Index zurücksetzen und erste Runde senden
        await self.reset_round_index(quiz.id)
        total_rounds = len(question_data['left_items'])
        current_left_item = question_data['left_items'][0] if question_data['left_items'] else None
        round_right_items = await self.get_round_right_items(question, 0)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'question_text': question.question_text,
                    'time_limit': effective_time_limit,
                    'points': question.points,
                    'left_items': question_data['left_items'],
                    'right_items': round_right_items,
                    'total_possible_points': question_data['total_possible_points'],
                    'round_index': 0,
                    'total_rounds': total_rounds,
                    'current_left_item': current_left_item,
                }
            }
        )

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question (nach Auflösung / 'Spiel beendet')."""
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

    async def handle_admin_next_round(self, data):
        """Handle admin advancing to the next round in round-based mode"""
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question:
            return

        # Guard gegen Doppel-Advance: Wenn Admin-Timer und Auto-Advance gleichzeitig feuern,
        # prüfen ob die Runde noch dem erwarteten Stand entspricht.
        expected_round = data.get('expected_round')
        if expected_round is not None:
            current_round = await self.get_current_round_index(quiz.id)
            if current_round != expected_round:
                return

        question = quiz.current_question
        question_data = await self.get_question_data(question)
        total_rounds = len(question_data['left_items'])

        # Nächsten Runden-Index ermitteln
        new_round_index = await self.increment_round_index(quiz.id)

        if new_round_index >= total_rounds:
            # Alle Runden abgeschlossen — Auflösung abwarten.
            # Frage bleibt aktiv, damit Teilnehmer ihre letzte Antwort noch per
            # participant_check_round einreichen können (auto-submit im Client).
            await self.reset_round_index(quiz.id)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_rounds_complete',
                    'message': 'Alle Zuordnungen abgeschlossen!'
                }
            )
        else:
            current_left_item = question_data['left_items'][new_round_index]
            round_right_items = await self.get_round_right_items(question, new_round_index)

            # Keine rechten Items mehr → alle Zuordnungen abgeschlossen, auf Admin-Auflösung warten
            if not round_right_items:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'question_rounds_complete',
                        'message': 'Alle Zuordnungen abgeschlossen!'
                    }
                )
                return

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'round_advanced',
                    'round_index': new_round_index,
                    'total_rounds': total_rounds,
                    'current_left_item': current_left_item,
                    'right_items': round_right_items,
                    'time_limit': self.__class__._effective_time_limits.get(self.room_code, quiz.current_question.time_limit),
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
                    'message': 'Drag & Drop Quiz has ended. Thank you for participating!',
                    'final_scores': final_scores
                }
            )

            # Mirror to hub to auto-advance session
            # await self.hub_mirror_event('game_ended', {
            #     'room_code': self.room_code,
            #     'game_key': 'assign'
            # })
            # Mirror to hub so hub can advance to next step or end session
            await self.hub_mirror_event('quiz_ended', {
                'room_code': self.room_code,
                'game_key': 'assign',
                'message': 'Quiz has ended. Thank you for participating!',
                'final_scores': final_scores
            })

    async def handle_participant_check_round(self, data):
        """Prüft die Zuordnung für eine einzelne Runde. Eliminiert Teilnehmer bei falscher Antwort."""
        # Bereits ausgeschiedene Teilnehmer ignorieren
        if self.channel_name in self.__class__._eliminated_participants.get(self.room_code, set()):
            return

        round_index = data.get('round_index', 0)
        user_match = data.get('user_match', {})  # {str(left_idx): shuffled_right_pos}

        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question:
            return

        is_correct = await self.check_round_answer(quiz.current_question, round_index, user_match)

        if not is_correct:
            # Teilnehmer als ausgeschieden markieren
            self.__class__._eliminated_participants.setdefault(self.room_code, set()).add(self.channel_name)

        await self.send(text_data=json.dumps({
            'type': 'round_checked',
            'is_correct': is_correct,
            'round_index': round_index,
            'eliminated': not is_correct,
        }))

        # Live-Response an Admin broadcasten
        participant_name = self.__class__._channel_participants.get(self.channel_name, '?')
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'participant_answered',
                'answer': {
                    'participant_name': participant_name,
                    'correct_matches': 1 if is_correct else 0,
                    'total_matches': 1,
                    'accuracy': 100 if is_correct else 0,
                    'points_earned': 0,
                    'time_taken': None,
                }
            }
        )

        if not is_correct:
            return  # Ausgeschieden — kein Auto-Advance-Tracking

        # Auto-advance: nur auslösen wenn round_index mit aktuellem Serverstand übereinstimmt
        current_server_round = await self.get_current_round_index(quiz.id)
        if current_server_round != round_index:
            return

        key = (self.room_code, round_index)
        if key not in self.__class__._round_submissions:
            self.__class__._round_submissions[key] = set()
        self.__class__._round_submissions[key].add(self.channel_name)

        active_count = await self.get_active_participant_count()
        submitted_count = len(self.__class__._round_submissions[key])
        advance_key = f'{self.room_code}_{round_index}'
        if active_count > 0 and submitted_count >= active_count and advance_key not in self.__class__._auto_advancing:
            self.__class__._auto_advancing.add(advance_key)
            del self.__class__._round_submissions[key]
            await asyncio.sleep(2)
            self.__class__._auto_advancing.discard(advance_key)
            # expected_round mitsenden: wenn Admin-Timer die Runde bereits vorangerückt hat,
            # erkennt der Guard in handle_admin_next_round den Konflikt und überspringt.
            await self.handle_admin_next_round({'expected_round': round_index})

    async def handle_participant_submit_answer(self, data):
        """Speichert alle gesammelten Runden-Antworten als AssignAnswer in der DB."""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        user_matches = data.get('user_matches', {})
        time_taken = data.get('time_taken', 0)
        question_id = data.get('question_id')

        answer = await self.save_participant_answer(
            participant_name, hub_session, user_matches, time_taken, question_id
        )

        if answer:
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'points_earned': answer['points_earned'],
                'correct_matches': answer['correct_matches'],
                'total_matches': answer['total_matches'],
                'accuracy': answer['accuracy']
            }))

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'points_earned': answer['points_earned'],
                        'correct_matches': answer['correct_matches'],
                        'total_matches': answer['total_matches'],
                        'time_taken': time_taken,
                        'accuracy': answer['accuracy']
                    }
                }
            )
        else:
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Already submitted or question not active',
                'points_earned': 0,
                'correct_matches': 0,
                'total_matches': 0,
                'accuracy': 0
            }))

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        participant = await self.get_participant_by_name(participant_name, hub_session)
        
        if participant:
            await self.mark_participant_active(participant['id'])
            # Verbundene Teilnehmer-Channel tracken
            if self.room_code not in self.__class__._participant_channels:
                self.__class__._participant_channels[self.room_code] = set()
            self.__class__._participant_channels[self.room_code].add(self.channel_name)
            # Channel → Name-Mapping für Live-Responses
            self.__class__._channel_participants[self.channel_name] = participant['name']
            
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
                # Aktuelle Runde mitsenden
                if quiz.current_question:
                    question_data = await self.get_question_data(quiz.current_question)
                    total_rounds = len(question_data['left_items'])
                    round_index = await self.get_current_round_index(quiz.id)
                    if round_index < total_rounds:
                        current_left_item = question_data['left_items'][round_index]
                        round_right_items = await self.get_round_right_items(quiz.current_question, round_index)
                        await self.send(text_data=json.dumps({
                            'type': 'question_started',
                            'question': {
                                'id': quiz.current_question.id,
                                'question_text': quiz.current_question.question_text,
                                'time_limit': quiz.current_question.time_limit,
                                'points': quiz.current_question.points,
                                'left_items': question_data['left_items'],
                                'right_items': round_right_items,
                                'total_possible_points': question_data['total_possible_points'],
                                'round_index': round_index,
                                'total_rounds': total_rounds,
                                'current_left_item': current_left_item,
                            }
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

    async def handle_admin_show_solution(self, data):
        """Admin zeigt die richtige Zuordnung für alle Teilnehmer an."""
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question:
            return
        solution_data = await self.get_solution_data(quiz.current_question)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'show_solution',
                'left_items': solution_data['left_items'],
                'right_items': solution_data['right_items'],
                'correct_matches': solution_data['correct_matches'],
            }
        )

    async def question_rounds_complete(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_rounds_complete',
            'message': event['message']
        }))

    async def show_solution(self, event):
        await self.send(text_data=json.dumps({
            'type': 'show_solution',
            'left_items': event['left_items'],
            'right_items': event['right_items'],
            'correct_matches': event['correct_matches'],
        }))

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

    async def round_advanced(self, event):
        """Nächste Runde an alle Clients senden"""
        await self.send(text_data=json.dumps({
            'type': 'round_advanced',
            'round_index': event['round_index'],
            'total_rounds': event['total_rounds'],
            'current_left_item': event['current_left_item'],
            'right_items': event['right_items'],
            'time_limit': event.get('time_limit', 60),
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
            return AssignQuiz.objects.select_related('current_question').get(room_code=self.room_code)
        except AssignQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def get_question(self, question_id):
        try:
            return AssignQuestion.objects.get(id=question_id)
        except AssignQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def quiz_has_selected_questions(self, quiz_id: int) -> bool:
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.exists()
        except AssignQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def is_question_in_selected(self, quiz_id: int, question_id: int) -> bool:
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            return quiz.selected_questions.filter(id=question_id).exists()
        except AssignQuiz.DoesNotExist:
            return False

    @database_sync_to_async
    def get_participant_by_name(self, participant_name, hub_session):
        try:
            quiz = AssignQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score
            }
        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return None

    # --- Hub mirroring helpers ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='assign', room_code=self.room_code)
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

    @database_sync_to_async
    def start_quiz_db(self, quiz_id):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            quiz.status = 'active'
            quiz.started_at = timezone.now()
            quiz.save()
        except AssignQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            quiz.status = 'completed'
            quiz.ended_at = timezone.now()
            quiz.current_question = None
            quiz.save()
        except AssignQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def update_quiz_question(self, quiz, question):
        quiz.current_question = question
        quiz.question_start_time = timezone.now()
        quiz.save()

    @database_sync_to_async
    def clear_current_question(self, quiz_id):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            quiz.current_question = None
            quiz.question_start_time = None
            quiz.save()
        except AssignQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_round_right_items(self, question, round_index):
        """Verbleibende rechte Items für diese Runde: alle Items minus die in Vorrunden korrekt genutzten."""
        randomized = question.get_randomized_items(room_code=self.room_code)
        all_right = randomized['right_items']            # [{'id': shuffled_pos, 'text': ...}]
        position_to_original = randomized['position_to_original']

        # Original-Indizes der in Vorrunden (0..round_index-1) korrekt gematchten rechten Items
        used_original_indices = set()
        for prev_round in range(round_index):
            correct_orig = question.correct_matches.get(str(prev_round))
            if correct_orig is not None:
                used_original_indices.add(int(correct_orig))

        # Alle rechten Items außer den bereits genutzten zurückgeben
        remaining = [
            item for item in all_right
            if int(position_to_original.get(item['id'], -1)) not in used_original_indices
        ]
        return remaining

    @database_sync_to_async
    def check_round_answer(self, question, round_index, user_match):
        """Gibt True zurück, wenn die Zuordnung für round_index korrekt ist."""
        randomized = question.get_randomized_items(room_code=self.room_code)
        position_to_original = randomized['position_to_original']

        correct_original_idx = question.correct_matches.get(str(round_index))
        if correct_original_idx is None:
            return False  # Distractor-Item → Zuordnung ist immer falsch

        # User-Antwort: shuffled right position für diesen left index
        # Explizite None-Prüfung, da 0 ein gültiger shuffled-Index ist (kein falsches Falsy!)
        shuffled_right_pos = user_match.get(str(round_index))
        if shuffled_right_pos is None:
            shuffled_right_pos = user_match.get(round_index)
        if shuffled_right_pos is None:
            return False

        original_right_idx = position_to_original.get(int(shuffled_right_pos))
        if original_right_idx is None:
            return False

        return int(original_right_idx) == int(correct_original_idx)

    @database_sync_to_async
    def get_question_data(self, question):
        # Get randomized items with room code for consistent shuffling
        randomized = question.get_randomized_items(room_code=self.room_code)
        
        return {
            'left_items': randomized['left_items'],
            'right_items': randomized['right_items'],
            'total_possible_points': question.get_total_possible_points()
        }

    @database_sync_to_async
    def get_solution_data(self, question):
        return {
            'left_items': question.left_items,
            'right_items': question.right_items,
            'correct_matches': question.correct_matches,
        }

    @database_sync_to_async
    def get_current_round_index(self, quiz_id):
        from .models import AssignSession
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            session = AssignSession.objects.get(quiz=quiz)
            return session.current_round_index
        except Exception:
            return 0

    @database_sync_to_async
    def reset_round_index(self, quiz_id):
        from .models import AssignSession
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            session, _ = AssignSession.objects.get_or_create(quiz=quiz)
            session.current_round_index = 0
            session.save()
        except Exception:
            pass

    @database_sync_to_async
    def increment_round_index(self, quiz_id):
        from .models import AssignSession
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            session, _ = AssignSession.objects.get_or_create(quiz=quiz)
            session.current_round_index += 1
            session.save()
            return session.current_round_index
        except Exception:
            return 0

    @database_sync_to_async
    def mark_participant_active(self, participant_id):
        try:
            participant = AssignParticipant.objects.get(id=participant_id)
            participant.is_active = True
            participant.last_activity = timezone.now()
            participant.save()
        except AssignParticipant.DoesNotExist:
            pass
    
    async def get_active_participant_count(self):
        """Anzahl aktiver Teilnehmer-Channels: verbunden UND nicht ausgeschieden."""
        channels = self.__class__._participant_channels.get(self.room_code, set())
        eliminated = self.__class__._eliminated_participants.get(self.room_code, set())
        return len(channels - eliminated)

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = AssignQuiz.objects.get(room_code=self.room_code)
            # Filter by hub session code if available via HubGameStep
            try:
                qs = HubGameStep.objects.select_related('session').filter(game_key='assign', room_code=self.room_code)
                active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
                step = active or qs.order_by('-id').first()
                session_code = step.session.code if step else None
            except Exception:
                session_code = None

            qs = quiz.participants
            if session_code:
                qs = qs.filter(hub_session_code=session_code)
            return list(qs.values('name', 'total_score'))
        except AssignQuiz.DoesNotExist:
            return []

    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session, user_matches, time_taken, question_id=None):
        """Konvertiert shuffled Positionen → Original-Indizes und speichert AssignAnswer."""
        try:
            quiz = AssignQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)

            # Frage per question_id nachschlagen (bevorzugt), Fallback auf current_question
            if question_id:
                try:
                    question = AssignQuestion.objects.get(id=question_id)
                except AssignQuestion.DoesNotExist:
                    return None
            elif quiz.current_question:
                question = quiz.current_question
            else:
                return None

            # Doppeltes Speichern verhindern
            existing = AssignAnswer.objects.filter(
                quiz=quiz, participant=participant, question=question
            ).first()
            if existing:
                return None

            # Shuffled Positionen → Original-Indizes umrechnen
            randomized_data = question.get_randomized_items(room_code=self.room_code)
            position_to_original = randomized_data['position_to_original']

            original_user_matches = {}
            for left_idx, shuffled_right_pos in user_matches.items():
                original_right_idx = position_to_original.get(int(shuffled_right_pos))
                if original_right_idx is not None:
                    original_user_matches[left_idx] = original_right_idx

            answer = AssignAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=question,
                user_matches=original_user_matches,
                time_taken=time_taken
            )

            return {
                'points_earned': answer.points_earned,
                'correct_matches': answer.get_correct_matches_count(),
                'total_matches': answer.get_total_matches_count(),
                'accuracy': answer.get_accuracy_percentage()
            }

        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return None
