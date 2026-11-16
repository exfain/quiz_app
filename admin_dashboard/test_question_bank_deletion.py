import json

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from Assign.models import AssignAnswer, AssignParticipant, AssignQuestion, AssignQuiz
from Estimation.models import EstimationAnswer, EstimationParticipant, EstimationQuestion, EstimationQuiz
from QuizGame.models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion
from black_jack_quiz.models import BlackJackAnswer, BlackJackParticipant, BlackJackQuestion, BlackJackQuiz
from clue_rush.models import ClueAnswer, ClueQuestion, ClueRushGame, ClueRushParticipant
from sorting_ladder.models import RoundSubmission, SortingItem, SortingLadderGame, SortingLadderParticipant, SortingQuestion
from wann_war_das.models import WannWarDasQuestion
from wer_weiss_mehr.models import WerWeissMehrQuestion
from where_is_this.models import WhereAnswer, WhereParticipant, WhereQuestion, WhereQuiz
from who_is_lying.models import WhoAnswer, WhoParticipant, WhoQuestion, WhoQuiz
from who_is_that.models import WhoThatAnswer, WhoThatParticipant, WhoThatQuestion, WhoThatQuiz


# Real endpoint/model pairs, including the separate topic_id contract for Sorting Ladder.
QUESTION_CASES = {
    'quiz': (QuizQuestion, {'question_text': 'Quiz question', 'correct_answer': 'A'}, 'delete_quiz_question'),
    'assign': (AssignQuestion, {
        'question_text': 'Assign question', 'left_items': ['Left'], 'right_items': ['Right'],
        'correct_matches': {'0': 0},
    }, 'delete_assign_question'),
    'estimation': (EstimationQuestion, {'question_text': 'Estimate', 'correct_answer': 100}, 'delete_estimation_question'),
    'where': (WhereQuestion, {
        'question_text': 'Location', 'correct_latitude': 0, 'correct_longitude': 0,
    }, 'delete_where_question'),
    'who': (WhoQuestion, {
        'statement': 'Statement', 'people': [{'name': 'Person', 'is_lying': True}],
    }, 'delete_who_question'),
    'who_that': (WhoThatQuestion, {
        'question_text': 'Who?', 'image': 'who_that_images/deletion-test.jpg', 'correct_answer': 'Person',
    }, 'delete_who_that_question'),
    'blackjack': (BlackJackQuestion, {'question_text': 'Number?', 'correct_answer': 10}, 'delete_blackjack_question'),
    'clue_rush': (ClueQuestion, {'question_text': 'Clue question', 'answer': 'Answer'}, 'delete_clue_rush_question'),
    'sorting_ladder': (SortingQuestion, {'question_text': 'Sorting topic'}, 'delete_sorting_topic'),
    'wer_weiss_mehr': (WerWeissMehrQuestion, {'question_text': 'Name answers'}, 'delete_wer_weiss_mehr_question'),
    'wann_war_das': (WannWarDasQuestion, {'question_text': 'Which year?', 'correct_answer': 2000}, 'delete_wann_war_das_question'),
}
ALWAYS_DEACTIVATE = {'wer_weiss_mehr', 'wann_war_das'}
ANSWER_CASES = {
    'quiz': (Quiz, QuizParticipant, QuizAnswer, {'answer_text': 'A'}),
    'assign': (AssignQuiz, AssignParticipant, AssignAnswer, {'user_matches': {'0': 0}}),
    'estimation': (EstimationQuiz, EstimationParticipant, EstimationAnswer, {'user_answer': 100}),
    'where': (WhereQuiz, WhereParticipant, WhereAnswer, {'user_latitude': 0, 'user_longitude': 0, 'distance_km': 0}),
    'who': (WhoQuiz, WhoParticipant, WhoAnswer, {'selected_liars': [0]}),
    'who_that': (WhoThatQuiz, WhoThatParticipant, WhoThatAnswer, {'user_answer': 'Person'}),
    'blackjack': (BlackJackQuiz, BlackJackParticipant, BlackJackAnswer, {'user_answer': 10}),
    'clue_rush': (ClueRushGame, ClueRushParticipant, ClueAnswer, {'answer_text': 'Answer'}),
    'sorting_ladder': (SortingLadderGame, SortingLadderParticipant, RoundSubmission, {'round_number': 1}),
}


class QuestionBankDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user('bank-owner', is_staff=True)
        cls.other = User.objects.create_user('bank-other', is_staff=True)
        cls.superuser = User.objects.create_user('bank-root', is_staff=True, is_superuser=True)
        cls.questions = {
            key: model.objects.create(created_by=cls.owner, **fields)
            for key, (model, fields, _) in QUESTION_CASES.items()
        }

    def payload(self, key, question_id):
        return {'topic_id' if key == 'sorting_ladder' else 'question_id': question_id}

    def delete(self, key, question_id=None, client=None):
        return (client or self.client).post(
            reverse('admin_dashboard:' + QUESTION_CASES[key][2]),
            json.dumps(self.payload(key, self.questions[key].pk if question_id is None else question_id)),
            content_type='application/json',
        )

    def assert_unused_removed(self, key):
        question = self.questions[key]
        if key in ALWAYS_DEACTIVATE:
            question.refresh_from_db()
            self.assertFalse(question.is_active)
        else:
            self.assertFalse(type(question).objects.filter(pk=question.pk).exists())

    def test_superuser_can_remove_other_creators_questions_for_every_type(self):
        self.client.force_login(self.superuser)
        for key in QUESTION_CASES:
            with self.subTest(game=key):
                response = self.delete(key)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertTrue(response.json()['success'])
                self.assert_unused_removed(key)

    def test_owner_can_remove_own_unused_questions_for_every_type(self):
        self.client.force_login(self.owner)
        for key in QUESTION_CASES:
            with self.subTest(game=key):
                response = self.delete(key)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertTrue(response.json()['success'])
                self.assert_unused_removed(key)

    def test_other_staff_cannot_remove_questions_for_any_type(self):
        self.client.force_login(self.other)
        for key in QUESTION_CASES:
            with self.subTest(game=key):
                response = self.delete(key)
                self.assertEqual(response.status_code, 403)
                self.assertFalse(response.json()['success'])
                self.questions[key].refresh_from_db()
                self.assertTrue(self.questions[key].is_active)

    def test_missing_questions_return_json_404_for_every_type(self):
        self.client.force_login(self.superuser)
        for key in QUESTION_CASES:
            with self.subTest(game=key):
                response = self.delete(key, self.questions[key].pk + 1000)
                self.assertEqual(response.status_code, 404)
                self.assertFalse(response.json()['success'])

    def test_invalid_payloads_return_json_400_for_every_type(self):
        self.client.force_login(self.superuser)
        for key in QUESTION_CASES:
            id_key = 'topic_id' if key == 'sorting_ladder' else 'question_id'
            payloads = ['{', '[]', 'null', '{}'] + [
                json.dumps({id_key: value})
                for value in [None, True, 1.5, 'invalid', 0, -1, 2**63]
            ]
            for payload in payloads:
                with self.subTest(game=key, payload=payload):
                    response = self.client.post(
                        reverse('admin_dashboard:' + QUESTION_CASES[key][2]),
                        payload, content_type='application/json',
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json()['success'])
            self.questions[key].refresh_from_db()
            self.assertTrue(self.questions[key].is_active)

    def test_string_ids_remain_supported_for_every_type(self):
        self.client.force_login(self.owner)
        for key in QUESTION_CASES:
            with self.subTest(game=key):
                response = self.delete(key, str(self.questions[key].pk))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'])

    def test_used_questions_preserve_answers_participants_and_game_state(self):
        self.client.force_login(self.superuser)
        for key, (game_model, participant_model, answer_model, fields) in ANSWER_CASES.items():
            with self.subTest(game=key):
                question = self.questions[key]
                game = game_model.objects.create(creator=self.owner, title='Recorded game')
                game.selected_questions.add(question)
                participant = participant_model.objects.create(quiz=game, name='Player')
                if key == 'sorting_ladder':
                    SortingItem.objects.create(topic=question, text='Recorded item', correct_rank=1)
                answer = answer_model.objects.create(quiz=game, participant=participant, question=question, **fields)
                answer_before = answer_model.objects.filter(pk=answer.pk).values().get()
                participant_before = participant_model.objects.filter(pk=participant.pk).values().get()
                game_before = game_model.objects.filter(pk=game.pk).values().get()

                response = self.delete(key)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertTrue(response.json()['success'])
                question.refresh_from_db()
                self.assertFalse(question.is_active)
                self.assertTrue(game.selected_questions.filter(pk=question.pk).exists())
                self.assertEqual(answer_model.objects.filter(pk=answer.pk).values().get(), answer_before)
                self.assertEqual(participant_model.objects.filter(pk=participant.pk).values().get(), participant_before)
                self.assertEqual(game_model.objects.filter(pk=game.pk).values().get(), game_before)

    def test_login_and_post_and_csrf_requirements_are_preserved(self):
        self.client.force_login(self.owner)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        for key in QUESTION_CASES:
            with self.subTest(game=key):
                self.assertEqual(self.delete(key, client=Client()).status_code, 302)
                url = reverse('admin_dashboard:' + QUESTION_CASES[key][2])
                self.assertEqual(self.client.get(url).status_code, 405)
                self.assertEqual(self.delete(key, client=csrf_client).status_code, 403)
                self.questions[key].refresh_from_db()
                self.assertTrue(self.questions[key].is_active)
