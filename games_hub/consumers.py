import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from django.core.cache import cache
from .authoritative_consumer import AuthoritativeGameConsumerMixin
from .lobby_join import check_nickname_availability, join_lobby_participant
from .models import HubSession, HubParticipant, HubGameStep
from .active_game_guard import is_game_routable_for_hub_auto_redirect, resolve_session_game_activation
from .game_intro import get_game_step, serialize_game_step, start_game_intro
from .check_in import (
    complete_session_check_in,
    get_check_in_state,
    participant_check_in,
    reset_session_check_in,
    start_session_check_in,
)
from .readiness import (
    end_readiness_check,
    get_readiness_state,
    mark_participant_ready,
    start_readiness_check,
)
from QuizGame.models import Quiz as QuizGameModel
from Assign.models import AssignQuiz
from Estimation.models import EstimationQuiz
from where_is_this.models import WhereQuiz
from who_is_lying.models import WhoQuiz
from who_is_that.models import WhoThatQuiz
from black_jack_quiz.models import BlackJackQuiz
from clue_rush.models import ClueRushGame
from sorting_ladder.models import SortingLadderGame
from wer_weiss_mehr.models import WerWeissMehrGame
from buzzer.models import BuzzerGame
from host_points.models import HostPointsGame
from wann_war_das.models import WannWarDasGame
from .voting import get_voting_state, submit_session_vote


logger = logging.getLogger(__name__)


