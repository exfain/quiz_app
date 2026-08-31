import json
import uuid
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import (
    AssignAnswer,
    AssignParticipant,
    AssignQuestion,
    AssignQuiz,
    AssignRoundParticipantState,
    AssignSession,
    AssignSetRuntime,
)
from .runtime import (
    ASSIGN_REVEAL_ANIMATION_MS,
    ASSIGN_REVEAL_STAGGER_MS,
    assign_reveal_counts,
    assign_reveal_ready_at,
    current_set_runtime,
    evaluate_and_advance,
    mark_ended,
    mark_revealed,
    open_round_answering,
    participant_round_snapshot,
    persist_final_answers,
    round_status,
    start_set_runtime,
    store_round_selection,
)
from .scoreboard import build_participant_progress_history, build_question_scoreboard
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import (
    QUESTION_PRESENTATION_DELAY_MS,
    QuestionPhaseDecision,
    current_snapshot,
    finish_question_flow,
    open_answering,
    present_question,
    reset_question_flow,
    reveal_question_content,
)
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import GameRuntimeState, HubGameStep
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


class AssignConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'assign'
    authoritative_required_actions = frozenset({
        'participant_log_round',
        'participant_submit_answer',
    })

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
        # Round state is participant-scoped and durable; disconnect never mutates it.
        # Teilnehmer-Channel entfernen
        # Teilnehmer-Name-Mapping entfernen
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
            elif message_type == 'admin_reveal_question_content':
                await self.handle_admin_reveal_question_content(text_data_json)
            elif message_type == 'admin_open_answering':
                await self.handle_admin_open_answering(text_data_json)
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
            show_tutorial = bool(data.get('show_tutorial', False))
            play_tutorial = bool(data.get('play_tutorial', False))
            hub_session_code = await self._get_hub_session_code_for_room()
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'assign',
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
            await database_sync_to_async(prepare_unit_tutorial_runtime)(
                'assign',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            await self.start_quiz_db(quiz.id)
            question_runtime = await database_sync_to_async(reset_question_flow)(
                game_key='assign',
                room_code=self.room_code,
                session_code=hub_session_code,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            tutorial_payload = await self.activate_tutorial_runtime(quiz.id, hub_session_code, show_tutorial)
            
            # Broadcast to all participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'Drag & Drop Quiz has started!',
                    **self.question_lifecycle_fields(question_runtime),
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
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        set_state, decision = await self.begin_assign_set_phase(
            quiz.id,
            question.id,
            hub_session,
            effective_time_limit,
            data,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question.id)
            return
        if not set_state.get('started'):
            return
        set_number = set_state['set_number']

        # Reset submission tracking for this room
        # Eliminierte Teilnehmer für neue Frage zurücksetzen
        # Verwendete rechte Items für neue Frage zurücksetzen

        # Determine the effective time limit for this send (do NOT persist on the question)
        # Für alle Folgerunden merken

        # Runden-Index zurücksetzen und erste Runde senden
        question_payload = self.build_round_payload(
            question,
            0,
            effective_time_limit,
            set_number,
            solved_matches=set_state['solved_matches'],
            starts_at=None,
            ends_at=None,
        )
        question_payload['is_tutorial_round'] = is_tutorial_round
        question_payload['points'] = 0 if is_tutorial_round else question_payload.get('points')
        lifecycle = self.question_lifecycle_fields(decision.snapshot)
        lifecycle.update(self.assign_reveal_fields(
            set_state.get('target_reveal_count', 0),
            set_state.get('element_reveal_count', 0),
            decision.snapshot,
        ))
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': question_payload,
                **lifecycle,
            }
        )
        await self.broadcast_round_log_status_for_round(0)

    async def handle_admin_reveal_question_content(self, data):
        context = await self.get_current_round_context()
        if not context:
            return
        decision = await self.reveal_assign_round(context, data)
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, context['question_id'])
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_content_revealed',
                **self.question_lifecycle_fields(decision.snapshot),
                **self.assign_reveal_fields(
                    context['target_reveal_count'],
                    context['element_reveal_count'],
                    decision.snapshot,
                ),
                'question_id': context['question_id'],
                'round_index': context['round_index'],
                'set_number': context['set_number'],
            },
        )

    async def handle_admin_open_answering(self, data):
        context = await self.get_current_round_context()
        if not context:
            return
        decision = await self.open_assign_round(context, data)
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, context['question_id'])
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_answering_opened',
                **self.question_lifecycle_fields(decision.snapshot),
                **self.assign_reveal_fields(
                    context['target_reveal_count'],
                    context['element_reveal_count'],
                    decision.snapshot,
                ),
                'question_id': context['question_id'],
                'round_index': context['round_index'],
                'set_number': context['set_number'],
            },
        )

    async def send_question_phase_rejection(self, decision, question_id=None):
        await self.send(text_data=json.dumps({
            'type': 'action_rejected',
            'code': decision.code,
            'message': decision.message,
            'question_id': question_id,
            'snapshot': decision.snapshot,
        }))

    @staticmethod
    def question_lifecycle_fields(snapshot):
        snapshot = snapshot or {}
        return {
            key: snapshot.get(key)
            for key in (
                'state_revision',
                'server_now',
                'game_id',
                'question_flow_mode',
                'question_phase',
                'question_presented_at',
                'question_visible_at',
                'content_revealed_at',
                'answering_started_at',
                'answering_deadline_at',
                'answering_allowed',
                'timer_running',
                'remaining_answer_time',
                'starts_at',
                'ends_at',
            )
        }

    @staticmethod
    def assign_reveal_fields(target_count, element_count, snapshot):
        snapshot = snapshot or {}
        revealed_at = snapshot.get('content_revealed_at')
        item_count = int(target_count or 0) + int(element_count or 0)
        duration_ms = (
            (item_count - 1) * ASSIGN_REVEAL_STAGGER_MS
            + ASSIGN_REVEAL_ANIMATION_MS
            if item_count
            else 0
        )
        ready_at = None
        if revealed_at:
            parsed = parse_datetime(str(revealed_at))
            ready_at = (
                parsed + timezone.timedelta(milliseconds=duration_ms)
            ).isoformat()
        return {
            'assign_target_reveal_count': int(target_count or 0),
            'assign_element_reveal_count': int(element_count or 0),
            'assign_reveal_stagger_ms': ASSIGN_REVEAL_STAGGER_MS,
            'assign_reveal_animation_ms': ASSIGN_REVEAL_ANIMATION_MS,
            'assign_reveal_ready_at': ready_at,
        }

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question (nach Auflösung / 'Spiel beendet')."""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
            unit_tutorial = await self.finish_current_unit_tutorial(hub_session)
            await self.persist_runtime_answers()
            await self.mark_runtime_ended()
            await self.set_question_active_db(quiz.id, False)
            await self.clear_current_question(quiz.id)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Question time is up!',
                    'is_tutorial_round': bool(unit_tutorial.get('is_tutorial_round')),
                }
            )

    async def handle_admin_next_round(self, data):
        """Evaluate and advance one round under a database lock."""
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question:
            return
        current_round = data.get('expected_round')
        if current_round is None:
            current_round = await self.get_current_round_index(quiz.id)
        set_number = await self.get_current_set_number(quiz.id)
        expected_set = data.get('expected_set')
        if expected_set is not None and int(expected_set) != int(set_number):
            return

        result = await self.evaluate_and_advance_round(current_round, set_number)
        if not result.get('advanced'):
            return
        for outcome in result.get('outcomes', []):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': outcome['participant_name'],
                        'logged': outcome['logged'],
                        'status': 'eingeloggt' if outcome['logged'] else 'nicht eingeloggt',
                        'round_index': current_round,
                    },
                },
            )
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'round_checked',
                    'target_participant_name': outcome['participant_name'],
                    'target_hub_session': outcome['hub_session_code'],
                    'is_correct': outcome['is_correct'],
                    'round_index': current_round,
                    'eliminated': not outcome['is_correct'],
                    'elimination_reason': outcome['elimination_reason'],
                    'set_number': set_number,
                },
            )

        question = quiz.current_question
        solved_pairs = self.get_solved_pairs_snapshot(
            question,
            solved_matches=result.get('solved_matches'),
        )
        if result.get('completed'):
            await self.finish_assign_round_phase(question.id)
            await self.persist_runtime_answers()
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_rounds_complete',
                    'message': 'Alle Zuordnungen abgeschlossen!',
                    'solved_pairs': solved_pairs,
                    'set_number': set_number,
                },
            )
            return

        new_round_index = result['next_round']
        phase_decision = await self.present_next_assign_round_phase(
            question.id,
            set_number,
            new_round_index,
            result['effective_time_limit'],
        )
        if not phase_decision.accepted:
            return
        round_payload = self.build_round_payload(
            question,
            new_round_index,
            result['effective_time_limit'],
            set_number,
            solved_matches=result.get('solved_matches'),
            starts_at=None,
            ends_at=None,
        )
        target_count = len(round_payload.get('right_items') or [])
        solved_left = {
            int(pair.get('left_index'))
            for pair in (round_payload.get('solved_pairs') or [])
            if pair.get('left_index') is not None
        }
        element_count = sum(
            1
            for index, _ in enumerate(round_payload.get('left_items') or [])
            if index not in solved_left
        )
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
                'starts_at': None,
                'ends_at': None,
                'server_now': round_payload['server_now'],
                'set_number': set_number,
                **self.question_lifecycle_fields(phase_decision.snapshot),
                **self.assign_reveal_fields(
                    target_count,
                    element_count,
                    phase_decision.snapshot,
                ),
            },
        )
        await self.broadcast_round_log_status_for_round(new_round_index)

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            await self.set_question_active_db(quiz.id, False)
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

    @database_sync_to_async
    def persist_round_selection(
        self,
        data,
        round_index,
        left_item_index,
        user_match,
        *,
        lock_selection,
    ):
        result = store_round_selection(
            room_code=self.room_code,
            participant_name=self._authoritative_participant_name(),
            hub_session_code=self._authoritative_session_code(),
            question_id=data.get('question_id'),
            round_index=round_index,
            left_item_index=left_item_index,
            user_match=user_match,
            lock_selection=lock_selection,
        )
        runtime = current_set_runtime(
            self.room_code,
            self._authoritative_session_code(),
        )
        return {
            'accepted': result.accepted,
            'code': result.code,
            'all_locked': result.all_locked,
            'set_number': runtime.set_number if runtime else None,
        }

    async def reject_round_action(self, code):
        await self.send(text_data=json.dumps({
            'type': 'action_rejected',
            'code': code or 'invalid_phase',
            'message': 'Der Rundenstatus hat sich geaendert.',
        }))
        await self._send_authoritative_snapshot()

    async def reject_ineligible_round_action(self):
        participant_name = self._authoritative_participant_name()
        hub_session = self._authoritative_session_code()
        state = await self.get_participant_set_state(participant_name, hub_session)
        if not state.get('authorized'):
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Teilnahme für diese Spielinstanz nicht bestätigt.',
            }))
            return True

        elimination_reason = state.get('elimination_reason')
        if not elimination_reason:
            return False

        await self.send(text_data=json.dumps({
            'type': 'round_checked',
            'is_correct': False,
            'round_index': await self.get_current_round_index_by_room(),
            'eliminated': True,
            'elimination_reason': elimination_reason,
            'set_number': state.get('set_number'),
        }))
        return True

    async def handle_participant_update_selection(self, data):
        """Store participant's temporary current selection for this round."""
        quiz = await self.get_quiz()
        if not quiz or quiz.status != 'active':
            return
        if await self.reject_ineligible_round_action():
            return
        round_index = data.get('round_index', 0)
        left_item_index, user_match = self.normalize_round_user_match(
            round_index,
            data.get('left_item_index', round_index),
            data.get('user_match', {}) or {},
        )
        result = await self.persist_round_selection(
            data,
            round_index,
            left_item_index,
            user_match,
            lock_selection=False,
        )
        if not result.get('accepted'):
            await self.reject_round_action(result.get('code'))

    async def handle_participant_log_round(self, data):
        """Participant explicitly logs/finalizes answer for this round."""
        quiz = await self.get_quiz()
        if not quiz or quiz.status != 'active':
            return
        if await self.reject_ineligible_round_action():
            return
        round_index = data.get('round_index', 0)
        left_item_index, user_match = self.normalize_round_user_match(
            round_index,
            data.get('left_item_index', round_index),
            data.get('user_match', {}) or {},
        )

        result = await self.persist_round_selection(
            data,
            round_index,
            left_item_index,
            user_match,
            lock_selection=True,
        )
        if not result.get('accepted'):
            await self.reject_round_action(result.get('code'))
            return

        await self.send(text_data=json.dumps({
            'type': 'round_logged',
            'round_index': round_index,
        }))
        await self.broadcast_round_log_status_for_round(round_index)
        if result.get('all_locked'):
            await self.handle_admin_next_round({
                'expected_round': round_index,
                'expected_set': result.get('set_number'),
            })

    async def handle_participant_submit_answer(self, data):
        """Speichert alle gesammelten Runden-Antworten als AssignAnswer in der DB."""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        user_matches = data.get('user_matches', {})
        time_taken = data.get('time_taken', 0)
        question_id = data.get('question_id')

        mapped_name = self._authoritative_participant_name()
        mapped_hub_session = self._authoritative_session_code()
        if participant_name != mapped_name or (hub_session or '') != (mapped_hub_session or ''):
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Teilnehmeridentität stimmt nicht mit der Verbindung überein.',
            }))
            return
        question_state = await database_sync_to_async(current_snapshot)(
            'assign',
            self.room_code,
            mapped_hub_session,
        )
        if (
            question_state.get('question_flow_mode')
            == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            and question_state.get('question_phase')
            != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
        ):
            await self.reject_round_action('invalid_phase')
            return
        if await self.reject_ineligible_round_action():
            return

        answer = await self.save_participant_answer(
            participant_name, hub_session, user_matches, time_taken, question_id
        )

        if answer:
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'points_earned': answer['points_earned'],
                'is_tutorial_round': answer['is_tutorial_round'],
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
                        'is_tutorial_round': answer['is_tutorial_round'],
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
        self._authoritative_participant = str(participant_name or '').strip()
        self._authoritative_session = str(hub_session or '').strip()
        participant = await self.get_participant_by_name(participant_name, hub_session)
        
        if participant:
            await self.mark_participant_active(participant['id'])
            # Verbundene Teilnehmer-Channel tracken
            # Channel → Name-Mapping für Live-Responses
            
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
            scoreboard_questions = await self.get_scoreboard_questions(hub_session)
            await self.send(text_data=json.dumps({
                'type': 'progress_history',
                'history': progress_history,
                'scoreboard_questions': scoreboard_questions,
            }))
            if quiz and quiz.status == 'active':
                await self.send_state({
                    'participant_name': participant_name,
                    'hub_session': hub_session,
                })
                return
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Quiz is already in progress'
                }))
                participant_state = await self.get_participant_set_state(
                    participant['name'],
                    hub_session,
                )
                elimination_reason = participant_state.get('elimination_reason')
                if elimination_reason:
                    set_number = participant_state.get('set_number', 0)
                    await self.send(text_data=json.dumps({
                        'type': 'round_checked',
                        'is_correct': False,
                        'round_index': await self.get_current_round_index(quiz.id),
                        'eliminated': True,
                        'elimination_reason': elimination_reason,
                        'set_number': set_number,
                    }))
                    return
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
                # Aktuelle Runde mitsenden
                round_index = 0
                if quiz.current_question:
                    round_index = await self.get_current_round_index(quiz.id)
                    if await self.is_question_active_db(quiz.id):
                        set_number = await self.get_current_set_number(quiz.id)
                        question_payload = self.build_round_payload(
                            quiz.current_question,
                            round_index,
                            quiz.current_question.time_limit,
                            set_number,
                        )
                        if round_index < question_payload['total_rounds']:
                            await self.send(text_data=json.dumps({
                                'type': 'question_started',
                                'question': question_payload
                            }))
                await self.broadcast_round_log_status_for_round(round_index)

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

    async def scoreboard_questions_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'scoreboard_questions_updated',
            'scoreboard_questions': event.get('scoreboard_questions', []),
        }))

    async def handle_admin_show_solution(self, data):
        """Admin zeigt die richtige Zuordnung für alle Teilnehmer an."""
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question:
            return
        solution_data = await self.get_solution_data(quiz.current_question)
        if not await self.mark_runtime_revealed():
            return
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
            'set_number': event.get('set_number'),
        }))

    async def show_solution(self, event):
        await self.send(text_data=json.dumps({
            'type': 'show_solution',
            'left_items': event['left_items'],
            'right_items': event['right_items'],
            'correct_matches': event['correct_matches'],
        }))

    @database_sync_to_async
    def mark_runtime_revealed(self):
        return bool(mark_revealed(
            self.room_code,
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync(),
        ))

    @database_sync_to_async
    def mark_runtime_ended(self):
        return bool(mark_ended(
            self.room_code,
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync(),
        ))

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
            'question': event['question'],
            **self.question_lifecycle_fields(event),
            'assign_target_reveal_count': event.get('assign_target_reveal_count', 0),
            'assign_element_reveal_count': event.get('assign_element_reveal_count', 0),
            'assign_reveal_stagger_ms': event.get(
                'assign_reveal_stagger_ms',
                ASSIGN_REVEAL_STAGGER_MS,
            ),
            'assign_reveal_animation_ms': event.get(
                'assign_reveal_animation_ms',
                ASSIGN_REVEAL_ANIMATION_MS,
            ),
            'assign_reveal_ready_at': event.get('assign_reveal_ready_at'),
        }))

    async def question_content_revealed(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_content_revealed',
            **event,
        }))

    async def question_answering_opened(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_answering_opened',
            **event,
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
            'starts_at': event.get('starts_at'),
            'ends_at': event.get('ends_at'),
            'server_now': event.get('server_now'),
            'set_number': event.get('set_number'),
            **self.question_lifecycle_fields(event),
            'assign_target_reveal_count': event.get('assign_target_reveal_count', 0),
            'assign_element_reveal_count': event.get('assign_element_reveal_count', 0),
            'assign_reveal_stagger_ms': event.get(
                'assign_reveal_stagger_ms',
                ASSIGN_REVEAL_STAGGER_MS,
            ),
            'assign_reveal_animation_ms': event.get(
                'assign_reveal_animation_ms',
                ASSIGN_REVEAL_ANIMATION_MS,
            ),
            'assign_reveal_ready_at': event.get('assign_reveal_ready_at'),
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
        target_name = event.get('target_participant_name')
        target_session = event.get('target_hub_session')
        if target_name and target_name != self._authoritative_participant_name():
            return
        if target_session is not None and str(target_session) != self._authoritative_session_code():
            return
        await self.send(text_data=json.dumps({
            'type': 'round_checked',
            'is_correct': event.get('is_correct'),
            'round_index': event.get('round_index'),
            'eliminated': event.get('eliminated'),
            'elimination_reason': event.get('elimination_reason'),
            'set_number': event.get('set_number'),
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
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('assign', self.room_code, None, quiz)
        except AssignQuiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            payload = get_tutorial_payload('assign', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except AssignQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = AssignQuiz.objects.get(id=quiz_id)
            return activate_tutorial_runtime('assign', self.room_code, hub_session_code, quiz, show_tutorial)
        except AssignQuiz.DoesNotExist:
            return None

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('assign', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('assign', self.room_code, hub_session_code)

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('assign', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('assign', self.room_code, hub_session_code)

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

    async def send_state(self, data):
        participant_name = (
            data.get('participant_name')
            or self._authoritative_participant_name()
        )
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or self._authoritative_session_code()
        )
        snapshot = await self.get_rejoin_snapshot(participant_name, hub_session)
        await self.send(text_data=json.dumps(snapshot))

    @database_sync_to_async
    def get_rejoin_snapshot(self, participant_name, hub_session):
        now = timezone.now()
        try:
            quiz = AssignQuiz.objects.select_related(
                'current_question',
                'session',
            ).get(room_code=self.room_code)
            participant = quiz.participants.get(
                name=participant_name,
                hub_session_code=hub_session,
            )
        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return {
                'type': 'assign_state',
                'phase': 'unavailable',
                'server_now': now.isoformat(),
            }

        session = quiz.session
        runtime = (
            AssignSetRuntime.objects
            .filter(
                quiz=quiz,
                hub_session_code=str(hub_session or '').strip(),
                set_number=session.current_question_number,
            )
            .select_related('question')
            .order_by('-id')
            .first()
        )
        question = runtime.question if runtime else quiz.current_question
        phase_snapshot = current_snapshot('assign', self.room_code, hub_session)
        manual_flow = (
            phase_snapshot.get('question_flow_mode')
            == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        )
        round_state = participant_round_snapshot(runtime, participant)
        phase = quiz.status
        question_payload = None
        solution = None
        if runtime and question:
            phase = runtime.phase
            question_payload = self.build_round_payload(
                question,
                runtime.current_round_index,
                runtime.effective_time_limit,
                runtime.set_number,
                solved_matches=runtime.solved_matches,
                starts_at=(
                    phase_snapshot.get('answering_started_at')
                    if manual_flow
                    else runtime.round_started_at
                ),
                ends_at=(
                    phase_snapshot.get('answering_deadline_at')
                    if manual_flow
                    else runtime.round_ends_at
                ),
            )
            if runtime.phase == AssignSetRuntime.PHASE_REVEALED:
                solution = {
                    'left_items': question.left_items,
                    'right_items': question.right_items,
                    'correct_matches': question.correct_matches,
                }
        answer = None
        if question:
            stored_answer = AssignAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).first()
            if stored_answer:
                answer = {
                    'question_id': stored_answer.question_id,
                    'user_matches': stored_answer.user_matches,
                    'points_earned': stored_answer.points_earned,
                    'time_taken': stored_answer.time_taken,
                    'submitted_at': stored_answer.submitted_at.isoformat(),
                }
        elimination_reason = None
        if runtime and participant.eliminated_set_number == runtime.set_number:
            elimination_reason = participant.elimination_reason or 'incorrect_assignment'
        target_count, element_count = assign_reveal_counts(runtime)
        lifecycle = self.question_lifecycle_fields(phase_snapshot)
        if not manual_flow:
            lifecycle['starts_at'] = (
                runtime.round_started_at.isoformat() if runtime else None
            )
            lifecycle['ends_at'] = (
                runtime.round_ends_at.isoformat()
                if runtime and runtime.phase == AssignSetRuntime.PHASE_ACTIVE
                else None
            )
        return {
            'type': 'assign_state',
            'phase': phase,
            'game': {
                'id': quiz.id,
                'status': quiz.status,
            },
            'question': question_payload,
            'current_question_id': question.id if question else None,
            'current_round_id': runtime.current_round_index if runtime else None,
            'current_set_id': runtime.set_number if runtime else None,
            'starts_at': (
                phase_snapshot.get('answering_started_at')
                if manual_flow
                else (runtime.round_started_at.isoformat() if runtime else None)
            ),
            'ends_at': (
                phase_snapshot.get('answering_deadline_at')
                if manual_flow
                else (
                    runtime.round_ends_at.isoformat()
                    if runtime and runtime.phase == AssignSetRuntime.PHASE_ACTIVE
                    else None
                )
            ),
            'server_now': now.isoformat(),
            'participant_state': {
                'participant_id': participant.id,
                'selection': {
                    'left_item_index': round_state.left_item_index,
                    'user_match': round_state.user_match,
                } if round_state and round_state.user_match else None,
                'answer_locked': bool(round_state and round_state.is_locked),
                'eliminated': bool(elimination_reason),
                'elimination_reason': elimination_reason,
                'answer': answer,
                'next_state': phase,
            },
            'revealed': phase == AssignSetRuntime.PHASE_REVEALED,
            'solution': solution,
            'solved_pairs': (
                self.get_solved_pairs_snapshot(
                    question,
                    solved_matches=runtime.solved_matches,
                )
                if runtime and question
                else []
            ),
            'progress_history': self._build_progress_history(quiz, participant),
            'scoreboard_questions': build_question_scoreboard(
                quiz,
                None,
                participant.hub_session_code,
            ),
            **lifecycle,
            **self.assign_reveal_fields(
                target_count,
                element_count,
                phase_snapshot,
            ),
        }

    # --- Hub mirroring helpers ---
    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        return self._resolve_hub_session_code_sync()

    def _resolve_hub_session_code_sync(self):
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
    def begin_set_db(self, quiz_id, question_id, hub_session_code, effective_time_limit):
        runtime, started = start_set_runtime(
            quiz_id=quiz_id,
            question_id=question_id,
            hub_session_code=hub_session_code,
            effective_time_limit=effective_time_limit,
        )
        if runtime is None:
            session = AssignSession.objects.filter(quiz_id=quiz_id).first()
            return {
                'set_number': session.current_question_number if session else 0,
                'started': False,
            }
        return {
            'set_number': runtime.set_number,
            'started': started,
            'solved_matches': runtime.solved_matches,
            'starts_at': runtime.round_started_at.isoformat(),
            'ends_at': runtime.round_ends_at.isoformat(),
        }

    @database_sync_to_async
    def begin_assign_set_phase(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        effective_time_limit,
        action,
    ):
        with transaction.atomic():
            phase_snapshot = current_snapshot(
                'assign',
                self.room_code,
                hub_session_code,
            )
            manual_flow = (
                phase_snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            runtime, started = start_set_runtime(
                quiz_id=quiz_id,
                question_id=question_id,
                hub_session_code=hub_session_code,
                effective_time_limit=effective_time_limit,
                start_answering=not manual_flow,
            )
            if not manual_flow:
                target_count, element_count = assign_reveal_counts(runtime)
                return {
                    'set_number': runtime.set_number,
                    'started': started,
                    'solved_matches': runtime.solved_matches,
                    'target_reveal_count': target_count,
                    'element_reveal_count': element_count,
                }, QuestionPhaseDecision(
                    True,
                    'accepted',
                    '',
                    state_revision=phase_snapshot.get('state_revision'),
                    snapshot=phase_snapshot,
                )
            decision = present_question(
                game_key='assign',
                room_code=self.room_code,
                session_code=hub_session_code,
                action={**action, 'question_id': question_id},
                answer_duration_seconds=effective_time_limit,
            )
            if started and not decision.accepted:
                transaction.set_rollback(True)
                return {'started': False}, decision
            if not runtime:
                return {'started': False}, decision
            target_count, element_count = assign_reveal_counts(runtime)
            return {
                'set_number': runtime.set_number,
                'started': started,
                'solved_matches': runtime.solved_matches,
                'target_reveal_count': target_count,
                'element_reveal_count': element_count,
            }, decision

    @database_sync_to_async
    def get_current_round_context(self):
        session_code = (
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync()
        )
        runtime = current_set_runtime(self.room_code, session_code)
        if not runtime:
            return None
        target_count, element_count = assign_reveal_counts(runtime)
        return {
            'question_id': runtime.question_id,
            'round_index': runtime.current_round_index,
            'set_number': runtime.set_number,
            'time_limit': runtime.effective_time_limit,
            'target_reveal_count': target_count,
            'element_reveal_count': element_count,
            'session_code': session_code,
        }

    @staticmethod
    def _assign_action_matches_context(action, context):
        try:
            return (
                int(action.get('expected_round', action.get('round_id')))
                == int(context['round_index'])
                and int(action.get('expected_set', action.get('set_id')))
                == int(context['set_number'])
            )
        except (TypeError, ValueError):
            return False

    @database_sync_to_async
    def reveal_assign_round(self, context, action):
        with transaction.atomic():
            if not self._assign_action_matches_context(action, context):
                snapshot = current_snapshot(
                    'assign',
                    self.room_code,
                    context['session_code'],
                )
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Aktion gehoert zu einer anderen Assign-Runde.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )
            snapshot = current_snapshot(
                'assign',
                self.room_code,
                context['session_code'],
            )
            if (
                snapshot.get('question_phase')
                == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
                and str(snapshot.get('current_question_id') or '')
                == str(context['question_id'])
            ):
                return QuestionPhaseDecision(
                    True,
                    'accepted',
                    '',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                    duplicate=True,
                )
            revealed = reveal_question_content(
                game_key='assign',
                room_code=self.room_code,
                session_code=context['session_code'],
                action={**action, 'question_id': context['question_id']},
            )
            if not revealed.accepted:
                return revealed
            runtime = current_set_runtime(
                self.room_code,
                context['session_code'],
                lock=True,
            )
            revealed_at = parse_datetime(
                revealed.snapshot.get('content_revealed_at') or ''
            )
            ready_at = assign_reveal_ready_at(runtime, revealed_at)
            if not runtime or not ready_at:
                transaction.set_rollback(True)
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Assign-Runde hat sich geaendert.',
                    state_revision=revealed.state_revision,
                    snapshot=revealed.snapshot,
                )
            opened = open_answering(
                game_key='assign',
                room_code=self.room_code,
                session_code=context['session_code'],
                action={
                    **action,
                    'client_action_id': str(uuid.uuid4()),
                    'state_revision': revealed.state_revision,
                    'question_id': context['question_id'],
                },
                answer_duration_seconds=context['time_limit'],
                at=ready_at,
            )
            if not opened.accepted:
                transaction.set_rollback(True)
                return opened
            # The phase transition is scheduled for the end of the reveal. Keep
            # the response clock at send time so clients replay the remaining reveal.
            opened.snapshot['server_now'] = timezone.now().isoformat()
            persisted = open_round_answering(
                room_code=self.room_code,
                hub_session_code=context['session_code'],
                expected_round=context['round_index'],
                set_number=context['set_number'],
                started_at=ready_at,
                ends_at=parse_datetime(opened.snapshot.get('answering_deadline_at') or ''),
            )
            if not persisted:
                transaction.set_rollback(True)
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Assign-Runde hat sich geaendert.',
                    state_revision=opened.state_revision,
                    snapshot=opened.snapshot,
                )
            return opened

    @database_sync_to_async
    def open_assign_round(self, context, action):
        with transaction.atomic():
            if not self._assign_action_matches_context(action, context):
                snapshot = current_snapshot(
                    'assign',
                    self.room_code,
                    context['session_code'],
                )
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Aktion gehoert zu einer anderen Assign-Runde.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )
            runtime = current_set_runtime(
                self.room_code,
                context['session_code'],
                lock=True,
            )
            snapshot = current_snapshot(
                'assign',
                self.room_code,
                context['session_code'],
            )
            revealed_at = parse_datetime(snapshot.get('content_revealed_at') or '')
            ready_at = assign_reveal_ready_at(runtime, revealed_at)
            now = timezone.now()
            if not ready_at or now < ready_at:
                return QuestionPhaseDecision(
                    False,
                    'content_reveal_in_progress',
                    'Die Elemente und Ziele sind noch nicht vollstaendig enthuellt.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )
            decision = open_answering(
                game_key='assign',
                room_code=self.room_code,
                session_code=context['session_code'],
                action={**action, 'question_id': context['question_id']},
                answer_duration_seconds=context['time_limit'],
                at=now,
            )
            if not decision.accepted:
                return decision
            started_at = parse_datetime(
                decision.snapshot.get('answering_started_at') or ''
            )
            ends_at = parse_datetime(
                decision.snapshot.get('answering_deadline_at') or ''
            )
            opened = open_round_answering(
                room_code=self.room_code,
                hub_session_code=context['session_code'],
                expected_round=context['round_index'],
                set_number=context['set_number'],
                started_at=started_at,
                ends_at=ends_at,
            )
            if not opened:
                transaction.set_rollback(True)
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Assign-Runde hat sich geaendert.',
                    state_revision=decision.state_revision,
                    snapshot=decision.snapshot,
                )
            return decision

    @database_sync_to_async
    def finish_assign_round_phase(self, question_id):
        session_code = (
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync()
        )
        snapshot = current_snapshot('assign', self.room_code, session_code)
        if (
            snapshot.get('question_flow_mode')
            != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            or snapshot.get('question_phase')
            != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
        ):
            return snapshot
        return finish_question_flow(
            game_key='assign',
            room_code=self.room_code,
            session_code=session_code,
            question_id=question_id,
        )

    @database_sync_to_async
    def present_next_assign_round_phase(
        self,
        question_id,
        set_number,
        round_index,
        effective_time_limit,
    ):
        session_code = (
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync()
        )
        snapshot = current_snapshot('assign', self.room_code, session_code)
        if (
            snapshot.get('question_flow_mode')
            != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        ):
            return QuestionPhaseDecision(
                True,
                'accepted',
                '',
                state_revision=snapshot.get('state_revision'),
                snapshot=snapshot,
            )
        with transaction.atomic():
            finished = finish_question_flow(
                game_key='assign',
                room_code=self.room_code,
                session_code=session_code,
                question_id=question_id,
            )
            now = timezone.now()
            presented = present_question(
                game_key='assign',
                room_code=self.room_code,
                session_code=session_code,
                action={
                    'client_action_id': str(uuid.uuid4()),
                    'state_revision': finished['state_revision'],
                    'game_id': finished.get('game_id'),
                    'question_id': question_id,
                    'round_id': round_index,
                    'set_id': set_number,
                },
                answer_duration_seconds=effective_time_limit,
                at=now - timezone.timedelta(milliseconds=QUESTION_PRESENTATION_DELAY_MS),
            )
            if not presented.accepted:
                transaction.set_rollback(True)
                return presented
            revealed = reveal_question_content(
                game_key='assign',
                room_code=self.room_code,
                session_code=session_code,
                action={
                    'client_action_id': str(uuid.uuid4()),
                    'state_revision': presented.state_revision,
                    'game_id': presented.snapshot.get('game_id'),
                    'question_id': question_id,
                    'round_id': round_index,
                    'set_id': set_number,
                },
                at=now,
            )
            if not revealed.accepted:
                transaction.set_rollback(True)
                return revealed
            opened = open_answering(
                game_key='assign',
                room_code=self.room_code,
                session_code=session_code,
                action={
                    'client_action_id': str(uuid.uuid4()),
                    'state_revision': revealed.state_revision,
                    'game_id': revealed.snapshot.get('game_id'),
                    'question_id': question_id,
                    'round_id': round_index,
                    'set_id': set_number,
                },
                answer_duration_seconds=effective_time_limit,
                at=now,
            )
            if not opened.accepted:
                transaction.set_rollback(True)
                return opened
            persisted = open_round_answering(
                room_code=self.room_code,
                hub_session_code=session_code,
                expected_round=round_index,
                set_number=set_number,
                started_at=now,
                ends_at=parse_datetime(opened.snapshot.get('answering_deadline_at') or ''),
            )
            if not persisted:
                transaction.set_rollback(True)
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Assign-Runde hat sich geaendert.',
                    state_revision=opened.state_revision,
                    snapshot=opened.snapshot,
                )
            return opened

    @database_sync_to_async
    def get_current_set_number(self, quiz_id):
        return AssignSession.objects.filter(quiz_id=quiz_id).values_list(
            'current_question_number',
            flat=True,
        ).first() or 0

    @database_sync_to_async
    def get_current_round_index_by_room(self):
        return AssignSession.objects.filter(quiz__room_code=self.room_code).values_list(
            'current_round_index',
            flat=True,
        ).first() or 0

    @database_sync_to_async
    def get_participant_set_state(self, participant_name, hub_session):
        try:
            quiz = AssignQuiz.objects.select_related('session').get(room_code=self.room_code)
            participant = quiz.participants.get(
                name=participant_name,
                hub_session_code=hub_session,
            )
        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return {'authorized': False, 'set_number': 0, 'elimination_reason': None}

        set_number = quiz.session.current_question_number if hasattr(quiz, 'session') else 0
        reason = None
        if participant.eliminated_set_number == set_number:
            reason = participant.elimination_reason or 'incorrect_assignment'
        return {
            'authorized': True,
            'set_number': set_number,
            'elimination_reason': reason,
        }

    @database_sync_to_async
    def mark_participant_eliminated_for_set(
        self,
        quiz_id,
        question_id,
        set_number,
        participant_name,
        hub_session,
        reason,
    ):
        if not participant_name:
            return False
        with transaction.atomic():
            quiz = AssignQuiz.objects.select_for_update().filter(id=quiz_id).first()
            session, _ = AssignSession.objects.select_for_update().get_or_create(quiz_id=quiz_id)
            if (
                not quiz
                or quiz.current_question_id != question_id
                or session.current_question_number != set_number
            ):
                return False
            try:
                participant = AssignParticipant.objects.select_for_update().get(
                    quiz_id=quiz_id,
                    name=participant_name,
                    hub_session_code=hub_session,
                )
            except AssignParticipant.DoesNotExist:
                return False
            if participant.eliminated_set_number != set_number:
                participant.eliminated_set_number = set_number
                participant.elimination_reason = reason
                participant.save(update_fields=[
                    'eliminated_set_number',
                    'elimination_reason',
                    'updated_at',
                ])
            return True

    @database_sync_to_async
    def get_persisted_eliminated_keys(self):
        try:
            quiz = AssignQuiz.objects.select_related('session').get(room_code=self.room_code)
        except AssignQuiz.DoesNotExist:
            return set()
        set_number = quiz.session.current_question_number if hasattr(quiz, 'session') else 0
        return {
            f"{(hub_session or '').strip().lower()}::{name.strip().lower()}"
            for name, hub_session in quiz.participants.filter(
                eliminated_set_number=set_number,
            ).values_list('name', 'hub_session_code')
        }

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
    def set_question_active_db(self, quiz_id, is_active):
        from .models import AssignSession
        session, _ = AssignSession.objects.get_or_create(quiz_id=quiz_id)
        if session.is_question_active != is_active:
            session.is_question_active = is_active
            session.save(update_fields=['is_question_active'])

    @database_sync_to_async
    def is_question_active_db(self, quiz_id):
        from .models import AssignSession
        return AssignSession.objects.filter(
            quiz_id=quiz_id,
            is_question_active=True,
        ).exists()

    def _get_item_text(self, item):
        if isinstance(item, dict):
            return str(item.get('text', ''))
        return str(item)

    def get_remaining_right_items(self, randomized, solved_matches=None):
        position_to_original = randomized['position_to_original']
        used_original_indices = {
            int(original_idx)
            for original_idx in (solved_matches or {}).values()
        }
        return [
            item for item in randomized['right_items']
            if int(position_to_original.get(item['id'], -1)) not in used_original_indices
        ]

    def get_solved_pairs_snapshot(self, question, randomized=None, solved_matches=None):
        randomized = randomized or question.get_randomized_items(room_code=self.room_code)
        original_to_position = {
            int(original_idx): int(shuffled_pos)
            for shuffled_pos, original_idx in randomized['position_to_original'].items()
        }
        solved_matches = solved_matches or {}
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

    def build_round_payload(
        self,
        question,
        round_index: int,
        time_limit: int,
        set_number=None,
        *,
        solved_matches=None,
        starts_at=None,
        ends_at=None,
    ):
        randomized = question.get_randomized_items(room_code=self.room_code)
        left_items = randomized['left_items']
        total_rounds = len(question.correct_matches or {})
        current_left_item = left_items[round_index] if 0 <= round_index < total_rounds else None
        payload = {
            'id': question.id,
            'question_text': question.question_text,
            'time_limit': time_limit,
            'left_items': left_items,
            'right_items': self.get_remaining_right_items(randomized, solved_matches),
            'all_right_items': randomized['right_items'],
            'solved_pairs': self.get_solved_pairs_snapshot(
                question,
                randomized,
                solved_matches,
            ),
            'total_possible_points': question.get_total_possible_points(),
            'round_index': round_index,
            'total_rounds': total_rounds,
            'current_left_item': current_left_item,
        }
        if set_number is not None:
            payload['set_number'] = set_number
        if starts_at is not None:
            payload['starts_at'] = (
                starts_at.isoformat() if hasattr(starts_at, 'isoformat') else starts_at
            )
        if ends_at is not None:
            payload['ends_at'] = (
                ends_at.isoformat() if hasattr(ends_at, 'isoformat') else ends_at
            )
        payload['server_now'] = timezone.now().isoformat()
        return payload

    @database_sync_to_async
    def get_round_right_items(self, question, round_index=None):
        """Verbleibende rechte Items: alle Items minus die tatsächlich korrekt gematchten."""
        randomized = question.get_randomized_items(room_code=self.room_code)
        runtime = current_set_runtime(self.room_code)
        return self.get_remaining_right_items(
            randomized,
            runtime.solved_matches if runtime else {},
        )

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
        statuses = await self.get_round_status(
            await self.get_current_round_index_by_room(),
        )
        return len(statuses)

    async def get_relevant_active_channels(self):
        statuses = await self.get_round_status(
            await self.get_current_round_index_by_room(),
        )
        return {
            f"{status['participant_name']}::{status['participant_id']}"
            for status in statuses
        }

    async def maybe_auto_advance_if_all_logged(self, round_index: int):
        statuses = await self.get_round_status(round_index)
        if statuses and all(status['logged'] for status in statuses):
            await self.handle_admin_next_round({'expected_round': round_index})

    async def broadcast_round_log_status_for_round(self, round_index: int):
        statuses = await self.get_round_status(round_index)
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

    @database_sync_to_async
    def get_round_status(self, round_index):
        return round_status(
            self.room_code,
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync(),
            round_index,
        )

    async def evaluate_current_round(self, quiz, round_index: int, set_number=None):
        """Compatibility wrapper around the atomic persistent transition."""
        if set_number is None:
            set_number = await self.get_current_set_number(quiz.id)
        return await self.evaluate_and_advance_round(round_index, set_number)

    @database_sync_to_async
    def evaluate_and_advance_round(self, round_index, set_number):
        session_code = (
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync()
        )
        snapshot = current_snapshot('assign', self.room_code, session_code)
        return evaluate_and_advance(
            room_code=self.room_code,
            hub_session_code=session_code,
            expected_round=round_index,
            set_number=set_number,
            prepare_next_round=(
                snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            ),
        )

    @database_sync_to_async
    def persist_runtime_answers(self):
        runtime = current_set_runtime(
            self.room_code,
            self._authoritative_session_code()
            or self._resolve_hub_session_code_sync(),
        )
        return [answer.id for answer in persist_final_answers(runtime)]

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
        with transaction.atomic():
            return self._save_participant_answer(
                participant_name,
                hub_session,
                user_matches,
                time_taken,
                question_id,
            )

    def _save_participant_answer(self, participant_name, hub_session, user_matches, time_taken, question_id=None):
        """Konvertiert shuffled Positionen → Original-Indizes und speichert AssignAnswer."""
        try:
            quiz = AssignQuiz.objects.select_for_update().select_related(
                'current_question',
            ).get(room_code=self.room_code)
            session, _ = AssignSession.objects.select_for_update().get_or_create(quiz=quiz)
            participant = AssignParticipant.objects.select_for_update().get(
                quiz=quiz,
                name=participant_name,
                hub_session_code=hub_session,
            )
            if quiz.status != 'active':
                return None
            if participant.eliminated_set_number == session.current_question_number:
                return None

            if (
                not question_id
                or not quiz.current_question_id
                or str(question_id) != str(quiz.current_question_id)
            ):
                return None
            question = quiz.current_question

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

            received_at = timezone.now()
            server_time_taken = (
                max(0, (received_at - quiz.question_start_time).total_seconds())
                if quiz.question_start_time
                else None
            )
            try:
                answer = AssignAnswer.objects.create(
                    quiz=quiz,
                    participant=participant,
                    question=question,
                    user_matches=original_user_matches,
                    time_taken=server_time_taken,
                )
            except IntegrityError:
                return None
            is_tutorial_answer = is_unit_tutorial_question(
                'assign',
                self.room_code,
                hub_session,
                question.id,
            )
            if is_tutorial_answer and answer.points_earned:
                answer.points_earned = 0
                answer.save(update_fields=['points_earned', 'updated_at'])

            return {
                'points_earned': answer.points_earned,
                'is_tutorial_round': is_tutorial_answer,
                'correct_matches': answer.get_correct_matches_count(),
                'total_matches': answer.get_total_matches_count(),
                'accuracy': answer.get_accuracy_percentage(),
                'progress_history': self._build_progress_history(quiz, participant),
                'scoreboard_questions': build_question_scoreboard(quiz, None, participant.hub_session_code),
            }

        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return None

    def _build_progress_history(self, quiz, participant):
        return build_participant_progress_history(quiz, participant)

    @database_sync_to_async
    def get_participant_progress_history(self, participant_name, hub_session):
        try:
            quiz = AssignQuiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
            return self._build_progress_history(quiz, participant)
        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return []

    @database_sync_to_async
    def get_scoreboard_questions(self, hub_session):
        try:
            quiz = AssignQuiz.objects.get(room_code=self.room_code)
            return build_question_scoreboard(quiz, None, hub_session)
        except AssignQuiz.DoesNotExist:
            return []
