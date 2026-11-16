from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class WwmSetMigrationTests(TransactionTestCase):
    def test_existing_scores_responses_drafts_and_running_set_survive(self):
        old = [('wer_weiss_mehr', '0004_werweissmehrroundresponse_time_taken'),
               ('games_hub', '0022_hubsession_creator')]
        new = [('wer_weiss_mehr', '0005_independent_sets')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-wwm')
            game = apps.get_model('wer_weiss_mehr', 'WerWeissMehrGame').objects.create(
                creator_id=user.pk, room_code='1984', status='active', started_at=timezone.now())
            question = apps.get_model('wer_weiss_mehr', 'WerWeissMehrQuestion').objects.create(
                created_by_id=user.pk, question_text='Legacy set')
            answer = apps.get_model('wer_weiss_mehr', 'WerWeissMehrAnswerOption').objects.create(
                question_id=question.pk, canonical_text='Bayern', normalized_text='bayern')
            game.current_question_id = question.pk
            game.save()
            hub = apps.get_model('games_hub', 'HubSession').objects.create(code='LEGACYWWM')
            apps.get_model('games_hub', 'HubGameStep').objects.create(
                session_id=hub.pk, game_key='wer_weiss_mehr', room_code=game.room_code, order=0)
            participant = apps.get_model('wer_weiss_mehr', 'WerWeissMehrParticipant').objects.create(
                quiz_id=game.pk, name='Team <West', hub_session_code=hub.code, total_score=2)
            state = apps.get_model('wer_weiss_mehr', 'WerWeissMehrParticipantState').objects.create(
                quiz_id=game.pk, question_id=question.pk, participant_id=participant.pk, survived_rounds=2)
            response = apps.get_model('wer_weiss_mehr', 'WerWeissMehrRoundResponse').objects.create(
                quiz_id=game.pk, question_id=question.pk, participant_id=participant.pk, round_number=2,
                answer_text='Bayern', matched_answer_id=answer.pk, is_correct=True, time_taken=1.2)
            draft = apps.get_model('wer_weiss_mehr', 'WerWeissMehrPendingInput').objects.create(
                quiz_id=game.pk, question_id=question.pk, participant_id=participant.pk, round_number=3,
                answer_text='Saarland')
            session = apps.get_model('wer_weiss_mehr', 'WerWeissMehrSession').objects.create(
                quiz_id=game.pk, current_round=3, phase='round_active',
                round_start_time=timezone.now(), round_end_time=timezone.now() + timezone.timedelta(seconds=30))
            saved = {obj._meta.model_name: dict(obj.__dict__) for obj in (participant, state, response, draft, session)}
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(new)
        apps = executor.loader.project_state(new).apps
        for model, values in saved.items():
            obj = apps.get_model('wer_weiss_mehr', model).objects.get(pk=values['id'])
            for field, value in values.items():
                if field != '_state':
                    self.assertEqual(getattr(obj, field), value, (model, field))
        from wer_weiss_mehr.models import WerWeissMehrGame
        from wer_weiss_mehr.services import build_game_state
        # Historical state assertions above target 0005. Current runtime code
        # requires the complete current schema (including Hub and WWM 0006).
        MigrationExecutor(connection).migrate(latest)
        game = WerWeissMehrGame.objects.get(pk=game.pk)
        snapshot = build_game_state(game, hub.code, participant.name)
        self.assertEqual(game.session.current_set_number, 0)
        self.assertEqual(game.session.hub_session_code, hub.code)
        self.assertEqual(snapshot['current_set_number'], 1)
        self.assertEqual(snapshot['current_set_id'], str(question.pk))
        self.assertEqual(snapshot['participant_state']['pending_answer'], 'Saarland')
        self.assertEqual(snapshot['participant_state']['total_score'], 2)
        self.assertEqual(snapshot['scorebox'][0]['scores'][0]['points'], 2)
