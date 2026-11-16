from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BuzzerAwardMigrationTests(TransactionTestCase):
    def test_saved_scores_and_rounds_survive_compatible_award_snapshot(self):
        old = [('buzzer', '0001_initial')]
        new = [('buzzer', '0002_round_awarded_points')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-buzzer')
            game = apps.get_model('buzzer', 'BuzzerGame').objects.create(
                creator_id=user.pk, room_code='8881', points_per_correct=3)
            player = apps.get_model('buzzer', 'BuzzerParticipant').objects.create(
                quiz_id=game.pk, name='Alice', total_score=3)
            round_obj = apps.get_model('buzzer', 'BuzzerRound').objects.create(
                quiz_id=game.pk, round_number=1, status='answered', correct_participant_id=player.pk)
            saved = {obj._meta.model_name: dict(obj.__dict__) for obj in [game, player, round_obj]}
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        for name, values in saved.items():
            obj = apps.get_model('buzzer', name).objects.get(pk=values['id'])
            for field, value in values.items():
                if field != '_state':
                    self.assertEqual(getattr(obj, field), value, (name, field))
        self.assertEqual(apps.get_model('buzzer', 'BuzzerRound').objects.get(pk=round_obj.pk).points_awarded, 3)
