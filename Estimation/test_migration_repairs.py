from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class EstimationScoreMigrationTests(TransactionTestCase):
    def test_existing_manual_totals_answers_and_progress_are_preserved(self):
        old = [('Estimation', '0016_estimationquiz_tutorial_question')]
        new = [('Estimation', '0017_rounds_and_score_adjustment')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-score')
            quiz = apps.get_model('Estimation', 'EstimationQuiz').objects.create(creator_id=user.pk, room_code='1984')
            participant = apps.get_model('Estimation', 'EstimationParticipant').objects.create(
                quiz_id=quiz.pk, name='Legacy', total_score=7)
            question = apps.get_model('Estimation', 'EstimationQuestion').objects.create(
                created_by_id=user.pk, question_text='Legacy estimate', correct_answer=100)
            apps.get_model('Estimation', 'EstimationAnswer').objects.create(
                quiz_id=quiz.pk, participant_id=participant.pk, question_id=question.pk,
                user_answer=100, points_earned=1)
            apps.get_model('Estimation', 'EstimationSession').objects.create(
                quiz_id=quiz.pk, current_question_number=3)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        saved = apps.get_model('Estimation', 'EstimationParticipant').objects.get(pk=participant.pk)
        self.assertEqual(saved.total_score, 7)
        self.assertEqual(saved.score_adjustment, 6)
        self.assertEqual(apps.get_model('Estimation', 'EstimationAnswer').objects.get(participant_id=participant.pk).round_number, 0)
        self.assertEqual(apps.get_model('Estimation', 'EstimationSession').objects.get(quiz_id=quiz.pk).current_question_number, 3)
