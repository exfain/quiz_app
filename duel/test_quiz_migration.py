"""The new material/state tables migrate forwards/backwards without MP writes."""
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from .test_quiz import make_quiz_material
from QuizGame.models import Quiz, QuizParticipant, QuizQuestion


class QuizDuelMigrationTests(TransactionTestCase):
    def test_additive_round_trip_keeps_existing_quiz_rows_and_schema(self):
        make_quiz_material(self)
        QuizParticipant.objects.create(quiz=self.quiz, name='Existing MP', total_score=17)
        models = (Quiz, QuizQuestion, QuizParticipant)
        snapshots = {m: list(m.objects.order_by('pk').values()) for m in models}
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [n for n in latest if n[0] != 'duel'] + [('duel', '0010_duelestimationroundstate')]
        after = [n for n in latest if n[0] != 'duel'] + [('duel', '0011_quick_quiz_duel')]
        migration = executor.loader.get_migration('duel', '0011_quick_quiz_duel')
        self.assertEqual(migration.dependencies, [
            ('QuizGame', '0003_quiz_selected_questions'), ('duel', '0010_duelestimationroundstate')])
        self.assertEqual(len(migration.operations), 2)
        self.assertTrue(all(isinstance(op, migrations.CreateModel) for op in migration.operations))
        executor.migrate(before)
        try:
            tables = set(connection.introspection.table_names())
            with connection.cursor() as cursor:
                schemas = {m: connection.introspection.get_table_description(cursor, m._meta.db_table) for m in models}
            executor = MigrationExecutor(connection); executor.migrate(after)
            self.assertEqual(set(connection.introspection.table_names()) - tables,
                             {'duel_duelquizquestionbinding', 'duel_duelquizroundstate'})
            for m in models:
                self.assertEqual(list(m.objects.order_by('pk').values()), snapshots[m])
                with connection.cursor() as cursor:
                    self.assertEqual(connection.introspection.get_table_description(cursor, m._meta.db_table), schemas[m])
            MigrationExecutor(connection).migrate(before)
            self.assertEqual(set(connection.introspection.table_names()), tables)
            for m in models:self.assertEqual(list(m.objects.order_by('pk').values()), snapshots[m])
        finally:
            MigrationExecutor(connection).migrate(latest)
