from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class BlackJackMigrationRepairs(TransactionTestCase):
    def test_existing_answers_corrections_scores_timer_and_scope_survive(self):
        old = [('black_jack_quiz', '0017_blackjackquiz_tutorial_set_number'),
            ('games_hub', '0022_hubsession_creator')]
        new = [('black_jack_quiz', '0018_persisted_set_runs')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-blackjack')
            hub = apps.get_model('games_hub', 'HubSession').objects.create(code='LEGBJ', started_at=timezone.now())
            game = apps.get_model('black_jack_quiz', 'BlackJackQuiz').objects.create(
                creator_id=user.pk, room_code='2841', status='active', started_at=timezone.now())
            apps.get_model('games_hub', 'HubGameStep').objects.create(
                session_id=hub.pk, game_key='blackjack', room_code=game.room_code, order=0)
            question = apps.get_model('black_jack_quiz', 'BlackJackQuestion').objects.create(
                created_by_id=user.pk, question_text='Legacy', correct_answer=10)
            game.current_question_id = question.pk
            game.question_order = [[question.pk]]
            game.save()
            participant = apps.get_model('black_jack_quiz', 'BlackJackParticipant').objects.create(
                quiz_id=game.pk, name='Team <West', hub_session_code=hub.code,
                total_points=7, overall_points=12, final_score=14)
            answer = apps.get_model('black_jack_quiz', 'BlackJackAnswer').objects.create(
                quiz_id=game.pk, participant_id=participant.pk, question_id=question.pk,
                user_answer=9, points_earned=1, question_number=1)
            session = apps.get_model('black_jack_quiz', 'BlackJackSession').objects.create(
                quiz_id=game.pk, current_question_number=1, total_questions_sent=1,
                asked_question_ids=[question.pk], is_question_active=True,
                question_end_time=timezone.now() + timezone.timedelta(seconds=30))
            saved = {obj._meta.model_name: dict(obj.__dict__) for obj in (game, participant, answer, session)}
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        for model, values in saved.items():
            obj = apps.get_model('black_jack_quiz', model).objects.get(pk=values['id'])
            for field, value in values.items():
                if field != '_state':
                    self.assertEqual(getattr(obj, field), value, (model, field))
        self.assertEqual(apps.get_model('black_jack_quiz', 'BlackJackQuiz').objects.get(pk=game.pk).active_hub_session_code, hub.code)
        self.assertEqual(apps.get_model('black_jack_quiz', 'BlackJackAnswer').objects.get(pk=answer.pk).set_round_number, 0)
