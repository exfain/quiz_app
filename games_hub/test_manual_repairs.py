import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User, AnonymousUser
from django.db import connections
from django.test import TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone

from buzzer.models import BuzzerGame
from buzzer.consumers import BuzzerConsumer
from host_points.models import HostPointsGame
from . import manual_game_runtime as runtime
from .models import HubSession, HubGameStep, HubParticipant, HubGameParticipantSnapshot
from .lobby_join import issue_rejoin_token


def with_transport_context(consumer, game, code):
    """Give direct handler fixtures the same captured context as native clients."""
    from channels.db import database_sync_to_async
    def payload(data, kind):
        data = dict(data)
        data.setdefault('type', kind)
        data.setdefault('hub_session', code)
        state = game.serialize_state(data['hub_session'])
        for key, value in {'game_id': state['game_id'], 'state_revision': state['state_revision'],
                'question_id': state.get('current_question_id'), 'round_id': state.get('current_round_id'),
                'set_id': state.get('current_set_id'), 'client_action_id': str(uuid.uuid4()),
                'buzz_id': state.get('round', {}).get('current_buzz_id')}.items():
            data.setdefault(key, value)
        name = data.get('participant_name') or data.get('name')
        if name:
            player = HubParticipant.objects.filter(session__code=data['hub_session'], nickname=name).first()
            if player:
                data.setdefault('player_token', issue_rejoin_token(player))
        return data
    for name in dir(consumer):
        if not name.startswith('handle_admin_') and name not in {'handle_participant_join', 'handle_participant_buzz'}:
            continue
        original = getattr(consumer, name)
        async def call(data, _original=original, _kind=name.removeprefix('handle_')):
            return await _original(await database_sync_to_async(payload)(data, _kind))
        setattr(consumer, name, call)
    return consumer


