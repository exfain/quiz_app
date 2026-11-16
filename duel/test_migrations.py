from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .models import DuelGameInstance


class DuelInitialMigrationTests(TransactionTestCase):
    def test_additive_round_trip_preserves_hub_schema_data_and_unmigrated_deletes(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_targets = [target for target in latest if target[0] != "duel"]
        initial_targets = [*old_targets, ("duel", "0001_initial")]
        old_state = executor.loader.project_state(old_targets)
        migration = executor.loader.get_migration("duel", "0001_initial")
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.CreateModel)
        self.assertEqual(migration.operations[0].name, "DuelGameInstance")
        table = "duel_duelgameinstance"

        executor.migrate([("duel", None)])
        try:
            before_tables = set(connection.introspection.table_names())
            self.assertNotIn(table, before_tables)
            hub = HubSession.objects.create(code="PRE-DUEL", name="Existing session")
            step = HubGameStep.objects.create(session=hub, order=0, game_key="estimation")
            party_a = HubParticipant.objects.create(session=hub, nickname="Alice", score_adjustment=7)
            party_b = HubParticipant.objects.create(session=hub, nickname="Bobby", score_adjustment=3)
            snapshots = {
                model: list(model.objects.order_by("pk").values())
                for model in (HubSession, HubGameStep, HubParticipant)
            }
            with connection.cursor() as cursor:
                descriptions = {
                    model._meta.db_table: connection.introspection.get_table_description(
                        cursor, model._meta.db_table
                    )
                    for model in snapshots
                }

            # Registering the app before applying its migration must not break
            # existing Hub deletion paths by querying its absent table.
            deletable = HubSession.objects.create(code="DELETE-NO-DUEL")
            HubGameStep.objects.create(session=deletable, order=0, game_key="quiz")
            HubParticipant.objects.create(session=deletable, nickname="Disposable")
            with CaptureQueriesContext(connection) as queries:
                deletable.delete()
            self.assertTrue(all(table not in query["sql"] for query in queries))

            executor = MigrationExecutor(connection)
            executor.migrate(initial_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables | {table})
            new_state = executor.loader.project_state(initial_targets)
            self.assertEqual(
                {key: state for key, state in new_state.models.items() if key[0] != "duel"},
                old_state.models,
            )
            for model, rows in snapshots.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), rows)
                with connection.cursor() as cursor:
                    self.assertEqual(
                        connection.introspection.get_table_description(cursor, model._meta.db_table),
                        descriptions[model._meta.db_table],
                    )
            instance_model = new_state.apps.get_model("duel", "DuelGameInstance")
            instance = instance_model.objects.create(
                hub_session_id=hub.pk, hub_game_step_id=step.pk,
                participant_a_id=party_a.pk, participant_b_id=party_b.pk,
                game_key="estimation",
            )
            self.assertEqual(instance.match_status, "not_started")
            self.assertEqual(instance.state_revision, 0)

            MigrationExecutor(connection).migrate([("duel", None)])
            self.assertEqual(set(connection.introspection.table_names()), before_tables)
            for model, rows in snapshots.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), rows)
        finally:
            MigrationExecutor(connection).migrate(latest)


class DuelUnitMigrationTests(TransactionTestCase):
    def test_unit_migration_round_trip_only_adds_unit_table(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_targets = [target for target in latest if target[0] != "duel"]
        old_targets.append(("duel", "0001_initial"))
        unit_targets = [target for target in latest if target[0] != "duel"]
        unit_targets.append(("duel", "0002_duelunit"))
        old_state = executor.loader.project_state(old_targets)
        migration = executor.loader.get_migration("duel", "0002_duelunit")
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.CreateModel)
        self.assertEqual(migration.operations[0].name, "DuelUnit")
        table = "duel_duelunit"

        executor.migrate(old_targets)
        try:
            before_tables = set(connection.introspection.table_names())
            self.assertNotIn(table, before_tables)
            hub = HubSession.objects.create(code="PRE-UNIT")
            step = HubGameStep.objects.create(session=hub, order=0, game_key="estimation")
            party_a = HubParticipant.objects.create(session=hub, nickname="Alice", score_adjustment=7)
            party_b = HubParticipant.objects.create(session=hub, nickname="Bobby", score_adjustment=3)
            instance_model = old_state.apps.get_model("duel", "DuelGameInstance")
            values = dict(
                hub_session_id=hub.pk, hub_game_step_id=step.pk,
                participant_a_id=party_a.pk, participant_b_id=party_b.pk,
                game_key="estimation", rule_config={"future_setting": 4},
                state_revision=9,
            )
            instance = instance_model.objects.create(**values)
            snapshots = {
                model: list(model.objects.order_by("pk").values())
                for model in (instance_model, HubSession, HubGameStep, HubParticipant)
            }
            with connection.cursor() as cursor:
                descriptions = {
                    model._meta.db_table: connection.introspection.get_table_description(
                        cursor, model._meta.db_table
                    )
                    for model in snapshots
                }

            # The current ORM must remain usable while 0002 is not yet applied.
            historical = instance_model.objects.create(**values)
            deletable = DuelGameInstance(pk=historical.pk, **values)
            with CaptureQueriesContext(connection) as queries:
                deletable.delete()
            self.assertTrue(all(table not in query["sql"] for query in queries))

            executor = MigrationExecutor(connection)
            executor.migrate(unit_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables | {table})
            new_state = executor.loader.project_state(unit_targets)
            self.assertEqual(
                {key: state for key, state in new_state.models.items() if key != ("duel", "duelunit")},
                old_state.models,
            )
            unit_model = new_state.apps.get_model("duel", "DuelUnit")
            unit = unit_model.objects.create(game_instance_id=instance.pk, number=1)
            self.assertEqual(unit.status, "prepared")
            self.assertIsNone(unit.started_at)
            self.assertIsNone(unit.completed_at)
            for model, rows in snapshots.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), rows)
                with connection.cursor() as cursor:
                    self.assertEqual(
                        connection.introspection.get_table_description(cursor, model._meta.db_table),
                        descriptions[model._meta.db_table],
                    )

            MigrationExecutor(connection).migrate(old_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables)
            for model, rows in snapshots.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), rows)
        finally:
            MigrationExecutor(connection).migrate(latest)
