import json
from pathlib import Path
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from Estimation.consumers import EstimationConsumer
from Estimation.models import EstimationParticipant, EstimationQuiz
from QuizGame.models import Quiz, QuizParticipant, QuizQuestion
from games_hub.models import HubGameStep, HubParticipant, HubSession


class LobbyReturnFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='lobby_guard_admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.user)

        self.session = HubSession.objects.create(code='LOBBY1', name='Lobby Return Session')
        HubParticipant.objects.create(session=self.session, nickname='Alice')
        HubParticipant.objects.create(session=self.session, nickname='Bob')

        self.quiz = Quiz.objects.create(
            creator=self.user,
            title='Quick Quiz',
            status='active',
        )
        self.question = QuizQuestion.objects.create(
            question_text='Was ist 2 + 2?',
            question_type='multiple_choice',
            option_a='4',
            option_b='5',
            correct_answer='A',
            created_by=self.user,
        )
        self.quiz.current_question = self.question
        self.quiz.save(update_fields=['current_question'])

        self.estimation = EstimationQuiz.objects.create(
            creator=self.user,
            title='Estimation',
            status='waiting',
        )

        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='estimation',
            room_code=self.estimation.room_code,
            title=self.estimation.title,
        )

    def test_session_lobby_presence_endpoint_reports_players_still_in_game(self):
        QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )

        response = self.client.get(
            reverse('games_hub:session_lobby_presence_api', args=[self.session.code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertFalse(payload['allowed'])
        self.assertFalse(payload['all_in_lobby'])
        self.assertEqual(payload['not_in_lobby_count'], 1)
        self.assertEqual(payload['in_lobby_count'], 1)
        self.assertEqual(payload['participants_not_in_lobby'][0]['name'], 'Alice')
        self.assertEqual(payload['participants_not_in_lobby'][0]['games'][0]['game_key'], 'quiz')

    def test_participant_return_to_lobby_marks_only_current_player_inactive(self):
        alice = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        bob = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )

        response = self.client.post(
            reverse('games_hub:participant_return_to_lobby', args=[self.session.code]),
            data=json.dumps({
                'game_key': 'quiz',
                'room_code': self.quiz.room_code,
                'participant_name': 'Alice',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertFalse(alice.is_active)
        self.assertTrue(bob.is_active)

    def test_recall_to_lobby_marks_all_active_session_participants_inactive(self):
        alice = QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        bob = EstimationParticipant.objects.create(
            quiz=self.estimation,
            name='Bob',
            hub_session_code=self.session.code,
            is_active=True,
        )

        response = self.client.post(
            reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['all_in_lobby'])

        alice.refresh_from_db()
        bob.refresh_from_db()
        self.assertFalse(alice.is_active)
        self.assertFalse(bob.is_active)

    def test_consumer_start_is_blocked_when_players_are_not_in_lobby(self):
        QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )

        consumer = EstimationConsumer()
        consumer.room_code = self.estimation.room_code
        consumer.room_group_name = f'estimation_{self.estimation.room_code}'
        consumer.send = AsyncMock()

        async_to_sync(consumer.handle_admin_start_quiz)({})

        self.estimation.refresh_from_db()
        self.assertEqual(self.estimation.status, 'waiting')
        consumer.send.assert_awaited_once()

        payload = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(payload['type'], 'participants_not_in_lobby')
        self.assertEqual(payload['not_in_lobby_count'], 1)
        self.assertEqual(payload['participants_not_in_lobby'][0]['name'], 'Alice')

    def test_game_monitor_renders_lobby_return_guard_modal(self):
        response = self.client.get(
            f"{reverse('admin_dashboard:quiz_monitor', args=[self.quiz.room_code])}?hub_session={self.session.code}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'lobbyReturnGuardModal')
        self.assertContains(response, 'Teilnehmer sind noch im Spiel')
        self.assertContains(response, 'Teilnehmer in die Lobby zurückschicken (10)')
        self.assertContains(response, '/lobby-presence/')
        self.assertContains(response, '/recall-to-lobby/')

    def test_session_monitor_uses_same_recall_button_flow(self):
        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'recallLobbyBtn')
        self.assertContains(response, 'Teilnehmer in die Lobby zur')
        self.assertContains(response, '/lobby-presence/')
        self.assertNotContains(response, 'Debug-only')
        self.assertNotContains(response, "type: 'recall_to_lobby'")

    def test_all_player_templates_use_manual_lobby_return_flow(self):
        template_paths = [
            'templates/quiz/play.html',
            'templates/estimation/play.html',
            'templates/assign/play.html',
            'templates/where_is_this/play.html',
            'templates/who_is_lying/play.html',
            'templates/who_is_that/play.html',
            'templates/black_jack_quiz/play.html',
            'templates/clue_rush/play.html',
            'templates/sorting_ladder/play.html',
        ]

        for relative_path in template_paths:
            with self.subTest(template=relative_path):
                content = Path(settings.BASE_DIR / relative_path).read_text(encoding='utf-8')
                self.assertIn("_hub_return_to_lobby_player.html", content)
                self.assertIn('createHubLobbyReturnController', content)
                self.assertIn("players_recalled_to_lobby", content)
                self.assertIn("returnToLobby({ markInactive: false });", content)
                self.assertNotIn("After 2 seconds, return to lobby with nickname preserved", content)