class ManualGameRepairs(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('manual-host', '', 'pw')
        self.hub = HubSession.objects.create(code='MANUAL1', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=2)
        self.hub_players = [HubParticipant.objects.create(session=self.hub, nickname=name,
            scoring_eligible=True, checked_in_at=timezone.now()) for name in ['Alice', 'Bob']]
        self.buzzer = BuzzerGame.objects.create(creator=self.user, points_per_correct=3, planned_rounds=1)
        self.points = HostPointsGame.objects.create(creator=self.user)
        for number, (key, game) in enumerate([('buzzer', self.buzzer), ('host_points', self.points)]):
            HubGameStep.objects.create(session=self.hub, game_key=key, room_code=game.room_code, order=number)
        self.client.force_login(self.user)

    def start(self, game):
        game.start_quiz(self.hub.code)
        return list(game.participants.order_by('name'))

    def open_buzzer(self):
        players = self.start(self.buzzer)
        self.buzzer.start_round(self.hub.code)
        self.buzzer.open_buzzer()
        return players

    def action(self, game, kind):
        state = game.serialize_state(self.hub.code)
        return {'type': kind, 'hub_session': self.hub.code, 'game_id': state['game_id'],
            'round_id': state['current_round_id'], 'set_id': state.get('current_set_id'),
            'question_id': state.get('current_question_id'), 'state_revision': state['state_revision'],
            'client_action_id': str(uuid.uuid4()), 'buzz_id': state.get('round', {}).get('current_buzz_id')}

    def buzz_body(self, player):
        return {**self.action(self.buzzer, 'participant_buzz'), 'participant_name': player.name,
            'player_token': issue_rejoin_token(self.hub_players[0 if player.name == 'Alice' else 1])}

    def test_concurrent_buzzes_have_one_winner_and_one_clean_rejection(self):
        players = self.open_buzzer()
        bodies = [self.buzz_body(player) for player in players]
        barrier = Barrier(2)
        def buzz(body):
            try:
                barrier.wait(timeout=5)
                return Client().post(reverse('buzzer:buzz', args=[self.buzzer.room_code]),
                    json.dumps(body), content_type='application/json').status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(buzz, bodies))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.buzzer.buzzes.count(), 1)

    def test_answered_round_cannot_be_reopened_or_scored_twice(self):
        first, second = self.open_buzzer()
        self.buzzer.accept_buzz(first)
        self.assertTrue(self.buzzer.mark_current_correct())
        self.assertFalse(self.buzzer.mark_current_wrong())
        self.assertFalse(self.buzzer.mark_current_correct())
        self.assertFalse(self.buzzer.accept_buzz(second)[0])
        self.assertEqual(list(self.buzzer.participants.order_by('name').values_list('total_score', flat=True)), [3, 0])

    def test_stale_host_decision_is_bound_to_buzz_attempt(self):
        first, second = self.open_buzzer()
        self.buzzer.accept_buzz(first)
        action = self.action(self.buzzer, 'admin_mark_correct')
        self.buzzer.mark_current_wrong()
        self.buzzer.accept_buzz(second)
        with self.assertRaisesMessage(runtime.ActionError, 'aktuellen Buzz'):
            runtime.perform(self.buzzer, action, self.hub.code, lambda game: game.mark_current_correct())
        self.assertEqual(sum(self.buzzer.participants.values_list('total_score', flat=True)), 0)

    def test_round_points_remain_exact_after_later_configuration_change(self):
        first, second = self.open_buzzer()
        self.buzzer.accept_buzz(first)
        self.buzzer.mark_current_correct()
        self.buzzer.points_per_correct = 8
        self.buzzer.save(update_fields=['points_per_correct'])
        winner = self.buzzer.serialize_state(self.hub.code, first.name)
        loser = self.buzzer.serialize_state(self.hub.code, second.name)
        self.assertEqual(winner['round_results'][0]['points_earned'], 3)
        self.assertEqual(winner['round_results'][0]['max_points'], 3)
        self.assertEqual(loser['round_results'][0]['points_earned'], 0)

    def test_buzzer_pause_and_reload_keep_round_right_and_player_eligibility(self):
        first, _ = self.open_buzzer()
        self.buzzer.accept_buzz(first)
        before = (self.buzzer.started_at, self.buzzer.current_round_id)
        self.buzzer.status = 'inactive'
        self.buzzer.save(update_fields=['status'])
        self.client.get(reverse('buzzer:play', args=[self.buzzer.room_code, first.name]), {'hub_session': self.hub.code})
        self.buzzer.start_quiz(self.hub.code)
        first.refresh_from_db()
        self.assertTrue(first.is_active)
        self.assertEqual((self.buzzer.started_at, self.buzzer.current_round_id), before)
        self.assertEqual(self.buzzer.current_buzz_participant_id, first.pk)

    def test_points_resume_keeps_history_and_start_time(self):
        player, _ = self.start(self.points)
        self.points.adjust_score(player.pk, 7)
        self.points.next_round()
        self.points.refresh_from_db()
        before = (self.points.started_at, self.points.current_round_number, self.points.adjustments.count())
        self.points.status = 'inactive'
        self.points.save(update_fields=['status'])
        self.points.start_quiz(self.hub.code)
        player.refresh_from_db()
        self.assertEqual(player.total_score, 7)
        self.assertEqual((self.points.started_at, self.points.current_round_number, self.points.adjustments.count()), before)

    def test_http_score_is_guarded_idempotent_and_broadcasts(self):
        player, _ = self.start(self.points)
        action = {**self.action(self.points, 'admin_adjust_score'), 'participant_id': player.pk, 'delta': 5}
        url = reverse('host_points:adjust_score', args=[self.points.room_code])
        with patch('games_hub.manual_game_runtime.broadcast') as broadcast:
            response = self.client.post(url, json.dumps(action), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(broadcast.called)
        self.assertEqual(self.client.post(url, json.dumps(action), content_type='application/json').status_code, 409)
        self.points.next_round()
        action['client_action_id'] = str(uuid.uuid4())
        self.assertEqual(self.client.post(url, json.dumps(action), content_type='application/json').status_code, 409)
        player.refresh_from_db()
        self.assertEqual(player.total_score, 5)

    def test_invalid_point_values_do_not_change_score(self):
        player, _ = self.start(self.points)
        for value in [True, 1.5, '1.9', 2 ** 80, float('inf')]:
            self.assertFalse(self.points.adjust_score(player.pk, value)[0])
        player.refresh_from_db()
        self.assertEqual(player.total_score, 0)

    def test_ending_inactive_points_game_finalizes_zero_scores(self):
        self.start(self.points)
        self.points.status = 'inactive'
        self.points.save(update_fields=['status'])
        self.points.end_quiz()
        self.assertEqual(list(self.points.adjustments.values_list('points_delta', flat=True)), [0, 0])

    def test_waiting_monitor_does_not_freeze_roster(self):
        self.hub.check_in_status = 'open'
        self.hub.save(update_fields=['check_in_status'])
        for game in [self.buzzer, self.points]:
            game.serialize_state(self.hub.code)
        self.assertFalse(HubGameParticipantSnapshot.objects.exists())
        HubParticipant.objects.create(session=self.hub, nickname='Cara', scoring_eligible=True)
        self.hub.check_in_status = 'completed'
        self.hub.save(update_fields=['check_in_status'])
        for game in [self.buzzer, self.points]:
            game.start_quiz(self.hub.code)
            self.assertEqual(game.participants.count(), 3)

    def test_existing_premature_snapshot_is_refreshed_only_at_first_start(self):
        for step in self.hub.steps.all():
            HubGameParticipantSnapshot.create_for_step(step)
        HubParticipant.objects.create(session=self.hub, nickname='Cara', scoring_eligible=True)
        self.start(self.buzzer)
        self.assertEqual(self.buzzer.participants.count(), 3)

    def test_future_session_cannot_stop_current_game(self):
        future = HubSession.objects.create(code='FUTURE', creator=self.user, is_active=True)
        for key, game in [('buzzer', self.buzzer), ('host_points', self.points)]:
            self.start(game)
            HubGameStep.objects.create(session=future, game_key=key, room_code=game.room_code, order=0 if key == 'buzzer' else 1)
            response = self.client.post(reverse('admin_dashboard:end_' + key + '_game_by_room_code', args=[game.room_code])
                + '?hub_session=' + future.code, '{}', content_type='application/json')
            self.assertEqual(response.status_code, 409)
            game.refresh_from_db()
            self.assertEqual(game.status, 'active')

    def test_http_buzz_rejects_missing_or_foreign_identity_and_stale_round(self):
        first, second = self.open_buzzer()
        body = self.buzz_body(first)
        url = reverse('buzzer:buzz', args=[self.buzzer.room_code])
        for token in ['', issue_rejoin_token(self.hub_players[1])]:
            response = Client().post(url, json.dumps({**body, 'player_token': token}), content_type='application/json')
            self.assertEqual(response.status_code, 403)
        self.buzzer.end_current_round()
        self.buzzer.start_round(self.hub.code)
        self.buzzer.open_buzzer()
        self.assertEqual(Client().post(url, json.dumps(body), content_type='application/json').status_code, 409)

    def test_direct_join_keeps_session_in_redirect(self):
        for key, game in [('buzzer', self.buzzer), ('host_points', self.points)]:
            response = self.client.post(reverse(key + ':join'), json.dumps({'room_code': game.room_code,
                'participant_name': 'Alice', 'hub_session': self.hub.code}), content_type='application/json')
            self.assertIn('hub_session=' + self.hub.code, response.json()['redirect_url'])

    def test_standalone_join_activates_without_reload_and_cannot_steal_name(self):
        game = BuzzerGame.objects.create(creator=self.user)
        client = Client()
        self.assertEqual(client.post(reverse('buzzer:join'), {'room_code': game.room_code, 'participant_name': 'Solo'}).status_code, 302)
        game.start_quiz()
        game.start_round()
        game.open_buzzer()
        self.assertTrue(game.serialize_state('', 'Solo')['can_buzz'])
        self.assertEqual(Client().get(reverse('buzzer:play', args=[game.room_code, 'Solo'])).status_code, 403)

    def test_extra_rounds_remain_allowed(self):
        self.open_buzzer()
        self.buzzer.end_current_round()
        self.buzzer.start_round(self.hub.code)
        self.assertEqual(self.buzzer.current_round_number, 2)

    def test_hub_pause_and_resume_preserve_both_games_and_broadcast(self):
        from .active_game_guard import _set_game_status
        first, _ = self.open_buzzer()
        self.buzzer.accept_buzz(first)
        points_player, _ = self.start(self.points)
        self.points.adjust_score(points_player.pk, 7)
        before = self.buzzer.serialize_state(self.hub.code)['round']
        with patch('games_hub.manual_game_runtime.broadcast') as broadcast:
            HubSession.pause_active_games_for_session_codes([self.hub.code])
            self.assertEqual(broadcast.call_count, 2)
            for game in [self.buzzer, self.points]:
                game.refresh_from_db()
                self.assertEqual(game.status, 'inactive')
                _set_game_status(game, 'active')
                self.assertEqual(game.status, 'active')
            self.assertEqual(broadcast.call_count, 4)
        self.assertEqual(self.buzzer.serialize_state(self.hub.code)['round'], before)
        points_player.refresh_from_db()
        self.assertEqual(points_player.total_score, 7)
        self.assertEqual(self.points.adjustments.count(), 1)

    def test_pausing_future_session_does_not_pause_reused_current_game(self):
        future = HubSession.objects.create(code='FUTURE', creator=self.user)
        for key, game in [('buzzer', self.buzzer), ('host_points', self.points)]:
            self.start(game)
            HubGameStep.objects.create(session=future, game_key=key, room_code=game.room_code,
                order=0 if key == 'buzzer' else 1)
        with patch('games_hub.manual_game_runtime.broadcast') as broadcast:
            HubSession.pause_active_games_for_session_codes([future.code])
            self.assertFalse(broadcast.called)
        for game in [self.buzzer, self.points]:
            game.refresh_from_db()
            self.assertEqual(game.status, 'active')

    def test_hub_end_finalizes_inactive_points_game_and_broadcasts(self):
        from .active_game_guard import _end_game_cleanly
        self.start(self.points)
        self.points.status = 'inactive'
        self.points.save(update_fields=['status'])
        with patch('games_hub.manual_game_runtime.broadcast') as broadcast:
            _end_game_cleanly(self.points)
            broadcast.assert_called_once_with(self.points, 'game_ended')
        self.assertEqual(self.points.status, 'completed')
        self.assertEqual(list(self.points.adjustments.values_list('points_delta', flat=True)), [0, 0])

    def test_hub_activation_cannot_resume_a_different_sessions_paused_game(self):
        from .active_game_guard import resolve_session_game_activation
        future = HubSession.objects.create(code='FUTURE', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed')
        for number, (key, game) in enumerate([('buzzer', self.buzzer), ('host_points', self.points)]):
            self.start(game)
            game.status = 'inactive'
            game.save(update_fields=['status'])
            HubGameStep.objects.create(session=future, game_key=key, room_code=game.room_code, order=number)
            with patch('games_hub.active_game_guard.start_game_intro') as intro:
                result = resolve_session_game_activation(future.code, key, game.room_code)
                self.assertFalse(result['success'])
                self.assertFalse(intro.called)
            game.refresh_from_db()
            self.assertEqual(game.status, 'inactive')
            self.assertEqual(game.active_hub_session_code, self.hub.code)

    def test_new_hub_run_gets_intro_but_same_session_resume_does_not(self):
        from .active_game_guard import resolve_session_game_activation
        future = HubSession.objects.create(code='FUTURE', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed')
        for number, (key, game) in enumerate([('buzzer', self.buzzer), ('host_points', self.points)]):
            self.start(game)
            game.status = 'inactive'
            game.save(update_fields=['status'])
            with patch('games_hub.active_game_guard.start_game_intro') as intro:
                self.assertTrue(resolve_session_game_activation(self.hub.code, key, game.room_code)['success'])
                self.assertFalse(intro.called)
            game.status = 'inactive'
            game.save(update_fields=['status'])
            HubGameStep.objects.create(session=future, game_key=key, room_code=game.room_code, order=number)
        self.hub.ended_at = timezone.now()
        self.hub.save(update_fields=['ended_at'])
        for key, game in [('buzzer', self.buzzer), ('host_points', self.points)]:
            with patch('games_hub.active_game_guard.start_game_intro') as intro:
                self.assertTrue(resolve_session_game_activation(future.code, key, game.room_code)['success'])
                intro.assert_called_once_with(future.code, key, game.room_code)
            game.status = 'inactive'
            game.save(update_fields=['status'])

    def test_socket_capability_and_session_filter(self):
        first, _ = self.open_buzzer()
        async def scenario():
            socket = WebsocketCommunicator(BuzzerConsumer.as_asgi(), '/')
            socket.scope.update(user=AnonymousUser(), url_route={'kwargs': {'room_code': self.buzzer.room_code}})
            await socket.connect()
            await socket.receive_json_from()
            await socket.send_json_to({'type': 'participant_join', 'participant_name': first.name, 'hub_session': self.hub.code})
            self.assertEqual((await socket.receive_json_from())['code'], 'unauthorized')
            await socket.send_json_to({'type': 'participant_join', 'participant_name': first.name,
                'hub_session': self.hub.code, 'player_token': issue_rejoin_token(self.hub_players[0])})
            self.assertEqual((await socket.receive_json_from())['type'], 'buzzer_state')
            await socket.disconnect()
        async_to_sync(scenario)()
        consumer = BuzzerConsumer()
        consumer._manual_session = 'OTHER'
        self.assertFalse(async_to_sync(consumer.event_in_session)({'hub_session_code': self.hub.code}))

    def test_future_session_socket_waits_for_its_own_game_start(self):
        from channels.db import database_sync_to_async
        from host_points.consumers import HostPointsConsumer
        self.start(self.points)
        future = HubSession.objects.create(code='FUTURE', creator=self.user)
        HubGameStep.objects.create(session=future, game_key='host_points', room_code=self.points.room_code, order=0)
        player = HubParticipant.objects.create(session=future, nickname='Future Alice', scoring_eligible=True)
        self.points.participants.create(name=player.nickname, hub_session_code=future.code, is_active=False)
        token = issue_rejoin_token(player)
        def start_future():
            self.points.end_quiz()
            self.points.start_quiz(future.code)
            runtime.broadcast(self.points, 'game_started')
        async def scenario():
            socket = WebsocketCommunicator(HostPointsConsumer.as_asgi(), '/')
            socket.scope.update(user=AnonymousUser(), url_route={'kwargs': {'room_code': self.points.room_code}})
            await socket.connect()
            await socket.receive_json_from()
            await socket.send_json_to({'type': 'participant_join', 'participant_name': player.nickname,
                'hub_session': future.code, 'player_token': token})
            waiting = await socket.receive_json_from()
            self.assertEqual(waiting['game']['status'], 'waiting')
            self.assertEqual(waiting['round']['number'], 0)
            await database_sync_to_async(runtime.broadcast)(self.points)
            self.assertTrue(await socket.receive_nothing(timeout=0.1))
            await database_sync_to_async(start_future)()
            started = await socket.receive_json_from()
            self.assertEqual(started['hub_session_code'], future.code)
            self.assertEqual(started['game']['status'], 'active')
            self.assertEqual(started['round']['number'], 1)
            await socket.disconnect()
        async_to_sync(scenario)()
