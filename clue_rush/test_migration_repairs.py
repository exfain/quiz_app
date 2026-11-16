from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ClueRushMigrationTests(TransactionTestCase):
    def test_existing_manual_score_answers_and_progress_survive(self):
        old = [('clue_rush', '0014_persistent_clue_schedule')]
        new = [('clue_rush', '0015_rounds_pause_and_score_adjustment')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='clue-legacy')
            quiz = apps.get_model('clue_rush', 'ClueRushGame').objects.create(creator_id=user.pk, room_code='1987')
            participant = apps.get_model('clue_rush', 'ClueRushParticipant').objects.create(quiz_id=quiz.pk, name='Legacy', total_score=7)
            question = apps.get_model('clue_rush', 'ClueQuestion').objects.create(created_by_id=user.pk, answer='Ada', question_text='Legacy')
            apps.get_model('clue_rush', 'ClueAnswer').objects.create(quiz_id=quiz.pk, participant_id=participant.pk,
                question_id=question.pk, answer_text='Ada', is_correct=True, points_earned=3)
            apps.get_model('clue_rush', 'ClueRushSession').objects.create(quiz_id=quiz.pk, current_question_number=3)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        saved = apps.get_model('clue_rush', 'ClueRushParticipant').objects.get(pk=participant.pk)
        self.assertEqual((saved.total_score, saved.score_adjustment), (7, 4))
        self.assertEqual(apps.get_model('clue_rush', 'ClueAnswer').objects.get(participant_id=participant.pk).round_number, 0)
        self.assertEqual(apps.get_model('clue_rush', 'ClueRushSession').objects.get(quiz_id=quiz.pk).current_question_number, 3)
