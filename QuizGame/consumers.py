import json
import uuid
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Quiz, QuizParticipant, QuizQuestion, QuizAnswer, QuizSession
from .presentation import (
    QUIZ_ANSWER_REVEAL_ANIMATION_MS,
    QUIZ_ANSWER_REVEAL_PAUSE_MS,
    QUIZ_ANSWER_REVEAL_STAGGER_MS,
    multiple_choice_answering_starts_at,
    question_typewriter_duration_ms,
    quiz_answers_open_automatically,
)
from admin_dashboard.models import DashboardSettings
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import (
    QuestionPhaseDecision,
    current_snapshot,
    finish_question_flow,
    get_runtime_state,
    open_answering,
    present_question,
    reset_question_flow,
    reveal_question_content,
)
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
from games_hub.models import GameRuntimeState, HubGameStep, HubSession
from games_hub.tutorial_runtime import (
    activate_tutorial_runtime,
    deactivate_tutorial_runtime,
    force_close_tutorial_runtime,
    get_tutorial_payload,
    get_tutorial_progress,
    get_tutorial_start_warning,
    mark_tutorial_completed,
)
from games_hub.unit_tutorial_runtime import (
    finish_current_unit_tutorial,
    get_scorebox_excluded_tutorial_question_ids,
    is_unit_tutorial_question,
    prepare_unit_tutorial_runtime,
    start_unit_tutorial_if_needed,
    validate_unit_tutorial_request,
)


class QuizConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'quiz'
    authoritative_required_actions = frozenset({'participant_submit_answer'})

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'quiz_{self.room_code}'
        self.participant_name = None
        self.participant_hub_session = None

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
            elif message_type == 'admin_prepare_question':
                await self.handle_admin_prepare_question(text_data_json)
            elif message_type == 'admin_clear_prepared_question':
                await self.handle_admin_clear_prepared_question(text_data_json)
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
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
            elif message_type == 'participant_update_pending_answer':
                await self.handle_participant_update_pending_answer(text_data_json)
            elif message_type == 'participant_join':
                await self.handle_participant_join(text_data_json)
            elif message_type == 'admin_show_leaderboard':
                await self.handle_admin_show_leaderboard()
            elif message_type == 'admin_hide_leaderboard':
                await self.handle_admin_hide_leaderboard()
            elif message_type == 'ping':
                await self.handle_ping()
            else:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f'Unknown action: {message_type or "missing type"}'
                }))
                
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
            show_tutorial = bool(data.get('show_tutorial', False))
            play_tutorial = bool(data.get('play_tutorial', False))
            hub_session_code = await self._get_hub_session_code_for_room()
            unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
                'quiz',
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
            await database_sync_to_async(prepare_unit_tutorial_runtime)(
                'quiz',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
            await self.start_quiz_db(quiz.id, hub_session_code)
            await database_sync_to_async(reset_question_flow)(
                game_key='quiz',
                room_code=self.room_code,
                session_code=hub_session_code,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            tutorial_payload = await self.activate_tutorial_runtime(
                quiz.id,
                hub_session_code,
                show_tutorial,
            )
            total_questions = await self.get_total_question_count(
                quiz.id,
                hub_session_code,
            )

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'Quiz has started!',
                    'total_questions': total_questions,
                }
            )
            await self.hub_mirror_event('quiz_started', {
                'room_code': self.room_code,
                'game_key': 'quiz',
                'message': 'Quiz has started!',
                'total_questions': total_questions,
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
        progress = await self.mark_tutorial_completed(participant_name, hub_session)

        quiz = await self.get_quiz()
        if not quiz:
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'tutorial_progress',
                **progress,
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
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Quiz not found.',
                'question_id': question_id,
            }))
            return

        if not question_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Frage konnte nicht gestartet werden: Frage-ID fehlt.'
            }))
            return

        if quiz.status != 'active':
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Start the quiz before sending questions.',
                'question_id': question_id,
            }))
            return
            
        question = await self.get_question(question_id)
        if not question:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Frage konnte nicht gestartet werden: Frage wurde nicht gefunden.',
                'question_id': question_id,
            }))
            return

        # If quiz has a predefined set, enforce membership
        try:
            has_selected = await self.quiz_has_selected_questions(quiz.id)
            if has_selected:
                allowed = await self.is_question_in_selected(quiz.id, question.id)
                if not allowed:
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'This question is not part of the selected set for this quiz.',
                        'question_id': question_id,
                    }))
                    return
        except Exception:
            pass

        # Determine the effective time limit for this send (do NOT persist on the question)
        effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit

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
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Frage konnte nicht gestartet werden: Tutorialfrage wurde nicht gefunden.',
                    'question_id': question_id,
                }))
                return
            effective_time_limit = custom_time_limit if custom_time_limit is not None else question.time_limit

        # Present the prompt without opening the answer window.
        await self.set_tutorial_active_db(quiz.id, False)
        phase_action = dict(data)
        phase_action['question_id'] = question.id
        decision = await self.present_quiz_question(
            quiz.id,
            question.id,
            hub_session,
            phase_action,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question.id)
            return
        total_questions = await self.get_total_question_count(quiz.id, hub_session)
        
        # Get question options
        options = await self.get_question_options(question)
        short_answer_fields = await self.get_short_answer_fields(question)
        question_payload = await self.get_current_question_data(hub_session)
        if question_payload:
            question_payload.update({
                'options': options,
                'short_answer_fields': short_answer_fields,
                'time_limit': effective_time_limit,
                'points': 0 if is_tutorial_round else question.get_effective_max_points(),
                'is_tutorial_round': is_tutorial_round,
                'total_questions': total_questions,
            })
        else:
            question_payload = {
                'id': question.id,
                'question_text': question.question_text,
                'question_type': question.get_effective_question_type(),
                'options': options,
                'short_answer_fields': short_answer_fields,
                'time_limit': effective_time_limit,
                'points': 0 if is_tutorial_round else question.get_effective_max_points(),
                'is_tutorial_round': is_tutorial_round,
                'total_questions': total_questions,
            }
        question_payload.update(self.question_lifecycle_fields(decision.snapshot))

        # Broadcast new question to all participants
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'question': question_payload,
                **self.question_lifecycle_fields(decision.snapshot),
            }
        )

        # Mirror to hub (Stage B): allow centralized listeners to react to question start
        await self.hub_mirror_event('question_started', {
            'room_code': self.room_code,
            'question': question_payload,
        })

    async def handle_admin_prepare_question(self, data):
        question_id = data.get('question_id')
        quiz = await self.get_quiz()
        if not quiz or quiz.status != 'active':
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Start the quiz before selecting questions.',
                'question_id': question_id,
            }))
            return
        question = await self.get_question(question_id)
        if not question:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Frage konnte nicht ausgewaehlt werden.',
                'question_id': question_id,
            }))
            return
        if await self.quiz_has_selected_questions(quiz.id):
            if not await self.is_question_in_selected(quiz.id, question.id):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'This question is not part of the selected set for this quiz.',
                    'question_id': question_id,
                }))
                return

        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.prepare_quiz_question(question.id, hub_session, data)
        if not decision.get('accepted'):
            await self.send(text_data=json.dumps({
                'type': 'action_rejected',
                'code': decision.get('code'),
                'message': decision.get('message'),
                'question_id': question.id,
                'snapshot': decision.get('snapshot'),
            }))
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_prepared',
                'question_id': question.id,
                **self.question_lifecycle_fields(decision.get('snapshot')),
            },
        )

    async def handle_admin_clear_prepared_question(self, data):
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.clear_prepared_quiz_question(hub_session, data)
        if not decision.get('accepted'):
            await self.send(text_data=json.dumps({
                'type': 'action_rejected',
                'code': decision.get('code'),
                'message': decision.get('message'),
                'snapshot': decision.get('snapshot'),
            }))
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_preparation_cleared',
                **self.question_lifecycle_fields(decision.get('snapshot')),
            },
        )

    async def handle_admin_reveal_question_content(self, data):
        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Es ist keine aktuelle Frage vorhanden.',
            }))
            return
        question = await self.get_question(quiz.current_question_id)
        if question and question.get_effective_question_type() == 'short_answer':
            await self.send(text_data=json.dumps({
                'type': 'action_rejected',
                'code': 'invalid_phase',
                'message': 'Freitextfragen besitzen keine Antwortanzeige.',
                'question_id': quiz.current_question_id,
            }))
            return
        hub_session = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        decision = await self.reveal_quiz_question_content(
            quiz.id,
            quiz.current_question_id,
            hub_session,
            data,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, quiz.current_question_id)
            return
        question_payload = await self.get_current_question_data(hub_session)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_content_revealed',
                'question': question_payload,
                **self.question_lifecycle_fields(decision.snapshot),
                **self.answer_reveal_fields(),
            },
        )

    async def handle_admin_open_answering(self, data):
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
        question_payload = await self.get_current_question_data(hub_session)
        if not question_payload:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Die aktuelle Frage konnte nicht geladen werden.',
            }))
            return
        if quiz_answers_open_automatically(question_payload.get('question_type')):
            await self.send(text_data=json.dumps({
                'type': 'action_rejected',
                'code': 'invalid_phase',
                'message': 'Die Antwortphase wird nach dem Antwort-Reveal automatisch geoeffnet.',
                'question_id': quiz.current_question_id,
            }))
            return
        duration = int(question_payload['time_limit'])
        decision = await self.open_quiz_answering(
            quiz.id,
            quiz.current_question_id,
            hub_session,
            data,
            duration,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, quiz.current_question_id)
            return
        question_payload = await self.get_current_question_data(hub_session)
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_answering_opened',
                'question': question_payload,
                **self.question_lifecycle_fields(decision.snapshot),
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
                'question_phase',
                'question_presented_at',
                'question_visible_at',
                'question_presentation_duration_ms',
                'question_reveal_ms_per_character',
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
    def answer_reveal_fields():
        return {
            'answer_reveal_stagger_ms': QUIZ_ANSWER_REVEAL_STAGGER_MS,
            'answer_reveal_animation_ms': QUIZ_ANSWER_REVEAL_ANIMATION_MS,
            'answer_reveal_pause_ms': QUIZ_ANSWER_REVEAL_PAUSE_MS,
        }

    async def handle_admin_end_question(self, data):
        """Handle admin ending current question"""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
            phase_snapshot = await database_sync_to_async(current_snapshot)(
                'quiz', self.room_code, hub_session,
            )
            if (
                phase_snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
                and phase_snapshot.get('question_phase')
                != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
            ):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Die Frage kann vor der Freigabe nicht beendet werden.',
                }))
                return
            ended_question_id = quiz.current_question_id
            finalization = await self.finalize_pending_answers_and_close_question(
                quiz.id,
                hub_session,
            )
            correct_payload = finalization['correct_answer']
            answer_results = finalization['answer_results']
            auto_finalized_answers = finalization['auto_finalized_answers']
            is_tutorial_round = finalization['is_tutorial_round']
            if ended_question_id:
                await database_sync_to_async(finish_question_flow)(
                    game_key='quiz',
                    room_code=self.room_code,
                    session_code=hub_session,
                    question_id=ended_question_id,
                )
            await self.finish_current_unit_tutorial(hub_session)
            
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Question time is up!',
                    'correct_answer': correct_payload,
                    'answer_results': answer_results,
                    'auto_finalized_answers': auto_finalized_answers,
                    'is_tutorial_round': is_tutorial_round,
                }
            )

            # Mirror to hub (Stage B)
            await self.hub_mirror_event('question_ended', {
                'room_code': self.room_code,
                'message': 'Question time is up!',
                'correct_answer': correct_payload,
                'answer_results': answer_results,
                'auto_finalized_answers': auto_finalized_answers,
                'is_tutorial_round': is_tutorial_round,
            })

    async def handle_admin_end_quiz(self, data):
        """Handle admin ending the quiz"""
        quiz = await self.get_quiz()
        if quiz:
            hub_session = (
                data.get('hub_session')
                or data.get('hub_session_code')
                or await self._get_hub_session_code_for_room()
            )
            await self.end_quiz_db(quiz.id)
            await database_sync_to_async(reset_question_flow)(
                game_key='quiz',
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
        question_id = data.get('question_id')

        # Save the answer
        answer = await self.save_participant_answer(
            participant_name,
            hub_session,
            answer_text,
            time_taken,
            question_id=question_id,
        )

        if answer:
            # Send confirmation to participant
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'question_id': answer['question_id'],
                'evaluation_pending': True,
                'is_tutorial_round': answer['is_tutorial_round'],
                'display_answer': answer['display_answer'],
                'time_taken': answer['time_taken'],
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
                        'field_results': answer['field_results'],
                        'points_earned': answer['points_earned'],
                        'is_tutorial_round': answer['is_tutorial_round'],
                        'time_taken': answer['time_taken'],
                        'submitted_at': answer['submitted_at'],
                    }
                }
            )
        else:
            await self.send(text_data=json.dumps({
                'type': 'answer_rejected',
                'message': 'Answer was not accepted.',
                'question_id': question_id,
            }))

    async def handle_participant_update_pending_answer(self, data):
        """Persist the latest unconfirmed selection for authoritative finalization."""
        await self.save_pending_answer(
            data.get('participant_name'),
            data.get('hub_session') or None,
            data.get('answer'),
            data.get('question_id'),
        )

    async def handle_participant_join(self, data):
        """Handle new participant joining"""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session') or None  # normalize '' → None
        participant = await self.get_participant_by_name(participant_name, hub_session)
        
        if participant:
            self.participant_name = participant_name
            self.participant_hub_session = hub_session
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
                total_questions = await self.get_total_question_count(
                    quiz.id,
                    hub_session,
                )
                await self.send(text_data=json.dumps({
                    'type': 'quiz_started',
                    'message': 'Quiz is already in progress',
                    'total_questions': total_questions,
                }))
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
                # If there is an active question, send it so the participant doesn't miss it
                current_question_data = await self.get_current_question_data(hub_session)
                if current_question_data:
                    await self.send(text_data=json.dumps({
                        'type': 'question_started',
                        'question': current_question_data
                    }))
                    existing_answer = await self.get_current_participant_answer(
                        participant['id'],
                        current_question_data['id'],
                    )
                    if existing_answer:
                        await self.send(text_data=json.dumps({
                            'type': 'answer_submitted',
                            'message': 'Answer already submitted',
                            'question_id': existing_answer['question_id'],
                            'evaluation_pending': True,
                            'is_tutorial_round': existing_answer['is_tutorial_round'],
                            'display_answer': existing_answer['display_answer'],
                            'time_taken': existing_answer['time_taken'],
                        }))
                else:
                    runtime_snapshot = await database_sync_to_async(current_snapshot)(
                        'quiz', self.room_code, hub_session,
                    )
                    if runtime_snapshot.get('question_shell_prepared'):
                        await self.send(text_data=json.dumps({
                            'type': 'question_prepared',
                            'question_id': runtime_snapshot.get('prepared_question_id'),
                            **self.question_lifecycle_fields(runtime_snapshot),
                        }))
                        return
                    last_result = await self.get_last_question_result()
                    if last_result:
                        await self.send(text_data=json.dumps({
                            'type': 'question_ended',
                            'message': 'Question time is up!',
                            **last_result,
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
            'message': event['message'],
            'total_questions': event.get('total_questions'),
        }))

    async def question_started(self, event):
        """Send new question to client"""
        question = dict(event['question'])
        if (
            self.participant_name
            and event.get('question_phase')
            == GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
        ):
            question['options'] = []
            question['short_answer_fields'] = []
        await self.send(text_data=json.dumps({
            'type': 'question_started',
            'question': question,
            **self.question_lifecycle_fields(event),
        }))

    async def question_prepared(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_prepared',
            'question_id': event.get('question_id'),
            **self.question_lifecycle_fields(event),
        }))

    async def question_preparation_cleared(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_preparation_cleared',
            **self.question_lifecycle_fields(event),
        }))

    async def question_content_revealed(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_content_revealed',
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
            'answer_results': event.get('answer_results', []),
            'auto_finalized_answers': event.get('auto_finalized_answers', []),
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
        if self.participant_name:
            return
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
        if self.participant_name and not event.get('visible_to_participants', False):
            return
        await self.send(text_data=json.dumps({
            'type': 'answer_corrected',
            'success': True,
            'answer_id': event.get('answer_id'),
            'participant_id': event.get('participant_id'),
            'participant_name': event.get('participant_name'),
            'question_id': event.get('question_id'),
            'answer_text': event.get('answer_text'),
            'is_correct': event.get('is_correct', True),
            'is_manual_override': event.get('is_manual_override', True),
            'can_mark_correct': event.get('can_mark_correct', False),
            'question_type': event.get('question_type'),
            'field_results': event.get('field_results', []),
            'points_earned': event.get('points_earned'),
            'total_score': event.get('total_score'),
            'time_taken': event.get('time_taken'),
            'submitted_at': event.get('submitted_at'),
            'visible_to_participants': event.get('visible_to_participants', False),
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
            'completed': event['completed'],
            'total': event['total'],
            'all_done': event['all_done'],
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

    # Database operations
    @database_sync_to_async
    def get_current_question_data(self, hub_session_code=None):
        """Return the current question in its authoritative presentation phase."""
        try:
            quiz = Quiz.objects.select_related('current_question').get(room_code=self.room_code)
            session = getattr(quiz, 'session', None)
            q = quiz.current_question
            if not q or quiz.status != 'active' or not session:
                return None
            snapshot = current_snapshot('quiz', self.room_code, hub_session_code)
            manual = (
                snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            if manual and not snapshot.get('question_phase'):
                return None
            if not manual and not session.is_question_active:
                return None
            if q.question_type == 'multiple_choice':
                options = [{'key': k, 'text': t} for k, t in q.get_options()]
            elif q.question_type == 'true_false':
                options = [{'key': 'True', 'text': 'True'}, {'key': 'False', 'text': 'False'}]
            else:
                options = []
            stored_question = snapshot.get('question') or {}
            effective_time_limit = int(stored_question.get('time_limit') or q.time_limit)
            return {
                'id': q.id,
                'question_text': q.question_text,
                'question_type': q.get_effective_question_type(),
                'options': options,
                'short_answer_fields': q.get_public_short_answer_fields(),
                'time_limit': effective_time_limit,
                'points': q.get_effective_max_points(),
                'starts_at': (
                    snapshot.get('answering_started_at')
                    if manual
                    else (quiz.question_start_time.isoformat() if quiz.question_start_time else None)
                ),
                'ends_at': (
                    snapshot.get('answering_deadline_at')
                    if manual
                    else (session.question_end_time.isoformat() if session.question_end_time else None)
                ),
                'server_now': snapshot.get('server_now') or timezone.now().isoformat(),
                'remaining_seconds': (
                    snapshot.get('remaining_answer_time')
                    if manual
                    else (
                        max(0, (session.question_end_time - timezone.now()).total_seconds())
                        if session.question_end_time else None
                    )
                ),
                'total_questions': self._total_question_count(quiz, hub_session_code),
                **self.answer_reveal_fields(),
                **self.question_lifecycle_fields(snapshot),
            }
        except Quiz.DoesNotExist:
            return None

    @staticmethod
    def _total_question_count(quiz, hub_session_code=None):
        excluded_ids = get_scorebox_excluded_tutorial_question_ids(
            'quiz',
            quiz.room_code,
            hub_session_code,
        )
        questions = quiz.selected_questions.all()
        if excluded_ids:
            questions = questions.exclude(id__in=excluded_ids)
        return questions.count()

    @database_sync_to_async
    def get_total_question_count(self, quiz_id, hub_session_code=None):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
        except Quiz.DoesNotExist:
            return 0
        return self._total_question_count(quiz, hub_session_code)

    @database_sync_to_async
    def get_current_participant_answer(self, participant_id, question_id):
        try:
            quiz = Quiz.objects.get(room_code=self.room_code)
            answer = QuizAnswer.objects.select_related('question', 'participant').get(
                quiz=quiz,
                participant_id=participant_id,
                question_id=question_id,
            )
            if quiz.question_start_time and answer.submitted_at < quiz.question_start_time:
                return None

            display_answer = answer.answer_text
            if answer.question.get_effective_question_type() == 'short_answer':
                display_answer = answer.question.format_short_answer_submission(answer.answer_text)
            elif answer.question.question_type == 'multiple_choice':
                display_answer = dict(answer.question.get_options()).get(
                    answer.answer_text.upper(),
                    answer.answer_text,
                )
            elif answer.question.question_type == 'true_false':
                display_answer = 'Stimmt' if answer.answer_text == 'True' else 'Stimmt nicht'

            return {
                'question_id': answer.question_id,
                'display_answer': display_answer,
                'time_taken': answer.time_taken,
                'is_tutorial_round': is_unit_tutorial_question(
                    'quiz',
                    self.room_code,
                    answer.participant.hub_session_code,
                    answer.question_id,
                ),
            }
        except (Quiz.DoesNotExist, QuizAnswer.DoesNotExist):
            return None

    @database_sync_to_async
    def get_last_question_result(self):
        try:
            quiz = Quiz.objects.select_related('session').get(room_code=self.room_code)
            if quiz.status != 'active' or quiz.current_question_id:
                return None
            result = dict(quiz.session.last_question_result or {})
            return result or None
        except (Quiz.DoesNotExist, QuizSession.DoesNotExist):
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
    def reset_tutorial_completed(self, quiz_id, hub_session_code=None):
        participants = QuizParticipant.objects.filter(quiz_id=quiz_id)
        if hub_session_code is not None:
            participants = participants.filter(hub_session_code=hub_session_code)
        participants.update(tutorial_completed=False)

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        progress = mark_tutorial_completed('quiz', self.room_code, hub_session_code, participant_name)
        try:
            quiz = Quiz.objects.get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
            participant.tutorial_completed = True
            participant.save(update_fields=['tutorial_completed'])
        except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
            pass
        return progress

    @database_sync_to_async
    def get_tutorial_progress(self, quiz_id, hub_session_code):
        return get_tutorial_progress('quiz', self.room_code, hub_session_code)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('quiz', self.room_code, hub_session_code)

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            if not active:
                deactivate_tutorial_runtime('quiz', self.room_code, self._get_hub_session_code_for_room_sync(), quiz)
            else:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
        except Quiz.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            payload = get_tutorial_payload('quiz', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except Quiz.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            return activate_tutorial_runtime(
                'quiz',
                self.room_code,
                hub_session_code,
                quiz,
                show_tutorial,
            )
        except Quiz.DoesNotExist:
            return None

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('quiz', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('quiz', self.room_code, hub_session_code)

    @database_sync_to_async
    def start_quiz_db(self, quiz_id, hub_session_code=None):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            quiz.start_quiz(session_code=hub_session_code)
        except Quiz.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = Quiz.objects.get(id=quiz_id)
            quiz.end_quiz('completed')
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
    @transaction.atomic
    def present_quiz_question(self, quiz_id, question_id, hub_session_code, action):
        quiz = Quiz.objects.select_for_update().get(id=quiz_id)
        question = QuizQuestion.objects.get(id=question_id)
        milliseconds_per_character = DashboardSettings.question_reveal_speed()
        decision = present_question(
            game_key='quiz',
            room_code=self.room_code,
            session_code=hub_session_code,
            action=action,
            question_presentation_duration_ms=question_typewriter_duration_ms(
                question.question_text,
                milliseconds_per_character,
            ),
            question_reveal_ms_per_character=milliseconds_per_character,
        )
        if decision.accepted and not decision.duplicate:
            runtime = get_runtime_state('quiz', self.room_code, hub_session_code)
            runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
            public_snapshot = dict(runtime.public_snapshot or {})
            public_snapshot.pop('prepared_question_id', None)
            public_snapshot.pop('question_shell_prepared', None)
            runtime.public_snapshot = public_snapshot
            runtime.save(update_fields=['public_snapshot', 'updated_at'])
            session, _ = QuizSession.objects.select_for_update().get_or_create(quiz=quiz)
            session.present_question(question)
        return decision

    @database_sync_to_async
    @transaction.atomic
    def prepare_quiz_question(self, question_id, hub_session_code, action):
        quiz = Quiz.objects.select_for_update().get(room_code=self.room_code)
        runtime = get_runtime_state('quiz', self.room_code, hub_session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        snapshot = dict(runtime.public_snapshot or {})
        current_prepared_id = str(snapshot.get('prepared_question_id') or '')
        requested_id = str(question_id)
        if quiz.current_question_id or runtime.current_question_id or runtime.question_phase:
            return {
                'accepted': False,
                'code': 'invalid_phase',
                'message': 'Die vorherige Frage ist noch aktiv.',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        if current_prepared_id == requested_id:
            return {
                'accepted': True,
                'code': 'accepted',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        try:
            action_revision = int(action.get('state_revision'))
        except (TypeError, ValueError):
            action_revision = -1
        if action_revision < runtime.context_revision or action_revision > runtime.state_revision:
            return {
                'accepted': False,
                'code': 'stale_action',
                'message': 'Der Spielzustand hat sich geaendert.',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        supplied_game_id = str(action.get('game_id') or '')
        if supplied_game_id and supplied_game_id != runtime.game_instance_id:
            return {
                'accepted': False,
                'code': 'stale_action',
                'message': 'Die Aktion gehoert zu einer anderen Spielinstanz.',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        if not action.get('client_action_id'):
            return {
                'accepted': False,
                'code': 'invalid_action_context',
                'message': 'client_action_id fehlt.',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        snapshot.pop('question', None)
        snapshot.pop('answer_options', None)
        snapshot.pop('answer_duration_seconds', None)
        snapshot['prepared_question_id'] = requested_id
        snapshot['question_shell_prepared'] = True
        snapshot['revealed'] = False
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        runtime.public_snapshot = snapshot
        runtime.save(update_fields=[
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        return {
            'accepted': True,
            'code': 'accepted',
            'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
        }

    @database_sync_to_async
    @transaction.atomic
    def clear_prepared_quiz_question(self, hub_session_code, action):
        quiz = Quiz.objects.select_for_update().get(room_code=self.room_code)
        runtime = get_runtime_state('quiz', self.room_code, hub_session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        if quiz.current_question_id or runtime.current_question_id or runtime.question_phase:
            return {
                'accepted': False,
                'code': 'invalid_phase',
                'message': 'Eine bereits gesendete Frage kann nicht zurueckgenommen werden.',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        snapshot = dict(runtime.public_snapshot or {})
        if not snapshot.get('prepared_question_id'):
            return {
                'accepted': True,
                'code': 'accepted',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        try:
            action_revision = int(action.get('state_revision'))
        except (TypeError, ValueError):
            action_revision = -1
        if action_revision < runtime.context_revision or action_revision > runtime.state_revision:
            return {
                'accepted': False,
                'code': 'stale_action',
                'message': 'Der Spielzustand hat sich geaendert.',
                'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
            }
        snapshot.pop('prepared_question_id', None)
        snapshot['question_shell_prepared'] = True
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        runtime.public_snapshot = snapshot
        runtime.save(update_fields=[
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        return {
            'accepted': True,
            'code': 'accepted',
            'snapshot': current_snapshot('quiz', self.room_code, hub_session_code),
        }

    @database_sync_to_async
    @transaction.atomic
    def reveal_quiz_question_content(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
    ):
        quiz = Quiz.objects.select_for_update().get(id=quiz_id)
        question = QuizQuestion.objects.get(id=question_id)
        snapshot = current_snapshot('quiz', self.room_code, hub_session_code)
        if (
            quiz_answers_open_automatically(question.question_type)
            and snapshot.get('question_phase')
            == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
            and str(snapshot.get('current_question_id') or '') == str(question.id)
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
            game_key='quiz',
            room_code=self.room_code,
            session_code=hub_session_code,
            action=action,
        )
        if not revealed.accepted or not quiz_answers_open_automatically(
            question.question_type
        ):
            return revealed

        revealed_at = parse_datetime(
            revealed.snapshot.get('content_revealed_at') or ''
        )
        answering_started_at = multiple_choice_answering_starts_at(
            revealed_at,
            2 if question.question_type == 'true_false' else len(question.get_options()),
        )
        if not answering_started_at:
            transaction.set_rollback(True)
            return QuestionPhaseDecision(
                False,
                'invalid_phase',
                'Der Antwort-Reveal konnte nicht terminiert werden.',
                state_revision=revealed.state_revision,
                snapshot=revealed.snapshot,
            )

        opened = open_answering(
            game_key='quiz',
            room_code=self.room_code,
            session_code=hub_session_code,
            action={
                **action,
                'client_action_id': str(uuid.uuid4()),
                'state_revision': revealed.state_revision,
                'question_id': question.id,
            },
            answer_duration_seconds=question.time_limit,
            at=answering_started_at,
        )
        if not opened.accepted:
            transaction.set_rollback(True)
            return opened

        session, _ = QuizSession.objects.select_for_update().get_or_create(quiz=quiz)
        session.open_answering(
            question,
            started_at=answering_started_at,
            answer_duration_seconds=question.time_limit,
        )
        opened.snapshot['server_now'] = timezone.now().isoformat()
        return opened

    @database_sync_to_async
    @transaction.atomic
    def open_quiz_answering(
        self,
        quiz_id,
        question_id,
        hub_session_code,
        action,
        answer_duration_seconds,
    ):
        quiz = Quiz.objects.select_for_update().get(id=quiz_id)
        question = QuizQuestion.objects.get(id=question_id)
        opened_at = timezone.now()
        decision = open_answering(
            game_key='quiz',
            room_code=self.room_code,
            session_code=hub_session_code,
            action=action,
            answer_duration_seconds=answer_duration_seconds,
            uses_content_phase=(
                question.get_effective_question_type() != 'short_answer'
            ),
            at=opened_at,
        )
        if decision.accepted and not decision.duplicate:
            session, _ = QuizSession.objects.select_for_update().get_or_create(quiz=quiz)
            session.open_answering(
                question,
                started_at=opened_at,
                answer_duration_seconds=answer_duration_seconds,
            )
        return decision

    @database_sync_to_async
    @transaction.atomic
    def clear_current_question(self, quiz_id):
        try:
            quiz = Quiz.objects.select_for_update().get(id=quiz_id)
            session = getattr(quiz, 'session', None)
            if session:
                session.end_current_question()
            else:
                quiz.current_question = None
                quiz.question_start_time = None
                quiz.save(update_fields=['current_question', 'question_start_time', 'updated_at'])
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

    def _get_hub_session_code_for_room_sync(self):
        try:
            qs = HubGameStep.objects.select_related('session').filter(game_key='quiz', room_code=self.room_code)
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

    @staticmethod
    def _format_answer_display(question, answer_text):
        if question.get_effective_question_type() == 'short_answer':
            return question.format_short_answer_submission(answer_text)
        if question.question_type == 'multiple_choice':
            return dict(question.get_options()).get(str(answer_text).upper(), answer_text)
        if question.question_type == 'true_false':
            return 'Stimmt' if str(answer_text) == 'True' else 'Stimmt nicht'
        return answer_text

    @database_sync_to_async
    @transaction.atomic
    def save_pending_answer(self, participant_name, hub_session_code, answer_text, question_id=None):
        try:
            quiz = (
                Quiz.objects.select_for_update()
                .select_related('current_question')
                .get(room_code=self.room_code)
            )
            session = QuizSession.objects.select_for_update().filter(quiz=quiz).first()
            if not session or quiz.status != 'active' or not quiz.current_question:
                return False
            if not session.is_question_active:
                return False
            now = timezone.now()
            if quiz.question_start_time and now < quiz.question_start_time:
                return False
            if session.question_end_time and now >= session.question_end_time:
                return False
            if question_id and str(quiz.current_question_id) != str(question_id):
                return False

            participant = quiz.participants.get(
                name=participant_name,
                hub_session_code=hub_session_code,
            )
            pending_answers = dict(session.pending_answers or {})
            entry_key = str(participant.id)
            existing_answer = QuizAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
            ).first()
            if existing_answer and (
                not quiz.question_start_time
                or existing_answer.submitted_at >= quiz.question_start_time
            ):
                if pending_answers.pop(entry_key, None) is not None:
                    session.pending_answers = pending_answers
                    session.save(update_fields=['pending_answers', 'updated_at'])
                return False

            if isinstance(answer_text, dict):
                cleaned_answer = {
                    str(key): str(value or '').strip()
                    for key, value in answer_text.items()
                }
                has_answer = bool(cleaned_answer) and all(cleaned_answer.values())
            else:
                cleaned_answer = '' if answer_text is None else str(answer_text).strip()
                has_answer = bool(cleaned_answer)

            if has_answer:
                pending_answers[entry_key] = {
                    'question_id': quiz.current_question_id,
                    'answer': cleaned_answer,
                    'updated_at': timezone.now().isoformat(),
                    'participant_name': participant.name,
                    'hub_session_code': participant.hub_session_code,
                }
            else:
                pending_answers.pop(entry_key, None)

            session.pending_answers = pending_answers
            session.save(update_fields=['pending_answers', 'updated_at'])
            return True
        except (Quiz.DoesNotExist, QuizParticipant.DoesNotExist):
            return False

    @database_sync_to_async
    @transaction.atomic
    def finalize_pending_answers_and_close_question(
        self,
        quiz_id,
        hub_session_code,
    ):
        quiz = (
            Quiz.objects.select_for_update()
            .select_related('current_question')
            .get(id=quiz_id)
        )
        session = QuizSession.objects.select_for_update().get(quiz=quiz)
        question = quiz.current_question
        if not question:
            return {
                'correct_answer': None,
                'answer_results': [],
                'auto_finalized_answers': [],
                'is_tutorial_round': False,
            }

        formatted_answer = question.get_formatted_correct_answer()
        raw_answer = question.correct_answer
        if question.get_effective_question_type() == 'short_answer' and question.get_short_answer_field_count() > 1:
            raw_answer = {
                field['key']: field['correct_answer']
                for field in question.get_short_answer_fields()
            }
        correct_payload = {
            'question_id': question.id,
            'formatted_answer': formatted_answer,
            'raw': raw_answer,
            'question_text': question.question_text,
            'question_type': question.get_effective_question_type(),
        }
        is_tutorial_round = is_unit_tutorial_question(
            'quiz',
            self.room_code,
            hub_session_code,
            question.id,
        )

        pending_answers = dict(session.pending_answers or {})
        auto_finalized_answers = []
        for participant_id, pending in list(pending_answers.items()):
            if str(pending.get('question_id')) != str(question.id):
                continue

            try:
                participant = quiz.participants.get(id=int(participant_id))
            except (QuizParticipant.DoesNotExist, TypeError, ValueError):
                continue

            existing_answer = QuizAnswer.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).first()
            if existing_answer and quiz.question_start_time and existing_answer.submitted_at < quiz.question_start_time:
                existing_answer.delete()
                existing_answer = None
            if existing_answer:
                continue

            answer_value = pending.get('answer')
            if isinstance(answer_value, dict):
                has_answer = bool(answer_value) and all(str(value or '').strip() for value in answer_value.values())
            else:
                has_answer = bool(str(answer_value or '').strip())
            if not has_answer:
                continue

            updated_at = parse_datetime(pending.get('updated_at') or '')
            if updated_at and timezone.is_naive(updated_at):
                updated_at = timezone.make_aware(updated_at, timezone.get_current_timezone())
            time_taken = 0
            if quiz.question_start_time and updated_at:
                time_taken = max(0, (updated_at - quiz.question_start_time).total_seconds())

            answer_to_store = answer_value
            if question.get_effective_question_type() == 'short_answer':
                answer_to_store = question.serialize_short_answer_submission(answer_value)
            answer = QuizAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=question,
                answer_text=answer_to_store,
                time_taken=time_taken,
            )
            if is_tutorial_round and answer.points_earned:
                answer.points_earned = 0
                answer.save(update_fields=['points_earned', 'updated_at'])

            auto_finalized_answers.append({
                'participant_name': participant.name,
                'question_id': question.id,
                'display_answer': self._format_answer_display(question, answer.answer_text),
                'is_correct': answer.is_correct,
                'points_earned': answer.points_earned,
            })

        answers = QuizAnswer.objects.filter(
            quiz=quiz,
            question=question,
        ).select_related('participant')
        if hub_session_code is not None:
            answers = answers.filter(participant__hub_session_code=hub_session_code)
        answer_results = [
            {
                'participant_name': answer.participant.name,
                'question_id': answer.question_id,
                'is_correct': answer.is_correct,
                'points_earned': answer.points_earned,
                'display_answer': self._format_answer_display(question, answer.answer_text),
            }
            for answer in answers
        ]

        last_question_result = {
            'correct_answer': correct_payload,
            'answer_results': answer_results,
            'auto_finalized_answers': auto_finalized_answers,
            'is_tutorial_round': bool(is_tutorial_round),
        }
        quiz.current_question = None
        quiz.question_start_time = None
        session.is_question_active = False
        session.question_end_time = None
        session.pending_answers = {}
        session.last_question_result = last_question_result
        quiz.save(update_fields=['current_question', 'question_start_time', 'updated_at'])
        session.save(update_fields=[
            'is_question_active',
            'question_end_time',
            'pending_answers',
            'last_question_result',
            'updated_at',
        ])
        return last_question_result

    @database_sync_to_async
    @transaction.atomic
    def save_participant_answer(
        self,
        participant_name,
        hub_session_code,
        answer_text,
        time_taken,
        question_id=None,
    ):
        try:
            quiz = (
                Quiz.objects.select_for_update()
                .select_related('current_question')
                .get(room_code=self.room_code)
            )
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)

            if quiz.status != 'active' or not quiz.current_question:
                return None

            current_question = quiz.current_question
            target_question = current_question
            question_id = str(question_id) if question_id is not None else None

            if question_id and str(target_question.id) != question_id:
                return None

            session = QuizSession.objects.select_for_update().filter(quiz=quiz).first()
            now = timezone.now()
            if session and (
                not session.is_question_active
                or (
                    quiz.question_start_time is not None
                    and now < quiz.question_start_time
                )
                or (
                    session.question_end_time is not None
                    and now >= session.question_end_time
                )
            ):
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
                    if session:
                        pending_answers = dict(session.pending_answers or {})
                        if pending_answers.pop(str(participant.id), None) is not None:
                            session.pending_answers = pending_answers
                            session.save(update_fields=['pending_answers', 'updated_at'])
                    return None  # Already answered in this round

            answer_to_store = answer_text
            display_answer = answer_text
            if target_question.get_effective_question_type() == 'short_answer':
                answer_to_store = target_question.serialize_short_answer_submission(answer_text)
                display_answer = target_question.format_short_answer_submission(answer_text)

            is_tutorial_answer = is_unit_tutorial_question(
                'quiz',
                self.room_code,
                hub_session_code,
                target_question.id,
            )

            received_at = now
            server_time_taken = (
                max(0.0, (received_at - quiz.question_start_time).total_seconds())
                if quiz.question_start_time
                else 0.0
            )

            # Create new answer
            answer, created = QuizAnswer.objects.get_or_create(
                quiz=quiz,
                participant=participant,
                question=target_question,
                defaults={
                    'answer_text': answer_to_store,
                    'time_taken': server_time_taken,
                },
            )
            if not created:
                return None
            if is_tutorial_answer and answer.points_earned:
                answer.points_earned = 0
                answer.save(update_fields=['points_earned', 'updated_at'])

            if session:
                pending_answers = dict(session.pending_answers or {})
                if pending_answers.pop(str(participant.id), None) is not None:
                    session.pending_answers = pending_answers
                    session.save(update_fields=['pending_answers', 'updated_at'])

            # Resolve key → full text for multiple choice
            if target_question.question_type == 'multiple_choice':
                option_map = dict(target_question.get_options())
                display_answer = option_map.get(answer_text.upper(), answer_text)

            field_results = answer.get_short_answer_field_results()
            is_manual_override = (
                target_question.get_effective_question_type() == 'short_answer'
                and answer.is_correct
                and not target_question.is_correct_answer(answer.answer_text)
            )
            participant.refresh_from_db(fields=['total_score'])
            return {
                'answer_id': answer.id,
                'question_id': target_question.id,
                'is_correct': answer.is_correct,
                'is_manual_override': is_manual_override,
                'points_earned': answer.points_earned,
                'display_answer': display_answer,
                'question_type': target_question.get_effective_question_type(),
                'field_results': field_results,
                'can_mark_correct': (
                    not is_tutorial_answer
                    and target_question.get_effective_question_type() == 'short_answer'
                    and not answer.is_correct
                ),
                'total_score': participant.total_score,
                'is_tutorial_round': is_tutorial_answer,
                'submitted_at': answer.submitted_at.isoformat(),
                'time_taken': answer.time_taken,
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
