"""Additive binding schema, rollback and unmigrated parent deletion safety."""
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from Estimation.models import EstimationQuestion, EstimationQuiz
from .models import DuelUnit


class EstimationBindingMigrationTests(TransactionTestCase):
    def test_additive_migration_round_trip_keeps_existing_schema_and_data(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        other = [node for node in latest if node[0] != "duel"]
        before, after = [*other, ("duel", "0008_duelmatchoutcome")], [*other, ("duel", "0009_duelestimationquestionbinding")]
        migration = executor.loader.get_migration("duel", "0009_duelestimationquestionbinding")
        self.assertEqual(migration.dependencies, [
            ("Estimation", "0002_estimationquiz_selected_questions"),
            ("duel", "0008_duelmatchoutcome"),
        ])
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.CreateModel)
        self.assertEqual(migration.operations[0].name, "DuelEstimationQuestionBinding")
        table = "duel_duelestimationquestionbinding"
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before)
            Hub, Step, Party = [old.apps.get_model("games_hub", name) for name in ("HubSession", "HubGameStep", "HubParticipant")]
            Instance, Unit = [old.apps.get_model("duel", name) for name in ("DuelGameInstance", "DuelUnit")]
            Quiz, Question = [old.apps.get_model("Estimation", name) for name in ("EstimationQuiz", "EstimationQuestion")]
            User = old.apps.get_model("auth", "User")
            user = User.objects.create(username="binding-migration")
            quiz = Quiz.objects.create(creator_id=user.pk, room_code="BIND")
            question = Question.objects.create(created_by_id=user.pk, question_text="Historical?", correct_answer=-0.125, unit="meters")
            quiz.selected_questions.add(question)
            hub = Hub.objects.create(code="BIND-MIGRATE")
            step = Step.objects.create(session=hub, order=0, game_key="estimation", room_code=quiz.room_code)
            a, b = [Party.objects.create(session=hub, nickname=name, score_adjustment=17) for name in ("Party A", "Party B")]
            instance = Instance.objects.create(hub_session=hub, hub_game_step=step, participant_a=a, participant_b=b,
                                               game_key="estimation", state_revision=4)
            unit = Unit.objects.create(game_instance=instance, number=1)
            tables_before = set(connection.introspection.table_names())
            self.assertNotIn(table, tables_before)
            models = (Hub, Step, Party, Instance, Unit, Quiz, Question)
            snapshots = {model: list(model.objects.order_by("pk").values()) for model in models}
            with connection.cursor() as cursor:
                schemas = {model._meta.db_table: connection.introspection.get_table_description(cursor, model._meta.db_table)
                           for model in models}
            # Current model declarations must not query a table before deploy.
            disposable_unit = DuelUnit.objects.create(game_instance_id=instance.pk, number=2)
            disposable_question = EstimationQuestion.objects.create(created_by_id=user.pk, question_text="Disposable", correct_answer=1)
            disposable_quiz = EstimationQuiz.objects.create(creator_id=user.pk, room_code="DISP")
            with CaptureQueriesContext(connection) as queries:
                disposable_unit.delete()
                disposable_question.delete()
                disposable_quiz.delete()
            self.assertTrue(all(table not in query["sql"] for query in queries))
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new = executor.loader.project_state(after)
            self.assertEqual(set(connection.introspection.table_names()), tables_before | {table})
            self.assertEqual({key: value for key, value in new.models.items()
                              if key != ("duel", "duelestimationquestionbinding")}, old.models)
            Binding = new.apps.get_model("duel", "DuelEstimationQuestionBinding")
            Binding.objects.create(unit_id=unit.pk, quiz_id=quiz.pk, question_id=question.pk,
                                    question_text=question.question_text, target_value="-0.125", unit_key="meters")
            self.assertEqual(Binding.objects.get().target_value, "-0.125")
            # Independent Multiplayer scoring rollback must leave duel material
            # in place. No existing Estimation migration test needs alteration.
            MigrationExecutor(connection).migrate([("Estimation", "0016_estimationquiz_tutorial_question")])
            self.assertIn(table, connection.introspection.table_names())
            self.assertEqual(Binding.objects.get().target_value, "-0.125")
            MigrationExecutor(connection).migrate([("Estimation", "0017_rounds_and_score_adjustment")])
            for model in models:
                self.assertEqual(list(model.objects.order_by("pk").values()), snapshots[model])
                with connection.cursor() as cursor:
                    self.assertEqual(connection.introspection.get_table_description(cursor, model._meta.db_table), schemas[model._meta.db_table])
            MigrationExecutor(connection).migrate(before)
            self.assertEqual(set(connection.introspection.table_names()), tables_before)
            for model in models:
                self.assertEqual(list(model.objects.order_by("pk").values()), snapshots[model])
            MigrationExecutor(connection).migrate(after)
            self.assertEqual(Binding.objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(latest)
