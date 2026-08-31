import json
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import (
    ClueAnswer,
    CluePendingInput,
    ClueQuestion,
    ClueRushGame,
    ClueRushParticipant,
    ClueRushSession,
)
from .runtime import (
    current_clue_runtime,
    finalize_clue_question,
    prepare_question_schedule,
    reconcile_clue_schedule,
    start_prepared_question_schedule,
    start_question_schedule,
)
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import (
    QuestionPhaseDecision,
    current_snapshot,
    finish_question_flow,
    open_answering,
    present_question,
    reset_question_flow,
)
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import GameRuntimeState, HubGameStep, HubSession
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
    get_scorebox_excluded_tutorial_question_ids,
    get_unit_tutorial_state,
    is_unit_tutorial_question,
    prepare_unit_tutorial_runtime,
    start_unit_tutorial_if_needed,
    validate_unit_tutorial_request,
)
try:
    from rapidfuzz import fuzz
except Exception:
    fuzz = None


def _question_number_for_game(quiz, question_id, hub_session_code=None):
    excluded_ids = get_scorebox_excluded_tutorial_question_ids(
        'clue_rush',
        quiz.room_code,
        hub_session_code,
    )
    selected_ids = list(
        quiz.selected_questions
        .filter(is_active=True)
        .exclude(id__in=excluded_ids)
        .order_by('id')
        .values_list('id', flat=True)
    )
    selected_id_set = set(selected_ids)
    ordered_ids = []
    for raw_id in quiz.question_order or []:
        try:
            normalized_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if normalized_id in selected_id_set and normalized_id not in ordered_ids:
            ordered_ids.append(normalized_id)
    ordered_ids.extend(
        question_id
        for question_id in selected_ids
        if question_id not in ordered_ids
    )
    try:
        return ordered_ids.index(int(question_id)) + 1
    except (TypeError, ValueError):
        return None


class ClueRushGameConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'clue_rush'
    authoritative_required_actions = frozenset({'participant_submit_answer'})

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
            'clue': event['clue'],
            'phase': event.get('phase', 'question_active'),
            'current_question_id': event.get('current_question_id'),
            'current_round_id': event.get('current_round_id'),
            'starts_at': event.get('starts_at'),
            'ends_at': event.get('ends_at'),
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
            elif message_type == 'admin_start_clues':
                await self.handle_admin_start_clues(text_data_json)
            elif message_type == 'admin_end_question':
                await self.handle_admin_end_question(text_data_json)
            elif message_type == 'admin_send_clue':
                await self.handle_admin_send_clue(text_data_json)
            elif message_type == 'admin_end_quiz':
                await self.handle_admin_end_quiz(text_data_json)
            elif message_type == 'admin_set_inactive':
                await self.handle_admin_set_inactive(text_data_json)
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
            elif message_type == 'participant_input_changed':
                await self.handle_participant_input_changed(text_data_json)
            elif message_type == 'participant_join':
                await self.handle_participant_join(text_data_json)
            elif message_type == 'tutorial_completed':
                await self.handle_tutorial_completed(text_data_json)
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
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'clue_rush',
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
            reset_runtime = quiz.status != 'active' or quiz.started_at is None
            show_tutorial = bool(data.get('show_tutorial', False))
            play_tutorial = bool(data.get('play_tutorial', False))
            hub_session_code = await self._get_hub_session_code_for_room()
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'clue_rush',
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
                'clue_rush',
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
                'clue_rush',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            await self.start_quiz_db(quiz.id, reset_runtime=reset_runtime)
            await database_sync_to_async(reset_question_flow)(
                game_key='clue_rush',
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
                    'message': 'ClueRushGame has started!'
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'clue_rush',
                'message': 'ClueRushGame has started!'
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

        if await self.guard_tutorial_before_first_unit(data, quiz.id):
            return

        await self.set_tutorial_active_db(quiz.id, False)
        hub_session = data.get('hub_session') or await self._get_hub_session_code_for_room()
        unit_tutorial = await self.start_unit_tutorial_if_needed(hub_session)
        is_tutorial_round = bool(unit_tutorial.get('is_tutorial_round'))
        if is_tutorial_round and str(unit_tutorial.get('tutorial_question_id') or '') != str(question.id):
            question = await self.get_question(unit_tutorial.get('tutorial_question_id'))
            if not question:
                return

        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit
        phase_action = {**data, 'question_id': question.id}
        decision = await self.present_clue_question(
            quiz.id,
            question.id,
            hub_session,
            phase_action,
            custom_time_limit,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question.id)
            return
        question_started_at = decision.snapshot.get('question_presented_at')
        question_number = (
            None
            if is_tutorial_round
            else await self.get_question_number(quiz.id, question.id, hub_session)
        )

        # Broadcast new question to all participants
        server_now = timezone.now().isoformat()
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': {
                    'id': question.id,
                    'question_number': question_number,
                    'question_text': question.question_text,
                    'time_limit': effective_time_limit,
                    'points': 0 if is_tutorial_round else question.points,
                    'is_tutorial_round': is_tutorial_round,
                    'question_started_at': question_started_at,
                    'starts_at': None,
                    'ends_at': None,
                    'server_now': server_now,
                },
                **self.question_lifecycle_fields(decision.snapshot),
            }
        )

        # Mirror to hub (Stage B): allow centralized listeners to react to question start
        await self.hub_mirror_event('question_started', {
            'room_code': self.room_code,
            'question': {
                'id': question.id,
                'question_number': question_number,
                'question_text': question.question_text,
                'time_limit': effective_time_limit,
                'points': 0 if is_tutorial_round else question.points,
                'is_tutorial_round': is_tutorial_round,
                'question_started_at': question_started_at,
                'starts_at': None,
                'ends_at': None,
                'server_now': server_now,
            }
        })

    async def handle_admin_start_clues(self, data):
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Es ist keine aktuelle Frage vorhanden.',
            }))
            return
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.open_clue_answering(
            quiz.id,
            quiz.current_question_id,
            hub_session,
            {**data, 'question_id': quiz.current_question_id},
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(
                decision,
                quiz.current_question_id,
            )
            return
        question = await self.get_question(quiz.current_question_id)
        question_number = await self.get_question_number(
            quiz.id,
            quiz.current_question_id,
            hub_session,
        )
        question_payload = {
            'id': question.id,
            'question_number': question_number,
            'question_text': question.question_text,
            'time_limit': question.time_limit,
            'points': question.points,
            'question_started_at': decision.snapshot.get('answering_started_at'),
            'starts_at': decision.snapshot.get('answering_started_at'),
            'ends_at': decision.snapshot.get('answering_deadline_at'),
            'server_now': decision.snapshot.get('server_now'),
        }
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_answering_opened',
                'question': question_payload,
                **self.question_lifecycle_fields(decision.snapshot),
            },
        )
        await self.reconcile_and_broadcast_clues()
        self._ensure_clue_reconciliation_task(question.id)

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

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = data.get('hub_session') or await self._get_hub_session_code_for_room()
            phase_snapshot = await database_sync_to_async(current_snapshot)(
                'clue_rush',
                self.room_code,
                hub_session,
            )
            if (
                phase_snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
                and phase_snapshot.get('question_phase')
                != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
            ):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Die Frage kann vor dem Hinweisstart nicht beendet werden.',
                }))
                return
            # Stop automatic clue sending if running
            try:
                if hasattr(self, 'auto_clue_task') and self.auto_clue_task and not self.auto_clue_task.done():
                    self.auto_clue_task.cancel()
            except Exception:
                pass
            ended_payload = await self.finalize_current_question_for_end(hub_session)
            if not ended_payload.get('newly_finalized'):
                return
            correct_payload = ended_payload.get('correct_answer')
            await database_sync_to_async(finish_question_flow)(
                game_key='clue_rush',
                room_code=self.room_code,
                session_code=hub_session,
                question_id=correct_payload.get('question_id'),
            )
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Question time is up!',
                    'correct_answer': correct_payload,
                    'answers': ended_payload.get('answers', []),
                    'is_tutorial_round': bool(ended_payload.get('is_tutorial_round')),
                }
            )

            # Mirror to hub (Stage B)
            await self.hub_mirror_event('question_ended', {
                'room_code': self.room_code,
                'message': 'Question time is up!',
                'correct_answer': correct_payload,
                'is_tutorial_round': bool(ended_payload.get('is_tutorial_round')),
            })

    async def handle_admin_send_clue(self, data):
        """Handle admin requesting to send the next clue for the current question."""
        # Avoid direct ORM attribute access in async context
        has_question = await self._has_current_question()
        if not has_question:
            return
        await self.reconcile_and_broadcast_clues()

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
            hub_session = (
                data.get('hub_session')
                or data.get('hub_session_code')
                or await self._get_hub_session_code_for_room()
            )
            await database_sync_to_async(reset_question_flow)(
                game_key='clue_rush',
                room_code=self.room_code,
                session_code=hub_session,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
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

    async def handle_admin_set_inactive(self, data):
        """Pause the quiz without clearing its current progress."""
        quiz = await self.get_quiz()
        if quiz and quiz.status == 'active':
            try:
                if hasattr(self, 'auto_clue_task') and self.auto_clue_task and not self.auto_clue_task.done():
                    self.auto_clue_task.cancel()
            except Exception:
                pass
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
        hub_session = data.get('hub_session')
        answer_text = data.get('answer')
        if (
            participant_name != self._authoritative_participant_name()
            or str(hub_session or '') != self._authoritative_session_code()
        ):
            await self.send_action_error('invalid_participant')
            return

        answer = await self.save_participant_answer(
            participant_name,
            hub_session,
            answer_text,
            data.get('question_id'),
        )
        if answer and answer.get('_error'):
            await self.send_action_error(answer['_error'])
            return
        if answer:
            # Send confirmation to participant
            progress_history = await self.get_participant_score_history(participant_name, hub_session)
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'is_correct': answer['is_correct'],
                'points_earned': answer['points_earned'],
                'is_tutorial_round': answer.get('is_tutorial_round', False),
                'is_close': answer.get('is_close', False),
                'progress_history': progress_history,
                'answer': {
                    'answer_text': answer.get('answer_text', answer_text),
                    'time_taken': answer.get('time_taken'),
                },
            }))

            # Broadcast to admin dashboard (live answers)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'answer_id': answer.get('answer_id'),
                        'participant_id': answer.get('participant_id'),
                        'participant_name': participant_name,
                        'question_id': answer.get('question_id'),
                        'answer_text': answer.get('answer_text', answer_text),
                        'is_correct': answer['is_correct'],
                        'is_manual_override': answer.get('is_manual_override', False),
                        'can_mark_correct': answer.get('can_mark_correct', False),
                        'points_earned': answer['points_earned'],
                        'is_tutorial_round': answer.get('is_tutorial_round', False),
                        'total_score': answer.get('total_score'),
                        'is_close': answer.get('is_close', False),
                        'time_taken': answer.get('time_taken'),
                        'submitted_at': answer.get('submitted_at'),
                        'submitted_clue_number': answer.get('submitted_clue_number'),
                    }
                }
            )

    async def handle_participant_input_changed(self, data):
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        if (
            participant_name != self._authoritative_participant_name()
            or str(hub_session or '') != self._authoritative_session_code()
        ):
            await self.send_action_error('invalid_participant')
            return
        answer_text = data.get('answer', '')
        await self.store_pending_input(
            participant_name,
            hub_session,
            answer_text,
            data.get('question_id'),
        )

    async def send_action_error(self, code):
        await self.send(text_data=json.dumps({
            'type': 'action_rejected',
            'code': code,
            'message': 'Die Antwort gehoert nicht mehr zur aktiven Frage.',
        }))
        await self._send_authoritative_snapshot()

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        self._authoritative_participant = str(participant_name or '').strip()
        self._authoritative_session = str(hub_session or '').strip()
        participant = await self.get_participant_by_name(participant_name, hub_session)
        
        if participant:
            await self.mark_participant_active(participant['id'])

            quiz = await self.get_quiz()
            progress_history = await self.get_participant_score_history(participant_name, hub_session)
            await self.send(text_data=json.dumps({
                'type': 'progress_history',
                'history': progress_history,
            }))
            if quiz and quiz.status == 'active':
                await self.send_state({
                    'participant_name': participant_name,
                    'hub_session': hub_session,
                })

            # The participant state may advance the shared context revision.
            # Notify the host only after that revision is authoritative.
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

            if quiz and quiz.status == 'active':
                return

    async def handle_tutorial_completed(self, data):
        participant_name = data.get('participant_name') or data.get('name')
        hub_session = data.get('hub_session') or data.get('hub_session_code')
        progress = await self.mark_tutorial_completed(participant_name, hub_session)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'tutorial_progress', **progress}
        )

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
        }))

    async def question_answering_opened(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_answering_opened',
            'question': event['question'],
            **self.question_lifecycle_fields(event),
        }))

    async def question_ended(self, event):
        """Send question ended message"""
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'message': event['message'],
            'correct_answer': event.get('correct_answer'),
            'answers': event.get('answers', []),
            'is_tutorial_round': event.get('is_tutorial_round', False),
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
        await self.send(text_data=json.dumps({
            'type': 'answer_corrected',
            'response': event.get('response'),
            'participant_id': event.get('participant_id'),
            'participant_name': event.get('participant_name'),
            'question_id': event.get('question_id'),
            'total_score': event.get('total_score'),
            'progress_history': event.get('progress_history', []),
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
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('clue_rush', self.room_code, None, quiz)
        except ClueRushGame.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            payload = get_tutorial_payload('clue_rush', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except ClueRushGame.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            return activate_tutorial_runtime('clue_rush', self.room_code, hub_session_code, quiz, show_tutorial)
        except ClueRushGame.DoesNotExist:
            return None

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('clue_rush', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('clue_rush', self.room_code, hub_session_code)

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('clue_rush', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('clue_rush', self.room_code, hub_session_code)

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
            if is_unit_tutorial_question('clue_rush', self.room_code, session_code, answer.question_id):
                return None

            # If already correct, no action
            if answer.is_correct:
                return {
                    'answer_id': answer.id,
                    'participant_id': participant.id,
                    'participant_name': participant.name,
                    'question_id': answer.question_id,
                    'points_earned': answer.points_earned,
                    'answer_text': answer.answer_text,
                    'time_taken': answer.time_taken,
                    'total_score': participant.total_score,
                    'submitted_clue_number': answer.submitted_clue_number,
                    'is_manual_override': answer.is_manually_corrected,
                    'can_mark_correct': False,
                }

            answer.is_correct = True
            answer.is_manually_corrected = True
            answer.points_earned = answer.calculate_points_from_submission_state()
            answer.save()
            participant.refresh_from_db(fields=['total_score'])

            return {
                'answer_id': answer.id,
                'participant_id': participant.id,
                'participant_name': participant.name,
                'question_id': answer.question_id,
                'points_earned': answer.points_earned,
                'answer_text': answer.answer_text,
                'time_taken': answer.time_taken,
                'total_score': participant.total_score,
                'submitted_clue_number': answer.submitted_clue_number,
                'is_manual_override': True,
                'can_mark_correct': False,
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
            if is_unit_tutorial_question('clue_rush', self.room_code, session_code, answer.question_id):
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

    def _ensure_clue_reconciliation_task(self, question_id):
        existing = getattr(self, 'auto_clue_task', None)
        if existing and not existing.done():
            if getattr(self, '_auto_clue_question_id', None) == question_id:
                return
            existing.cancel()
        self._auto_clue_question_id = question_id
        self.auto_clue_task = asyncio.create_task(self._auto_send_clues(question_id))

    async def _auto_send_clues(self, question_id=None):
        """Wake up broadcasts; persistent timestamps remain the source of truth."""
        while await self._has_current_question():
            result = await self.reconcile_and_broadcast_clues()
            if (
                result.get('deadline_reached')
                or (
                    question_id is not None
                    and str(result.get('question_id')) != str(question_id)
                )
            ):
                break
            try:
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                break

    async def reconcile_and_broadcast_clues(self):
        result = await self.reconcile_clues_db()
        await self.broadcast_reconciliation_result(result)
        return result

    async def broadcast_reconciliation_result(self, result, *, include_clues=True):
        for clue in (result.get('new_clues', []) if include_clues else []):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'clue_started',
                    'clue': clue,
                    'phase': 'question_active',
                    'current_question_id': clue.get('question_id'),
                    'current_round_id': clue.get('order'),
                    'starts_at': clue.get('starts_at'),
                    'ends_at': result.get('answer_deadline'),
                },
            )
        if (
            include_clues
            and result.get('new_clues')
            and not result['new_clues'][-1].get('has_next_clue')
        ):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'clue_sequence_completed',
                    'message': 'All clues have been sent.',
                },
            )
        if result.get('deadline_reached'):
            finalization = result.get('finalization') or {}
            ended = await self.get_finalized_question_payload(
                finalization,
                await self._get_hub_session_code_for_room(),
            )
            if ended.get('newly_finalized'):
                await database_sync_to_async(finish_question_flow)(
                    game_key='clue_rush',
                    room_code=self.room_code,
                    session_code=await self._get_hub_session_code_for_room(),
                    question_id=finalization.get('question_id'),
                )
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'question_ended',
                        'message': 'Question time is up!',
                        'correct_answer': ended.get('correct_answer'),
                        'answers': ended.get('answers', []),
                        'is_tutorial_round': bool(ended.get('is_tutorial_round')),
                    },
                )

    @database_sync_to_async
    def reconcile_clues_db(self):
        return reconcile_clue_schedule(self.room_code)

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

    async def send_state(self, data):
        reconciliation = await self.reconcile_clues_db()
        snapshot = await self.get_state_snapshot(
            data.get('participant_name') or self._authoritative_participant_name(),
            data.get('hub_session')
            or data.get('hub_session_code')
            or self._authoritative_session_code(),
        )
        await self.send(text_data=json.dumps(snapshot))
        await self.broadcast_reconciliation_result(
            reconciliation,
            include_clues=(
                str(snapshot.get('current_question_id') or '')
                == str(reconciliation.get('question_id') or '')
            ),
        )
        question_id = snapshot.get('current_question_id')
        if question_id and snapshot.get('phase') == 'question_active':
            self._ensure_clue_reconciliation_task(question_id)

    @database_sync_to_async
    def get_state_snapshot(self, participant_name, hub_session):
        now = timezone.now()
        try:
            quiz = ClueRushGame.objects.select_related(
                'current_question',
                'session',
            ).get(room_code=self.room_code)
            participant = quiz.participants.get(
                name=participant_name,
                hub_session_code=hub_session,
            )
        except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
            return {
                'type': 'clue_rush_state',
                'phase': 'unavailable',
                'server_now': now.isoformat(),
            }

        session = quiz.session
        question = quiz.current_question
        schedule = [
            entry
            for entry in (session.clue_schedule or [])
            if question and str(entry.get('question_id')) == str(question.id)
        ]
        answer = None
        pending = None
        if question:
            answer = ClueAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).first()
            if not answer:
                pending = CluePendingInput.objects.filter(
                    quiz=quiz,
                    participant=participant,
                    question=question,
                ).first()
        phase = 'question_active' if question and session.is_question_active else quiz.status
        question_payload = None
        if question:
            is_tutorial_round = is_unit_tutorial_question(
                'clue_rush',
                self.room_code,
                hub_session,
                question.id,
            )
            question_payload = {
                'id': question.id,
                'question_number': (
                    None
                    if is_tutorial_round
                    else _question_number_for_game(quiz, question.id, hub_session)
                ),
                'question_text': question.question_text,
                'time_limit': question.time_limit,
                'points': 0 if is_tutorial_round else question.points,
                'is_tutorial_round': is_tutorial_round,
                'question_started_at': (
                    quiz.question_start_time.isoformat()
                    if quiz.question_start_time
                    else None
                ),
                'starts_at': (
                    quiz.question_start_time.isoformat()
                    if quiz.question_start_time
                    else None
                ),
                'ends_at': (
                    session.answer_deadline.isoformat()
                    if session.answer_deadline
                    else None
                ),
                'server_now': now.isoformat(),
            }

        latest_reveal = None
        if not question and session.finalized_question_id:
            latest_answer = (
                ClueAnswer.objects
                .filter(
                    quiz=quiz,
                    participant=participant,
                    question_id=session.finalized_question_id,
                )
                .select_related('question', 'participant')
                .first()
            )
            if latest_answer:
                phase = 'revealed'
                latest_reveal = {
                    'correct_answer': {
                        'question_id': latest_answer.question_id,
                        'formatted_answer': latest_answer.question.answer.strip(),
                        'raw': latest_answer.question.answer,
                    },
                    'answer': self._serialize_answer_for_event(latest_answer),
                }

        serialized_answer = None
        if answer:
            serialized_answer = self._serialize_answer_for_event(answer)
        return {
            'type': 'clue_rush_state',
            'phase': phase,
            'game': {'id': quiz.id, 'status': quiz.status},
            'question': question_payload,
            'current_question_id': question.id if question else None,
            'current_round_id': session.current_clue_number if question else None,
            'starts_at': question_payload.get('starts_at') if question_payload else None,
            'ends_at': question_payload.get('ends_at') if question_payload else None,
            'server_now': now.isoformat(),
            'revealed_clues': [
                {**entry, 'server_now': now.isoformat()}
                for entry in schedule[:session.current_clue_number]
            ],
            'participant_state': {
                'answer': serialized_answer or (
                    {
                        'answer_text': pending.answer_text,
                        'time_taken': pending.time_taken,
                    }
                    if pending
                    else None
                ),
                'answer_locked': bool(answer),
                'timed_out': bool(answer and not answer.answer_text),
                'points': answer.points_earned if answer else None,
                'next_state': phase,
            },
            'revealed': phase == 'revealed',
            'correct_answer': latest_reveal.get('correct_answer') if latest_reveal else None,
            'revealed_answer': latest_reveal.get('answer') if latest_reveal else None,
            'progress_history': self._build_participant_score_history(quiz, participant),
        }

    @database_sync_to_async
    def start_quiz_db(self, quiz_id, reset_runtime=False):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            quiz.start_quiz(reset_runtime=reset_runtime)
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
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except ClueRushGame.DoesNotExist:
            pass

    @database_sync_to_async
    @transaction.atomic
    def present_clue_question(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
        duration_override=None,
        at=None,
    ):
        quiz = ClueRushGame.objects.select_for_update().get(pk=quiz_id)
        question = ClueQuestion.objects.get(pk=question_id)
        durations = [
            max(1, int(duration_override or clue.duration))
            for clue in question.clues.order_by('order', 'id')
        ]
        decision = present_question(
            game_key='clue_rush',
            room_code=self.room_code,
            session_code=hub_session_code,
            action={**action, 'question_id': question_id},
            answer_duration_seconds=sum(durations),
            at=at,
        )
        if decision.accepted and not decision.duplicate:
            prepared = prepare_question_schedule(
                quiz_id=quiz.id,
                question_id=question.id,
                duration_override=duration_override,
            )
            if not prepared.get('prepared'):
                transaction.set_rollback(True)
                return QuestionPhaseDecision(
                    False,
                    prepared.get('code', 'invalid_phase'),
                    'Die Clue-Rush-Frage konnte nicht vorbereitet werden.',
                    state_revision=decision.state_revision,
                    snapshot=decision.snapshot,
                )
        return decision

    @database_sync_to_async
    @transaction.atomic
    def open_clue_answering(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
        at=None,
    ):
        quiz = (
            ClueRushGame.objects
            .select_for_update()
            .select_related('current_question')
            .get(pk=quiz_id)
        )
        if quiz.current_question_id != question_id:
            return open_answering(
                game_key='clue_rush',
                room_code=self.room_code,
                session_code=hub_session_code,
                action={**action, 'question_id': question_id},
                at=at,
            )
        decision = open_answering(
            game_key='clue_rush',
            room_code=self.room_code,
            session_code=hub_session_code,
            action={**action, 'question_id': question_id},
            at=at or timezone.now(),
        )
        if decision.accepted and not decision.duplicate:
            started_at = parse_datetime(
                decision.snapshot.get('answering_started_at') or ''
            )
            deadline = parse_datetime(
                decision.snapshot.get('answering_deadline_at') or ''
            )
            if not started_at or not deadline:
                raise ValueError('Authoritative answering timestamps are missing.')
            started = start_prepared_question_schedule(
                quiz_id=quiz.id,
                question_id=question_id,
                starts_at=started_at,
            )
            if (
                not started.get('started')
                or started.get('answer_deadline') != deadline
            ):
                transaction.set_rollback(True)
                return QuestionPhaseDecision(
                    False,
                    started.get('code', 'invalid_phase'),
                    'Der Clue-Rush-Hinweisplan konnte nicht gestartet werden.',
                    state_revision=decision.state_revision,
                    snapshot=decision.snapshot,
                )
        return decision

    @database_sync_to_async
    def update_quiz_question(self, quiz, question, runtime_clue_duration=None):
        result = start_question_schedule(
            quiz_id=quiz.id,
            question_id=question.id,
            duration_override=runtime_clue_duration,
        )
        started_at = result.get('question_started_at')
        return started_at.isoformat() if started_at else None

    @database_sync_to_async
    def start_question_runtime(self, quiz_id, question_id, duration_override=None):
        result = start_question_schedule(
            quiz_id=quiz_id,
            question_id=question_id,
            duration_override=duration_override,
        )
        return {
            **result,
            'question_started_at': (
                result['question_started_at'].isoformat()
                if result.get('question_started_at')
                else None
            ),
            'answer_deadline': (
                result['answer_deadline'].isoformat()
                if result.get('answer_deadline')
                else None
            ),
        }

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
                    session.is_question_active = False
                    session.is_clue_active = False
                    session.question_end_time = None
                    session.clue_end_time = None
                    session.save()
            except Exception:
                pass
            quiz.save()
        except ClueRushGame.DoesNotExist:
            pass

    @database_sync_to_async
    def store_pending_input(
        self,
        participant_name,
        hub_session_code,
        answer_text,
        question_id,
    ):
        try:
            with transaction.atomic():
                quiz = (
                    ClueRushGame.objects
                    .select_for_update()
                    .select_related('current_question', 'current_clue')
                    .get(room_code=self.room_code)
                )
                session = ClueRushSession.objects.select_for_update().get(quiz=quiz)
                if (
                    quiz.status != 'active'
                    or not quiz.current_question_id
                    or str(quiz.current_question_id) != str(question_id)
                    or not session.is_question_active
                    or not session.answer_deadline
                    or timezone.now() >= session.answer_deadline
                ):
                    return None
                participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
                if ClueAnswer.objects.filter(quiz=quiz, participant=participant, question=quiz.current_question).exists():
                    CluePendingInput.objects.filter(quiz=quiz, participant=participant, question=quiz.current_question).delete()
                    return None

                submitted_clue_number, total_clues = self._get_current_clue_submission_state(quiz, quiz.current_question)
                pending, _ = CluePendingInput.objects.update_or_create(
                    quiz=quiz,
                    participant=participant,
                    question=quiz.current_question,
                    defaults={
                        'answer_text': (answer_text or '')[:200],
                        'submitted_clue_number': submitted_clue_number,
                        'total_clues_at_input': total_clues,
                        'time_taken': max(
                            0,
                            (timezone.now() - quiz.question_start_time).total_seconds(),
                        ) if quiz.question_start_time else None,
                    },
                )
                return pending.id
        except (ClueRushGame.DoesNotExist, ClueRushParticipant.DoesNotExist):
            return None

    @database_sync_to_async
    def finalize_current_question_for_end(
        self,
        hub_session_code=None,
        expected_question_id=None,
    ):
        finalization = finalize_clue_question(
            self.room_code,
            expected_question_id=expected_question_id,
            hub_session_code=hub_session_code,
            force=True,
        )
        return self._build_finalized_question_payload(
            finalization,
            hub_session_code,
        )

    @database_sync_to_async
    def get_finalized_question_payload(self, finalization, hub_session_code=None):
        return self._build_finalized_question_payload(
            finalization,
            hub_session_code,
        )

    def _build_finalized_question_payload(self, finalization, hub_session_code=None):
        question_id = (finalization or {}).get('question_id')
        if not question_id:
            return {
                'correct_answer': None,
                'answers': [],
                'newly_finalized': False,
            }
        try:
            quiz = ClueRushGame.objects.get(room_code=self.room_code)
            question = ClueQuestion.objects.get(pk=question_id)
        except (ClueRushGame.DoesNotExist, ClueQuestion.DoesNotExist):
            return {
                'correct_answer': None,
                'answers': [],
                'newly_finalized': False,
            }
        answers = ClueAnswer.objects.filter(
            quiz=quiz,
            question=question,
        ).select_related('participant', 'question')
        if hub_session_code is not None:
            answers = answers.filter(participant__hub_session_code=hub_session_code)
        answer_payloads = [
            self._serialize_answer_for_event(answer)
            for answer in answers.order_by('participant__name')
        ]
        return {
            'correct_answer': {
                'question_id': question.id,
                'formatted_answer': (question.answer or '').strip(),
                'raw': question.answer,
            },
            'answers': answer_payloads,
            'is_tutorial_round': bool(
                (finalization or {}).get('is_tutorial_round')
            ),
            'newly_finalized': bool(
                (finalization or {}).get('newly_finalized')
            ),
        }

    def _get_current_clue_submission_state(self, quiz, question):
        current_clue = quiz.current_clue if (
            quiz.current_clue_id and
            quiz.current_clue and
            quiz.current_clue.clue_question_id == question.id
        ) else None
        try:
            session = quiz.session
            current_clue_number = session.current_clue_number if session else None
        except Exception:
            current_clue_number = None

        submitted_clue_number = question.get_revealed_clue_count(
            current_clue=current_clue,
            current_clue_order=current_clue_number,
        )
        total_clues = question.clues.count()
        if submitted_clue_number <= 0 and total_clues > 0:
            submitted_clue_number = 1
        return submitted_clue_number, total_clues

    def _serialize_answer_for_event(self, answer):
        is_tutorial_round = is_unit_tutorial_question(
            'clue_rush',
            self.room_code,
            answer.participant.hub_session_code,
            answer.question_id,
        )
        answer.participant.refresh_from_db(fields=['total_score'])
        return {
            'answer_id': answer.id,
            'participant_id': answer.participant_id,
            'participant_name': answer.participant.name,
            'question_id': answer.question_id,
            'answer_text': answer.answer_text,
            'is_correct': answer.is_correct,
            'is_manual_override': answer.is_manually_corrected,
            'can_mark_correct': (not answer.is_correct) and not is_tutorial_round,
            'points_earned': answer.points_earned,
            'is_tutorial_round': is_tutorial_round,
            'time_taken': answer.time_taken,
            'total_score': answer.participant.total_score,
            'submitted_at': answer.submitted_at.isoformat() if answer.submitted_at else None,
            'submitted_clue_number': answer.submitted_clue_number,
            'progress_history': self._build_participant_score_history(answer.quiz, answer.participant),
        }

    def _build_participant_score_history(self, quiz, participant):
        tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids(
            'clue_rush',
            self.room_code,
            participant.hub_session_code,
        )
        answers = (
            ClueAnswer.objects
            .filter(quiz=quiz, participant=participant)
            .select_related('question')
            .order_by('submitted_at', 'id')
        )
        if quiz.started_at:
            answers = answers.filter(submitted_at__gte=quiz.started_at)

        history = []
        for idx, answer in enumerate(
            [answer for answer in answers if answer.question_id not in tutorial_question_ids],
            start=1,
        ):
            max_points = answer.total_clues_at_submission or answer.question.clues.count()
            achieved_points = 0
            if answer.is_correct and max_points > 0:
                achieved_points = max(0, min(answer.points_earned, max_points))
            history.append({
                'question_number': idx,
                'correct_answer': answer.question.answer,
                'achieved_points': achieved_points,
                'max_points': max_points,
            })
        return history

    def _coerce_positive_float(self, value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None


    @database_sync_to_async
    def advance_next_clue(self):
        """Return the next clue that is due in the persistent schedule."""
        result = reconcile_clue_schedule(self.room_code)
        clues = result.get('new_clues') or []
        return clues[0] if clues else None

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
    @transaction.atomic
    def save_participant_answer(
        self,
        participant_name,
        hub_session_code,
        answer_text,
        question_id,
    ):
        try:
            quiz = (
                ClueRushGame.objects
                .select_for_update()
                .select_related('current_question', 'session')
                .get(room_code=self.room_code)
            )
            session = ClueRushSession.objects.select_for_update().get(quiz=quiz)
            participant = quiz.participants.select_for_update().get(
                name=participant_name,
                hub_session_code=hub_session_code,
            )
            
            if (
                quiz.status != 'active'
                or not quiz.current_question
                or not session.is_question_active
            ):
                return {'_error': 'invalid_phase'}
            if str(quiz.current_question_id) != str(question_id):
                return {'_error': 'stale_action'}
            received_at = timezone.now()
            if not session.answer_deadline or received_at >= session.answer_deadline:
                return {'_error': 'deadline_expired'}
            
            # Check if answer already exists
            existing_answer = ClueAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question
            ).first()
            
            if existing_answer:
                return {'_error': 'already_submitted'}
            
            # Create new answer
            try:
                answer = ClueAnswer.objects.create(
                    quiz=quiz,
                    participant=participant,
                    question=quiz.current_question,
                    answer_text=answer_text,
                    time_taken=(
                        max(0, (received_at - quiz.question_start_time).total_seconds())
                        if quiz.question_start_time
                        else None
                    ),
                )
            except IntegrityError:
                return {'_error': 'already_submitted'}
            is_tutorial_answer = is_unit_tutorial_question(
                'clue_rush',
                self.room_code,
                hub_session_code,
                quiz.current_question_id,
            )
            if is_tutorial_answer and answer.points_earned:
                answer.points_earned = 0
                answer.save()
            participant.refresh_from_db(fields=['total_score'])
            CluePendingInput.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
            ).delete()
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
                'answer_id': answer.id,
                'participant_id': participant.id,
                'participant_name': participant.name,
                'question_id': answer.question_id,
                'answer_text': answer.answer_text,
                'is_correct': answer.is_correct,
                'is_manual_override': answer.is_manually_corrected,
                'points_earned': answer.points_earned,
                'is_tutorial_round': is_tutorial_answer,
                'is_close': is_close,
                'time_taken': answer.time_taken,
                'total_score': participant.total_score,
                'submitted_at': answer.submitted_at.isoformat() if answer.submitted_at else None,
                'submitted_clue_number': answer.submitted_clue_number,
                'can_mark_correct': (not answer.is_correct) and not is_tutorial_answer,
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
        revealed_answer = None
        correct_answer = None

        current_question = quiz.current_question
        if current_question:
            try:
                session = quiz.session
            except ClueRushSession.DoesNotExist:
                session = None
            schedule = [
                entry
                for entry in ((session.clue_schedule or []) if session else [])
                if str(entry.get('question_id')) == str(current_question.id)
            ]
            server_now = timezone.now().isoformat()
            is_tutorial_round = is_unit_tutorial_question('clue_rush', self.room_code, hub_session, current_question.id)
            question_payload = {
                'id': current_question.id,
                'question_number': (
                    None
                    if is_tutorial_round
                    else _question_number_for_game(
                        quiz,
                        current_question.id,
                        hub_session,
                    )
                ),
                'question_text': current_question.question_text,
                'time_limit': current_question.time_limit,
                'points': 0 if is_tutorial_round else current_question.points,
                'is_tutorial_round': is_tutorial_round,
                'question_started_at': quiz.question_start_time.isoformat() if quiz.question_start_time else None,
                'starts_at': quiz.question_start_time.isoformat() if quiz.question_start_time else None,
                'ends_at': session.answer_deadline.isoformat() if session and session.answer_deadline else None,
                'server_now': server_now,
            }
            revealed_clues = [
                {**entry, 'server_now': server_now}
                for entry in schedule[:session.current_clue_number]
            ] if session else []

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
                        'is_manual_override': answer.is_manually_corrected,
                        'points_earned': answer.points_earned,
                        'time_taken': answer.time_taken,
                        'submitted_at': answer.submitted_at.isoformat() if answer.submitted_at else None,
                    }
            except ClueRushParticipant.DoesNotExist:
                participant_answer = None
        else:
            try:
                participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)
                latest_answer_qs = ClueAnswer.objects.filter(
                    quiz=quiz,
                    participant=participant,
                ).select_related('question', 'participant').order_by('-submitted_at', '-id')
                if quiz.started_at:
                    latest_answer_qs = latest_answer_qs.filter(submitted_at__gte=quiz.started_at)
                latest_answer = latest_answer_qs.first()
                if latest_answer:
                    correct_answer = {
                        'question_id': latest_answer.question_id,
                        'formatted_answer': (latest_answer.question.answer or '').strip(),
                        'raw': latest_answer.question.answer,
                    }
                    revealed_answer = self._serialize_answer_for_event(latest_answer)
            except ClueRushParticipant.DoesNotExist:
                revealed_answer = None

        return {
            'question': question_payload,
            'revealed_clues': revealed_clues,
            'participant_answer': participant_answer,
            'revealed_answer': revealed_answer,
            'correct_answer': correct_answer,
        }

    @database_sync_to_async
    def get_question_number(self, quiz_id, question_id, hub_session_code=None):
        try:
            quiz = ClueRushGame.objects.get(id=quiz_id)
        except ClueRushGame.DoesNotExist:
            return None
        return _question_number_for_game(quiz, question_id, hub_session_code)

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
        tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids(
            'clue_rush',
            self.room_code,
            hub_session_code,
        )

        history = []
        for idx, answer in enumerate(
            [answer for answer in answers if answer.question_id not in tutorial_question_ids],
            start=1,
        ):
            max_points = answer.total_clues_at_submission or answer.question.clues.count()
            achieved_points = 0
            if answer.is_correct and max_points > 0:
                achieved_points = answer.points_earned
                if achieved_points > max_points:
                    achieved_points = answer.points_earned - answer.question.points
                achieved_points = max(0, min(achieved_points, max_points))

            history.append({
                'question_number': idx,
                'correct_answer': answer.question.answer,
                'achieved_points': achieved_points,
                'max_points': max_points,
            })
        return history
