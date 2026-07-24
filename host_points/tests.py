import json
from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession
from games_hub.views import get_leaderboard_data

from .consumers import HostPointsConsumer
from .models import HostPointsAdjustment, HostPointsGame, HostPointsParticipant


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class HostPointsFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('host-points-host', password='pw', is_staff=True)
        self.game = HostPointsGame.objects.create(title='Manuelle Punkte', creator=self.user)
        self.session = HubSession.objects.create(
            code='HP001',
            name='Host Points Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(
                session=self.session,
                nickname=name,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='host_points',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)

    def make_consumer(self):
        consumer = HostPointsConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'host_points_{self.game.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_creation_manage_games_and_session_registration(self):
        client = Client()
        client.force_login(self.user)

        create_response = client.post(
            reverse('admin_dashboard:create_host_points_game'),
            data=json.dumps({'title': 'Externe Challenge'}),
            content_type='application/json',
        )
        manage_response = client.get(reverse('admin_dashboard:manage_games'))
        monitor_response = client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(create_response.status_code, 200)
        self.assertTrue(HostPointsGame.objects.filter(title='Externe Challenge').exists())
        self.assertContains(manage_response, 'Host-Punktevergabe')
        self.assertContains(monitor_response, 'host_points')

    def test_host_monitor_uses_common_start_guard_and_consistent_controls(self):
        client = Client()
        client.force_login(self.user)

        response = client.get(
            reverse('admin_dashboard:host_points_monitor', args=[self.game.room_code]),
            {'hub_session': self.session.code},
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertContains(response, 'id="lobbyReturnGuardModal"')
        self.assertContains(response, 'id="startQuizBtn"')
        self.assertNotContains(response, 'id="startGameBtn"')
        self.assertIn('id="nextRoundBtn"', content)
        monitor_content = content.split('id="activeGameConflictModal"', 1)[0]
        self.assertIn('Spiel starten', content)
        self.assertIn('Nächste Runde', content)
        self.assertIn('Spiel beenden', monitor_content)
        self.assertNotIn('End Quiz', monitor_content)
        self.assertNotIn(f'href="/hub/lobby/{self.session.code}/"', content)
        self.assertIn(f'href="/hub/monitor/{self.session.code}/"', content)

    def test_start_adjust_round_and_rejoin_state(self):
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_start_game)({'hub_session': self.session.code})
        self.game.refresh_from_db()
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.game.current_round_number, 1)

        success, _ = self.game.adjust_score(alice.id, 5)
        self.assertTrue(success)
        success, _ = self.game.adjust_score(bob.id, -1)
        self.assertTrue(success)
        self.assertTrue(self.game.next_round())

        state = self.game.serialize_state(self.session.code, 'Alice')
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertEqual(state['round']['number'], 2)
        self.assertEqual(state['participant']['score'], 5)
        self.assertEqual(state['round_scores'], [
            {'number': 1, 'points': 5},
            {'number': 2, 'points': None},
        ])
        self.assertEqual(alice.total_score, 5)
        self.assertEqual(bob.total_score, -1)

        self.assertTrue(self.game.adjust_score(alice.id, 2)[0])
        self.assertTrue(self.game.adjust_score(alice.id, -2)[0])
        rejoin_state = self.game.serialize_state(self.session.code, 'Alice')
        self.assertEqual(rejoin_state['round_scores'], [
            {'number': 1, 'points': 5},
            {'number': 2, 'points': 0},
        ])

    def test_round_scores_exclude_adjustments_from_another_hub_session(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertTrue(self.game.adjust_score(alice.id, 5)[0])
        HostPointsAdjustment.objects.create(
            quiz=self.game,
            participant=alice,
            hub_session_code='OLD001',
            round_number=1,
            points_delta=99,
        )

        state = self.game.serialize_state(self.session.code, 'Alice')

        self.assertEqual(state['round_scores'], [{'number': 1, 'points': 5}])

    def test_late_pending_and_spectator_participants_are_not_score_targets(self):
        self.game.start_quiz(self.session.code)
        late = HubParticipant.objects.create(
            session=self.session,
            nickname='Charlie',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        spectator = HubParticipant.objects.create(
            session=self.session,
            nickname='Spec',
            scoring_eligible=False,
        )
        HubGameParticipantSnapshot.objects.create(
            session=self.session,
            game_step=self.step,
            participant=spectator,
            included_in_scoring=False,
            active_player=True,
            reason=HubGameParticipantSnapshot.REASON_EXCLUDED,
        )

        client = Client()
        client.get(
            reverse('host_points:play', args=[self.game.room_code, late.nickname]),
            {'hub_session': self.session.code},
        )
        client.get(
            reverse('host_points:play', args=[self.game.room_code, spectator.nickname]),
            {'hub_session': self.session.code},
        )

        state = self.game.serialize_state(self.session.code)
        names = [participant['name'] for participant in state['participants']]
        late_participant = HostPointsParticipant.objects.get(name='Charlie', hub_session_code=self.session.code)
        spectator_participant = HostPointsParticipant.objects.get(name='Spec', hub_session_code=self.session.code)

        self.assertEqual(names, ['Alice', 'Bob'])
        self.assertFalse(late_participant.is_active)
        self.assertFalse(spectator_participant.is_active)
        self.assertFalse(self.game.adjust_score(late_participant.id, 1)[0])
        self.assertFalse(self.game.adjust_score(spectator_participant.id, 1)[0])

    def test_player_view_hides_lobby_return_during_active_game_and_result_has_lobby_return(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        play_response = Client().get(
            reverse('host_points:play', args=[self.game.room_code, alice.name]),
            {'hub_session': self.session.code},
        )
        self.game.end_quiz()
        result_response = Client().get(
            reverse('host_points:result', args=[self.game.room_code, alice.name]),
            {'hub_session': self.session.code},
        )

        self.assertContains(play_response, 'Der Host vergibt die Punkte manuell.')
        play_content = play_response.content.decode('utf-8')
        play_main = play_content.split('</main>', 1)[0]
        self.assertNotIn('<input', play_main)
        self.assertIn('<title>Host-Punktevergabe: Manuelle Punkte - QuizMaster</title>', play_content)
        self.assertIn('data-participant-name="Alice"', play_content)
        self.assertIn('MANUELLE PUNKTE · SPIEL 1', play_content)
        self.assertIn('id="hostPointsScoreRows"', play_content)
        self.assertIn('score.points !== null && score.points !== undefined', play_content)
        self.assertIn('class="mt-3 d-none" id="returnToLobbyContainer"', play_content)
        self.assertIn("returnToLobbyContainer.classList.toggle('d-none', gameStarted)", play_content)
        self.assertContains(result_response, 'Zur Lobby zurückkehren')
        self.assertContains(result_response, 'participant-return-to-lobby')

    def test_vhs_host_points_overrides_are_scoped_to_the_participant_screen(self):
        css_path = finders.find('themes/vhs/vhs.css')
        self.assertIsNotNone(css_path)
        css = Path(css_path).read_text(encoding='utf-8')

        self.assertIn(
            'body.host-points-play-page .qa-score-widget .host-points-score-box',
            css,
        )
        self.assertIn(
            'body.host-points-play-page .host-points-vhs-game-meta',
            css,
        )
        self.assertIn(
            'body.host-points-play-page :is(\n'
            '  .host-points-summary,\n'
            '  #connectionStatus,\n'
            '  #statusText\n'
            ')',
            css,
        )

    def test_end_game_idempotent_and_actions_after_end_are_rejected(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.assertTrue(self.game.adjust_score(alice.id, 3)[0])
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})
        first_message_count = len(consumer.channel_layer.group_messages)
        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})
        adjust_success, adjust_message = self.game.adjust_score(alice.id, 2)
        next_round_success = self.game.next_round()

        self.game.refresh_from_db()
        alice.refresh_from_db()
        self.assertEqual(self.game.status, 'completed')
        self.assertEqual(first_message_count, 2)
        self.assertEqual(len(consumer.channel_layer.group_messages), first_message_count)
        self.assertEqual(sent_messages[-1]['type'], 'host_points_state')
        self.assertFalse(adjust_success)
        self.assertIn('nicht aktiv', adjust_message)
        self.assertFalse(next_round_success)
        self.assertEqual(alice.total_score, 3)

    def test_lobby_return_keeps_participant_out_of_game_after_end_event(self):
        self.game.start_quiz(self.session.code)
        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        response = Client().post(
            reverse('games_hub:participant_return_to_lobby', args=[self.session.code]),
            data=json.dumps({
                'game_key': 'host_points',
                'room_code': self.game.room_code,
                'participant_name': alice.name,
            }),
            content_type='application/json',
        )
        consumer, _ = self.make_consumer()
        async_to_sync(consumer.handle_admin_end_game)({'hub_session': self.session.code})

        alice.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(alice.is_active)


class HostPointsLeaderboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('host-points-score-host', password='pw', is_staff=True)
        self.game = HostPointsGame.objects.create(title='Score only', creator=self.user)
        self.session = HubSession.objects.create(
            code='HP002',
            name='Leaderboard Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(
                session=self.session,
                nickname=name,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='host_points',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(self.step)
        self.game.start_quiz(self.session.code)
        self.alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        self.bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)

    def test_simple_and_ranking_leaderboards_use_host_scores(self):
        self.alice.total_score = 7
        self.alice.save(update_fields=['total_score'])
        self.bob.total_score = 0
        self.bob.save(update_fields=['total_score'])
        self.game.end_quiz()

        simple = get_leaderboard_data(self.session)
        alice = next(row for row in simple['participants'] if row['name'] == 'Alice')
        bob = next(row for row in simple['participants'] if row['name'] == 'Bob')
        self.assertEqual(alice['game_scores'][f'step:{self.step.id}'], 7)
        self.assertEqual(bob['game_scores'][f'step:{self.step.id}'], 0)

        self.session.overall_scoring_mode = HubSession.OVERALL_SCORING_RANKING
        self.session.save(update_fields=['overall_scoring_mode'])
        ranking = get_leaderboard_data(self.session)
        alice_ranked = next(row for row in ranking['participants'] if row['name'] == 'Alice')
        bob_ranked = next(row for row in ranking['participants'] if row['name'] == 'Bob')
        self.assertEqual(alice_ranked['game_base_scores'][f'step:{self.step.id}'], 2)
        self.assertEqual(bob_ranked['game_base_scores'][f'step:{self.step.id}'], 1)