class HubConsumer(AuthoritativeGameConsumerMixin, AsyncWebsocketConsumer):
    authoritative_scope_kind = 'lobby'

    async def connect(self):
        self.session_code = self.scope['url_route']['kwargs']['session_code']
        self.group_name = f"hub_{self.session_code}"
        self.hub_participant_id = None
        self.hub_participant_name = None
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.info(
            'Hub lobby socket connected',
            extra={
                'hub_session_code': self.session_code,
                'hub_channel_name': self.channel_name,
                'lobby_connected_at': timezone.now().isoformat(),
            },
        )
        await self.send_json({'type': 'connection_established', 'message': 'Connected to hub', 'session_code': self.session_code})

    async def disconnect(self, close_code):
        logger.info(
            'Hub lobby socket disconnected without changing participant presence',
            extra={
                'hub_session_code': self.session_code,
                'hub_participant_id': self.hub_participant_id,
                'hub_channel_name': self.channel_name,
                'lobby_disconnected_at': timezone.now().isoformat(),
                'close_code': close_code,
            },
        )
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json({'type': 'error', 'message': 'Invalid JSON'})
            return

        msg_type = data.get('type')
        if msg_type == 'join':
            await self.handle_join(data)
        elif msg_type == 'check_nickname':
            await self.handle_check_nickname(data)
        elif msg_type == 'start_session':
            await self.handle_start_session()
        elif msg_type == 'next_step':
            await self.handle_next_step()
        elif msg_type == 'broadcast':
            await self.channel_layer.group_send(self.group_name, {'type': 'hub_event', 'event': data.get('event', {})})
        elif msg_type == 'navigate_to_game':
            await self.handle_navigate_to_game(data)
        elif msg_type == 'navigate_direct':
            await self.handle_navigate_direct(data)
        elif msg_type == 'recall_to_lobby':
            await self.channel_layer.group_send(self.group_name, {'type': 'recall_to_lobby'})
        elif msg_type == 'inactivate_session':
            await self.handle_inactivate_session()
        elif msg_type == 'end_session':
            await self.handle_end_session()
        elif msg_type == 'toggle_scoreboard':
            await self.handle_toggle_scoreboard()
        elif msg_type == 'vote':
            await self.handle_vote(data)
        elif msg_type == 'start_check_in':
            await self.handle_start_check_in()
        elif msg_type == 'participant_check_in':
            await self.handle_participant_check_in(data)
        elif msg_type == 'complete_check_in':
            await self.handle_complete_check_in(data)
        elif msg_type == 'reset_check_in':
            await self.handle_reset_check_in()
        elif msg_type == 'start_readiness_check':
            await self.handle_start_readiness_check()
        elif msg_type == 'participant_ready':
            await self.handle_participant_ready(data)
        elif msg_type == 'end_readiness_check':
            await self.handle_end_readiness_check(data)
        elif msg_type == 'get_state':
            await self.send_state()
        elif msg_type == 'ping':
            await self.send_json({'type': 'pong'})

    async def handle_join(self, data):
        nickname = data.get('nickname')
        result = await database_sync_to_async(join_lobby_participant)(
            self.session_code,
            nickname,
            data.get('rejoin_token') or '',
        )
        if not result.get('success'):
            self.hub_participant_id = None
            self.hub_participant_name = None
            self._authoritative_participant = None
            await self.send_json({
                'type': 'join_error',
                'code': result.get('code', 'join_failed'),
                'message': result.get('message', 'Der Beitritt ist derzeit nicht möglich.'),
            })
            return
        nickname = result['nickname']
        self.hub_participant_id = result['participant_id']
        self.hub_participant_name = nickname
        self._authoritative_participant = nickname
        
        # Determine if there's an active game to redirect the participant
        game_key, room_code = await self.get_active_game_for_session()
        active_step = (
            await self.get_game_step_for_room(game_key, room_code)
            if game_key and room_code
            else None
        )
        
        # Notify the joining client immediately
        payload = {
            'type': 'lobby_join_success',
            'game_key': game_key,
            'room_code': room_code,
            'nickname': nickname,
            'rejoined': result['rejoined'],
            'rejoin_token': result['rejoin_token'],
            'step': active_step,
        }
        await self.send_json(payload)
        
        # Notify others and send updated state
        await self.channel_layer.group_send(self.group_name, {'type': 'lobby_update'})
        await self.send_state()

    async def handle_check_nickname(self, data):
        result = await database_sync_to_async(check_nickname_availability)(
            self.session_code,
            data.get('candidate_name'),
            data.get('rejoin_token') or '',
        )
        await self.send_json({
            'type': 'nickname_status',
            'request_id': data.get('request_id'),
            **result,
        })

    async def handle_start_session(self):
        await self.start_session_db()
        await self.pause_games_for_inactive_sessions()
        await self.channel_layer.group_send(self.group_name, {'type': 'session_started'})

    async def handle_next_step(self):
        print("Handle Next Step")
        await self.advance_step_db()
        await self.handle_navigate_to_current()

    async def handle_navigate_to_current(self):
        step = await self.get_current_step()
        await self.channel_layer.group_send(self.group_name, {'type': 'navigate', 'step': step})

    async def handle_navigate_to_game(self, data):
        index = data.get('index')
        await self.set_step_index(index)
        await self.handle_navigate_to_current()

    async def handle_navigate_direct(self, data):
        """Broadcast a navigate event with an explicit game selection.
        Expected payload: { type: 'navigate_direct', game_key: str, room_code: str }
        Only redirects participants if the game is actually started and not completed.
        """
        game_key = data.get('game_key')
        room_code = data.get('room_code')
        if not game_key or not room_code:
            await self.send_json({'type': 'error', 'message': 'Missing game_key or room_code'})
            return

        game_route_state = await self.get_game_route_state(game_key, room_code)
        if game_route_state.get('status') == 'completed':
            # Host navigates on their own; participants stay in the lobby
            return
        if not game_route_state.get('routable'):
            # The real quiz_started mirror event performs participant routing
            # after the game has been started in the game-specific backend.
            return
        activation = await self.activate_game_for_session(game_key, room_code)
        if not activation.get('success'):
            payload = {
                'type': 'active_game_conflict' if activation.get('conflict') else 'error',
                'message': activation.get('message') or activation.get('error') or 'Unable to activate game.',
            }
            if activation.get('check_in_required'):
                payload['check_in_required'] = True
                payload['check_in_status'] = activation.get('check_in_status')
                payload['locked_participant_count'] = activation.get('locked_participant_count')
            if activation.get('active_game'):
                payload['active_game'] = activation['active_game']
            await self.send_json(payload)
            return

        step = await self.start_game_intro_for_room(game_key, room_code)
        if not step:
            step = {
                'index': -1,
                'order': -1,
                'game_key': game_key,
                'room_code': room_code,
                'title': '',
                'intro': {'intro_active': False},
            }
        await self.channel_layer.group_send(self.group_name, {'type': 'navigate', 'step': step})

    async def send_state(self):
        state = await self.get_state()
        await self.send_json({'type': 'state', **state})

    async def send_json(self, payload):
        await self.send(text_data=json.dumps(payload))

    # group events
    async def lobby_update(self, event):
        await self.send_state()

    async def session_started(self, event):
        await self.send_json({'type': 'session_started'})

    async def session_inactivated(self, event):
        await self.send_json({
            'type': 'session_inactivated',
            'session_code': event.get('session_code', self.session_code),
        })

    async def navigate(self, event):
        await self.send_json({'type': 'navigate', 'step': event.get('step')})

    async def hub_event(self, event):
        ev = event.get('event', {})
        etype = ev.get('type')
        print("Hub Consumer:", etype, "event", ev)

        # Generate a per-connection event key so duplicate suppression does not
        # block delivery to other clients in the same hub session.
        event_key = f"{self.channel_name}:{etype}:{ev.get('game_key')}:{ev.get('room_code')}"

        # Check if this event was already processed recently
        if cache.get(event_key):
            print(f"Duplicate event ignored: {event_key}")
            return  # ignore duplicate

        # Mark this event as processed for 5 seconds (adjust as needed)
        cache.set(event_key, True, timeout=5)

        print("Hub Consumer: event_key", event_key)
        print("Hub Consumer: etype", etype)
        print("Hub Consumer: ev", ev)
        # If a game ended, ensure we add/record it as a step for Game Flow when launched via navigate_direct
        if etype in ('quiz_started', 'game_ended') and ev.get('game_key') and ev.get('room_code'):
            await self.ensure_step_for_room(ev.get('game_key'), ev.get('room_code'), ev.get('title', ''))

        # Forward to clients
        await self.send_json({'type': 'event', **ev})

        # When a game starts, redirect all lobby participants to the play page
        if etype == 'quiz_started' and ev.get('game_key') and ev.get('room_code'):
            step = await self.start_game_intro_for_room(
                ev.get('game_key'),
                ev.get('room_code'),
            )
            if not step:
                step = {
                    'index': -1,
                    'order': -1,
                    'game_key': ev.get('game_key'),
                    'room_code': ev.get('room_code'),
                    'title': ev.get('title', ''),
                    'intro': {'intro_active': False},
                }
            await self.channel_layer.group_send(self.group_name, {'type': 'navigate', 'step': step})

        # If a game signals it ended, auto-advance or end session
        # if etype in ('quiz_ended', 'game_ended'):
        #     current_step = await self.get_current_step()
        #     # if (
        #     #     current_step and
        #     #     current_step.get('game_key') == ev.get('game_key') and
        #     #     current_step.get('room_code') == ev.get('room_code')
        #     # ):
        #     if await self._is_last_step():
        #         await self.end_session_db()
        #         await self.channel_layer.group_send(
        #             self.group_name,
        #             {'type': 'session_ended'}
        #         )
        #     else:
        #         await self.advance_step_db()
        #         await self.handle_navigate_to_current()
            # else:
            #     print(f"Ignoring {etype} event for non-current game")
                
    async def recall_to_lobby(self, event):
        await self.send_json({'type': 'recall_to_lobby', 'session_code': self.session_code})

    async def lobby_return_countdown_started(self, event):
        await self.send_json({
            'type': 'lobby_return_countdown_started',
            'session_code': event.get('session_code', self.session_code),
            'duration_seconds': event.get('duration_seconds'),
            'ends_at': event.get('ends_at'),
            'server_now': event.get('server_now'),
        })

    async def players_recalled_to_lobby(self, event):
        await self.send_json({
            'type': 'players_recalled_to_lobby',
            'session_code': event.get('session_code', self.session_code),
        })

    async def session_ended(self, event):
        """Handle session ended event"""
        await self.send_json({
            'type': 'session_ended',
            'message': 'The game session has ended',
            'session_code': self.session_code
        })
        # Close the connection after a short delay to ensure the message is sent
        await self.close(code=1000)

    # db helpers
    @database_sync_to_async
    def start_session_db(self):
        try:
            HubSession.activate_exclusive(self.session_code)
        except HubSession.DoesNotExist:
            pass

    @database_sync_to_async
    def inactivate_session_db(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}

        if session.ended_at:
            return {'success': False, 'error': 'Session ist bereits beendet.'}

        if session.is_active:
            session.is_active = False
            session.save(update_fields=['is_active'])
        HubSession.pause_active_games_for_session_codes([self.session_code])
        return {'success': True}

    @database_sync_to_async
    def pause_games_for_inactive_sessions(self):
        """
        If a hub session is inactive (but not ended), any currently active game
        that belongs to that session is marked inactive.
        """
        inactive_session_codes = list(
            HubSession.objects.filter(is_active=False, ended_at__isnull=True).values_list('code', flat=True)
        )
        if not inactive_session_codes:
            return

        for model in self.get_game_model_map().values():
            model.objects.filter(
                status='active',
                room_code__in=HubGameStep.objects.filter(
                    session__code__in=inactive_session_codes
                ).exclude(room_code='').values_list('room_code', flat=True),
            ).update(status='inactive')

    @database_sync_to_async
    def advance_step_db(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
            total = session.steps.count()
            if total == 0:
                return
            
            print("Advancing step for session", self.session_code, "from", session.current_step_index, "to", session.current_step_index + 1, "total", total)
            session.current_step_index = min(session.current_step_index + 1, total - 1)
            session.save()
        except HubSession.DoesNotExist:
            pass

    @database_sync_to_async
    def set_step_index(self, index):
        try:
            session = HubSession.objects.get(code=self.session_code)
            total = session.steps.count()
            if total == 0:
                return
            if index is None:
                return
            session.current_step_index = max(0, min(index, total - 1))
            session.save()
        except HubSession.DoesNotExist:
            pass

    @database_sync_to_async
    def _is_last_step(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
            total = session.steps.count()
            if total == 0:
                return True
            return session.current_step_index >= total - 1
        except HubSession.DoesNotExist:
            return True

    @database_sync_to_async
    def end_session_db(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
            if not session.ended_at:
                session.ended_at = timezone.now()
                session.is_active = False
                session.save()
        except HubSession.DoesNotExist:
            pass

    async def handle_vote(self, data):
        nickname = self.hub_participant_name
        step_order = data.get('step_order')
        if not nickname or step_order is None:
            return
        result = await self.save_vote(nickname, step_order)
        if not result.get('success'):
            await self.send_json({
                'type': 'voting_error',
                'error': result.get('error') or 'Voting fehlgeschlagen.',
            })
            return
        state = await self.get_vote_state()
        await self.channel_layer.group_send(self.group_name, {'type': 'voting_update', 'state': state})

    async def vote_update(self, event):
        await self.send_json({'type': 'vote_update', 'votes': event['votes']})

    async def voting_update(self, event):
        state = event.get('state') or {}
        await self.send_json({'type': 'vote_update', **state})

    @database_sync_to_async
    def save_vote(self, nickname, step_order):
        try:
            session = HubSession.objects.get(code=self.session_code)
            return submit_session_vote(session, nickname, step_order)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}

    @database_sync_to_async
    def get_vote_state(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
            return get_voting_state(session)
        except HubSession.DoesNotExist:
            return {'success': False, 'votes': []}

    async def handle_toggle_scoreboard(self):
        visible = await self.toggle_scoreboard_db()
        await self.channel_layer.group_send(
            self.group_name,
            {'type': 'scoreboard_visibility', 'visible': visible}
        )

    async def scoreboard_visibility(self, event):
        await self.send_json({'type': 'scoreboard_visibility', 'visible': event['visible']})

    async def check_in_update(self, event):
        await self.send_json({
            'type': event.get('event_type', 'check_in_update'),
            **(event.get('state') or {}),
        })

    async def readiness_update(self, event):
        await self.send_json({
            'type': event.get('event_type', 'readiness_update'),
            **(event.get('state') or {}),
        })

    @database_sync_to_async
    def toggle_scoreboard_db(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
            session.scoreboard_visible = not session.scoreboard_visible
            session.save(update_fields=['scoreboard_visible'])
            return session.scoreboard_visible
        except HubSession.DoesNotExist:
            return False

    async def handle_end_session(self):
        await self.complete_games_for_session()
        await self.end_session_db()
        await self.channel_layer.group_send(self.group_name, {'type': 'session_ended'})

    async def handle_inactivate_session(self):
        result = await self.inactivate_session_db()
        if not result.get('success'):
            await self.send_json({
                'type': 'error',
                'message': result.get('error') or 'Session konnte nicht inaktiv gesetzt werden.',
            })
            return
        await self.channel_layer.group_send(
            self.group_name,
            {'type': 'session_inactivated', 'session_code': self.session_code},
        )

    async def _broadcast_check_in_result(self, result, event_type):
        if result.get('success'):
            await self.channel_layer.group_send(
                self.group_name,
                {
                    'type': 'check_in_update',
                    'event_type': event_type,
                    'state': {
                        'check_in': result.get('check_in', {}),
                        'participants': result.get('participants', []),
                        'counts': result.get('counts', {}),
                    },
                },
            )
            return
        await self.send_json({
            'type': 'check_in_error',
            'error': result.get('error') or 'Check-in fehlgeschlagen.',
        })

    async def handle_start_check_in(self):
        result = await self.start_check_in_db()
        await self._broadcast_check_in_result(result, 'check_in_started')

    async def handle_participant_check_in(self, data):
        nickname = self.hub_participant_name
        if not nickname:
            await self.send_json({
                'type': 'check_in_error',
                'error': 'Die Teilnahme wurde noch nicht bestätigt.',
            })
            return
        result = await self.participant_check_in_db(nickname)
        await self._broadcast_check_in_result(result, 'participant_checked_in')

    async def handle_complete_check_in(self, data):
        result = await self.complete_check_in_db(bool(data.get('allow_empty')))
        await self._broadcast_check_in_result(result, 'check_in_completed')

    async def handle_reset_check_in(self):
        result = await self.reset_check_in_db()
        await self._broadcast_check_in_result(result, 'check_in_reset')

    async def _broadcast_readiness_result(self, result, event_type):
        if result.get('success'):
            await self.channel_layer.group_send(
                self.group_name,
                {
                    'type': 'readiness_update',
                    'event_type': event_type,
                    'state': {
                        'readiness_check': result.get('readiness_check', {}),
                        'readiness_participants': result.get('readiness_participants', []),
                        'readiness_counts': result.get('readiness_counts', {}),
                    },
                },
            )
            return
        if result.get('requires_confirmation'):
            await self.send_json({
                'type': 'readiness_end_requires_confirmation',
                'error': result.get('error'),
                'readiness_check': result.get('readiness_check', {}),
                'readiness_participants': result.get('readiness_participants', []),
                'readiness_counts': result.get('readiness_counts', {}),
            })
            return
        await self.send_json({
            'type': 'readiness_error',
            'error': result.get('error') or 'Bereitschaftscheck fehlgeschlagen.',
        })

    async def handle_start_readiness_check(self):
        result = await self.start_readiness_check_db()
        await self._broadcast_readiness_result(result, 'readiness_started')

    async def handle_participant_ready(self, data):
        nickname = self.hub_participant_name
        if not nickname:
            await self.send_json({
                'type': 'readiness_error',
                'error': 'Die Teilnahme wurde noch nicht bestätigt.',
            })
            return
        result = await self.participant_ready_db(nickname)
        await self._broadcast_readiness_result(result, 'readiness_participant_ready')

    async def handle_end_readiness_check(self, data):
        result = await self.end_readiness_check_db(force=bool(data.get('force')))
        await self._broadcast_readiness_result(result, 'readiness_ended')

    @database_sync_to_async
    def get_current_step(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
            steps = list(session.steps.order_by('order', 'pk'))
            if not steps:
                return None
            idx = max(0, min(session.current_step_index, len(steps) - 1))
            return serialize_game_step(steps[idx])
        except HubSession.DoesNotExist:
            return None

    @database_sync_to_async
    def start_game_intro_for_room(self, game_key, room_code):
        return start_game_intro(self.session_code, game_key, room_code)

    @database_sync_to_async
    def get_game_step_for_room(self, game_key, room_code):
        return get_game_step(self.session_code, game_key, room_code)

    @database_sync_to_async
    def activate_game_for_session(self, game_key, room_code):
        return resolve_session_game_activation(self.session_code, game_key, room_code)

    @database_sync_to_async
    def complete_games_for_session(self):
        """
        When a session ends, mark all active/inactive games that belong to this
        session as completed.
        """
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return

        session_room_codes = list(
            session.steps.exclude(room_code='').values_list('room_code', flat=True)
        )
        if not session_room_codes:
            return

        for model in self.get_game_model_map().values():
            model.objects.filter(
                room_code__in=session_room_codes,
                status__in=['active', 'inactive'],
            ).update(status='completed', ended_at=timezone.now())

    @database_sync_to_async
    def start_check_in_db(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}
        return start_session_check_in(session)

    @database_sync_to_async
    def participant_check_in_db(self, nickname):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}
        return participant_check_in(session, nickname)

    @database_sync_to_async
    def complete_check_in_db(self, allow_empty=False):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}
        return complete_session_check_in(session, allow_empty=allow_empty)

    @database_sync_to_async
    def reset_check_in_db(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}
        return reset_session_check_in(session)

    @database_sync_to_async
    def get_state(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
            participants = list(session.participants.values('nickname'))
            server_now = timezone.now()
            steps = [
                serialize_game_step(step, server_now=server_now)
                for step in session.steps.order_by('order', 'pk')
            ]
            next_game_number = session.get_next_game_number()
            check_in_state = get_check_in_state(session)
            voting_state = get_voting_state(session)
            readiness_state = get_readiness_state(session)
            return {
                'session': {'code': session.code, 'name': session.name, 'started_at': session.started_at is not None},
                'participants': participants,
                'steps': steps,
                'current_step_index': session.current_step_index,
                'next_game_number': next_game_number,
                'next_game_complete': next_game_number is None,
                'scoreboard_visible': session.scoreboard_visible,
                **voting_state,
                **check_in_state,
                **readiness_state,
            }
        except HubSession.DoesNotExist:
            return {'error': 'session_not_found'}

    @database_sync_to_async
    def start_readiness_check_db(self):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}
        return start_readiness_check(session)

    @database_sync_to_async
    def participant_ready_db(self, nickname):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}
        return mark_participant_ready(session, nickname)

    @database_sync_to_async
    def end_readiness_check_db(self, force=False):
        try:
            session = HubSession.objects.get(code=self.session_code)
        except HubSession.DoesNotExist:
            return {'success': False, 'error': 'Session nicht gefunden.'}
        return end_readiness_check(session, force=force)

    @database_sync_to_async
    def get_game_route_state(self, game_key, room_code):
        """Return routing-relevant state for a game instance."""
        model_map = {
            'quiz': QuizGameModel,
            'assign': AssignQuiz,
            'estimation': EstimationQuiz,
            'where': WhereQuiz,
            'who': WhoQuiz,
            'who_that': WhoThatQuiz,
            'blackjack': BlackJackQuiz,
            'clue_rush': ClueRushGame,
            'sorting_ladder': SortingLadderGame,
            'wer_weiss_mehr': WerWeissMehrGame,
            'buzzer': BuzzerGame,
            'host_points': HostPointsGame,
            'wann_war_das': WannWarDasGame,
        }
        model = model_map.get(game_key)
        if not model:
            return {'status': None, 'routable': False}
        game = model.objects.filter(room_code=room_code).first()
        return {
            'status': getattr(game, 'status', None) if game else None,
            'routable': is_game_routable_for_hub_auto_redirect(game),
        }

    @database_sync_to_async
    def get_active_game_for_session(self):
        """Return (game_key, room_code) for the first step whose game is currently active."""
        model_map = {
            'quiz': QuizGameModel,
            'assign': AssignQuiz,
            'estimation': EstimationQuiz,
            'where': WhereQuiz,
            'who': WhoQuiz,
            'who_that': WhoThatQuiz,
            'blackjack': BlackJackQuiz,
            'clue_rush': ClueRushGame,
            'sorting_ladder': SortingLadderGame,
            'wer_weiss_mehr': WerWeissMehrGame,
            'buzzer': BuzzerGame,
            'host_points': HostPointsGame,
            'wann_war_das': WannWarDasGame,
        }
        try:
            session = HubSession.objects.get(code=self.session_code)
            for step in session.steps.filter(room_code__isnull=False).exclude(room_code='').order_by('order'):
                model = model_map.get(step.game_key)
                if not model:
                    continue
                game = model.objects.filter(room_code=step.room_code).first()
                if is_game_routable_for_hub_auto_redirect(game):
                    return step.game_key, step.room_code
            return None, None
        except HubSession.DoesNotExist:
            return None, None

    @database_sync_to_async
    def ensure_step_for_room(self, game_key: str, room_code: str, title: str = ''):
        try:
            session = HubSession.objects.get(code=self.session_code)
            exists = session.steps.filter(game_key=game_key, room_code=room_code).exists()
            if exists:
                return
            order = session.steps.count()
            HubGameStep.objects.create(session=session, order=order, game_key=game_key, room_code=room_code, title=title)
        except HubSession.DoesNotExist:
            return

    @database_sync_to_async
    def reset_all_quizzes_to_waiting(self):
        # Reset all quizzes across game types to 'waiting' so they appear as available
        QuizGameModel.objects.update(status='waiting')
        AssignQuiz.objects.update(status='waiting')
        EstimationQuiz.objects.update(status='waiting')
        WhereQuiz.objects.update(status='waiting')
        WhoQuiz.objects.update(status='waiting')
        WhoThatQuiz.objects.update(status='waiting')
        BlackJackQuiz.objects.update(status='waiting')
        ClueRushGame.objects.update(status='waiting')
        SortingLadderGame.objects.update(status='waiting')
        WerWeissMehrGame.objects.update(status='waiting')
        BuzzerGame.objects.update(status='waiting')
        HostPointsGame.objects.update(status='waiting')
        WannWarDasGame.objects.update(status='waiting')

    def get_game_model_map(self):
        return {
            'quiz': QuizGameModel,
            'assign': AssignQuiz,
            'estimation': EstimationQuiz,
            'where': WhereQuiz,
            'who': WhoQuiz,
            'who_that': WhoThatQuiz,
            'blackjack': BlackJackQuiz,
            'clue_rush': ClueRushGame,
            'sorting_ladder': SortingLadderGame,
            'wer_weiss_mehr': WerWeissMehrGame,
            'buzzer': BuzzerGame,
            'host_points': HostPointsGame,
            'wann_war_das': WannWarDasGame,
        }
