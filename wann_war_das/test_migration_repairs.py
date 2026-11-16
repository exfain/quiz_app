from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class WannWarDasMigrationRepairs(TransactionTestCase):
    def test_legacy_scores_answers_and_unknown_round_slots_are_preserved(self):
        old = [('wann_war_das', '0001_initial')]
        new = [('wann_war_das', '0002_rounds_and_pending_inputs')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-wann')
            game = apps.get_model('wann_war_das', 'WannWarDasGame').objects.create(
                creator_id=user.pk, room_code='9876', status='active', current_question_number=3,
                active_hub_session_code='OLD', started_at=timezone.now())
            player = apps.get_model('wann_war_das', 'WannWarDasParticipant').objects.create(
                quiz_id=game.pk, name='Legacy', hub_session_code='OLD', total_score=17)
            question = apps.get_model('wann_war_das', 'WannWarDasQuestion').objects.create(
                created_by_id=user.pk, question_text='Frühere Frage', correct_answer=2000)
            answer = apps.get_model('wann_war_das', 'WannWarDasAnswer').objects.create(
                quiz_id=game.pk, participant_id=player.pk, question_id=question.pk,
                hub_session_code='OLD', raw_answer='2000', points_earned=10, is_correct=True)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        saved_game = apps.get_model('wann_war_das', 'WannWarDasGame').objects.get(pk=game.pk)
        saved_answer = apps.get_model('wann_war_das', 'WannWarDasAnswer').objects.get(pk=answer.pk)
        saved_player = apps.get_model('wann_war_das', 'WannWarDasParticipant').objects.get(pk=player.pk)
        self.assertEqual(saved_player.total_score, 17)
        self.assertEqual(saved_answer.points_earned, 10)
        self.assertEqual(saved_answer.round_number, 3)
        self.assertEqual(saved_game.current_question_number, 3)
        self.assertEqual(len(saved_game.question_rounds), 3)
        self.assertEqual(saved_game.question_rounds[-1]['question_id'], question.pk)
