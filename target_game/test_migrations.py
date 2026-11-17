from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from games_hub.models import HubSession,HubGameStep


class TargetMigrationTests(TransactionTestCase):
    def test_additive_round_trip_keeps_hub_data_and_schema(self):
        executor=MigrationExecutor(connection);latest=executor.loader.graph.leaf_nodes()
        before=[n for n in latest if n[0]!='target_game']+[('target_game',None)]
        executor.migrate(before)
        try:
            hub=HubSession.objects.create(code='TARGET-MIGRATE')
            step=HubGameStep.objects.create(session=hub,order=0,game_key='quiz')
            original=list(HubGameStep.objects.filter(pk=step.pk).values())
            tables=set(connection.introspection.table_names())
            disposable=HubSession.objects.create(code='NO-TARGET-TABLE');disposable.delete()
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(set(connection.introspection.table_names())-tables,{
                'target_game_targetgame','target_game_task','target_game_run','target_game_round','target_game_enrollment',
                'target_game_teamround','target_game_answer','target_game_receipt'})
            apps=MigrationExecutor(connection).loader.project_state(latest).apps
            Game=apps.get_model('target_game','TargetGame');Task=apps.get_model('target_game','Task')
            user=User.objects.create(username='migration-target')
            game=Game.objects.create(creator_id=user.pk,title='Zielansage',answer_seconds=30,scoring_mode='ranking')
            task=Task.objects.create(game=game,question_text='Stadt?',solutions=[{'text':'München','aliases':['Munich']}])
            self.assertEqual(Task.objects.get(pk=task.pk).solutions[0]['aliases'],['Munich'])
            MigrationExecutor(connection).migrate(before)
            self.assertEqual(set(connection.introspection.table_names()),tables)
            self.assertEqual(list(HubGameStep.objects.filter(pk=step.pk).values()),original)
        finally:MigrationExecutor(connection).migrate(latest)
