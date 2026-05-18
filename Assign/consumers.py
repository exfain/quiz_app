import asyncio
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import AssignQuiz, AssignParticipant, AssignQuestion, AssignAnswer
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import HubGameStep


class AssignConsumer(AsyncWebsocketConsumer):
    # Tracks which channels have submitted for a given (room_code, server_round_index)
    _round_submissions: dict[tuple, set] = {}
    # Tracks explicit "eingeloggt/final" channels for a given (room_code, server_round_index)
    _round_logged: dict[tuple, set] = {}
    # Tracks latest temporary selection per channel for a given (room_code, server_round_index)
    _round_selections: dict[tuple, dict] = {}
    # Prevents duplicate auto-advance/auto-end triggers
    _auto_advancing: set = set()
    # Tracks participant channels per room (nur Teilnehmer, nicht Admins)
    _participant_channels: dict[str, set] = {}
    # Maps channel_name → participant_name (für Live-Response-Anzeige)
    _channel_participants: dict[str, str] = {}
    # Maps channel_name → hub_session_code (für session-scope Filter)
    _channel_hub_sessions: dict[str, str] = {}
    # Effektives Zeit-Limit pro Raum (kann vom gespeicherten Wert abweichen)
    _effective_time_limits: dict[str, int] = {}
    # Tracks eliminated participants per room (stable participant keys, not channels)
    _eliminated_participants: dict[str, set] = {}
    # Tracks original right-item indices that were correctly matched per room
    _room_matched_originals: dict[str, set] = {}
    # Tracks solved left->right pairs per room so completed matches survive round transitions
    _room_solved_matches: dict[str, dict[int, int]] = {}

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
        # Remove this channel from any round-related in-memory tracking
        for key in list(self.__class__._round_submissions):
            self.__class__._round_submissions[key].discard(self.channel_name)
            if not self.__class__._round_submissions[key]:
                del self.__class__._round_submissions[key]
        for key in list(self.__class__._round_logged):
            self.__class__._round_logged[key].discard(self.channel_name)
            if not self.__class__._round_logged[key]:
                del self.__class__._round_logged[key]
        for key in list(self.__class__._round_selections):
            self.__class__._round_selections[key].pop(self.channel_name, None)
            if not self.__class__._round_selections[key]:
                del self.__class__._round_selections[key]
        # Teilnehmer-Channel entfernen
        if self.room_code in self.__class__._participant_channels:
            self.__class__._participant_channels[self.room_code].discard(self.channel_name)
        # Teilnehmer-Name-Mapping entfernen
        self.__class__._channel_participants.pop(self.channel_name, None)
        self.__class__._channel_hub_sessions.pop(self.channel_name, None)
        # Eliminated-Status wird absichtlich NICHT beim Disconnect entfernt:
        # Elimination ist fachlich pro Frage/Spieler gültig und soll Reconnect überleben.

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
            elif message_type == 'admin_set_inactive':
                await self.handle_admin_set_inactive(text_data_json)
            elif message_type == 'admin_next_round':
                await self.handle_admin_next_round(text_data_json)
            elif message_type == 'admin_show_solution':
                await self.handle_admin_show_solution(text_data_json)
            elif message_type == 'participant_check_round':
                await self.handle_participant_check_round(text_data_json)
            elif message_type == 'participant_update_selection':
                await self.handle_participant_update_selection(text_data_json)
            elif message_type == 'participant_log_round':
                await self.handle_participant_log_round(text_data_json)
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
            'assign',
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
                'assign',
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
                    'message': 'Drag & Drop Quiz has started!'
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'assign',
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
        await self.update_quiz_question(quiz, question)

        # Reset submission tracking for this room
        for key in list(self.__class__._round_submissions):
            if key[0] == self.room_code:
                del self.__class__._round_submissions[key]
        for key in list(self.__class__._round_logged):
            if key[0] == self.room_code:
                del self.__class__._round_logged[key]
        for key in list(self.__class__._round_selections):
            if key[0] == self.room_code:
                del self.__class__._round_selections[key]
        self.__class__._auto_advancing.discard(self.room_code)
        # Eliminierte Teilnehmer für neue Frage zurücksetzen
        self.__class__._eliminated_participants[self.room_code] = set()
        # Verwendete rechte Items für neue Frage zurücksetzen
        self.__class__._room_matched_originals[self.room_code] = set()
        self.__class__._room_solved_matches[self.room_code] = {}

        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        # Für alle Folgerunden merken
        self.__class__._effective_time_limits[self.room_code] = effective_time_limit

        # Runden-Index zurücksetzen und erste Runde senden
        await self.reset_round_index(quiz.id)
        question_payload = self.build_round_payload(question, 0, effective_time_limit)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': question_payload
            }
        )
        await self.broadcast_round_log_status_for_round(0)

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
        current_round = await self.get_current_round_index(quiz.id)

        # Kurze Gnadenfrist für in-flight participant_log_round Nachrichten
        # (wichtig bei Timer-Ablauf: Client onTimeUp + Admin next_round feuern fast gleichzeitig).
        await asyncio.sleep(0.35)

        # Nach der Gnadenfrist erneut prüfen, damit wir keine falsche Runde auswerten,
        # falls bereits ein anderer Trigger weitergeschaltet hat.
        latest_round = await self.get_current_round_index(quiz.id)
        if latest_round != current_round:
            return

        await self.evaluate_current_round(quiz, current_round)
        total_rounds = len(question.correct_matches or {})

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
                    'message': 'Alle Zuordnungen abgeschlossen!',
                    'solved_pairs': self.get_solved_pairs_snapshot(question),
                }
            )
        else:
            round_payload = self.build_round_payload(
                question,
                new_round_index,
                self.__class__._effective_time_limits.get(self.room_code, quiz.current_question.time_limit),
            )

            # Keine rechten Items mehr → alle Zuordnungen abgeschlossen, auf Admin-Auflösung warten
            if not round_payload['right_items']:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'question_rounds_complete',
                        'message': 'Alle Zuordnungen abgeschlossen!',
                        'solved_pairs': round_payload['solved_pairs'],
                    }
                )
                return

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'round_advanced',
                    'round_index': round_payload['round_index'],
                    'total_rounds': round_payload['total_rounds'],
                    'current_left_item': round_payload['current_left_item'],
                    'right_items': round_payload['right_items'],
                    'all_right_items': round_payload['all_right_items'],
                    'solved_pairs': round_payload['solved_pairs'],
                    'time_limit': round_payload['time_limit'],
                }
            )
            await self.broadcast_round_log_status_for_round(new_round_index)

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

    async def handle_participant_check_round(self, data):
        """Legacy compatibility: treated as final round login."""
        await self.handle_participant_log_round(data)

    def normalize_round_user_match(self, round_index, left_item_index, user_match):
        """Keep exactly one right-side selection for the chosen open left item."""
        normalized_match = {}
        raw_match = user_match if isinstance(user_match, dict) else {}

        normalized_left_index = None
        for candidate_left_index in raw_match.keys():
            try:
                normalized_left_index = int(candidate_left_index)
                break
            except (TypeError, ValueError):
                continue

        if normalized_left_index is None:
            try:
                normalized_left_index = int(left_item_index)
            except (TypeError, ValueError):
                normalized_left_index = int(round_index)

        chosen_right_pos = raw_match.get(str(normalized_left_index))
        if chosen_right_pos is None:
            chosen_right_pos = raw_match.get(normalized_left_index)
        if chosen_right_pos is None and raw_match:
            chosen_right_pos = next(iter(raw_match.values()))

        if chosen_right_pos is not None:
            normalized_match[str(normalized_left_index)] = chosen_right_pos
        return normalized_left_index, normalized_match

    async def handle_participant_update_selection(self, data):
        """Store participant's temporary current selection for this round."""
        quiz = await self.get_quiz()
        if not quiz or quiz.status != 'active':
            return
        participant_key = self.get_participant_key_for_channel(self.channel_name)
        if participant_key in self.__class__._eliminated_participants.get(self.room_code, set()):
            return
        round_index = data.get('round_index', 0)
        key = (self.room_code, round_index)
        if self.channel_name in self.__class__._round_logged.get(key, set()):
            return
        left_item_index, user_match = self.normalize_round_user_match(
            round_index,
            data.get('left_item_index', round_index),
            data.get('user_match', {}) or {},
        )
        self.__class__._round_selections.setdefault(key, {})[self.channel_name] = {
            'left_item_index': left_item_index,
            'user_match': user_match,
        }

    async def handle_participant_log_round(self, data):
        """Participant explicitly logs/finalizes answer for this round."""
        quiz = await self.get_quiz()
        if not quiz or quiz.status != 'active':
            return
        participant_key = self.get_participant_key_for_channel(self.channel_name)
        if participant_key in self.__class__._eliminated_participants.get(self.room_code, set()):
            return
        round_index = data.get('round_index', 0)
        key = (self.room_code, round_index)
        if self.channel_name in self.__class__._round_logged.get(key, set()):
            return
        left_item_index, user_match = self.normalize_round_user_match(
            round_index,
            data.get('left_item_index', round_index),
            data.get('user_match', {}) or {},
        )

        self.__class__._round_selections.setdefault(key, {})[self.channel_name] = {
            'left_item_index': left_item_index,
            'user_match': user_match,
        }
        self.__class__._round_logged.setdefault(key, set()).add(self.channel_name)

        await self.send(text_data=json.dumps({
            'type': 'round_logged',
            'round_index': round_index,
        }))
        await self.broadcast_round_log_status_for_round(round_index)

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
                'accuracy': answer['accuracy'],
                'progress_history': answer.get('progress_history', [])
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
            self.__class__._channel_hub_sessions[self.channel_name] = hub_session or ''
            
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
            progress_history = await self.get_participant_progress_history(participant_name, hub_session)
            await self.send(text_data=json.dumps({
                'type': 'progress_history',
                'history': progress_history,
            }))
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
                # Aktuelle Runde mitsenden
                round_index = 0
                if quiz.current_question:
                    round_index = await self.get_current_round_index(quiz.id)
                    question_payload = self.build_round_payload(
                        quiz.current_question,
                        round_index,
                        self.__class__._effective_time_limits.get(self.room_code, quiz.current_question.time_limit),
                    )
                    if round_index < question_payload['total_rounds']:
                        await self.send(text_data=json.dumps({
                            'type': 'question_started',
                            'question': question_payload
                        }))
                await self.broadcast_round_log_status_for_round(round_index)

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
            'message': event['message'],
            'solved_pairs': event.get('solved_pairs', []),
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

    async def round_advanced(self, event):
        """Nächste Runde an alle Clients senden"""
        await self.send(text_data=json.dumps({
            'type': 'round_advanced',
            'round_index': event['round_index'],
            'total_rounds': event['total_rounds'],
            'current_left_item': event['current_left_item'],
            'right_items': event['right_items'],
            'all_right_items': event.get('all_right_items', []),
            'solved_pairs': event.get('solved_pairs', []),
            'time_limit': event.get('time_limit', 60),
        }))

    async def participant_answered(self, event):
        """Send participant answer to admin"""
        await self.send(text_data=json.dumps({
            'type': 'participant_answered',
            'answer': event['answer']
        }))

    async def round_log_status(self, event):
        await self.send(text_data=json.dumps({
            'type': 'round_log_status',
            'round_index': event.get('round_index', 0),
            'statuses': event.get('statuses', []),
            'active_count': event.get('active_count', 0),
            'logged_count': event.get('logged_count', 0),
            'all_logged': event.get('all_logged', False),
        }))

    async def participant_joined(self, event):
        """Send new participant info to admin"""
        await self.send(text_data=json.dumps({
            'type': 'participant_joined',
            'participant': event['participant']
        }))

    async def round_checked(self, event):
        target_channel = event.get('target_channel')
        if target_channel and target_channel != self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'round_checked',
            'is_correct': event.get('is_correct'),
            'round_index': event.get('round_index'),
            'eliminated': event.get('eliminated'),
        }))

    # Database operations
    @database_sync_to_async
    def get_quiz(self):
        try:
            return AssignQuiz.objects.select_related('current_question').get(room_code=self.room_code)
        except AssignQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            quiz.tutorial_active = bool(active)
            quiz.save(update_fields=['tutorial_active'])
        except AssignQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            if not quiz.tutorial_enabled or not quiz.tutorial_active:
                return None
            return {
                'game_title': quiz.title,
                'tutorial_title': quiz.tutorial_title or 'Tutorial',
                'tutorial_text': quiz.tutorial_text or '',
            }
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
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
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

    def _get_item_text(self, item):
        if isinstance(item, dict):
            return str(item.get('text', ''))
        return str(item)

    def get_used_original_indices(self):
        solved_matches = self.__class__._room_solved_matches.get(self.room_code)
        if solved_matches is not None:
            return {int(original_idx) for original_idx in solved_matches.values()}
        return {int(original_idx) for original_idx in self.__class__._room_matched_originals.get(self.room_code, set())}

    def get_remaining_right_items(self, randomized):
        position_to_original = randomized['position_to_original']
        used_original_indices = self.get_used_original_indices()
        return [
            item for item in randomized['right_items']
            if int(position_to_original.get(item['id'], -1)) not in used_original_indices
        ]

    def get_solved_pairs_snapshot(self, question, randomized=None):
        randomized = randomized or question.get_randomized_items(room_code=self.room_code)
        original_to_position = {
            int(original_idx): int(shuffled_pos)
            for shuffled_pos, original_idx in randomized['position_to_original'].items()
        }
        solved_matches = self.__class__._room_solved_matches.get(self.room_code, {})
        solved_pairs = []
        for left_idx, original_right_idx in sorted(solved_matches.items(), key=lambda pair: int(pair[0])):
            left_idx = int(left_idx)
            original_right_idx = int(original_right_idx)
            right_position = original_to_position.get(original_right_idx)
            if right_position is None:
                continue
            left_text = ''
            right_text = ''
            if 0 <= left_idx < len(question.left_items or []):
                left_text = self._get_item_text(question.left_items[left_idx])
            if 0 <= original_right_idx < len(question.right_items or []):
                right_text = self._get_item_text(question.right_items[original_right_idx])
            solved_pairs.append({
                'left_index': left_idx,
                'left_text': left_text,
                'right_original_index': original_right_idx,
                'right_position': right_position,
                'right_text': right_text,
            })
        return solved_pairs

    def build_round_payload(self, question, round_index: int, time_limit: int):
        randomized = question.get_randomized_items(room_code=self.room_code)
        left_items = randomized['left_items']
        total_rounds = len(question.correct_matches or {})
        current_left_item = left_items[round_index] if 0 <= round_index < total_rounds else None
        return {
            'id': question.id,
            'question_text': question.question_text,
            'time_limit': time_limit,
            'left_items': left_items,
            'right_items': self.get_remaining_right_items(randomized),
            'all_right_items': randomized['right_items'],
            'solved_pairs': self.get_solved_pairs_snapshot(question, randomized),
            'total_possible_points': question.get_total_possible_points(),
            'round_index': round_index,
            'total_rounds': total_rounds,
            'current_left_item': current_left_item,
        }

    @database_sync_to_async
    def get_round_right_items(self, question, round_index=None):
        """Verbleibende rechte Items: alle Items minus die tatsächlich korrekt gematchten."""
        randomized = question.get_randomized_items(room_code=self.room_code)
        return self.get_remaining_right_items(randomized)

    @database_sync_to_async
    def check_round_answer(self, question, round_index, user_match):
        """Gibt (is_correct, original_right_idx) zurück."""
        randomized = question.get_randomized_items(room_code=self.room_code)
        position_to_original = randomized['position_to_original']

        correct_original_idx = question.correct_matches.get(str(round_index))
        if correct_original_idx is None:
            return False, None  # Distractor-Item → Zuordnung ist immer falsch

        # User-Antwort: shuffled right position für diesen left index
        # Explizite None-Prüfung, da 0 ein gültiger shuffled-Index ist (kein falsches Falsy!)
        shuffled_right_pos = user_match.get(str(round_index))
        if shuffled_right_pos is None:
            shuffled_right_pos = user_match.get(round_index)
        if shuffled_right_pos is None:
            return False, None

        original_right_idx = position_to_original.get(int(shuffled_right_pos))
        if original_right_idx is None:
            return False, None

        is_correct = int(original_right_idx) == int(correct_original_idx)
        return is_correct, original_right_idx

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
        return len(await self.get_relevant_active_channels())

    async def get_relevant_active_channels(self):
        channels = self.__class__._participant_channels.get(self.room_code, set())
        eliminated = self.__class__._eliminated_participants.get(self.room_code, set())
        active_channels = {
            channel for channel in channels
            if self.get_participant_key_for_channel(channel) not in eliminated
        }

        session_code = await self._get_hub_session_code_for_room()
        if not session_code:
            return active_channels

        return {
            channel for channel in active_channels
            if (self.__class__._channel_hub_sessions.get(channel) in (session_code, '', None))
        }

    async def maybe_auto_advance_if_all_logged(self, round_index: int):
        key = (self.room_code, round_index)
        logged = self.__class__._round_logged.get(key, set())
        active_channels = await self.get_relevant_active_channels()
        active_count = len(active_channels)
        logged_count = len(logged.intersection(active_channels))
        advance_key = f'{self.room_code}_{round_index}'
        if active_count > 0 and logged_count >= active_count and advance_key not in self.__class__._auto_advancing:
            self.__class__._auto_advancing.add(advance_key)
            await asyncio.sleep(0.6)
            self.__class__._auto_advancing.discard(advance_key)
            await self.handle_admin_next_round({'expected_round': round_index})

    async def broadcast_round_log_status_for_round(self, round_index: int):
        active_channels = await self.get_relevant_active_channels()
        key = (self.room_code, round_index)
        logged = self.__class__._round_logged.get(key, set())
        statuses_by_name = {}
        for channel in active_channels:
            pname = self.__class__._channel_participants.get(channel, '?')
            if pname not in statuses_by_name:
                statuses_by_name[pname] = {
                    'participant_name': pname,
                    'logged': False,
                }
            # If one active channel for this participant has logged, treat participant as logged.
            if channel in logged:
                statuses_by_name[pname]['logged'] = True

        statuses = sorted(statuses_by_name.values(), key=lambda x: x['participant_name'].lower())
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'round_log_status',
                'round_index': round_index,
                'statuses': statuses,
                'active_count': len(statuses),
                'logged_count': sum(1 for s in statuses if s['logged']),
                'all_logged': len(statuses) > 0 and all(s['logged'] for s in statuses),
            }
        )

    async def evaluate_current_round(self, quiz, round_index: int):
        """Evaluate this round once (at round end), not at login time."""
        key = (self.room_code, round_index)
        active_channels = await self.get_relevant_active_channels()
        round_selections = self.__class__._round_selections.get(key, {})
        round_logged = self.__class__._round_logged.get(key, set())

        for channel in list(active_channels):
            selection = round_selections.get(channel, {})
            left_item_index = selection.get('left_item_index', round_index)
            user_match = selection.get('user_match', {}) or {}
            is_correct, original_right_idx = await self.check_round_answer(quiz.current_question, left_item_index, user_match)

            if not is_correct:
                participant_key = self.get_participant_key_for_channel(channel)
                self.__class__._eliminated_participants.setdefault(self.room_code, set()).add(participant_key)
            elif original_right_idx is not None:
                self.__class__._room_solved_matches.setdefault(self.room_code, {})[int(left_item_index)] = int(original_right_idx)
                self.__class__._room_matched_originals.setdefault(self.room_code, set()).add(original_right_idx)

            participant_name = self.__class__._channel_participants.get(channel, '?')
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'logged': channel in round_logged,
                        'status': 'eingeloggt' if channel in round_logged else 'nicht eingeloggt',
                        'round_index': round_index,
                    }
                }
            )
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'round_checked',
                    'target_channel': channel,
                    'is_correct': is_correct,
                    'round_index': round_index,
                    'eliminated': not is_correct,
                }
            )

        # cleanup this round caches
        self.__class__._round_logged.pop(key, None)
        self.__class__._round_selections.pop(key, None)
        self.__class__._round_submissions.pop(key, None)

    def get_participant_key_for_channel(self, channel: str) -> str:
        """Stable participant identity key for room-scoped transient state."""
        name = (self.__class__._channel_participants.get(channel) or '').strip().lower()
        hub_session = (self.__class__._channel_hub_sessions.get(channel) or '').strip().lower()
        return f"{hub_session}::{name}"

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
            if quiz.status != 'active':
                return None

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
                'accuracy': answer.get_accuracy_percentage(),
                'progress_history': self._build_progress_history(quiz, participant),
            }

        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return None

    def _build_progress_history(self, quiz, participant):
        question_number_by_id = {}
        answer_qs = (
            AssignAnswer.objects
            .filter(quiz=quiz)
            .select_related('question')
            .order_by('submitted_at', 'id')
        )
        if participant.hub_session_code:
            answer_qs = answer_qs.filter(participant__hub_session_code=participant.hub_session_code)
        answers = list(answer_qs)
        seen_question_ids = []
        for answer in answers:
            if answer.question_id not in seen_question_ids:
                seen_question_ids.append(answer.question_id)
        if quiz.current_question_id and quiz.current_question_id not in seen_question_ids:
            seen_question_ids.append(quiz.current_question_id)
        if quiz.selected_questions.exists():
            configured_questions = list(quiz.selected_questions.all())
            order = [int(question_id) for question_id in (quiz.question_order or [])]
            if order:
                order_map = {question_id: index for index, question_id in enumerate(order)}
                configured_questions.sort(key=lambda question: order_map.get(question.id, len(order)))
            for question in configured_questions:
                if question.id not in seen_question_ids:
                    seen_question_ids.append(question.id)
        question_number_by_id = {
            question_id: index
            for index, question_id in enumerate(seen_question_ids, start=1)
        }

        answers = list(
            AssignAnswer.objects
            .filter(quiz=quiz, participant=participant)
            .select_related('question')
            .order_by('submitted_at', 'id')
        )
        history = []
        seen_participant_question_ids = set()
        for answer in answers:
            if answer.question_id in seen_participant_question_ids:
                continue
            question_number = question_number_by_id.get(answer.question_id)
            if question_number is None:
                continue
            seen_participant_question_ids.add(answer.question_id)
            max_rounds = len(answer.question.correct_matches or {})
            survived_rounds = answer.get_correct_matches_count()
            history.append({
                'question_id': answer.question_id,
                'question_number': question_number,
                'survived_rounds': survived_rounds,
                'max_rounds': max_rounds,
            })
        history.sort(key=lambda entry: entry['question_number'])
        return history

    @database_sync_to_async
    def get_participant_progress_history(self, participant_name, hub_session):
        try:
            quiz = AssignQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return self._build_progress_history(quiz, participant)
        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return []
