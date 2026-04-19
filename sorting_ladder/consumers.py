import json
import asyncio
import random
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone

from .models import (
    SortingLadderGame,
    SortingLadderParticipant,
    SortingQuestion,
    SortingItem,
    RoundSubmission,
    SortingLadderSession,
)
from games_hub.models import HubGameStep, HubSession


class SortingLadderGameConsumer(AsyncWebsocketConsumer):
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
        elif msg_type == 'admin_send_question':
            await self.handle_admin_send_question(data)
        elif msg_type == 'admin_end_question':
            await self.handle_admin_end_question(data)
        elif msg_type == 'participant_join':
            await self.handle_participant_join(data)
        elif msg_type == 'participant_submit_move':
            # Legacy move submission (gap placement). Kept for backwards compatibility.
            await self.handle_participant_submit_move(data)
        elif msg_type == 'participant_submit_round':
            await self.handle_participant_submit_round(data)
        elif msg_type == 'ping':
            await self.handle_ping()
        elif msg_type == 'admin_show_leaderboard':
            await self.handle_admin_show_leaderboard()
        elif msg_type == 'admin_hide_leaderboard':
            await self.handle_admin_hide_leaderboard()

    # -------- Admin handlers --------

    async def handle_admin_start_quiz(self, data):
        quiz = await self.get_quiz()
        if not quiz:
            return
        await self.start_quiz_db(quiz.id)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'quiz_started',
                'message': 'Sorting Ladder quiz has started!'
            }
        )

        await self.hub_mirror_event('quiz_started', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
        })

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
                    'title': topic.title,
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

        round_state = await self.start_next_round_db(quiz.id)
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

        survivors = await self.end_round_db(quiz.id)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'round_ended',
                'survivors': survivors
            }
        )

        await self.hub_mirror_event('round_ended', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
            'survivors': survivors,
        })

    async def handle_admin_end_quiz(self, data):
        """
        Ends the entire game and broadcasts the final standings (rounds survived).
        """
        quiz = await self.get_quiz()
        if not quiz:
            return

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

        payload = await self.initialize_question_for_quiz(
            quiz_id=quiz.id,
            question_id=question_id,
            time_limit_seconds=custom_time_limit,
        )
        if not payload:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Unable to start question. Ensure it has at least 2 items.',
            }))
            return

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

    async def handle_admin_end_question(self, data):
        """Ends the current question early, preventing further round submissions."""
        quiz = await self.get_quiz()
        if not quiz:
            return

        await self.end_question_db(quiz.id)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'question_ended',
                'message': 'Question has ended.',
            }
        )

        await self.hub_mirror_event('question_ended', {
            'room_code': self.room_code,
            'game_key': 'sorting_ladder',
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

        # If game is already active, send quiz_started directly to this participant
        game = await self.get_quiz()
        if game and game.status == 'active':
            await self.send(text_data=json.dumps({
                'type': 'quiz_started',
                'message': 'Game is already in progress'
            }))

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
            # Could be duplicate submission or no active round
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
            return

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

    async def round_ended(self, event):
        await self.send(text_data=json.dumps({
            'type': 'round_ended',
            'survivors': event['survivors'],
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

    async def question_started(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_started',
            **event['payload'],
        }))

    async def question_ended(self, event):
        await self.send(text_data=json.dumps({
            'type': 'question_ended',
            'message': event.get('message', ''),
        }))

    async def round_result(self, event):
        await self.send(text_data=json.dumps({
            'type': 'round_result',
            'participant_name': event['participant_name'],
            'is_correct': event['is_correct'],
            'rounds_survived': event['rounds_survived'],
            'is_eliminated': event['is_eliminated'],
            'points': event['points'],
            'has_more_rounds': event['has_more_rounds'],
            'per_question_rounds': event.get('per_question_rounds'),
            'correct_order_ids': event.get('correct_order_ids'),
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
    def start_quiz_db(self, quiz_id):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            quiz.start_quiz()
        except SortingLadderGame.DoesNotExist:
            pass

    @database_sync_to_async
    def end_quiz_db(self, quiz_id):
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            quiz.end_quiz()
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

    @database_sync_to_async
    def initialize_question_for_quiz(self, quiz_id, question_id, time_limit_seconds=None):
        """Initialize SortingLadderSession for a specific SortingQuestion.

        This sets a shared shuffled order of items for the current question,
        stores it on the session, and returns a payload for clients.
        """
        try:
            quiz = SortingLadderGame.objects.get(id=quiz_id)
            question = SortingQuestion.objects.get(id=question_id, is_active=True)
        except (SortingLadderGame.DoesNotExist, SortingQuestion.DoesNotExist):
            return None

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
        session.is_round_active = True

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

        session.round_start_time = timezone.now()
        session.round_end_time = timezone.now() + timezone.timedelta(seconds=session.time_limit_seconds)
        session.placed_elements.clear()
        session.active_element = None
        session.save()

        quiz.current_question = question
        quiz.save(update_fields=['current_question'])

        return {
            'question': {
                'id': question.id,
                'text': question.question_text,
                'description': question.description,
                'upper_label': question.upper_label,
                'lower_label': question.lower_label,
                'points': question.points,
                'time_limit': effective_time_limit,
            },
            'items': [
                {'id': e.id, 'text': e.text}
                for e in shuffled
            ],
            'time_limit_seconds': effective_time_limit,
        }

    @database_sync_to_async
    def start_next_round_db(self, quiz_id):
        """
        Chooses the next active element and starts the round.
        """
        try:
            quiz = SortingLadderGame.objects.select_related('session', 'current_question').get(id=quiz_id)
            session = quiz.session
            topic = quiz.current_question
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, AttributeError):
            return None

        if not topic:
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

            session.current_round += 1
            session.is_round_active = True
            session.round_start_time = timezone.now()
            session.round_end_time = timezone.now() + timezone.timedelta(seconds=session.time_limit_seconds)
            session.save(update_fields=['current_round', 'is_round_active', 'round_start_time', 'round_end_time'])

            return {
                'round_number': session.current_round,
                'time_limit_seconds': session.time_limit_seconds,
            }

        placed_ids = list(session.placed_elements.values_list('id', flat=True))
        active_id = session.active_element_id

        remaining = topic.elements.exclude(id__in=placed_ids + ([active_id] if active_id else [])) \
                                  .order_by('correct_rank')
        next_element = remaining.first()
        if not next_element:
            return None

        session.start_next_round(next_element)

        return {
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
        active_participants = list(
            quiz.participants.filter(is_active=True, is_eliminated=False)
            .order_by('name')
            .values('id', 'name')
        )

        statuses = []
        for participant in active_participants:
            submissions_count = RoundSubmission.objects.filter(
                quiz=quiz,
                participant_id=participant['id'],
                question=question,
            ).count()
            statuses.append({
                'participant_name': participant['name'],
                'has_answered': submissions_count >= current_round,
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

        survivors = quiz.participants.filter(is_eliminated=False, is_active=True) \
                                     .values('id', 'name', 'rounds_survived')
        return list(survivors)

    @database_sync_to_async
    def end_question_db(self, quiz_id):
        """Mark the current question as ended on the session."""
        try:
            quiz = SortingLadderGame.objects.select_related('session').get(id=quiz_id)
            session = quiz.session
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, AttributeError):
            return

        # Reset current question to None
        quiz.current_question = None
        quiz.save()
        
        session.is_round_active = False
        session.round_end_time = timezone.now()
        session.save(update_fields=['is_round_active', 'round_end_time'])

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
    def save_round_submission(self, participant_name, hub_session_code, placed_after_id, placed_before_id):
        try:
            quiz = SortingLadderGame.objects.select_related('session').get(room_code=self.room_code)
            session = quiz.session
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, SortingLadderParticipant.DoesNotExist, AttributeError):
            return None

        if not session.is_round_active or not session.active_element:
            return None

        existing = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            element=session.active_element,
        ).first()
        if existing:
            return None

        after_item = None
        before_item = None
        if placed_after_id:
            try:
                after_item = SortingItem.objects.get(id=placed_after_id)
            except SortingItem.DoesNotExist:
                pass
        if placed_before_id:
            try:
                before_item = SortingItem.objects.get(id=placed_before_id)
            except SortingItem.DoesNotExist:
                pass

        submission = RoundSubmission.objects.create(
            quiz=quiz,
            participant=participant,
            element=session.active_element,
            placed_after_element=after_item,
            placed_before_element=before_item,
        )

        participant.refresh_from_db()

        return {
            'is_correct': submission.is_correct,
            'rounds_survived': participant.rounds_survived,
            'is_eliminated': participant.is_eliminated,
        }

    @database_sync_to_async
    def save_round_full_order(self, participant_name, hub_session_code, ordered_item_ids, round_time_out=False):
        """Validate and persist a participant's result for this round.

        When ``round_time_out`` is False, this behaves as a normal round
        submission, validating the submitted ordering and computing
        correctness based on ``SortingItem.correct_rank``.

        When ``round_time_out`` is True, no ordering is required; we record
        a failed ``RoundSubmission`` for the current question, advance the
        round counter, and leave the shuffled item order unchanged.
        """
        try:
            quiz = SortingLadderGame.objects.select_related('session', 'current_question').get(room_code=self.room_code)
            session = quiz.session
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session_code)
        except (SortingLadderGame.DoesNotExist, SortingLadderSession.DoesNotExist, SortingLadderParticipant.DoesNotExist, AttributeError):
            return None

        question = quiz.current_question
        if not question:
            print("No question found")
            return None

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
        if not session.is_round_active or (session.round_end_time and now > session.round_end_time):
            return None

        # Ignore submissions from already eliminated participants.
        if participant.is_eliminated:
            return None

        # If the round ended due to timeout, we record a failed submission
        # without requiring any ordered_item_ids and without modifying the
        # shuffled order.
        if round_time_out:
            submission = RoundSubmission.objects.create(
                quiz=quiz,
                participant=participant,
                question=question,
                all_elements=[],
            )
            if not participant.is_eliminated:
                participant.is_eliminated = True
                participant.save(update_fields=['is_eliminated'])

            # Per-question points for this participant in this quiz/question
            correct_rounds_for_question = RoundSubmission.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
                is_correct=True,
            ).count()
            points_for_question = correct_rounds_for_question * question.points

            # Total rounds played (including this timeout) for this
            # participant and question. This is used to determine whether
            # they personally have more rounds available, independent of
            # other participants.
            total_rounds_for_participant = RoundSubmission.objects.filter(
                quiz=quiz,
                participant=participant,
                question=question,
            ).count()

            # Refresh the participant's overall total_score across the quiz
            try:
                participant.calculate_total_score()
            except Exception:
                # Scoring errors should not break the round flow
                pass

            # There are more rounds for THIS participant as long as the
            # number of rounds they have already played (submissions,
            # including timeouts) is strictly less than the total number of
            # items for this question. This caps rounds per participant and
            # avoids relying on the shared session.current_round.
            print("total_rounds_for_participant", total_rounds_for_participant)
            print("shuffled ids", shuffled_ids)
            print("shuffled ids len", len(shuffled_ids))
            has_more_rounds = (not participant.is_eliminated) and total_rounds_for_participant < len(shuffled_ids) - 1

            full_sorted_ids = list(SortingItem.objects.filter(topic=question).order_by('correct_rank'))
            full_sorted_ids = [item.id for item in full_sorted_ids]

            return {
                'is_correct': False,
                'rounds_survived': participant.rounds_survived,
                'is_eliminated': participant.is_eliminated,
                'points': points_for_question,
                'has_more_rounds': bool(has_more_rounds),
                'per_question_rounds': correct_rounds_for_question,
                'correct_order_ids': [] if bool(has_more_rounds) else full_sorted_ids,
            }

        # Parse shared shuffled order from session. This is treated as the
        # master list of all item IDs for this question, but we no longer
        # require the submitted ladder to match its prefix.
        try:
            shuffled_ids = [int(x) for x in session.shuffled_item_ids.split(',') if x]
        except ValueError:
            print("Invalid shuffled IDs")
            return None

        try:
            visible_ids = [int(x) for x in ordered_item_ids]
        except (TypeError, ValueError):
            print("Invalid visible IDs")
            return None

        if not visible_ids:
            print("No visible IDs")
            return None

        # Ensure submitted IDs are unique and belong to the master list
        if len(set(visible_ids)) != len(visible_ids):
            print("Duplicate IDs")
            return None
        if any(i not in shuffled_ids for i in visible_ids):
            print("Invalid IDs")
            return None

        # Prevent duplicate submissions for the same round by this participant.
        current_round = max(int(session.current_round or 1), 1)
        played_rounds = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
        ).count()
        if played_rounds >= current_round:
            return None

        # Enforce ladder growth by exactly one item per round and keep
        # previously locked items immutable.
        expected_count = min(current_round + 1, len(shuffled_ids))
        if len(visible_ids) != expected_count:
            return None

        previous_correct = (
            RoundSubmission.objects
            .filter(
                quiz=quiz,
                participant=participant,
                question=question,
                is_correct=True,
            )
            .order_by('-submitted_at')
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

        # Persist this round as a RoundSubmission row. RoundSubmission.save()
        # will compute the is_correct flag from the submitted ordering based
        # on SortingItem.correct_rank.
        submission = RoundSubmission.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            all_elements=visible_ids,
        )

        # Compute the correct ordered prefix for the ladder.
        items = list(SortingItem.objects.filter(id__in=visible_ids))
        if len(items) != len(visible_ids):
            print("Invalid items")
            return None
        rank_map = {item.id: item.correct_rank for item in items}
        sorted_visible_ids = sorted(visible_ids, key=lambda i: rank_map[i])

        # Update participant progression based on correctness
        if submission.is_correct:
            participant.rounds_survived += 1
            participant.save(update_fields=['rounds_survived'])
        else:
            participant.is_eliminated = True
            participant.save(update_fields=['is_eliminated'])

        # Total rounds played (including this one) for this participant and
        # question. This determines if they personally can play more rounds.
        total_rounds_for_participant = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
        ).count()

        # Determine if more rounds are possible for this participant. We cap
        # rounds by the number of items available for this question so each
        # player only ever gets a fixed number of rounds, independent of
        # other players' progress.
        print("total_rounds_for_participant", total_rounds_for_participant)
        print("shuffled ids", shuffled_ids)
        print("shuffled ids len", len(shuffled_ids))
        has_more_rounds = (not participant.is_eliminated) and total_rounds_for_participant < len(shuffled_ids) - 1

        # Per-question points for this participant in this quiz/question
        # = (number of correct RoundSubmission rows) * question.points
        correct_rounds_for_question = RoundSubmission.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
            is_correct=True,
        ).count()
        points_for_question = correct_rounds_for_question * question.points

        # Refresh the participant's overall total_score across the quiz
        try:
            participant.calculate_total_score()
        except Exception:
            # Scoring errors should not break the round flow
            pass

        full_sorted_ids = list(
            SortingItem.objects.filter(topic=question).order_by('correct_rank').values_list('id', flat=True)
        )

        return {
            'is_correct': submission.is_correct,
            'rounds_survived': participant.rounds_survived,
            'is_eliminated': participant.is_eliminated,
            'points': points_for_question,
            'has_more_rounds': bool(has_more_rounds),
            'per_question_rounds': correct_rounds_for_question,
            'correct_order_ids': sorted_visible_ids if bool(has_more_rounds) else full_sorted_ids,
        }

    @database_sync_to_async
    def get_final_scores(self):
        try:
            quiz = SortingLadderGame.objects.get(room_code=self.room_code)
        except SortingLadderGame.DoesNotExist:
            return []

        qs = quiz.participants.order_by('-rounds_survived', 'name') \
                              .values('name', 'rounds_survived', 'is_eliminated')
        return list(qs)

    # --- Hub mirroring helpers ---

    @database_sync_to_async
    def _get_hub_session_code_for_room(self):
        try:
            qs = HubGameStep.objects.select_related('session') \
                .filter(game_key='sorting_ladder', room_code=self.room_code)
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
