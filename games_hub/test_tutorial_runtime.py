from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from QuizGame.models import Quiz, QuizQuestion
from black_jack_quiz.models import BlackJackQuestion, BlackJackQuiz
from games_hub.check_in import complete_session_check_in, participant_check_in, start_session_check_in
from games_hub.models import (
    HubGameParticipantSnapshot,
    HubGameStep,
    HubGameTutorialRuntime,
    HubGameUnitTutorialRuntime,
    HubParticipant,
    HubSession,
)
from games_hub.tutorial_runtime import (
    activate_tutorial_runtime,
    force_close_tutorial_runtime,
    get_tutorial_payload,
    get_tutorial_start_warning,
    mark_tutorial_completed,
)
from games_hub.unit_tutorial_runtime import (
    TUTORIAL_QUESTION_MISSING_MESSAGE,
    TUTORIAL_SET_MISSING_MESSAGE,
    finish_current_unit_tutorial,
    get_scorebox_excluded_tutorial_question_ids,
    prepare_unit_tutorial_runtime,
    is_current_unit_tutorial_question,
    start_unit_tutorial_if_needed,
    validate_unit_tutorial_request,
)


class GameTutorialRuntimeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tutorial-runtime-user', password='pass')
        self.session = HubSession.objects.create(
            code='TUT1',
            name='Tutorial Session',
            is_active=True,
            started_at=timezone.now(),
        )
        self.quiz = Quiz.objects.create(
            title='Tutorial Quiz',
            creator=self.user,
            room_code='T001',
            tutorial_enabled=True,
            tutorial_title='Intro',
            tutorial_text='Read first.',
        )
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )

    def _complete_check_in(self, names):
        for name in names:
            HubParticipant.objects.create(session=self.session, nickname=name)
        self.assertTrue(start_session_check_in(self.session)['success'])
        for name in names:
            self.assertTrue(participant_check_in(self.session, name)['success'])
        self.assertTrue(complete_session_check_in(self.session)['success'])
        self.session.refresh_from_db()

    def test_runtime_activation_is_opt_in_only(self):
        payload = activate_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, self.quiz, False)

        self.quiz.refresh_from_db()
        runtime = HubGameTutorialRuntime.objects.get(game_step=self.step)
        self.assertIsNone(payload)
        self.assertFalse(self.quiz.tutorial_active)
        self.assertFalse(runtime.active)

    def test_empty_player_explanation_text_is_not_activated_even_when_requested(self):
        self.quiz.tutorial_enabled = True
        self.quiz.tutorial_title = 'Only a title'
        self.quiz.tutorial_text = ''
        self.quiz.save(update_fields=['tutorial_enabled', 'tutorial_title', 'tutorial_text'])

        payload = activate_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, self.quiz, True)

        self.quiz.refresh_from_db()
        runtime = HubGameTutorialRuntime.objects.get(game_step=self.step)
        self.assertIsNone(payload)
        self.assertFalse(self.quiz.tutorial_active)
        self.assertFalse(runtime.active)
        self.assertEqual(runtime.text, '')

    def test_runtime_uses_active_official_snapshot_and_persists_acknowledgement(self):
        self._complete_check_in(['Alice', 'Bob', 'Left'])
        HubParticipant.objects.filter(session=self.session, nickname='Left').update(
            left_permanently_at=timezone.now()
        )

        payload = activate_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, self.quiz, True)
        HubParticipant.objects.create(session=self.session, nickname='Late')

        self.quiz.refresh_from_db()
        self.assertTrue(self.quiz.tutorial_active)
        self.assertEqual(set(payload['official_participants']), {'Alice', 'Bob'})
        self.assertEqual(payload['completed'], 0)
        self.assertEqual(payload['total'], 2)
        self.assertEqual(HubGameParticipantSnapshot.objects.filter(game_step=self.step).count(), 3)
        self.assertIsNone(get_tutorial_payload('quiz', self.quiz.room_code, self.session.code, 'Left'))
        self.assertIsNone(get_tutorial_payload('quiz', self.quiz.room_code, self.session.code, 'Late'))

        progress = mark_tutorial_completed('quiz', self.quiz.room_code, self.session.code, 'Alice')
        by_name = {participant['name']: participant for participant in progress['participants']}

        self.assertEqual(progress['completed'], 1)
        self.assertEqual(progress['total'], 2)
        self.assertTrue(by_name['Alice']['completed'])
        self.assertFalse(by_name['Bob']['completed'])
        self.assertIsNone(get_tutorial_payload('quiz', self.quiz.room_code, self.session.code, 'Alice'))
        self.assertIsNotNone(get_tutorial_payload('quiz', self.quiz.room_code, self.session.code, 'Bob'))

    def test_start_warning_only_exists_for_open_official_acknowledgements(self):
        self._complete_check_in(['Alice', 'Bob'])
        activate_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, self.quiz, True)
        mark_tutorial_completed('quiz', self.quiz.room_code, self.session.code, 'Alice')

        warning = get_tutorial_start_warning('quiz', self.quiz.room_code, self.session.code)

        self.assertIsNotNone(warning)
        self.assertEqual(warning['message'], 'Nicht alle Teilnehmer haben die Erläuterung bestätigt')
        self.assertEqual(warning['completed'], 1)
        self.assertEqual(warning['total'], 2)

        mark_tutorial_completed('quiz', self.quiz.room_code, self.session.code, 'Bob')
        self.assertIsNone(get_tutorial_start_warning('quiz', self.quiz.room_code, self.session.code))

    def test_force_close_deactivates_runtime_and_hides_rejoin_payload(self):
        self._complete_check_in(['Alice', 'Bob'])
        activate_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, self.quiz, True)

        closed = force_close_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, self.quiz)

        self.assertTrue(closed)
        self.quiz.refresh_from_db()
        runtime = HubGameTutorialRuntime.objects.get(game_step=self.step)
        self.assertFalse(self.quiz.tutorial_active)
        self.assertFalse(runtime.active)
        self.assertIsNotNone(runtime.completed_at)
        self.assertIsNone(get_tutorial_payload('quiz', self.quiz.room_code, self.session.code, 'Alice'))

    def test_unit_tutorial_runtime_defaults_to_not_requested(self):
        result = prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, False)

        runtime = HubGameUnitTutorialRuntime.objects.get(game_step=self.step)
        self.assertTrue(result['success'])
        self.assertFalse(runtime.requested)
        self.assertIsNone(runtime.tutorial_question_id)
        self.assertFalse(runtime.tutorial_has_been_played)
        self.assertFalse(runtime.current_unit_is_tutorial)

    def test_unit_tutorial_request_without_configured_question_blocks_start(self):
        result = validate_unit_tutorial_request('quiz', self.quiz.room_code, True)

        self.assertFalse(result['success'])
        self.assertEqual(result['type'], 'tutorial_question_missing')
        self.assertEqual(result['message'], TUTORIAL_QUESTION_MISSING_MESSAGE)
        self.assertFalse(HubGameUnitTutorialRuntime.objects.exists())

    def test_unit_tutorial_request_without_blackjack_tutorial_set_blocks_start(self):
        blackjack = BlackJackQuiz.objects.create(
            title='Black Jack without tutorial set',
            creator=self.user,
            room_code='BJ01',
        )
        HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='blackjack',
            room_code=blackjack.room_code,
            title=blackjack.title,
        )

        result = validate_unit_tutorial_request('blackjack', blackjack.room_code, True)

        self.assertFalse(result['success'])
        self.assertEqual(result['type'], 'tutorial_question_missing')
        self.assertEqual(result['message'], TUTORIAL_SET_MISSING_MESSAGE)
        self.assertFalse(
            HubGameUnitTutorialRuntime.objects.filter(game_step__room_code=blackjack.room_code).exists()
        )

    def test_unit_tutorial_request_with_configured_question_persists_run_state(self):
        question = QuizQuestion.objects.create(
            question_text='Tutorial question',
            question_type='short_answer',
            correct_answer='Answer',
            created_by=self.user,
        )
        self.quiz.tutorial_question = question
        self.quiz.save(update_fields=['tutorial_question'])

        result = prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, True)

        runtime = HubGameUnitTutorialRuntime.objects.get(game_step=self.step)
        self.assertTrue(result['success'])
        self.assertTrue(runtime.requested)
        self.assertEqual(runtime.tutorial_question_id, question.id)
        self.assertFalse(runtime.tutorial_has_been_played)
        self.assertFalse(runtime.current_unit_is_tutorial)

    def test_configured_tutorial_question_is_excluded_from_regular_scorebox_sources(self):
        tutorial_question = QuizQuestion.objects.create(
            question_text='Tutorial question',
            question_type='short_answer',
            correct_answer='Answer',
            created_by=self.user,
        )
        scored_question = QuizQuestion.objects.create(
            question_text='Scored question',
            question_type='short_answer',
            correct_answer='Score',
            created_by=self.user,
        )
        self.quiz.selected_questions.set([tutorial_question, scored_question])
        self.quiz.tutorial_question = tutorial_question
        self.quiz.save(update_fields=['tutorial_question'])

        excluded_ids = get_scorebox_excluded_tutorial_question_ids('quiz', self.quiz.room_code, self.session.code)

        self.assertEqual(excluded_ids, {tutorial_question.id})

    def test_blackjack_unit_tutorial_uses_configured_tutorial_set(self):
        tutorial_questions = [
            BlackJackQuestion.objects.create(
                question_text=f'Tutorial set question {index}',
                correct_answer=10 + index,
                created_by=self.user,
            )
            for index in range(2)
        ]
        scored_question = BlackJackQuestion.objects.create(
            question_text='Scored set question',
            correct_answer=21,
            created_by=self.user,
        )
        blackjack = BlackJackQuiz.objects.create(
            title='Black Jack Tutorial Set',
            creator=self.user,
            room_code='BJ02',
            question_order=[
                [question.id for question in tutorial_questions],
                [scored_question.id],
            ],
            tutorial_set_number=1,
        )
        blackjack.selected_questions.set([*tutorial_questions, scored_question])
        HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='blackjack',
            room_code=blackjack.room_code,
            title=blackjack.title,
        )

        result = prepare_unit_tutorial_runtime('blackjack', blackjack.room_code, self.session.code, True)

        blackjack.refresh_from_db()
        runtime = HubGameUnitTutorialRuntime.objects.get(game_step__room_code=blackjack.room_code)
        self.assertTrue(result['success'])
        self.assertEqual(blackjack.tutorial_set_number, 1)
        self.assertEqual(blackjack.get_tutorial_set_question_ids(active_only=False), [q.id for q in tutorial_questions])
        self.assertEqual(
            get_scorebox_excluded_tutorial_question_ids('blackjack', blackjack.room_code, self.session.code),
            {question.id for question in tutorial_questions},
        )
        self.assertTrue(runtime.requested)
        self.assertEqual(runtime.tutorial_question_id, tutorial_questions[0].id)
        self.assertFalse(runtime.tutorial_has_been_played)
        self.assertFalse(runtime.current_unit_is_tutorial)

    def test_unit_tutorial_runtime_starts_finishes_and_does_not_restart(self):
        question = QuizQuestion.objects.create(
            question_text='Tutorial question',
            question_type='short_answer',
            correct_answer='Answer',
            created_by=self.user,
        )
        self.quiz.tutorial_question = question
        self.quiz.save(update_fields=['tutorial_question'])
        prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, True)

        started = start_unit_tutorial_if_needed('quiz', self.quiz.room_code, self.session.code)
        runtime = HubGameUnitTutorialRuntime.objects.get(game_step=self.step)

        self.assertTrue(started['is_tutorial_round'])
        self.assertEqual(started['tutorial_question_id'], question.id)
        self.assertTrue(runtime.current_unit_is_tutorial)
        self.assertTrue(is_current_unit_tutorial_question('quiz', self.quiz.room_code, self.session.code, question.id))
        self.assertFalse(runtime.tutorial_has_been_played)

        finished = finish_current_unit_tutorial('quiz', self.quiz.room_code, self.session.code)
        runtime.refresh_from_db()
        restarted = start_unit_tutorial_if_needed('quiz', self.quiz.room_code, self.session.code)

        self.assertTrue(finished['is_tutorial_round'])
        self.assertFalse(runtime.current_unit_is_tutorial)
        self.assertFalse(is_current_unit_tutorial_question('quiz', self.quiz.room_code, self.session.code, question.id))
        self.assertTrue(runtime.tutorial_has_been_played)
        self.assertFalse(restarted['is_tutorial_round'])

    def test_unit_tutorial_runtime_clears_previous_request_on_default_start(self):
        question = QuizQuestion.objects.create(
            question_text='Tutorial question',
            question_type='short_answer',
            correct_answer='Answer',
            created_by=self.user,
        )
        self.quiz.tutorial_question = question
        self.quiz.save(update_fields=['tutorial_question'])
        prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, True)

        result = prepare_unit_tutorial_runtime('quiz', self.quiz.room_code, self.session.code, False)

        runtime = HubGameUnitTutorialRuntime.objects.get(game_step=self.step)
        self.assertTrue(result['success'])
        self.assertFalse(runtime.requested)
        self.assertIsNone(runtime.tutorial_question_id)
