from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class WhoThatScoreMigrationTests(TransactionTestCase):
    def test_existing_manual_totals_answers_and_progress_are_preserved(self):
        old = [('who_is_that', '0014_whothatquiz_tutorial_question')]
        new = [('who_is_that', '0015_rounds_and_score_adjustment')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-score')
            quiz = apps.get_model('who_is_that', 'WhoThatQuiz').objects.create(creator_id=user.pk, room_code='1984')
            participant = apps.get_model('who_is_that', 'WhoThatParticipant').objects.create(
                quiz_id=quiz.pk, name='Legacy', total_score=7)
            question = apps.get_model('who_is_that', 'WhoThatQuestion').objects.create(
                created_by_id=user.pk, correct_answer='Ada', image='old.png')
            apps.get_model('who_is_that', 'WhoThatAnswer').objects.create(
                quiz_id=quiz.pk, participant_id=participant.pk, question_id=question.pk,
                user_answer='Ada', is_correct=True, points_earned=1)
            apps.get_model('who_is_that', 'WhoThatSession').objects.create(
                quiz_id=quiz.pk, current_question_number=3)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        saved = apps.get_model('who_is_that', 'WhoThatParticipant').objects.get(pk=participant.pk)
        self.assertEqual(saved.total_score, 7)
        self.assertEqual(saved.score_adjustment, 6)
        self.assertEqual(apps.get_model('who_is_that', 'WhoThatAnswer').objects.get(participant_id=participant.pk).round_number, 0)
        self.assertEqual(apps.get_model('who_is_that', 'WhoThatSession').objects.get(quiz_id=quiz.pk).current_question_number, 3)
