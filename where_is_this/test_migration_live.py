from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class WhereMigrationTests(TransactionTestCase):
    def test_existing_scores_answers_and_unknown_question_slots_survive(self):
        old = [('where_is_this', '0013_whereanswer_rank_position_whereanswer_scoring_mode_and_more')]
        new = [('where_is_this', '0014_live_rounds_and_zones')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-where')
            question = apps.get_model('where_is_this', 'WhereQuestion').objects.create(created_by_id=user.pk,
                question_text='Alte Frage', correct_latitude=52, correct_longitude=13)
            game = apps.get_model('where_is_this', 'WhereQuiz').objects.create(creator_id=user.pk,
                room_code='9872', status='active', current_question_id=question.pk, started_at=timezone.now())
            player = apps.get_model('where_is_this', 'WhereParticipant').objects.create(quiz_id=game.pk,
                name='Legacy', hub_session_code='OLD', total_score=17)
            session = apps.get_model('where_is_this', 'WhereSession').objects.create(quiz_id=game.pk,
                current_question_number=3, total_questions_sent=3, is_question_active=True)
            answer = apps.get_model('where_is_this', 'WhereAnswer').objects.create(quiz_id=game.pk,
                participant_id=player.pk, question_id=question.pk, user_latitude=52, user_longitude=13,
                distance_km=0, points_earned=10)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        player = apps.get_model('where_is_this', 'WhereParticipant').objects.get(pk=player.pk)
        answer = apps.get_model('where_is_this', 'WhereAnswer').objects.get(pk=answer.pk)
        session = apps.get_model('where_is_this', 'WhereSession').objects.get(pk=session.pk)
        game = apps.get_model('where_is_this', 'WhereQuiz').objects.get(pk=game.pk)
        self.assertEqual(player.total_score, 17)
        self.assertEqual(answer.points_earned, 10)
        self.assertEqual(answer.round_number, 3)
        self.assertEqual(session.current_question_number, 3)
        self.assertEqual(len(session.question_rounds), 3)
        self.assertEqual(session.question_rounds[-1]['question_id'], question.pk)
        self.assertEqual(game.active_hub_session_code, 'OLD')
        self.assertIsNone(game.zone_count)
