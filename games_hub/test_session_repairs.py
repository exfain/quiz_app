import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from host_points.models import HostPointsGame, HostPointsParticipant
from buzzer.models import BuzzerGame
from .check_in import participant_check_in
from .consumers import HubConsumer
from .lobby_join import check_nickname_availability, issue_rejoin_token, join_lobby_participant
from .models import HubGameStep, HubParticipant, HubSession
from .session_results import freeze_previous_results
from .team_names import ALLOWED_CHARACTERS_MESSAGE
from .views import get_leaderboard_data, get_participant_leaderboard_data
from .voting import is_step_votable


class SessionNameTests(TestCase):
    def setUp(self):
        self.hub = HubSession.objects.create(code='NAMES1')

    def test_invalid_characters_are_rejected_with_allowed_characters(self):
        for name in ['<', '\tTeam', 'Team <X', 'Team & X', 'Team/X', 'Team\\X', 'Team 🐝', "O'Malley", 'Team\nX']:
            with self.subTest(name=name):
                result = join_lobby_participant(self.hub.code, name)
                self.assertFalse(result['success'])
                self.assertEqual(result['code'], 'invalid_name_characters')
                self.assertEqual(result['message'], ALLOWED_CHARACTERS_MESSAGE)
        self.assertFalse(self.hub.participants.exists())

    def test_allowed_characters_and_token_bound_legacy_rejoin(self):
        self.assertTrue(join_lobby_participant(self.hub.code, 'Team ÄÖÜäöüß 42-_')['success'])
        legacy = HubParticipant.objects.create(session=self.hub, nickname='Team & Alt')
        token = issue_rejoin_token(legacy)
        self.assertEqual(check_nickname_availability(self.hub.code, legacy.nickname, token)['status'], 'rejoin')
        self.assertTrue(join_lobby_participant(self.hub.code, legacy.nickname, token)['success'])
        self.assertFalse(join_lobby_participant(self.hub.code, legacy.nickname)['success'])

    def test_direct_join_endpoints_enforce_same_character_policy(self):
        for key in ['quiz', 'who', 'who-is-that', 'assign', 'estimation', 'clue-rush',
                    'sorting-ladder', 'wer-weiss-mehr', 'wann-war-das', 'blackjack', 'buzzer', 'host-points']:
            with self.subTest(game=key):
                response = self.client.post('/' + key + '/join/', json.dumps({
                    'participant_name': 'New <Team', 'room_code': '1234'}), content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], ALLOWED_CHARACTERS_MESSAGE)


class SessionHistoryAndRanksTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('session-repair-host', is_staff=True)
        self.hub = HubSession.objects.create(code='OLD25', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=25)
        self.game = HostPointsGame.objects.create(creator=self.user, room_code='9998', status='completed',
            active_hub_session_code=self.hub.code, started_at=timezone.now(), ended_at=timezone.now())
        self.step = HubGameStep.objects.create(session=self.hub, game_key='host_points', room_code=self.game.room_code, order=0)
        for n in range(25):
            name = f'Team {n:02}'
            HubParticipant.objects.create(session=self.hub, nickname=name, scoring_eligible=True)
            HostPointsParticipant.objects.create(quiz=self.game, name=name, hub_session_code=self.hub.code, total_score=7 if n < 3 else 6)

    def test_three_equal_leaders_have_competition_rank_one_then_four(self):
        data = get_leaderboard_data(self.hub)
        self.assertEqual([p['rank'] for p in data['participants'][:4]], [1, 1, 1, 4])
        personal = get_participant_leaderboard_data(self.hub, 'Team 02')
        self.assertEqual([p['rank'] for p in personal['participants']], [1, 1, 4])

    def test_reused_room_does_not_change_finished_session_results(self):
        before = get_leaderboard_data(self.hub)
        self.hub.ended_at = timezone.now()
        self.hub.save()
        other = HubSession.objects.create(code='NEXT25', creator=self.user, is_active=True, started_at=timezone.now())
        new_step = HubGameStep.objects.create(session=other, game_key='host_points', room_code=self.game.room_code, order=0)
        self.assertTrue(is_step_votable(new_step))
        freeze_previous_results('host_points', self.game, other.code)
        self.game.status = 'active'
        self.game.active_hub_session_code = other.code
        self.game.save()
        after = get_leaderboard_data(self.hub)
        self.assertEqual(before['participants'], after['participants'])
        self.assertEqual(after['games'][0]['status'], 'completed')

    def test_ending_session_does_not_end_other_game_with_same_room_code(self):
        unrelated = BuzzerGame.objects.create(creator=self.user, room_code=self.game.room_code, status='active')
        consumer = HubConsumer()
        consumer.session_code = self.hub.code
        async_to_sync(consumer.complete_games_for_session)()
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, 'active')
        self.step.refresh_from_db()
        self.assertEqual(self.step.result_snapshot['status'], 'completed')

    def test_ending_old_session_does_not_end_room_owned_by_new_session(self):
        self.game.status = 'active'
        self.game.active_hub_session_code = 'OTHER'
        self.game.save()
        consumer = HubConsumer()
        consumer.session_code = self.hub.code
        async_to_sync(consumer.complete_games_for_session)()
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')

    def test_each_of_25_hub_connections_only_navigates_its_own_client(self):
        cache.clear()
        async def event():
            for n in range(25):
                consumer = HubConsumer()
                consumer.channel_name = f'test-{n}'
                consumer.group_name = 'hub_OLD25'
                consumer.session_code = self.hub.code
                consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
                consumer.send_json = AsyncMock()
                consumer.ensure_step_for_room = AsyncMock()
                consumer.start_game_intro_for_room = AsyncMock(return_value={'game_key': 'host_points', 'room_code': '9998'})
                await consumer.hub_event({'event': {'type': 'quiz_started', 'game_key': 'host_points', 'room_code': '9998'}})
                consumer.channel_layer.group_send.assert_not_called()
                self.assertEqual(sum(call.args[0]['type'] == 'navigate' for call in consumer.send_json.call_args_list), 1)
        async_to_sync(event)()


class TwentyFiveCheckIns(TransactionTestCase):
    def test_25_concurrent_read_then_write_transactions_complete(self):
        hub = HubSession.objects.create(code='LOCK25', check_in_status='open')
        names = [f'Team {n:02}' for n in range(25)]
        for name in names:
            HubParticipant.objects.create(session=hub, nickname=name)
        barrier = Barrier(25)
        def check_in(name):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return participant_check_in(hub, name)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=25) as pool:
            results = list(pool.map(check_in, names))
        self.assertEqual(sum(result['success'] for result in results), 25)
        self.assertEqual(hub.participants.filter(checked_in_at__isnull=False).count(), 25)
