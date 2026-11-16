"""Only the phase table is deployed; parent/Multiplayer data stay untouched."""
from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from .models import DuelEstimationRoundState, DuelUnit


class EstimationRoundMigrationTests(TransactionTestCase):
    def test_additive_round_trip_preserves_material_instances_and_multiplayer_schema(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        other = [node for node in latest if node[0] != "duel"]
        before = [*other, ("duel", "0009_duelestimationquestionbinding")]
        after = [*other, ("duel", "0010_duelestimationroundstate")]
        migration = executor.loader.get_migration("duel", "0010_duelestimationroundstate")
        self.assertEqual(migration.dependencies, [("duel", "0009_duelestimationquestionbinding")])
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.CreateModel)
        self.assertEqual(migration.operations[0].name, "DuelEstimationRoundState")
        table = "duel_duelestimationroundstate"
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before)
            Hub, Step, Party = [old.apps.get_model("games_hub", name) for name in ("HubSession", "HubGameStep", "HubParticipant")]
            Instance, Unit, Binding = [old.apps.get_model("duel", name) for name in
                ("DuelGameInstance", "DuelUnit", "DuelEstimationQuestionBinding")]
            Quiz, Question = [old.apps.get_model("Estimation", name) for name in ("EstimationQuiz", "EstimationQuestion")]
            user = old.apps.get_model("auth", "User").objects.create(username="phase-migration")
            quiz = Quiz.objects.create(creator_id=user.pk, room_code="PHASE")
            question = Question.objects.create(created_by_id=user.pk, question_text="Historical?", correct_answer=100, unit="meters")
            quiz.selected_questions.add(question)
            hub = Hub.objects.create(code="PHASE-MIGRATE")
            step = Step.objects.create(session=hub, order=0, game_key="estimation", room_code=quiz.room_code)
            a, b = [Party.objects.create(session=hub, nickname=name, score_adjustment=17) for name in ("A", "B")]
            instance = Instance.objects.create(hub_session=hub, hub_game_step=step, participant_a=a,
                participant_b=b, game_key="estimation", state_revision=4)
            unit = Unit.objects.create(game_instance=instance, number=1)
            Binding.objects.create(unit=unit, quiz=quiz, question=question,
                question_text="Historical?", target_value="100", unit_key="meters")
            models = (Hub, Step, Party, Instance, Unit, Binding, Quiz, Question)
            tables = set(connection.introspection.table_names())
            snapshots = {model: list(model.objects.order_by("pk").values()) for model in models}
            with connection.cursor() as cursor:
                schemas = {model._meta.db_table: connection.introspection.get_table_description(cursor, model._meta.db_table)
                    for model in models}
            disposable = DuelUnit.objects.create(game_instance_id=instance.pk, number=2)
            with CaptureQueriesContext(connection) as queries:
                disposable.delete()
            self.assertTrue(all(table not in query["sql"] for query in queries))
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new = executor.loader.project_state(after)
            self.assertEqual(set(connection.introspection.table_names()), tables | {table})
            self.assertEqual({key: value for key, value in new.models.items()
                if key != ("duel", "duelestimationroundstate")}, old.models)
            State = new.apps.get_model("duel", "DuelEstimationRoundState")
            # Old active material is deliberately NOT given an invented clock.
            self.assertEqual(State.objects.count(), 0)
            State.objects.create(unit_id=unit.pk)
            self.assertEqual(State.objects.get().phase, "prepared")
            MigrationExecutor(connection).migrate([("Estimation", "0016_estimationquiz_tutorial_question")])
            self.assertIn(table, connection.introspection.table_names())
            self.assertEqual(State.objects.get().unit_id, unit.pk)
            MigrationExecutor(connection).migrate([("Estimation", "0017_rounds_and_score_adjustment")])
            for model in models:
                self.assertEqual(list(model.objects.order_by("pk").values()), snapshots[model])
                with connection.cursor() as cursor:
                    self.assertEqual(connection.introspection.get_table_description(cursor, model._meta.db_table), schemas[model._meta.db_table])
            MigrationExecutor(connection).migrate(before)
            self.assertEqual(set(connection.introspection.table_names()), tables)
            for model in models:
                self.assertEqual(list(model.objects.order_by("pk").values()), snapshots[model])
            MigrationExecutor(connection).migrate(after)
            self.assertEqual(State.objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_existing_app_exclusion_skips_new_model_before_sync_or_restore_discovery(self):
        config = apps.get_app_config("duel")
        self.assertIn(DuelEstimationRoundState, list(config.get_models()))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__), patch("games_website.services.apps.get_app_configs", return_value=[config]):
                with patch.object(config, "get_models", side_effect=AssertionError("skip before discovery")):
                    self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))
