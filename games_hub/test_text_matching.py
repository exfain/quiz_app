import json

from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from admin_dashboard.models import DashboardSettings
from .models import HubSession, HubGameStep
from .text_matching import matches, similarity, validate_threshold


class TextSimilarityTests(SimpleTestCase):
    def test_threshold_inclusive_and_full_string_comparison(self):
        self.assertTrue(matches('Albert Einstien', 'Albert Einstein', threshold=90))
        self.assertFalse(matches('Albert Einstien', 'Albert Einstein', threshold=100))
        self.assertFalse(matches('Einstein', 'Albert Einstein', threshold=90))
        self.assertTrue(matches('  BERLIN ', 'Berlin', threshold=100))
        self.assertFalse(matches('', '', threshold=65))
        self.assertFalse(matches('   ', 'Berlin', threshold=65))
        score = similarity('abcdefghij', 'abcdefghiX')
        self.assertEqual(score, 90)
        self.assertTrue(matches('abcdefghij', 'abcdefghiX', threshold=90))
        self.assertFalse(matches('abcdefghij', 'abcdefghiX', threshold=91))

    def test_numeric_answers_never_receive_fuzzy_credit(self):
        for value, expected in [('1985', '1984'), ('1.24', '1.25'), ('-5', '5'), ('100', '1000')]:
            with self.subTest(value=value, expected=expected):
                self.assertFalse(matches(value, expected, threshold=65))
        self.assertTrue(matches(' 1984 ', '1984', threshold=65))

    def test_numbers_inside_text_must_match_in_order_including_sign_and_decimal(self):
        self.assertTrue(matches('Appollo 11', 'Apollo 11', threshold=90))
        for value, expected in [('Apollo 12', 'Apollo 11'), ('Jahr 1985', 'Jahr 1984'),
                                ('Wert 1.25', 'Wert 1.24'), ('Wert 5', 'Wert -5'),
                                ('Route 10 bis 5', 'Route 5 bis 10'), ('Apollo', 'Apollo 11')]:
            with self.subTest(value=value):
                self.assertFalse(matches(value, expected, threshold=65))

    def test_invalid_thresholds_are_rejected(self):
        for value in [64, 101, 90.5, True, None, 'NaN', '90.0', {}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_threshold(value)
        self.assertEqual(validate_threshold('65'), 65)
        self.assertEqual(validate_threshold(100), 100)


class TextMatchingSettingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('text-setting-host', is_staff=True)
        self.client.force_login(self.user)
        self.defaults = DashboardSettings.load()

    def session(self, code='MATCH', **values):
        return HubSession.objects.create(creator=self.user, code=code, **values)

    def update(self, session, value):
        return self.client.post(reverse('games_hub:update_session_text_matching', args=[session.code]),
            json.dumps({'text_match_threshold': value}), content_type='application/json')

    def test_new_sessions_snapshot_current_default_and_never_follow_later_changes(self):
        first = self.session()
        self.assertEqual(first.text_match_threshold, 90)
        self.defaults.text_match_threshold = 75
        self.defaults.save()
        second = self.session('SECOND')
        first.refresh_from_db()
        self.assertEqual((first.text_match_threshold, second.text_match_threshold), (90, 75))

    def test_creation_form_uses_default_but_override_only_changes_new_session(self):
        response = self.client.get(reverse('games_hub:create_session'))
        self.assertContains(response, 'id="sessionTextMatchThreshold"')
        self.assertEqual(response.context['text_match_threshold'], 90)
        response = self.client.post(reverse('games_hub:create_session'),
            {'code': 'CUSTOM', 'name': 'Session', 'text_match_threshold': 83, 'games_order': '[]'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(HubSession.objects.get(code='CUSTOM').text_match_threshold, 83)
        self.defaults.refresh_from_db()
        self.assertEqual(self.defaults.text_match_threshold, 90)

    def test_default_change_warns_about_running_session_and_preserves_its_value(self):
        session = self.session(started_at=timezone.now())
        response = self.client.post(reverse('admin_dashboard:settings'), {'text_match_threshold': 80}, follow=True)
        self.assertContains(response, 'laufende Session ist von dieser Änderung nicht betroffen')
        session.refresh_from_db()
        self.assertEqual(session.text_match_threshold, 90)
        self.defaults.refresh_from_db()
        self.assertEqual(self.defaults.text_match_threshold, 80)

    def test_session_value_can_change_after_completed_game_but_not_during_active_or_paused_game(self):
        from QuizGame.models import Quiz
        session = self.session(started_at=timezone.now())
        game = Quiz.objects.create(creator=self.user, status='completed', started_at=timezone.now())
        HubGameStep.objects.create(session=session, game_key='quiz', room_code=game.room_code, order=0)
        self.assertEqual(self.update(session, 70).status_code, 200)
        for status in ['active', 'inactive']:
            game.status = status
            game.save(update_fields=['status'])
            response = self.update(session, 80)
            self.assertEqual(response.status_code, 409)
            session.refresh_from_db()
            self.assertEqual(session.text_match_threshold, 70)

    def test_ended_sessions_and_foreign_users_cannot_change_policy(self):
        session = self.session(ended_at=timezone.now())
        self.assertEqual(self.update(session, 80).status_code, 409)
        other = User.objects.create_user('other-text-host', is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.update(session, 80).status_code, 403)
        self.client.logout()
        self.assertEqual(self.update(session, 80).status_code, 302)

    def test_invalid_values_never_persist(self):
        session = self.session()
        for value in [64, 101, 89.5, True, None, 'bad']:
            self.assertEqual(self.update(session, value).status_code, 400)
            self.assertEqual(self.client.post(reverse('admin_dashboard:settings'), {'text_match_threshold': '' if value is None else value}).status_code, 400)
        session.refresh_from_db()
        self.defaults.refresh_from_db()
        self.assertEqual((session.text_match_threshold, self.defaults.text_match_threshold), (90, 90))


class TextMatchingGameTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('match-host')
        self.hub = HubSession.objects.create(code='TEXTGAME', creator=self.user, text_match_threshold=90)

    def test_quick_quiz_multifield_grade_is_persisted_and_manual_override_survives_policy_change(self):
        from QuizGame.models import Quiz, QuizParticipant, QuizQuestion, QuizAnswer
        game = Quiz.objects.create(creator=self.user)
        player = QuizParticipant.objects.create(quiz=game, name='Team', hub_session_code=self.hub.code)
        question = QuizQuestion.objects.create(created_by=self.user, question_text='Name und Jahr',
            question_type='short_answer', correct_answer='Albert Einstein', correct_answer_2='1984')
        answer = QuizAnswer.objects.create(quiz=game, participant=player, question=question,
            answer_text=json.dumps({'answer_1': 'Albert Einstien', 'answer_2': '1985'}))
        self.assertEqual(answer.points_earned, 1)
        self.assertEqual([f['auto_correct'] for f in answer.field_results], [True, False])
        self.hub.text_match_threshold = 100
        self.hub.save()
        self.assertEqual([f['is_correct'] for f in answer.get_short_answer_field_results()], [True, False])
        answer.set_short_answer_field_correctness('answer_2', True)
        self.assertEqual(answer.points_earned, 2)
        self.assertTrue(answer.get_short_answer_field_results()[1]['manual_override'])

    def test_who_is_that_uses_threshold_and_alternatives_without_fuzzy_numbers(self):
        from who_is_that.models import WhoThatQuiz, WhoThatQuestion, WhoThatParticipant, WhoThatAnswer
        game = WhoThatQuiz.objects.create(creator=self.user)
        player = WhoThatParticipant.objects.create(quiz=game, name='Team', hub_session_code=self.hub.code)
        question = WhoThatQuestion.objects.create(created_by=self.user, question_text='Wer?', correct_answer='Albert Einstein',
            alternative_answers=['Professor Einstein'])
        answer = WhoThatAnswer.objects.create(quiz=game, participant=player, question=question, user_answer='Albert Einstien')
        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 1)
        self.assertTrue(question.check_answer('Profesor Einstein', threshold=90))
        self.assertFalse(question.check_answer('Apollo 12', threshold=65))

    def test_clue_rush_applies_session_policy_to_answer_creation(self):
        from clue_rush.models import ClueRushGame, ClueQuestion, Clue, ClueRushParticipant, ClueAnswer
        game = ClueRushGame.objects.create(creator=self.user)
        player = ClueRushParticipant.objects.create(quiz=game, name='Team', hub_session_code=self.hub.code)
        question = ClueQuestion.objects.create(created_by=self.user, question_text='Wer?', answer='Albert Einstein')
        for i in range(3):
            Clue.objects.create(clue_question=question, clue_text='Hinweis', order=i + 1)
        answer = ClueAnswer.objects.create(quiz=game, participant=player, question=question,
            answer_text='Albert Einstien', submitted_clue_number=1, total_clues_at_submission=3)
        self.assertTrue(answer.is_correct)
        self.assertEqual(answer.points_earned, 3)

    def test_wer_weiss_mehr_uses_unique_best_match_and_flags_ties_for_host(self):
        from wer_weiss_mehr.models import WerWeissMehrGame, WerWeissMehrQuestion, WerWeissMehrAnswerOption, WerWeissMehrParticipant, WerWeissMehrRoundResponse
        game = WerWeissMehrGame.objects.create(creator=self.user)
        player = WerWeissMehrParticipant.objects.create(quiz=game, name='Team', hub_session_code=self.hub.code)
        question = WerWeissMehrQuestion.objects.create(created_by=self.user, question_text='Namen')
        option = WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Albert Einstein')
        response = WerWeissMehrRoundResponse.objects.create(quiz=game, participant=player, question=question,
            round_number=1, answer_text='Albert Einstien')
        response.evaluate()
        self.assertEqual(response.matched_answer_id, option.pk)
        self.assertTrue(response.is_correct)
        # Two equally strong aliases must never be resolved by database order.
        option.aliases = ['Einstein']
        option.save()
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Andere Person', aliases=['Einstein'])
        response.answer_text = 'Einsteinn'
        response.evaluate()
        self.assertFalse(response.is_correct)
        self.assertTrue(response.needs_manual_review)
        self.assertIsNone(response.matched_answer_id)
        from types import SimpleNamespace
        from wer_weiss_mehr.models import WerWeissMehrSession
        from wer_weiss_mehr.services import _serialize_own_state, _serialize_response
        own = _serialize_own_state(game, player, question, None, response, None,
            SimpleNamespace(phase=WerWeissMehrSession.PHASE_ROUND_ACTIVE))
        self.assertFalse(own['response']['needs_manual_review'])
        self.assertTrue(_serialize_response(response)['needs_manual_review'])
        response.apply_manual_correction(option)
        self.assertTrue(response.is_correct)
        self.assertTrue(response.is_manual_override)

    def test_standalone_games_keep_exact_answer_policy(self):
        from clue_rush.models import ClueRushGame, ClueQuestion, ClueRushParticipant, ClueAnswer
        game = ClueRushGame.objects.create(creator=self.user)
        player = ClueRushParticipant.objects.create(quiz=game, name='Team')
        question = ClueQuestion.objects.create(created_by=self.user, question_text='Wer?', answer='Albert Einstein')
        answer = ClueAnswer.objects.create(quiz=game, participant=player, question=question, answer_text='Albert Einstien')
        self.assertFalse(answer.is_correct)
