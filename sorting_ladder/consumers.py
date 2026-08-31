import json
import asyncio
import random
import uuid
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.authoritative_consumer import AuthoritativeGameConsumerMixin
from games_hub.authoritative_state import (
    QuestionPhaseDecision,
    current_snapshot,
    finish_question_flow,
    open_answering,
    present_question,
    reset_question_flow,
    reveal_question_content,
)
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room
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

from .models import (
    SortingLadderGame,
    SortingLadderParticipant,
    SortingQuestion,
    SortingItem,
    RoundSubmission,
    SortingLadderSession,
    SortingPendingRoundSelection,
)
from games_hub.models import GameRuntimeState, HubGameStep, HubSession
from .runtime import (
    SORTING_LADDER_REVEAL_ANIMATION_MS,
    SORTING_LADDER_REVEAL_STAGGER_MS,
    sorting_ladder_reveal_counts,
    sorting_ladder_reveal_ready_at,
    sorting_ladder_reveal_step_count,
)


def _set_number_for_game(quiz, question_id, hub_session_code=None):
    excluded_ids = get_scorebox_excluded_tutorial_question_ids(
        'sorting_ladder',
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


class SortingLadderGameConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_game_key = 'sorting_ladder'
    authoritative_required_actions = frozenset({
        'participant_submit_move',
        'participant_submit_round',
        'participant_update_selection',
    })

    @staticmethod
    def _pending_round_selection(
        room_code,
        question_id,
        round_number,
        participant_name,
        hub_session_code,
    ):
        return SortingPendingRoundSelection.objects.filter(
            quiz__room_code=room_code,
            question_id=question_id,
            round_number=max(int(round_number or 0), 1),
            participant__name=participant_name,
            participant__hub_session_code=hub_session_code,
        )

    @classmethod
    def _get_pending_round_order(cls, room_code, question_id, round_number, participant_name, hub_session_code):
        selection = cls._pending_round_selection(
            room_code,
            question_id,
            round_number,
            participant_name,
            hub_session_code,
        ).first()
        return list(selection.ordered_item_ids or []) if selection else []

    @classmethod
    def _set_pending_round_order(cls, room_code, question_id, round_number, participant_name, hub_session_code, ordered_item_ids):
        participant = SortingLadderParticipant.objects.filter(
            quiz__room_code=room_code,
            name=participant_name,
            hub_session_code=hub_session_code,
        ).first()
        if not participant:
            return
        SortingPendingRoundSelection.objects.update_or_create(
            quiz=participant.quiz,
            participant=participant,
            question_id=question_id,
            round_number=max(int(round_number or 0), 1),
            defaults={'ordered_item_ids': list(ordered_item_ids or [])},
        )

    @classmethod
    def _pop_pending_round_order(cls, room_code, question_id, round_number, participant_name, hub_session_code):
        selection = cls._pending_round_selection(
            room_code,
            question_id,
            round_number,
            participant_name,
            hub_session_code,
        ).first()
        if not selection:
            return []
        ordered_item_ids = list(selection.ordered_item_ids or [])
        selection.delete()
        return ordered_item_ids

    @staticmethod
    def _clear_room_pending_round_orders(room_code):
        SortingPendingRoundSelection.objects.filter(quiz__room_code=room_code).delete()

    async def connect(self):
        self.room_code = self.scope['url_route']['kwargs']['room_code']
        self.room_group_name = f'sortingladder_{self.room_code}'

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Connected to sorting ladder session'
        }))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))
            return

        msg_type = data.get('type')
        print("SortingLadderGame Consumer:", msg_type)

        if msg_type == 'admin_start_quiz':
            await self.handle_admin_start_quiz(data)
        elif msg_type == 'admin_set_topic':
            # Legacy handler (topic-based flow). Kept for backwards compatibility.
            await self.handle_admin_set_topic(data)
        elif msg_type == 'admin_start_round':
            await self.handle_admin_start_round(data)
        elif msg_type == 'admin_end_round':
            await self.handle_admin_end_round(data)
        elif msg_type == 'admin_end_quiz':
            await self.handle_admin_end_quiz(data)
        elif msg_type == 'admin_set_inactive':
            await self.handle_admin_set_inactive(data)
        elif msg_type == 'admin_send_question':
            await self.handle_admin_send_question(data)
        elif msg_type == 'admin_reveal_question_content':
            await self.handle_admin_reveal_question_content(data)
        elif msg_type == 'admin_open_answering':
            await self.handle_admin_open_answering(data)
        elif msg_type == 'admin_end_question':
            await self.handle_admin_end_question(data)
        elif msg_type == 'admin_show_solution':
            await self.handle_admin_show_solution(data)
        elif msg_type == 'participant_join':
            await self.handle_participant_join(data)
        elif msg_type == 'tutorial_completed':
            await self.handle_tutorial_completed(data)
        elif msg_type == 'participant_submit_move':
            # Legacy move submission (gap placement). Kept for backwards compatibility.
            await self.handle_participant_submit_move(data)
        elif msg_type == 'participant_submit_round':
            await self.handle_participant_submit_round(data)
        elif msg_type == 'participant_update_selection':
            await self.handle_participant_update_selection(data)
        elif msg_type == 'ping':
            await self.handle_ping()
        elif msg_type == 'admin_show_leaderboard':
            await self.handle_admin_show_leaderboard()
        elif msg_type == 'admin_hide_leaderboard':
            await self.handle_admin_hide_leaderboard()

    # -------- Admin handlers --------

    async def handle_admin_start_quiz(self, data):
        lobby_ready = await database_sync_to_async(ensure_session_players_ready_for_game_start_for_room)(
            'sorting_ladder',
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
        if not quiz:
            return
        fresh_start = quiz.status == 'waiting'
        show_tutorial = bool(data.get('show_tutorial', False))
        play_tutorial = bool(data.get('play_tutorial', False))
        hub_session_code = (
            data.get('hub_session')
            or data.get('hub_session_code')
            or await self._get_hub_session_code_for_room()
        )
        unit_tutorial_validation = await database_sync_to_async(validate_unit_tutorial_request)(
            'sorting_ladder',
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
            'sorting_ladder',
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
        if fresh_start:
            await database_sync_to_async(prepare_unit_tutorial_runtime)(
                'sorting_ladder',
                self.room_code,
                hub_session_code,
                play_tutorial,
                validate=False,
            )
        await self.start_quiz_db(quiz.id, reset_runtime=fresh_start)
        if fresh_start:
            question_runtime = await database_sync_to_async(reset_question_flow)(
                game_key='sorting_ladder',
                room_code=self.room_code,
                session_code=hub_session_code,
                mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
            )
            tutorial_payload = await self.activate_tutorial_runtime(
                quiz.id,
                hub_session_code,
                show_tutorial,
            )
        else:
            question_runtime = await database_sync_to_async(current_snapshot)(
                'sorting_ladder',
                self.room_code,
                hub_session_code,
            )
            tutorial_payload = None

        await self.channel_layer.group_send(
            self.room_group_name,
                {
                    'type': 'quiz_started',
                    'message': 'Sorting Ladder quiz has started!',
                    **self.question_lifecycle_fields(question_runtime),
                }
        )

        await self.hub_mirror_event('quiz_started', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
        })
        if tutorial_payload:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'tutorial_start',
                    **tutorial_payload,
                }
            )

    async def handle_admin_set_topic(self, data):
        """
        Admin chooses which SortingQuestion (topic) to play.
        Also initializes the SortingLadderSession with 2 reference items.
        """
        topic_id = data.get('topic_id')
        time_limit = data.get('time_limit_seconds')

        quiz = await self.get_quiz()
        if not quiz:
            return

        topic = await self.get_topic(topic_id)
        if not topic:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid topic selected.'
            }))
            return

        # Initialize session: two reference items + upcoming active items
        session_payload = await self.initialize_session_for_topic(
            quiz_id=quiz.id,
            topic_id=topic.id,
            time_limit_seconds=time_limit
        )
        if not session_payload:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Not enough items for this topic (need at least 3).'
            }))
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'topic_selected',
                'topic': {
                    'id': topic.id,
                    'title': topic.question_text,
                    'description': topic.description,
                },
                'session': session_payload,
            }
        )

        await self.hub_mirror_event('topic_selected', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'topic_id': topic.id,
        })

    async def handle_admin_start_round(self, data):
        """
        Admin explicitly moves to the next round (next active element).
        """
        quiz = await self.get_quiz()
        if not quiz:
            return

        if await self.guard_tutorial_before_first_unit(data, quiz.id):
            return

        round_state, phase_decision = await self.prepare_next_round_phase(quiz.id, data)
        if not phase_decision.accepted:
            await self.send_question_phase_rejection(
                phase_decision,
                quiz.current_question_id,
            )
            return
        if not round_state:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'no_more_rounds',
                    'message': 'All elements have been placed.'
                }
            )
            await self.hub_mirror_event('no_more_rounds', {
                'room_code': self.room_code,
                'game_key': 'sorting_ladder',
            })
            return

        round_state.update(self.question_lifecycle_fields(phase_decision.snapshot))
        round_state.update(self.sorting_reveal_fields(
            item_count=round_state.get('item_count', 0),
            round_number=round_state.get('round_number', 1),
            snapshot=phase_decision.snapshot,
        ))

        await self.set_tutorial_active_db(quiz.id, False)

        for auto_result in round_state.get('auto_results', []):
            progress_history = await self.get_participant_progress_history_for_round_result(
                auto_result['participant_name'],
                auto_result['hub_session_code'],
            )
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'round_result',
                    'participant_name': auto_result['participant_name'],
                    **auto_result['result'],
                    'progress_history': progress_history,
                }
            )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'round_started',
                'round': round_state
            }
        )

        await self.hub_mirror_event('round_started', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'round': round_state,
        })
        await self.broadcast_round_answer_status(quiz.id)

    async def handle_admin_end_round(self, data):
        """
        Ends the current round: freeze submissions and show who is still alive.
        """
        quiz = await self.get_quiz()
        if not quiz:
            return

        round_end_payload = await self.end_round_db(quiz.id)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'round_ended',
                'survivors': round_end_payload.get('survivors', []),
                'has_next_round': round_end_payload.get('has_next_round', False),
            }
        )

        await self.hub_mirror_event('round_ended', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'survivors': round_end_payload.get('survivors', []),
            'has_next_round': round_end_payload.get('has_next_round', False),
        })

    async def handle_admin_end_quiz(self, data):
        """
        Ends the entire game and broadcasts the final standings (rounds survived).
        """
        quiz = await self.get_quiz()
        if not quiz:
            return

        if quiz.current_question_id:
            await self.finish_sorting_round_phase(quiz.current_question_id)
        await self.end_quiz_db(quiz.id)
        final_scores = await self.get_final_scores()

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'quiz_ended',
                'message': 'Sorting Ladder quiz has ended.',
                'final_scores': final_scores,
            }
        )

        await self.hub_mirror_event('quiz_ended', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'final_scores': final_scores,
        })

    async def handle_admin_set_inactive(self, data):
        """Pause the game without clearing its current progress."""
        quiz = await self.get_quiz()
        if quiz and quiz.status == 'active':
            await self.set_quiz_inactive_db(quiz.id)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'quiz_inactive',
                    'message': 'Game has been set inactive.',
                }
            )

    async def handle_admin_send_question(self, data):
        """Admin selects a SortingQuestion to play for this quiz.

        This initializes a shared shuffled order of SortingItem records and
        broadcasts the question + shuffled items to all participants.
        """
        question_id = data.get('question_id')
        custom_time_limit = data.get('custom_time_limit')

        quiz = await self.get_quiz()
        if not quiz:
            return

        if await self.guard_tutorial_before_first_unit(data, quiz.id):
            return

        hub_session = data.get('hub_session') or data.get('hub_session_code') or await self._get_hub_session_code_for_room()
        unit_tutorial = await self.start_unit_tutorial_if_needed(hub_session)
        is_tutorial_round = bool(unit_tutorial.get('is_tutorial_round'))
        if is_tutorial_round:
            question_id = unit_tutorial.get('tutorial_question_id')

        payload, decision = await self.begin_sorting_question_phase(
            quiz_id=quiz.id,
            question_id=question_id,
            time_limit_seconds=custom_time_limit,
            hub_session_code=hub_session,
            action=data,
        )
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, question_id)
            return
        if not payload:
            if decision.duplicate:
                return
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Unable to start question. Ensure it has at least 2 items.',
            }))
            return
        await self.set_tutorial_active_db(quiz.id, False)
        payload['is_tutorial_round'] = is_tutorial_round
        payload.setdefault('question', {})['is_tutorial_round'] = is_tutorial_round
        payload.update(self.question_lifecycle_fields(decision.snapshot))
        payload.update(self.sorting_reveal_fields(
            item_count=len(payload.get('items') or []),
            round_number=payload.get('round_number', 1),
            snapshot=decision.snapshot,
        ))

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_started',
                'payload': payload,
            }
        )
        await self.broadcast_round_answer_status(quiz.id)

        await self.hub_mirror_event('question_started', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            **payload,
        })

    async def handle_admin_reveal_question_content(self, data):
        context = await self.get_current_round_context()
        if not context:
            return
        decision = await self.reveal_sorting_round(context, data)
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, context['question_id'])
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_content_revealed',
                'question_id': context['question_id'],
                'round_number': context['round_number'],
                'set_number': context['set_number'],
                **self.question_lifecycle_fields(decision.snapshot),
                **self.sorting_reveal_fields(
                    item_count=context['item_count'],
                    round_number=context['round_number'],
                    snapshot=decision.snapshot,
                ),
            },
        )

    async def handle_admin_open_answering(self, data):
        context = await self.get_current_round_context()
        if not context:
            return
        decision = await self.open_sorting_round(context, data)
        if not decision.accepted:
            await self.send_question_phase_rejection(decision, context['question_id'])
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_answering_opened',
                'question_id': context['question_id'],
                'round_number': context['round_number'],
                'set_number': context['set_number'],
                **self.question_lifecycle_fields(decision.snapshot),
                **self.sorting_reveal_fields(
                    item_count=context['item_count'],
                    round_number=context['round_number'],
                    snapshot=decision.snapshot,
                ),
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
    def sorting_reveal_fields(*, item_count, round_number, snapshot):
        snapshot = snapshot or {}
        counts = sorting_ladder_reveal_counts(
            item_count=item_count,
            round_number=round_number,
        )
        content_revealed_at = parse_datetime(
            str(snapshot.get('content_revealed_at') or '')
        )
        ready_at = sorting_ladder_reveal_ready_at(
            content_revealed_at=content_revealed_at,
            item_count=item_count,
            round_number=round_number,
        )
        return {
            'sorting_ladder_reveal_stagger_ms': SORTING_LADDER_REVEAL_STAGGER_MS,
            'sorting_ladder_reveal_animation_ms': SORTING_LADDER_REVEAL_ANIMATION_MS,
            'sorting_ladder_reveal_step_count': sorting_ladder_reveal_step_count(
                item_count=item_count,
                round_number=round_number,
            ),
            'sorting_ladder_reveal_label_count': counts['label_count'],
            'sorting_ladder_reveal_element_count': counts['element_count'],
            'sorting_ladder_reveal_fixed_count': counts['fixed_count'],
            'sorting_ladder_reveal_marker_group_count': counts['marker_group_count'],
            'sorting_ladder_reveal_ready_at': ready_at.isoformat() if ready_at else None,
        }

    async def handle_admin_end_question(self, data):
        """Complete the final round, or clear an already revealed set."""
        quiz = await self.get_quiz()
        if not quiz:
            return

        end_payload = await self.end_question_db(quiz.id)
        if not end_payload:
            return

        if end_payload.get('status') == 'rounds_remaining':
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Dieses Set besitzt noch weitere Runden.',
            }))
            return

        if end_payload.get('status') == SortingLadderSession.REVEAL_REVEALED:
            cleared = await self.clear_revealed_question_db(quiz.id)
            if not cleared:
                return
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'question_ended',
                    'message': 'Set has ended.',
                    'is_tutorial_round': bool(end_payload.get('is_tutorial_round')),
                }
            )
            await self.hub_mirror_event('question_ended', {
                'room_code': self.room_code,
                'game_key': 'sorting_ladder',
                'is_tutorial_round': bool(end_payload.get('is_tutorial_round')),
            })
            return

        if not end_payload.get('transitioned'):
            return

        await self.finish_sorting_round_phase(quiz.current_question_id)

        for auto_result in end_payload.get('auto_results', []):
            progress_history = await self.get_participant_progress_history_for_round_result(
                auto_result['participant_name'],
                auto_result['hub_session_code'],
            )
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'round_result',
                    'participant_name': auto_result['participant_name'],
                    **auto_result['result'],
                    'progress_history': progress_history,
                }
            )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_rounds_complete',
                'message': 'Set abgeschlossen. Warte auf die Reveal-Freigabe.',
                'reveal_state': SortingLadderSession.REVEAL_AWAITING,
                'is_tutorial_round': bool(end_payload.get('is_tutorial_round')),
            }
        )

        await self.hub_mirror_event('question_rounds_complete', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'is_tutorial_round': bool(end_payload.get('is_tutorial_round')),
        })

    async def handle_admin_show_solution(self, data):
        """Reveal the completed set exactly once."""
        quiz = await self.get_quiz()
        if not quiz:
            return

        reveal_payload = await self.reveal_solution_db(quiz.id)
        if not reveal_payload or not reveal_payload.get('transitioned'):
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'show_solution',
                'correct_order_ids': reveal_payload['correct_order_ids'],
                'reveal_state': SortingLadderSession.REVEAL_REVEALED,
                'is_tutorial_round': bool(reveal_payload.get('is_tutorial_round')),
            }
        )
        await self.hub_mirror_event('show_solution', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'is_tutorial_round': bool(reveal_payload.get('is_tutorial_round')),
        })

    # -------- Participant handlers --------

    async def handle_participant_join(self, data):
        """
        Player joins the room.
        """
        name = data.get('name')
        hub_session_code = data.get('hub_session_code')

        if not name:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Name is required to join.'
            }))
            return

        participant_payload = await self.get_or_create_participant(name, hub_session_code)
        if not participant_payload:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Unable to join game.'
            }))
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'participant_joined',
                'participant': participant_payload
            }
        )

        await self.hub_mirror_event('participant_joined', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'participant': participant_payload,
        })

        progress_history = await self.get_participant_progress_history(name, hub_session_code)
        await self.send(text_data=json.dumps({
            'type': 'progress_history',
            'history': progress_history,
        }))

        # If game is already active, send quiz_started directly to this participant
        game = await self.get_quiz()
        if game and game.status == 'active':
            await self.send(text_data=json.dumps({
                'type': 'quiz_started',
                'message': 'Game is already in progress'
            }))
            tutorial_payload = await self.get_tutorial_payload(
                game.id,
                hub_session_code,
                participant_name=name,
            )
            if tutorial_payload:
                await self.send(text_data=json.dumps({
                    'type': 'tutorial_start',
                    **tutorial_payload,
                }))
            snapshot = await self.get_rejoin_snapshot(name, hub_session_code)
            if snapshot.get('question_payload'):
                await self.send(text_data=json.dumps({
                    'type': 'question_started',
                    **snapshot['question_payload'],
                }))
            if snapshot.get('latest_round_result'):
                await self.send(text_data=json.dumps({
                    'type': 'round_result',
                    'participant_name': name,
                    **snapshot['latest_round_result'],
                }))
            if snapshot.get('reveal_state') == SortingLadderSession.REVEAL_AWAITING:
                await self.send(text_data=json.dumps({
                    'type': 'question_rounds_complete',
                    'message': 'Set abgeschlossen. Warte auf die Reveal-Freigabe.',
                    'reveal_state': SortingLadderSession.REVEAL_AWAITING,
                }))
            elif snapshot.get('reveal_state') == SortingLadderSession.REVEAL_REVEALED:
                await self.send(text_data=json.dumps({
                    'type': 'show_solution',
                    'correct_order_ids': snapshot.get('reveal_order_ids', []),
                    'reveal_state': SortingLadderSession.REVEAL_REVEALED,
                }))
            if snapshot.get('round_started'):
                await self.send(text_data=json.dumps({
                    'type': 'round_started',
                    'round': snapshot['round_started'],
                }))

    async def handle_tutorial_completed(self, data):
        participant_name = data.get('participant_name') or data.get('name')
        hub_session = data.get('hub_session') or data.get('hub_session_code')
        progress = await self.mark_tutorial_completed(participant_name, hub_session)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'tutorial_progress', **progress}
        )

    async def handle_participant_submit_move(self, data):
        """
        Player attempts to place the active element between two reference items.

        Expected payload:
        - participant_name
        - hub_session_code
        - placed_after_id (or null)
        - placed_before_id (or null)
        """
        participant_name = data.get('participant_name')
        hub_session_code = data.get('hub_session_code')
        placed_after_id = data.get('placed_after_id')
        placed_before_id = data.get('placed_before_id')

        if not participant_name or not hub_session_code:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid participant.'
            }))
            return

        result = await self.save_round_submission(
            participant_name=participant_name,
            hub_session_code=hub_session_code,
            placed_after_id=placed_after_id,
            placed_before_id=placed_before_id,
        )

        if not result:
            await self.send(text_data=json.dumps({
                'type': 'round_submission_rejected',
                'message': 'Legacy move submissions are not supported. Submit ordered_item_ids instead.',
            }))
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'move_submitted',
                'participant_name': participant_name,
                'is_correct': result['is_correct'],
                'rounds_survived': result['rounds_survived'],
                'is_eliminated': result['is_eliminated'],
            }
        )

        await self.hub_mirror_event('move_submitted', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            **result,
            'participant_name': participant_name,
        })

    async def handle_participant_submit_round(self, data):
        """Participant submits their full ordering of visible items for this round.

        Expected payload:
        - participant_name
        - hub_session_code
        - ordered_item_ids: list of SortingItem IDs in the order the player chose
        """
        participant_name = data.get('participant_name')
        hub_session_code = data.get('hub_session_code')
        ordered_item_ids = data.get('ordered_item_ids') or []
        round_time_out = data.get('round_time_out', False)

        if not participant_name or not hub_session_code or not isinstance(ordered_item_ids, list):
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid round submission.',
            }))
            return

        result = await self.save_round_full_order(
            participant_name=participant_name,
            hub_session_code=hub_session_code,
            ordered_item_ids=ordered_item_ids,
            round_time_out=round_time_out,
        )
        print("result  ", result)

        if not result:
            # Could be late submission, invalid state, or player already eliminated
            await self.send(text_data=json.dumps({
                'type': 'round_submission_rejected',
                'message': 'Round submission rejected. Please try again.',
            }))
            return

        progress_history = await self.get_participant_progress_history_for_round_result(
            participant_name,
            hub_session_code,
        )
        result['progress_history'] = progress_history

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'round_result',
                'participant_name': participant_name,
                **result,
            }
        )

        await self.hub_mirror_event('round_result', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'participant_name': participant_name,
            **result,
        })
        quiz = await self.get_quiz()
        if quiz:
            await self.broadcast_round_answer_status(quiz.id)

    async def handle_participant_update_selection(self, data):
        """Persist the current, not-yet-logged ladder state for host-side early round ends."""
        participant_name = data.get('participant_name')
        hub_session_code = data.get('hub_session_code')
        ordered_item_ids = data.get('ordered_item_ids') or []

        if not participant_name or not hub_session_code or not isinstance(ordered_item_ids, list):
            return

        updated = await self.update_pending_round_selection_db(
            participant_name=participant_name,
            hub_session_code=hub_session_code,
            ordered_item_ids=ordered_item_ids,
        )
        if not updated:
            return

        quiz = await self.get_quiz()
        if quiz:
            await self.broadcast_round_answer_status(quiz.id)

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
        await self.send(text_data=json.dumps({
            'type': 'pong',
            'timestamp': timezone.now().isoformat()
        }))

    # -------- Group event handlers (for group_send) --------

    async def quiz_started(self, event):
        await self.send(text_data=json.dumps({
            'type': 'quiz_started',
            'message': event.get('message', '')
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

    async def topic_selected(self, event):
        await self.send(text_data=json.dumps({
            'type': 'topic_selected',
            'topic': event['topic'],
            'session': event['session'],
        }))

    async def round_started(self, event):
        await self.send(text_data=json.dumps({
            'type': 'round_started',
            'round': event['round'],
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

    async def round_ended(self, event):
        await self.send(text_data=json.dumps({
            'type': 'round_ended',
            'survivors': event['survivors'],
            'has_next_round': event.get('has_next_round', False),
        }))

    async def no_more_rounds(self, event):
        await self.send(text_data=json.dumps({
            'type': 'no_more_rounds',
            'message': event.get('message', '')
        }))

    async def participant_joined(self, event):
        await self.send(text_data=json.dumps({
            'type': 'participant_joined',
            'participant': event['participant'],
        }))

    async def move_submitted(self, event):
        await self.send(text_data=json.dumps({
            'type': 'move_submitted',
            'participant_name': event['participant_name'],
            'is_correct': event['is_correct'],
            'rounds_survived': event['rounds_survived'],
            'is_eliminated': event['is_eliminated'],
        }))

    async def quiz_ended(self, event):
        await self.send(text_data=json.dumps({
            'type': 'quiz_ended',
            'message': event.get('message', ''),
            'final_scores': event.get('final_scores', []),
        }))

    async def quiz_inactive(self, event):
        await self.send(text_data=json.dumps({
            'type': 'quiz_inactive',
            'message': event.get('message', 'Game has been set inactive.'),
        }))

    async def question_started(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_started',
            **event['payload'],
        }))

    async def question_ended(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'message': event.get('message', ''),
            'is_tutorial_round': event.get('is_tutorial_round', False),
        }))

    async def question_rounds_complete(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_rounds_complete',
            'message': event.get('message', ''),
            'reveal_state': event.get(
                'reveal_state',
                SortingLadderSession.REVEAL_AWAITING,
            ),
            'is_tutorial_round': event.get('is_tutorial_round', False),
        }))

    async def show_solution(self, event):
        await self.send(text_data=json.dumps({
            'type': 'show_solution',
            'correct_order_ids': event.get('correct_order_ids', []),
            'reveal_state': event.get(
                'reveal_state',
                SortingLadderSession.REVEAL_REVEALED,
            ),
            'is_tutorial_round': event.get('is_tutorial_round', False),
        }))

    async def round_result(self, event):
        await self.send(text_data=json.dumps({
            'type': 'round_result',
            'participant_name': event['participant_name'],
            'question_id': event.get('question_id'),
            'round_number': event.get('round_number'),
            'is_correct': event['is_correct'],
            'rounds_survived': event['rounds_survived'],
            'is_eliminated': event['is_eliminated'],
            'points': event['points'],
            'is_tutorial_round': event.get('is_tutorial_round', False),
            'has_more_rounds': event['has_more_rounds'],
            'set_has_more_rounds': event.get('set_has_more_rounds', False),
            'per_question_rounds': event.get('per_question_rounds'),
            'correct_order_ids': event.get('correct_order_ids'),
            'progress_history': event.get('progress_history', []),
        }))

    async def round_answer_status(self, event):
        await self.send(text_data=json.dumps({
            'type': 'round_answer_status',
            'round_number': event.get('round_number'),
            'statuses': event.get('statuses', []),
        }))

    # -------- DB helpers --------

    @database_sync_to_async
    def get_quiz(self):
        try:
            return SortingLadderGame.objects.get(room_code=self.room_code)
        except SortingLadderGame.DoesNotExist:
            return None

    @database_sync_to_async
    def start_quiz_db(self, quiz_id, reset_runtime=False):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            if reset_runtime:
                quiz.reset_runtime_state()
            quiz.start_quiz()
        except SortingLadderGame.DoesNotExist:
            pass

    @database_sync_to_async
    def set_tutorial_active_db(self, quiz_id, active):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            if active:
                quiz.tutorial_active = True
                quiz.save(update_fields=['tutorial_active'])
            else:
                deactivate_tutorial_runtime('sorting_ladder', self.room_code, None, quiz)
        except SortingLadderGame.DoesNotExist:
            pass

    @database_sync_to_async
    def get_tutorial_payload(self, quiz_id, hub_session_code=None, participant_name=None):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            payload = get_tutorial_payload('sorting_ladder', self.room_code, hub_session_code, participant_name)
            if payload:
                payload['game_title'] = quiz.title
            return payload
        except SortingLadderGame.DoesNotExist:
            return None

    @database_sync_to_async
    def activate_tutorial_runtime(self, quiz_id, hub_session_code, show_tutorial):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            return activate_tutorial_runtime('sorting_ladder', self.room_code, hub_session_code, quiz, show_tutorial)
        except SortingLadderGame.DoesNotExist:
            return None

    @database_sync_to_async
    def mark_tutorial_completed(self, participant_name, hub_session_code):
        return mark_tutorial_completed('sorting_ladder', self.room_code, hub_session_code, participant_name)

    @database_sync_to_async
    def get_tutorial_start_warning(self, hub_session_code):
        return get_tutorial_start_warning('sorting_ladder', self.room_code, hub_session_code)

    @database_sync_to_async
    def start_unit_tutorial_if_needed(self, hub_session_code):
        return start_unit_tutorial_if_needed('sorting_ladder', self.room_code, hub_session_code)

    @database_sync_to_async
    def finish_current_unit_tutorial(self, hub_session_code):
        return finish_current_unit_tutorial('sorting_ladder', self.room_code, hub_session_code)

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            quiz.end_quiz()
        except SortingLadderGame.DoesNotExist:
            pass

    @database_sync_to_async
    def set_quiz_inactive_db(self, quiz_id):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            quiz.status = 'inactive'
            quiz.save(update_fields=['status'])
        except SortingLadderGame.DoesNotExist:
            pass

    @database_sync_to_async
    def get_topic(self, topic_id):
        try:
            return SortingQuestion.objects.get(id=topic_id, is_active=True)
        except SortingQuestion.DoesNotExist:
            return None

    @database_sync_to_async
    def initialize_session_for_topic(self, quiz_id, topic_id, time_limit_seconds=None):
        """
        Creates/updates SortingLadderSession:
        - Picks two reference items (smallest and largest).
        - Ensures there is at least one remaining item to be the first active element.
        """
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            topic = SortingQuestion.objects.get(id=topic_id)
        except (SortingLadderGame.DoesNotExist, SortingQuestion.DoesNotExist):
            return None

        elements = list(topic.elements.order_by('correct_rank'))
        if len(elements) < 3:
            return None

        smallest = elements[0]
        largest = elements[-1]

        session, _ = SortingLadderSession.objects.get_or_create(quiz=quiz)
        session.placed_elements.clear()
        session.placed_elements.add(smallest, largest)
        session.active_element = None
        session.current_round = 0
        session.is_round_active = False
        session.reveal_state = SortingLadderSession.REVEAL_ACTIVE

        if time_limit_seconds:
            try:
                session.time_limit_seconds = int(time_limit_seconds)
            except (TypeError, ValueError):
                pass

        session.round_start_time = None
        session.round_end_time = None
        session.save()

        quiz.current_question = topic
        quiz.save()

        return {
            'current_round': session.current_round,
            'time_limit_seconds': session.time_limit_seconds,
            'placed_elements': [
                {'id': smallest.id, 'text': smallest.text},
                {'id': largest.id, 'text': largest.text},
            ],
            'active_element': None,
        }

    def _initialize_question_for_quiz_sync(
        self,
        quiz_id,
        question_id,
        time_limit_seconds=None,
        hub_session_code=None,
        *,
        start_answering=True,
    ):
        """Initialize SortingLadderSession for a specific SortingQuestion.

        This sets a shared shuffled order of items for the current question,
        stores it on the session, and returns a payload for clients.
        """
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            question = SortingQuestion.objects.get(id=question_id, is_active=True)
        except (SortingLadderGame.DoesNotExist, SortingQuestion.DoesNotExist):
            return None
        is_tutorial_round = is_unit_tutorial_question(
            'sorting_ladder',
            self.room_code,
            hub_session_code,
            question.id,
        )
        set_number = (
            None
            if is_tutorial_round
            else _set_number_for_game(quiz, question.id, hub_session_code)
        )

        elements = list(question.elements.all())
        if len(elements) < 2:
            return None

        # Shuffle once for all participants; if a starting_item is defined,
        # ensure it appears first in the shuffled order.
        shuffled = elements[:]
        random.shuffle(shuffled)
        starting_item_id = question.starting_item_id
        if starting_item_id:
            idx = next((i for i, e in enumerate(shuffled) if e.id == starting_item_id), None)
            if idx is not None and idx != 0:
                shuffled.insert(0, shuffled.pop(idx))
        shuffled_ids = [str(e.id) for e in shuffled]

        session, _ = SortingLadderSession.objects.get_or_create(quiz=quiz)
        session.shuffled_item_ids = ",".join(shuffled_ids)
        session.current_round = 1
        session.is_round_active = bool(start_answering)
        session.reveal_state = SortingLadderSession.REVEAL_ACTIVE

        # Determine the effective per-round time limit: explicit override from
        # the admin/session if provided, otherwise fall back to the
        # SortingQuestion.round_time_limit field.
        effective_time_limit = time_limit_seconds if time_limit_seconds is not None else question.round_time_limit

        # Persist the effective time on the session so future rounds and
        # clients have a consistent source of truth.
        try:
            session.time_limit_seconds = int(effective_time_limit)
        except (TypeError, ValueError):
            # If something goes wrong, keep the existing value but avoid crash.
            pass

        if start_answering:
            session.round_start_time = timezone.now()
            session.round_end_time = (
                session.round_start_time
                + timezone.timedelta(seconds=session.time_limit_seconds)
            )
        else:
            session.round_start_time = None
            session.round_end_time = None
        session.placed_elements.clear()
        session.active_element = None
        session.save()
        self._clear_room_pending_round_orders(quiz.room_code)

        # Replay-safety: if the same question is started again in the same quiz,
        # old submissions for that (quiz, question) must not leak into the new run.
        RoundSubmission.objects.filter(quiz=quiz, question=question).delete()

        # Elimination is scoped to the current question/set. Reset it when a
        # new question starts so participants can play the next set.
        quiz.participants.filter(is_eliminated=True).update(is_eliminated=False)
        for participant in quiz.participants.all():
            try:
                participant.calculate_total_score()
            except Exception:
                pass

        quiz.current_question = question
        quiz.save(update_fields=['current_question'])

        return {
            'question': {
                'id': question.id,
                'set_number': set_number,
                'text': question.question_text,
                'description': question.description,
                'upper_label': question.upper_label,
                'lower_label': question.lower_label,
                'points': 0 if is_tutorial_round else question.points,
                'time_limit': effective_time_limit,
                'is_tutorial_round': is_tutorial_round,
            },
            'items': [
                {'id': e.id, 'text': e.text}
                for e in shuffled
            ],
            'time_limit_seconds': effective_time_limit,
            'set_number': set_number,
            'round_number': 1,
            'is_tutorial_round': is_tutorial_round,
            'starts_at': session.round_start_time.isoformat() if session.round_start_time else None,
            'ends_at': session.round_end_time.isoformat() if session.round_end_time else None,
            'server_now': timezone.now().isoformat(),
        }

    @database_sync_to_async
    def initialize_question_for_quiz(self, quiz_id, question_id, time_limit_seconds=None, hub_session_code=None):
        return self._initialize_question_for_quiz_sync(
            quiz_id,
            question_id,
            time_limit_seconds,
            hub_session_code,
        )

    @database_sync_to_async
    def begin_sorting_question_phase(
        self,
        quiz_id,
        question_id,
        time_limit_seconds,
        hub_session_code,
        action,
        *,
        at=None,
    ):
        with transaction.atomic():
            snapshot = current_snapshot(
                'sorting_ladder',
                self.room_code,
                hub_session_code,
            )
            manual_flow = (
                snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            if not manual_flow:
                payload = self._initialize_question_for_quiz_sync(
                    quiz_id,
                    question_id,
                    time_limit_seconds,
                    hub_session_code,
                )
                return payload, QuestionPhaseDecision(
                    bool(payload),
                    'accepted' if payload else 'invalid_action_context',
                    '',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )

            try:
                question = SortingQuestion.objects.get(pk=question_id, is_active=True)
            except SortingQuestion.DoesNotExist:
                return None, QuestionPhaseDecision(False, 'invalid_action_context', 'Frage nicht gefunden.')
            if question.elements.count() < 2:
                return None, QuestionPhaseDecision(False, 'invalid_action_context', 'Zu wenige Elemente.')

            effective_time_limit = (
                time_limit_seconds
                if time_limit_seconds is not None
                else question.round_time_limit
            )
            decision = present_question(
                game_key='sorting_ladder',
                room_code=self.room_code,
                session_code=hub_session_code,
                action={**action, 'question_id': question_id},
                answer_duration_seconds=effective_time_limit,
                at=at,
            )
            if not decision.accepted or decision.duplicate:
                return None, decision
            payload = self._initialize_question_for_quiz_sync(
                quiz_id,
                question_id,
                time_limit_seconds,
                hub_session_code,
                start_answering=False,
            )
            if payload is None:
                transaction.set_rollback(True)
                return None, QuestionPhaseDecision(
                    False,
                    'invalid_action_context',
                    'Die Sorting-Ladder-Runde konnte nicht vorbereitet werden.',
                )
            return payload, decision

    def _save_round_full_order_sync(
        self,
        quiz,
        session,
        participant,
        question,
        ordered_item_ids,
        round_time_out=False,
        allow_after_deadline=False,
    ):
        """Synchronous core implementation used by explicit submits and host-forced resolution."""
        is_tutorial_round = is_unit_tutorial_question(
            'sorting_ladder',
            self.room_code,
            participant.hub_session_code,
            question.id,
        )
        # Ensure shuffled_item_ids is initialized so we can consistently
        # reason about remaining rounds even on a timeout-only submission.
        if not session.shuffled_item_ids:
            all_ids = list(question.elements.values_list('id', flat=True))
            if not all_ids:
                print("No elements found")
                return None
            session.shuffled_item_ids = ",".join(str(i) for i in all_ids)
            session.save(update_fields=['shuffled_item_ids'])

        shuffled_ids = [int(x) for x in session.shuffled_item_ids.split(',') if x]

        # Do not accept submissions if question is no longer active.
        now = timezone.now()
        if (
            not session.is_round_active
            or session.reveal_state != SortingLadderSession.REVEAL_ACTIVE
        ):
            return None
        round_has_timed_out = bool(session.round_end_time and now >= session.round_end_time)
        if round_has_timed_out and not allow_after_deadline:
            return None

        # Ignore submissions from already eliminated participants.
        if participant.is_eliminated:
            return None

        # If the round ended due to timeout, we record a failed submission
        # without requiring any ordered_item_ids and without modifying the
        # shuffled order.
        if round_time_out:
            round_number = max(int(session.current_round or 0), 1)
            if RoundSubmission.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
                round_number=round_number,
            ).exists():
                return None
            _, created = RoundSubmission.objects.get_or_create(
                quiz=quiz,
                participant=participant,
                question=question,
                round_number=round_number,
                defaults={'all_elements': []},
            )
            if not created:
                return None
            if not is_tutorial_round and not participant.is_eliminated:
                participant.is_eliminated = True
                participant.save(update_fields=['is_eliminated'])

            correct_rounds_for_question = RoundSubmission.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
                is_correct=True,
            ).count()
            points_for_question = 0 if is_tutorial_round else correct_rounds_for_question

            total_rounds_for_participant = RoundSubmission.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).count()

            if not is_tutorial_round:
                try:
                    participant.calculate_total_score()
                except Exception:
                    pass

            max_rounds = max(len(shuffled_ids) - 1, 0)
            set_has_more_rounds = round_number < max_rounds
            has_more_rounds = (not participant.is_eliminated) and set_has_more_rounds

            return {
                'question_id': question.id,
                'round_number': round_number,
                'is_correct': False,
                'rounds_survived': participant.rounds_survived,
                'is_eliminated': participant.is_eliminated,
                'points': points_for_question,
                'is_tutorial_round': is_tutorial_round,
                'has_more_rounds': bool(has_more_rounds),
                'set_has_more_rounds': bool(set_has_more_rounds),
                'per_question_rounds': correct_rounds_for_question,
                'correct_order_ids': [],
            }

        try:
            visible_ids = [int(x) for x in ordered_item_ids]
        except (TypeError, ValueError):
            print("Invalid visible IDs")
            return None

        if not visible_ids:
            print("No visible IDs")
            return None

        if len(set(visible_ids)) != len(visible_ids):
            print("Duplicate IDs")
            return None
        if any(i not in shuffled_ids for i in visible_ids):
            print("Invalid IDs")
            return None

        played_rounds = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
        ).count()
        max_rounds = max(len(shuffled_ids) - 1, 0)
        if played_rounds >= max_rounds:
            return None

        expected_round = max(int(session.current_round or 0), 1)
        if RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
            round_number=expected_round,
        ).exists():
            return None
        expected_count = min(expected_round + 1, len(shuffled_ids))
        if len(visible_ids) != expected_count:
            return None

        previous_correct = (
            RoundSubmission.objects
            .filter(
                quiz=quiz,
                participant=participant,
                question=question,
                is_correct=True,
                round_number__lt=expected_round,
            )
            .order_by('-round_number')
            .first()
        )
        if previous_correct and isinstance(previous_correct.all_elements, list) and previous_correct.all_elements:
            try:
                locked_ids = [int(x) for x in previous_correct.all_elements]
            except (TypeError, ValueError):
                locked_ids = []
        else:
            locked_ids = [shuffled_ids[0]] if shuffled_ids else []

        if len(locked_ids) != max(expected_count - 1, 0):
            return None

        new_ids = [i for i in visible_ids if i not in locked_ids]
        if len(new_ids) != 1:
            return None
        reduced_visible = [i for i in visible_ids if i != new_ids[0]]
        if reduced_visible != locked_ids:
            return None

        submission, created = RoundSubmission.objects.get_or_create(
            quiz=quiz,
            participant=participant,
            question=question,
            round_number=expected_round,
            defaults={'all_elements': visible_ids},
        )
        if not created:
            return None

        items = list(SortingItem.objects.filter(id__in=visible_ids))
        if len(items) != len(visible_ids):
            print("Invalid items")
            return None
        rank_map = {item.id: item.correct_rank for item in items}
        sorted_visible_ids = sorted(visible_ids, key=lambda i: rank_map[i])

        if is_tutorial_round:
            pass
        elif submission.is_correct:
            participant.rounds_survived += 1
            participant.save(update_fields=['rounds_survived'])
        else:
            participant.is_eliminated = True
            participant.save(update_fields=['is_eliminated'])

        total_rounds_for_participant = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
        ).count()
        set_has_more_rounds = expected_round < max_rounds
        has_more_rounds = (not participant.is_eliminated) and set_has_more_rounds

        correct_rounds_for_question = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
            is_correct=True,
        ).count()
        points_for_question = 0 if is_tutorial_round else correct_rounds_for_question

        if not is_tutorial_round:
            try:
                participant.calculate_total_score()
            except Exception:
                pass

        return {
            'question_id': question.id,
            'round_number': expected_round,
            'is_correct': submission.is_correct,
            'rounds_survived': participant.rounds_survived,
            'is_eliminated': participant.is_eliminated,
            'points': points_for_question,
            'is_tutorial_round': is_tutorial_round,
            'has_more_rounds': bool(has_more_rounds),
            'set_has_more_rounds': bool(set_has_more_rounds),
            'per_question_rounds': correct_rounds_for_question,
            'correct_order_ids': sorted_visible_ids if set_has_more_rounds else [],
        }

    def _finalize_pending_round_for_participant(self, quiz, session, question, participant):
        current_round = max(int(session.current_round or 0), 1)
        already_submitted = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
            round_number=current_round,
        ).exists()
        if already_submitted:
            self._pop_pending_round_order(
                quiz.room_code,
                question.id,
                current_round,
                participant.name,
                participant.hub_session_code,
            )
            return None

        pending_order = self._pop_pending_round_order(
            quiz.room_code,
            question.id,
            current_round,
            participant.name,
            participant.hub_session_code,
        )

        result = None
        if pending_order:
            result = self._save_round_full_order_sync(
                quiz=quiz,
                session=session,
                participant=participant,
                question=question,
                ordered_item_ids=pending_order,
                round_time_out=False,
                allow_after_deadline=True,
            )

        if not result:
            result = self._save_round_full_order_sync(
                quiz=quiz,
                session=session,
                participant=participant,
                question=question,
                ordered_item_ids=[],
                round_time_out=True,
                allow_after_deadline=True,
            )

        if not result:
            return None

        return {
            'participant_name': participant.name,
            'hub_session_code': participant.hub_session_code,
            'result': result,
        }

    def _start_next_round_sync(self, quiz_id, *, start_answering=True):
        """
        Chooses the next active element and starts the round.
        """
        try:
            quiz = (
                SortingLadderGame.objects.select_for_update()
                .select_related('current_question')
                .get(id=quiz_id)
            )
            session = SortingLadderSession.objects.select_for_update().get(quiz=quiz)
            topic = quiz.current_question
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, AttributeError):
            return None

        if not topic:
            return None
        if session.reveal_state != SortingLadderSession.REVEAL_ACTIVE:
            return None

        # Current question-based flow: advance round marker/timer without
        # changing ordering logic.
        if session.shuffled_item_ids:
            try:
                shuffled_ids = [int(x) for x in session.shuffled_item_ids.split(',') if x]
            except ValueError:
                shuffled_ids = []
            max_rounds = max(len(shuffled_ids) - 1, 0)
            if session.current_round >= max_rounds:
                return None

            auto_results = []
            active_participants = list(
                quiz.participants.filter(is_active=True, is_eliminated=False)
            )
            for participant in active_participants:
                auto_result = self._finalize_pending_round_for_participant(quiz, session, topic, participant)
                if auto_result:
                    auto_results.append(auto_result)

            session.current_round += 1
            session.is_round_active = bool(start_answering)
            if start_answering:
                session.round_start_time = timezone.now()
                session.round_end_time = (
                    session.round_start_time
                    + timezone.timedelta(seconds=session.time_limit_seconds)
                )
            else:
                session.round_start_time = None
                session.round_end_time = None
            session.save(update_fields=['current_round', 'is_round_active', 'round_start_time', 'round_end_time'])

            return {
                'question_id': topic.id,
                'round_number': session.current_round,
                'set_number': _set_number_for_game(
                    quiz,
                    topic.id,
                    self._get_hub_session_code_for_room_sync(),
                ),
                'item_count': len(shuffled_ids),
                'time_limit_seconds': session.time_limit_seconds,
                'starts_at': session.round_start_time.isoformat() if session.round_start_time else None,
                'ends_at': session.round_end_time.isoformat() if session.round_end_time else None,
                'server_now': timezone.now().isoformat(),
                'auto_results': auto_results,
            }

        placed_ids = list(session.placed_elements.values_list('id', flat=True))
        active_id = session.active_element_id

        remaining = topic.elements.exclude(id__in=placed_ids + ([active_id] if active_id else [])) \
                                  .order_by('correct_rank')
        next_element = remaining.first()
        if not next_element:
            return None

        session.start_next_round(next_element)
        if not start_answering:
            session.is_round_active = False
            session.round_start_time = None
            session.round_end_time = None
            session.save(update_fields=['is_round_active', 'round_start_time', 'round_end_time'])

        return {
            'question_id': topic.id,
            'round_number': session.current_round,
            'time_limit_seconds': session.time_limit_seconds,
            'active_element': {
                'id': session.active_element.id,
                'text': session.active_element.text,
            },
            'placed_elements': list(
                session.placed_elements.order_by('correct_rank')
                .values('id', 'text')
            ),
        }

    @database_sync_to_async
    def start_next_round_db(self, quiz_id):
        return self._start_next_round_sync(quiz_id)

    @database_sync_to_async
    def prepare_next_round_phase(self, quiz_id, action):
        with transaction.atomic():
            session_code = self._get_hub_session_code_for_room_sync()
            snapshot = current_snapshot(
                'sorting_ladder',
                self.room_code,
                session_code,
            )
            manual_flow = (
                snapshot.get('question_flow_mode')
                == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            )
            if not manual_flow:
                round_state = self._start_next_round_sync(quiz_id)
                return round_state, QuestionPhaseDecision(
                    bool(round_state),
                    'accepted' if round_state else 'invalid_action_context',
                    '',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )

            try:
                quiz = SortingLadderGame.objects.select_related('session', 'current_question').get(pk=quiz_id)
                current_round = int(quiz.session.current_round or 0)
                question_id = quiz.current_question_id
            except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, AttributeError):
                return None, QuestionPhaseDecision(False, 'invalid_action_context', 'Runde nicht gefunden.')
            set_number = _set_number_for_game(quiz, question_id, session_code)
            try:
                action_matches = (
                    int(action.get('expected_round', action.get('round_id'))) == current_round
                    and int(action.get('expected_set', action.get('set_id'))) == int(set_number or 0)
                    and int(action.get('state_revision')) == int(snapshot.get('state_revision'))
                    and str(action.get('game_id') or '') == str(snapshot.get('game_id') or '')
                )
            except (TypeError, ValueError):
                action_matches = False
            if not action_matches:
                return None, QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Aktion gehoert zu einer anderen Sorting-Ladder-Runde.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )
            if snapshot.get('question_phase') != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN:
                return None, QuestionPhaseDecision(
                    False,
                    'invalid_phase',
                    'Die aktuelle Runde ist noch nicht freigegeben.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )

            round_state = self._start_next_round_sync(quiz_id, start_answering=False)
            if not round_state:
                return None, QuestionPhaseDecision(
                    False,
                    'invalid_action_context',
                    'Es gibt keine weitere Runde.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )
            finished = finish_question_flow(
                game_key='sorting_ladder',
                room_code=self.room_code,
                session_code=session_code,
                question_id=question_id,
            )
            decision = present_question(
                game_key='sorting_ladder',
                room_code=self.room_code,
                session_code=session_code,
                action={
                    **action,
                    'state_revision': finished['state_revision'],
                    'game_id': finished.get('game_id'),
                    'question_id': question_id,
                    'round_id': round_state['round_number'],
                    'set_id': set_number,
                },
                answer_duration_seconds=round_state['time_limit_seconds'],
            )
            if not decision.accepted:
                transaction.set_rollback(True)
                return None, decision
            return round_state, decision

    @database_sync_to_async
    def get_current_round_context(self):
        session_code = self._get_hub_session_code_for_room_sync()
        try:
            quiz = SortingLadderGame.objects.select_related('session', 'current_question').get(
                room_code=self.room_code,
            )
            session = quiz.session
            question = quiz.current_question
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, AttributeError):
            return None
        if not question:
            return None
        item_count = len([item for item in session.shuffled_item_ids.split(',') if item])
        return {
            'question_id': question.id,
            'round_number': max(int(session.current_round or 0), 1),
            'set_number': _set_number_for_game(quiz, question.id, session_code),
            'time_limit': session.time_limit_seconds,
            'item_count': item_count,
            'session_code': session_code,
        }

    @staticmethod
    def _sorting_action_matches_context(action, context):
        try:
            return (
                int(action.get('expected_round', action.get('round_id')))
                == int(context['round_number'])
                and int(action.get('expected_set', action.get('set_id')))
                == int(context['set_number'] or 0)
            )
        except (TypeError, ValueError):
            return False

    @database_sync_to_async
    def reveal_sorting_round(self, context, action, *, at=None):
        if not self._sorting_action_matches_context(action, context):
            snapshot = current_snapshot(
                'sorting_ladder',
                self.room_code,
                context['session_code'],
            )
            return QuestionPhaseDecision(
                False,
                'stale_action',
                'Die Aktion gehoert zu einer anderen Sorting-Ladder-Runde.',
                state_revision=snapshot.get('state_revision'),
                snapshot=snapshot,
            )
        return reveal_question_content(
            game_key='sorting_ladder',
            room_code=self.room_code,
            session_code=context['session_code'],
            action={**action, 'question_id': context['question_id']},
            at=at,
        )

    @database_sync_to_async
    def open_sorting_round(self, context, action, *, at=None):
        with transaction.atomic():
            if not self._sorting_action_matches_context(action, context):
                snapshot = current_snapshot(
                    'sorting_ladder',
                    self.room_code,
                    context['session_code'],
                )
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Aktion gehoert zu einer anderen Sorting-Ladder-Runde.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )
            snapshot = current_snapshot(
                'sorting_ladder',
                self.room_code,
                context['session_code'],
            )
            revealed_at = parse_datetime(str(snapshot.get('content_revealed_at') or ''))
            ready_at = sorting_ladder_reveal_ready_at(
                content_revealed_at=revealed_at,
                item_count=context['item_count'],
                round_number=context['round_number'],
            )
            transition_at = at or timezone.now()
            if not ready_at or transition_at < ready_at:
                return QuestionPhaseDecision(
                    False,
                    'content_reveal_in_progress',
                    'Leiter und Elemente sind noch nicht vollstaendig enthuellt.',
                    state_revision=snapshot.get('state_revision'),
                    snapshot=snapshot,
                )
            decision = open_answering(
                game_key='sorting_ladder',
                room_code=self.room_code,
                session_code=context['session_code'],
                action={**action, 'question_id': context['question_id']},
                answer_duration_seconds=context['time_limit'],
                at=transition_at,
            )
            if not decision.accepted or decision.duplicate:
                return decision
            started_at = parse_datetime(str(decision.snapshot.get('answering_started_at') or ''))
            ends_at = parse_datetime(str(decision.snapshot.get('answering_deadline_at') or ''))
            try:
                quiz = SortingLadderGame.objects.select_for_update().get(room_code=self.room_code)
                session = SortingLadderSession.objects.select_for_update().get(quiz=quiz)
            except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist):
                transaction.set_rollback(True)
                return QuestionPhaseDecision(False, 'stale_action', 'Runde nicht gefunden.')
            if (
                quiz.current_question_id != context['question_id']
                or int(session.current_round or 0) != int(context['round_number'])
                or not started_at
                or not ends_at
            ):
                transaction.set_rollback(True)
                return QuestionPhaseDecision(
                    False,
                    'stale_action',
                    'Die Sorting-Ladder-Runde hat sich geaendert.',
                    state_revision=decision.state_revision,
                    snapshot=decision.snapshot,
                )
            session.is_round_active = True
            session.round_start_time = started_at
            session.round_end_time = ends_at
            session.save(update_fields=['is_round_active', 'round_start_time', 'round_end_time'])
            return decision

    @database_sync_to_async
    def finish_sorting_round_phase(self, question_id):
        session_code = self._get_hub_session_code_for_room_sync()
        snapshot = current_snapshot('sorting_ladder', self.room_code, session_code)
        if (
            snapshot.get('question_flow_mode')
            != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            or snapshot.get('question_phase')
            != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
        ):
            return snapshot
        return finish_question_flow(
            game_key='sorting_ladder',
            room_code=self.room_code,
            session_code=session_code,
            question_id=question_id,
        )

    async def broadcast_round_answer_status(self, quiz_id):
        status_payload = await self.get_round_answer_status_db(quiz_id)
        if not status_payload:
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'round_answer_status',
                **status_payload,
            }
        )

    @database_sync_to_async
    def get_round_answer_status_db(self, quiz_id):
        try:
            quiz = SortingLadderGame.objects.select_related('session', 'current_question').get(id=quiz_id)
            session = quiz.session
            question = quiz.current_question
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, AttributeError):
            return None

        if not question:
            return None

        current_round = max(int(session.current_round or 0), 1)
        try:
            shuffled_ids = [int(x) for x in session.shuffled_item_ids.split(',') if x]
        except ValueError:
            shuffled_ids = []
        expected_count = min(current_round + 1, len(shuffled_ids)) if shuffled_ids else 0
        active_participants_qs = quiz.participants.filter(is_active=True, is_eliminated=False)
        active_hub_session_code = self._get_hub_session_code_for_room_sync()
        if active_hub_session_code:
            active_participants_qs = active_participants_qs.filter(hub_session_code=active_hub_session_code)
        active_participants = list(
            active_participants_qs.order_by('name').values('id', 'name', 'hub_session_code')
        )

        statuses = []
        for participant in active_participants:
            submissions_count = RoundSubmission.objects.filter(
                quiz=quiz,
                participant_id=participant['id'],
                question=question,
            ).count()
            pending_order = self._get_pending_round_order(
                quiz.room_code,
                question.id,
                current_round,
                participant['name'],
                participant.get('hub_session_code'),
            )
            has_logged_answer = submissions_count >= current_round
            has_pending_selection = expected_count > 0 and len(pending_order) == expected_count
            statuses.append({
                'participant_name': participant['name'],
                'has_answered': has_logged_answer or has_pending_selection,
                'has_selection': has_pending_selection,
                'is_logged': has_logged_answer,
                'answer_status': 'eingeloggt' if has_logged_answer else ('gegeben' if has_pending_selection else 'offen'),
            })

        return {
            'round_number': current_round,
            'statuses': statuses,
        }

    @database_sync_to_async
    def end_round_db(self, quiz_id):
        """
        Ends the current round and returns list of surviving participants.
        """
        try:
            quiz = SortingLadderGame.objects.select_related('session').get(id=quiz_id)
            session = quiz.session
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, AttributeError):
            return []

        session.end_round()

        survivors = list(
            quiz.participants.filter(is_eliminated=False, is_active=True)
            .values('id', 'name', 'rounds_survived')
        )

        has_next_round = False
        if quiz.current_question and session.shuffled_item_ids:
            try:
                shuffled_ids = [int(x) for x in session.shuffled_item_ids.split(',') if x]
            except ValueError:
                shuffled_ids = []
            max_rounds = max(len(shuffled_ids) - 1, 0)
            has_next_round = session.current_round < max_rounds

        return {
            'survivors': survivors,
            'has_next_round': bool(has_next_round),
        }

    @database_sync_to_async
    def end_question_db(self, quiz_id):
        """Atomically complete the final round without revealing its solution."""
        with transaction.atomic():
            try:
                quiz = (
                    SortingLadderGame.objects
                    .select_for_update()
                    .select_related('current_question')
                    .get(id=quiz_id)
                )
                session = SortingLadderSession.objects.select_for_update().get(quiz=quiz)
            except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist):
                return None

            question = quiz.current_question
            if not question:
                return None

            if session.reveal_state == SortingLadderSession.REVEAL_REVEALED:
                return {
                    'status': SortingLadderSession.REVEAL_REVEALED,
                    'transitioned': False,
                }
            if session.reveal_state == SortingLadderSession.REVEAL_AWAITING:
                return {
                    'status': SortingLadderSession.REVEAL_AWAITING,
                    'transitioned': False,
                }

            try:
                shuffled_ids = [int(value) for value in session.shuffled_item_ids.split(',') if value]
            except ValueError:
                shuffled_ids = []
            max_rounds = max(len(shuffled_ids) - 1, 0)
            if max_rounds <= 0 or int(session.current_round or 0) < max_rounds:
                return {
                    'status': 'rounds_remaining',
                    'transitioned': False,
                }

            auto_results = []
            if session.is_round_active:
                active_participants = list(
                    quiz.participants.filter(is_active=True, is_eliminated=False)
                )
                for participant in active_participants:
                    auto_result = self._finalize_pending_round_for_participant(
                        quiz,
                        session,
                        question,
                        participant,
                    )
                    if auto_result:
                        auto_results.append(auto_result)

            session.is_round_active = False
            session.round_end_time = timezone.now()
            session.reveal_state = SortingLadderSession.REVEAL_AWAITING
            session.save(update_fields=[
                'is_round_active',
                'round_end_time',
                'reveal_state',
            ])

        self._clear_room_pending_round_orders(quiz.room_code)
        resolved_session_code = self._get_hub_session_code_for_room_sync()
        is_tutorial_round = is_unit_tutorial_question(
            'sorting_ladder',
            self.room_code,
            resolved_session_code,
            question.id,
        )
        unit_tutorial = finish_current_unit_tutorial(
            'sorting_ladder',
            self.room_code,
            resolved_session_code,
        )
        return {
            'status': SortingLadderSession.REVEAL_AWAITING,
            'transitioned': True,
            'auto_results': auto_results,
            'is_tutorial_round': bool(unit_tutorial.get('is_tutorial_round')) or is_tutorial_round,
        }

    @database_sync_to_async
    def reveal_solution_db(self, quiz_id):
        """Move an awaiting set to revealed and return its solution once."""
        with transaction.atomic():
            try:
                quiz = (
                    SortingLadderGame.objects
                    .select_for_update()
                    .select_related('current_question')
                    .get(id=quiz_id)
                )
                session = SortingLadderSession.objects.select_for_update().get(quiz=quiz)
            except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist):
                return None

            question = quiz.current_question
            if not question or session.reveal_state != SortingLadderSession.REVEAL_AWAITING:
                return {
                    'transitioned': False,
                    'status': session.reveal_state,
                }

            correct_order_ids = list(
                SortingItem.objects.filter(topic=question)
                .order_by('correct_rank')
                .values_list('id', flat=True)
            )
            session.reveal_state = SortingLadderSession.REVEAL_REVEALED
            session.save(update_fields=['reveal_state'])

        resolved_session_code = self._get_hub_session_code_for_room_sync()
        return {
            'transitioned': True,
            'status': SortingLadderSession.REVEAL_REVEALED,
            'correct_order_ids': correct_order_ids,
            'is_tutorial_round': is_unit_tutorial_question(
                'sorting_ladder',
                self.room_code,
                resolved_session_code,
                question.id,
            ),
        }

    @database_sync_to_async
    def clear_revealed_question_db(self, quiz_id):
        """Clear a revealed set so the host can select the next one."""
        with transaction.atomic():
            try:
                quiz = SortingLadderGame.objects.select_for_update().get(id=quiz_id)
                session = SortingLadderSession.objects.select_for_update().get(quiz=quiz)
            except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist):
                return False

            if (
                not quiz.current_question_id
                or session.reveal_state != SortingLadderSession.REVEAL_REVEALED
            ):
                return False

            quiz.current_question = None
            quiz.save(update_fields=['current_question'])
            session.reveal_state = SortingLadderSession.REVEAL_ACTIVE
            session.is_round_active = False
            session.current_round = 0
            session.round_start_time = None
            session.round_end_time = None
            session.shuffled_item_ids = ''
            session.save(update_fields=[
                'reveal_state',
                'is_round_active',
                'current_round',
                'round_start_time',
                'round_end_time',
                'shuffled_item_ids',
            ])

        self._clear_room_pending_round_orders(quiz.room_code)
        return True

    @database_sync_to_async
    def get_or_create_participant(self, name, hub_session_code):
        try:
            quiz = SortingLadderGame.objects.get(room_code=self.room_code)
        except SortingLadderGame.DoesNotExist:
            return None

        participant, _ = SortingLadderParticipant.objects.get_or_create(
            quiz=quiz,
            name=name,
            hub_session_code=hub_session_code,
        )
        participant.is_active = True
        participant.save()

        return {
            'id': participant.id,
            'name': participant.name,
            'rounds_survived': participant.rounds_survived,
            'is_eliminated': participant.is_eliminated,
        }

    @database_sync_to_async
    def get_rejoin_snapshot(self, participant_name, hub_session_code):
        """Return minimal server-authoritative snapshot for participant rejoin."""
        try:
            quiz = SortingLadderGame.objects.select_related('session', 'current_question').get(room_code=self.room_code)
            session = quiz.session
            question = quiz.current_question
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, SortingLadderParticipant.DoesNotExist, AttributeError):
            return {}

        if not question or not session.shuffled_item_ids:
            return {}

        try:
            shuffled_ids = [int(x) for x in session.shuffled_item_ids.split(',') if x]
        except ValueError:
            return {}
        if not shuffled_ids:
            return {}

        item_map = {item.id: item for item in SortingItem.objects.filter(id__in=shuffled_ids)}
        shuffled_items = [item_map[i] for i in shuffled_ids if i in item_map]
        if not shuffled_items:
            return {}
        is_tutorial_round = is_unit_tutorial_question(
            'sorting_ladder',
            self.room_code,
            hub_session_code,
            question.id,
        )
        set_number = (
            None
            if is_tutorial_round
            else _set_number_for_game(quiz, question.id, hub_session_code)
        )
        phase_snapshot = current_snapshot(
            'sorting_ladder',
            self.room_code,
            hub_session_code,
        )
        manual_flow = (
            phase_snapshot.get('question_flow_mode')
            == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        )

        question_payload = {
            'question': {
                'id': question.id,
                'set_number': set_number,
                'text': question.question_text,
                'description': question.description,
                'upper_label': question.upper_label,
                'lower_label': question.lower_label,
                'points': 0 if is_tutorial_round else question.points,
                'time_limit': session.time_limit_seconds,
                'is_tutorial_round': is_tutorial_round,
            },
            'items': [{'id': i.id, 'text': i.text} for i in shuffled_items],
            'time_limit_seconds': session.time_limit_seconds,
            'set_number': set_number,
            'round_number': max(int(session.current_round or 0), 1),
            'is_tutorial_round': is_tutorial_round,
            'starts_at': (
                phase_snapshot.get('answering_started_at')
                if manual_flow
                else session.round_start_time.isoformat()
                if session.round_start_time
                else None
            ),
            'ends_at': (
                phase_snapshot.get('answering_deadline_at')
                if manual_flow
                else session.round_end_time.isoformat()
                if session.round_end_time
                else None
            ),
            'server_now': timezone.now().isoformat(),
        }
        question_payload.update(self.question_lifecycle_fields(phase_snapshot))
        question_payload.update(self.sorting_reveal_fields(
            item_count=len(shuffled_items),
            round_number=session.current_round,
            snapshot=phase_snapshot,
        ))

        submissions = list(
            RoundSubmission.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).order_by('submitted_at')
        )
        latest_round_result = None
        round_started = None
        max_rounds = max(len(shuffled_ids) - 1, 0)
        reveal_order_ids = []
        if session.reveal_state == SortingLadderSession.REVEAL_REVEALED:
            reveal_order_ids = list(
                SortingItem.objects.filter(topic=question)
                .order_by('correct_rank')
                .values_list('id', flat=True)
            )

        if submissions:
            latest = submissions[-1]
            correct_rounds = sum(1 for s in submissions if s.is_correct)
            points_for_question = 0 if is_tutorial_round else correct_rounds
            has_more_rounds = (not participant.is_eliminated) and len(submissions) < max_rounds

            visible_ids = latest.all_elements if isinstance(latest.all_elements, list) else []
            try:
                visible_ids = [int(x) for x in visible_ids]
            except (TypeError, ValueError):
                visible_ids = []

            if visible_ids:
                vis_items = list(SortingItem.objects.filter(id__in=visible_ids))
                vis_rank = {i.id: i.correct_rank for i in vis_items}
                sorted_visible = sorted([i for i in visible_ids if i in vis_rank], key=lambda i: vis_rank[i])
            else:
                sorted_visible = []

            result_round = latest.round_number
            set_has_more_rounds = result_round < max_rounds

            latest_round_result = {
                'question_id': question.id,
                'round_number': result_round,
                'is_correct': latest.is_correct,
                'rounds_survived': participant.rounds_survived,
                'is_eliminated': participant.is_eliminated,
                'points': points_for_question,
                'is_tutorial_round': is_tutorial_round,
                'has_more_rounds': bool(has_more_rounds),
                'set_has_more_rounds': bool(set_has_more_rounds),
                'per_question_rounds': correct_rounds,
                'correct_order_ids': sorted_visible if set_has_more_rounds else [],
                'progress_history': self._build_participant_progress_history_sync(
                    quiz,
                    participant,
                    reveal_eliminated_current_question=True,
                ),
            }

        # If participant has not submitted the current server round yet and is
        # still active, explicitly sync round start so interaction unlocks.
        if (
            manual_flow
            and session.reveal_state == SortingLadderSession.REVEAL_ACTIVE
            and int(session.current_round or 0) > 1
            and phase_snapshot.get('question_phase') in {
                GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
                GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE,
                GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN,
            }
        ):
            round_started = {
                'question_id': question.id,
                'round_number': session.current_round,
                'set_number': set_number,
                'item_count': len(shuffled_items),
                'time_limit_seconds': session.time_limit_seconds,
                'is_tutorial_round': is_tutorial_round,
                'starts_at': phase_snapshot.get('answering_started_at'),
                'ends_at': phase_snapshot.get('answering_deadline_at'),
                'server_now': timezone.now().isoformat(),
                **self.question_lifecycle_fields(phase_snapshot),
                **self.sorting_reveal_fields(
                    item_count=len(shuffled_items),
                    round_number=session.current_round,
                    snapshot=phase_snapshot,
                ),
            }
        elif (
            session.reveal_state == SortingLadderSession.REVEAL_ACTIVE
            and session.is_round_active
            and not participant.is_eliminated
            and not any(
                submission.round_number == int(session.current_round or 0)
                for submission in submissions
            )
        ):
            round_started = {
                'question_id': question.id,
                'round_number': session.current_round,
                'time_limit_seconds': session.time_limit_seconds,
                'is_tutorial_round': is_tutorial_round,
                'starts_at': (
                    session.round_start_time.isoformat()
                    if session.round_start_time
                    else None
                ),
                'ends_at': (
                    session.round_end_time.isoformat()
                    if session.round_end_time
                    else None
                ),
                'server_now': timezone.now().isoformat(),
            }

        pending_order = self._get_pending_round_order(
            quiz.room_code,
            question.id,
            max(int(session.current_round or 0), 1),
            participant.name,
            participant.hub_session_code,
        )
        if session.reveal_state == SortingLadderSession.REVEAL_REVEALED:
            phase = 'solution'
        elif session.reveal_state == SortingLadderSession.REVEAL_AWAITING:
            phase = 'waiting_reveal'
        elif latest_round_result and not round_started:
            phase = 'round_result'
        elif round_started:
            phase = 'answer_locked' if any(
                submission.round_number == int(session.current_round or 0)
                for submission in submissions
            ) else 'active_round'
        else:
            phase = 'waiting'

        return {
            'phase': phase,
            'question_payload': question_payload,
            'latest_round_result': latest_round_result,
            'round_started': round_started,
            'reveal_state': session.reveal_state,
            'reveal_order_ids': reveal_order_ids,
            'own_selection': pending_order or [],
            'answer_locked': phase == 'answer_locked',
            'starts_at': (
                session.round_start_time.isoformat()
                if session.round_start_time
                else None
            ),
            'ends_at': (
                session.round_end_time.isoformat()
                if session.round_end_time
                else None
            ),
            'server_now': timezone.now().isoformat(),
            **self.question_lifecycle_fields(phase_snapshot),
        }

    @database_sync_to_async
    def update_pending_round_selection_db(self, participant_name, hub_session_code, ordered_item_ids):
        try:
            quiz = SortingLadderGame.objects.select_related('session', 'current_question').get(room_code=self.room_code)
            session = quiz.session
            question = quiz.current_question
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, SortingLadderParticipant.DoesNotExist, AttributeError):
            return False

        if quiz.status != 'active':
            return False
        if (
            not question
            or not session.is_round_active
            or session.reveal_state != SortingLadderSession.REVEAL_ACTIVE
            or participant.is_eliminated
        ):
            return False

        try:
            normalized_ids = [int(x) for x in ordered_item_ids]
        except (TypeError, ValueError):
            normalized_ids = []

        try:
            shuffled_ids = [int(x) for x in session.shuffled_item_ids.split(',') if x]
        except ValueError:
            shuffled_ids = []

        if len(set(normalized_ids)) != len(normalized_ids):
            normalized_ids = []
        elif shuffled_ids and any(item_id not in shuffled_ids for item_id in normalized_ids):
            normalized_ids = []

        self._set_pending_round_order(
            quiz.room_code,
            question.id,
            max(int(session.current_round or 0), 1),
            participant_name,
            hub_session_code,
            normalized_ids,
        )
        return True

    @database_sync_to_async
    def save_round_submission(self, participant_name, hub_session_code, placed_after_id, placed_before_id):
        # Legacy gap-placement flow intentionally disabled:
        # RoundSubmission no longer has element/placed_* fields.
        return None

    @database_sync_to_async
    @transaction.atomic
    def save_round_full_order(self, participant_name, hub_session_code, ordered_item_ids, round_time_out=False):
        """Validate and persist a participant's result for this round."""
        try:
            quiz = (
                SortingLadderGame.objects.select_for_update()
                .select_related('current_question')
                .get(room_code=self.room_code)
            )
            session = SortingLadderSession.objects.select_for_update().get(quiz=quiz)
            participant = SortingLadderParticipant.objects.select_for_update().get(
                quiz=quiz,
                name=participant_name,
                hub_session_code=hub_session_code,
            )
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, SortingLadderParticipant.DoesNotExist, AttributeError):
            return None

        if quiz.status != 'active':
            return None
        question = quiz.current_question
        if not question:
            print("No question found")
            return None

        if round_time_out and not ordered_item_ids:
            pending_order = self._get_pending_round_order(
                quiz.room_code,
                question.id,
                max(int(session.current_round or 0), 1),
                participant_name,
                hub_session_code,
            )
            if pending_order:
                ordered_item_ids = pending_order
                round_time_out = False

        result = self._save_round_full_order_sync(
            quiz=quiz,
            session=session,
            participant=participant,
            question=question,
            ordered_item_ids=ordered_item_ids,
            round_time_out=round_time_out,
        )
        if result:
            self._pop_pending_round_order(
                quiz.room_code,
                question.id,
                result.get('round_number'),
                participant_name,
                hub_session_code,
            )
        return result

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = SortingLadderGame.objects.get(room_code=self.room_code)
        except SortingLadderGame.DoesNotExist:
            return []

        qs = quiz.participants.order_by('-rounds_survived', 'name') \
                              .values('name', 'rounds_survived', 'is_eliminated')
        return list(qs)

    @database_sync_to_async
    def get_participant_progress_history(self, participant_name, hub_session_code):
        try:
            quiz = SortingLadderGame.objects.select_related('current_question', 'session').get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
        except (SortingLadderGame.DoesNotExist, SortingLadderParticipant.DoesNotExist):
            return []

        return self._build_participant_progress_history_sync(quiz, participant)

    @database_sync_to_async
    def get_participant_progress_history_for_round_result(self, participant_name, hub_session_code):
        try:
            quiz = SortingLadderGame.objects.select_related('current_question', 'session').get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
        except (SortingLadderGame.DoesNotExist, SortingLadderParticipant.DoesNotExist):
            return []

        return self._build_participant_progress_history_sync(
            quiz,
            participant,
            reveal_eliminated_current_question=True,
        )

    def _build_participant_progress_history_sync(self, quiz, participant, reveal_eliminated_current_question=False):
        tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids(
            'sorting_ladder',
            self.room_code,
            participant.hub_session_code,
        )
        submissions = (
            RoundSubmission.objects
            .filter(quiz=quiz, participant=participant)
            .select_related('question')
            .order_by('submitted_at', 'id')
        )

        grouped = {}
        for sub in submissions:
            if sub.question_id in tutorial_question_ids:
                continue
            qid = sub.question_id
            entry = grouped.get(qid)
            if not entry:
                max_rounds = max(sub.question.elements.count() - 1, 0)
                entry = {
                    'question_id': qid,
                    'first_submitted_at': sub.submitted_at,
                    'survived_rounds': 0,
                    'max_rounds': max_rounds,
                    'submission_count': 0,
                }
                grouped[qid] = entry
            entry['submission_count'] += 1
            if sub.is_correct:
                entry['survived_rounds'] += 1

        try:
            session = quiz.session
            current_round = int(session.current_round or 0)
        except (SortingLadderSession.DoesNotExist, AttributeError, TypeError, ValueError):
            current_round = 0

        completed = []
        current_question_id = quiz.current_question_id
        for entry in grouped.values():
            is_current_question = entry['question_id'] == current_question_id
            if not is_current_question:
                completed.append(entry)
                continue

            if participant.is_eliminated:
                should_reveal_current = (
                    reveal_eliminated_current_question
                    or current_round > entry['submission_count']
                )
                if should_reveal_current:
                    completed.append(entry)
                continue

            if entry['submission_count'] >= entry['max_rounds']:
                completed.append(entry)

        completed.sort(key=lambda e: (e['first_submitted_at'], e['question_id']))

        history = []
        for index, entry in enumerate(completed, start=1):
            history.append({
                'question_number': index,
                'survived_rounds': entry['survived_rounds'],
                'max_rounds': entry['max_rounds'],
            })
        return history

    # --- Hub mirroring helpers ---

    def _get_hub_session_code_for_room_sync(self):
        try:
            qs = HubGameStep.objects.select_related('session') \
                .filter(game_key='sorting_ladder', room_code=self.room_code)
            active = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
            step = active or qs.order_by('-id').first()
            return step.session.code if step else None
        except Exception:
            return None

    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        return self._get_hub_session_code_for_room_sync()

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
