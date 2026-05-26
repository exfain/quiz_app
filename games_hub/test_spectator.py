import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant, QuizQuestion, QuizSession
from clue_rush.models import Clue, ClueAnswer, ClueQuestion, ClueRushGame, ClueRushParticipant, ClueRushSession
from games_hub.models import HubGameStep, HubParticipant, HubSession


class HubSpectatorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='admin', password='pass')
        self.session = HubSession.objects.create(
            code='SPEC1',
            name='Spectator Session',
            is_active=True,
            started_at=timezone.now(),
        )

    def test_spectator_route_renders_without_creating_participant(self):
        response = self.client.get(reverse('games_hub:spectate_session', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'hub/spectate.html')
        self.assertContains(response, 'Spectator View')
        self.assertContains(response, reverse('games_hub:spectate_session_state', args=[self.session.code]))
        self.assertEqual(HubParticipant.objects.count(), 0)

    def test_spectator_state_waits_without_active_game(self):
        response = self.client.get(reverse('games_hub:spectate_session_state', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['phase'], 'waiting')
        self.assertIsNone(payload['game'])
        self.assertEqual(HubParticipant.objects.count(), 0)

    def test_quick_quiz_spectator_uses_runtime_state_without_participant(self):
        question = QuizQuestion.objects.create(
            created_by=self.user,
            question_text='Was ist 2 + 2?',
            question_type='multiple_choice',
            option_a='4',
            option_b='5',
            correct_answer='A',
        )
        quiz = Quiz.objects.create(
            creator=self.user,
            title='Spectator Quick Quiz',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        quiz_session = QuizSession.objects.create(quiz=quiz)
        quiz_session.send_question(question)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
        )

        response = self.client.get(reverse('games_hub:spectate_session_state', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['phase'], 'question')
        self.assertEqual(payload['game']['game_key'], 'quiz')
        self.assertEqual(payload['game']['question']['text'], 'Was ist 2 + 2?')
        self.assertEqual(
            payload['game']['question']['options'],
            [{'key': 'A', 'text': '4'}, {'key': 'B', 'text': '5'}],
        )
        self.assertNotIn('correct_answer', payload['game']['question'])
        self.assertEqual(HubParticipant.objects.count(), 0)
        self.assertEqual(QuizParticipant.objects.count(), 0)

    def test_clue_rush_spectator_shows_answer_lock_clue_without_answer_text(self):
        game = ClueRushGame.objects.create(
            creator=self.user,
            title='Spectator Clue Rush',
            status='active',
            started_at=self.session.started_at,
        )
        question = ClueQuestion.objects.create(
            created_by=self.user,
            question_text='Gesuchter Begriff',
            answer='Secret Answer',
        )
        clue_one = Clue.objects.create(clue_question=question, order=1, clue_text='Erster Hinweis')
        Clue.objects.create(clue_question=question, order=2, clue_text='Zweiter Hinweis')
        clue_three = Clue.objects.create(clue_question=question, order=3, clue_text='Dritter Hinweis')
        game.current_question = question
        game.current_clue = clue_three
        game.save(update_fields=['current_question', 'current_clue'])
        ClueRushSession.objects.create(
            quiz=game,
            total_questions_sent=1,
            current_question_number=1,
            is_question_active=True,
            current_clue_number=3,
            is_clue_active=True,
        )
        participant = ClueRushParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=self.session.code,
            is_active=True,
        )
        ClueAnswer.objects.create(
            quiz=game,
            participant=participant,
            question=question,
            answer_text='Secret Answer',
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='clue_rush',
            room_code=game.room_code,
            title=game.title,
        )

        response = self.client.get(reverse('games_hub:spectate_session_state', args=[self.session.code]))
        response_text = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response_text)
        self.assertEqual(payload['phase'], 'question')
        self.assertEqual(payload['game']['game_key'], 'clue_rush')
        self.assertEqual(
            payload['game']['question']['clues'],
            [
                {'number': 1, 'text': clue_one.clue_text},
                {'number': 2, 'text': 'Zweiter Hinweis'},
                {'number': 3, 'text': 'Dritter Hinweis'},
            ],
        )
        self.assertEqual(payload['game']['question']['answer_locks'], [{'participant': 'Lisa', 'clue_number': 3}])
        self.assertNotIn('Secret Answer', response_text)
        self.assertEqual(HubParticipant.objects.count(), 0)
