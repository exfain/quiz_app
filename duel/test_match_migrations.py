from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from .models import DuelGameInstance


class MatchMigrationTests(TransactionTestCase):
    def round_trip(self, old_name, new_name, model_name):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        non_duel = [target for target in latest if target[0] != "duel"]
        old_targets, new_targets = [*non_duel, ("duel", old_name)], [*non_duel, ("duel", new_name)]
        migration = executor.loader.get_migration("duel", new_name)
        self.assertEqual(migration.dependencies, [("duel", old_name)])
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.CreateModel)
        self.assertEqual(migration.operations[0].name, model_name)
        executor.migrate(old_targets)
        try:
            old = executor.loader.project_state(old_targets)
            Hub = old.apps.get_model("games_hub", "HubSession")
            Step = old.apps.get_model("games_hub", "HubGameStep")
            Party = old.apps.get_model("games_hub", "HubParticipant")
            Instance = old.apps.get_model("duel", "DuelGameInstance")
            Unit = old.apps.get_model("duel", "DuelUnit")
            Score = old.apps.get_model("duel", "DuelScoreEntry")
            hub = Hub.objects.create(code="MATCH-MIGRATION")
            step = Step.objects.create(session=hub, order=0, game_key="estimation")
            a = Party.objects.create(session=hub, nickname="Party A", score_adjustment=19)
            b = Party.objects.create(session=hub, nickname="Party B")
            values = dict(hub_session_id=hub.pk, hub_game_step_id=step.pk,
                          participant_a_id=a.pk, participant_b_id=b.pk, game_key="estimation",
                          rule_config={"match": {"mode": "best_of_n", "best_of": 3}})
            instance = Instance.objects.create(**values)
            unit = Unit.objects.create(game_instance_id=instance.pk, number=1)
            Score.objects.create(game_instance_id=instance.pk, participant_id=a.pk, delta=8, source_key="previous")
            models = [Hub, Step, Party, Instance, Unit, Score]
            if model_name == "DuelMatchOutcome":
                Outcome = old.apps.get_model("duel", "DuelUnitOutcome")
                Outcome.objects.create(unit_id=unit.pk, outcome="party_a_wins", winner_id=a.pk)
                models.append(Outcome)
            rows = {model: list(model.objects.order_by("pk").values()) for model in models}
            tables = set(connection.introspection.table_names())
            with connection.cursor() as cursor:
                columns = {model: connection.introspection.get_table_description(cursor, model._meta.db_table) for model in models}
            historical = Instance.objects.create(**values)
            # INSERT uses the historical schema; deletion still exercises the
            # current ORM without querying tables/columns not yet migrated.
            deletable = DuelGameInstance(pk=historical.pk, **values)
            with CaptureQueriesContext(connection) as queries:
                deletable.delete()
            table = f"duel_{model_name.lower()}"
            self.assertTrue(all(table not in query["sql"] for query in queries))
            executor = MigrationExecutor(connection)
            executor.migrate(new_targets)
            new = executor.loader.project_state(new_targets)
            self.assertEqual(set(connection.introspection.table_names()), tables | {table})
            self.assertEqual({key: value for key, value in new.models.items() if key != ("duel", model_name.lower())}, old.models)
            Model = new.apps.get_model("duel", model_name)
            if model_name == "DuelUnitOutcome":
                Model.objects.create(unit_id=unit.pk, outcome="party_a_wins", winner_id=a.pk)
            else:
                Model.objects.create(game_instance_id=instance.pk, winner_id=a.pk, mode="best_of_n", threshold=3)
            for model, before in rows.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), before)
                with connection.cursor() as cursor:
                    self.assertEqual(connection.introspection.get_table_description(cursor, model._meta.db_table), columns[model])
            MigrationExecutor(connection).migrate(old_targets)
            self.assertEqual(set(connection.introspection.table_names()), tables)
            for model, before in rows.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), before)
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_unit_outcome_migration_round_trip(self):
        self.round_trip("0006_dueldecision", "0007_duelunitoutcome", "DuelUnitOutcome")

    def test_match_outcome_migration_round_trip(self):
        self.round_trip("0007_duelunitoutcome", "0008_duelmatchoutcome", "DuelMatchOutcome")
