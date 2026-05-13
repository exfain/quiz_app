from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .consumers import ClueRushGameConsumer
from .models import Clue, ClueAnswer, ClueQuestion, ClueRushGame, ClueRushParticipant, ClueRushSession


class ClueRushScoreBoxTests(TestCase):
    def _create_question(self, user, answer='Brazil', clue_count=3):
        question = ClueQuestion.objects.create(
            question_text='Guess the country',
            answer=answer,
            points=10,
            time_limit=30,
            created_by=user,
        )
        for index in range(1, clue_count + 1):
            Clue.objects.create(
                clue_question=question,
                order=index,
                clue_text=f'Clue {index}',
                duration=10,
            )
        return question

    def test_progress_history_includes_solution_text_and_round_score(self):
        user = User.objects.create_user(username='clue-score-history')
        question = self._create_question(user, answer='Brazil', clue_count=3)
        quiz = ClueRushGame.objects.create(
            title='Clue History',
            room_code='8411',
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now(),
        )
        session = ClueRushSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            current_clue_number=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=10),
        )
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )
        ClueAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            answer_text='Brazil',
            time_taken=1.5,
        )

        consumer = ClueRushGameConsumer()
        consumer.room_code = quiz.room_code
        history = async_to_sync(consumer.get_participant_score_history)(
            participant.name,
            participant.hub_session_code,
        )

        self.assertEqual(session.current_clue_number, 2)
        self.assertEqual(
            history,
            [{
                'question_number': 1,
                'correct_answer': 'Brazil',
                'achieved_points': 2,
                'max_points': 3,
            }],
        )

    def test_play_template_contains_score_total_and_solution_hooks(self):
        user = User.objects.create_user(username='clue-score-template')
        quiz = ClueRushGame.objects.create(
            title='Clue Template',
            room_code='8412',
            creator=user,
            status='active',
        )
        participant = ClueRushParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=None,
        )

        response = self.client.get(
            reverse('clue_rush:play', args=[quiz.room_code, participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="clueHistoryTotal"', html=False)
        self.assertContains(response, 'clue-history-solution')
        self.assertContains(response, 'score-box__row')
        self.assertContains(response, 'score-box__title')
