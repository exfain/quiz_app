from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz
from games_hub.consumers import HubConsumer
from games_hub.models import HubGameStep, HubSession


class LobbyNextGameNumberTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='lobby-order', password='testpass123')
        self.session = HubSession.objects.create(code='ORDER123', name='Order session')
        self.games = []
        for order in range(4):
            game = Quiz.objects.create(
                creator=self.user,
                room_code=f'41{order:02d}',
                title=f'Quick Quiz {order + 1}',
                status='waiting',
            )
            self.games.append(game)
            HubGameStep.objects.create(
                session=self.session,
                order=order,
                game_key='quiz',
                room_code=game.room_code,
                title=game.title,
            )

    def _complete_games(self, count):
        for game in self.games[:count]:
            game.status = 'completed'
            game.ended_at = timezone.now()
            game.save(update_fields=['status', 'ended_at'])

    def test_first_game_uses_one_based_plan_order(self):
        self.assertEqual(self.session.get_next_game_number(), 1)

    def test_completed_games_advance_to_actual_next_plan_position(self):
        self._complete_games(3)

        self.session.refresh_from_db()

        self.assertEqual(self.session.current_step_index, 0)
        self.assertEqual(self.session.get_next_game_number(), 4)

    def test_same_game_type_steps_remain_separate_sequence_positions(self):
        self._complete_games(2)

        next_step = self.session.get_next_planned_step()

        self.assertEqual(next_step.room_code, self.games[2].room_code)
        self.assertEqual(next_step.order, 2)
        self.assertEqual(self.session.get_next_game_number(), 3)

    def test_twelfth_configured_game_keeps_its_visible_position(self):
        for order in range(4, 12):
            game = Quiz.objects.create(
                creator=self.user,
                room_code=f'42{order:02d}',
                title=f'Quick Quiz {order + 1}',
                status='waiting',
            )
            self.games.append(game)
            HubGameStep.objects.create(
                session=self.session,
                order=order,
                game_key='quiz',
                room_code=game.room_code,
                title=game.title,
            )
        self._complete_games(11)

        self.assertEqual(self.session.get_next_game_number(), 12)

    def test_deleted_step_is_renumbered_and_not_counted(self):
        self.client.force_login(self.user)
        removed_step = self.session.steps.get(order=1)

        response = self.client.post(
            reverse('games_hub:delete_step', args=[self.session.code, removed_step.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(self.session.steps.order_by('order').values_list('order', flat=True)),
            [0, 1, 2],
        )

    def test_lobby_reload_and_websocket_state_use_same_authoritative_number(self):
        self._complete_games(2)

        response = self.client.get(reverse('games_hub:lobby', args=[self.session.code]))
        consumer = HubConsumer()
        consumer.session_code = self.session.code
        state = async_to_sync(consumer.get_state)()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['next_game_number'], 3)
        self.assertContains(response, 'data-vhs-next-game-number="3"')
        self.assertEqual(state['next_game_number'], 3)
        self.assertFalse(state['next_game_complete'])

    def test_finished_sequence_has_neutral_terminal_state(self):
        self._complete_games(4)

        response = self.client.get(reverse('games_hub:lobby', args=[self.session.code]))
        consumer = HubConsumer()
        consumer.session_code = self.session.code
        state = async_to_sync(consumer.get_state)()

        self.assertIsNone(response.context['next_game_number'])
        self.assertContains(response, 'data-vhs-next-game-state="complete"')
        self.assertIsNone(state['next_game_number'])
        self.assertTrue(state['next_game_complete'])

    def test_lobby_client_rejects_older_game_number_updates(self):
        project_root = Path(__file__).resolve().parent.parent
        lobby_source = (project_root / 'templates' / 'hub' / 'lobby.html').read_text(encoding='utf-8')
        widget_source = (
            project_root / 'templates' / 'includes' / 'accessibility_widget.html'
        ).read_text(encoding='utf-8')

        self.assertIn("state, 'next_game_number'", lobby_source)
        self.assertIn('nextGameNumber < latestNextGameNumber', lobby_source)
        self.assertIn('nextGameSequenceComplete', lobby_source)
        self.assertNotIn('state.current_step_index', lobby_source)
        self.assertIn('KEINE WEITEREN SPIELE', widget_source)
        self.assertIn('data-vhs-next-game-state', widget_source)
