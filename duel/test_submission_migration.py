from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from games_hub.models import HubParticipant

from .models import DuelUnit


class DuelSubmissionMigrationTests(TransactionTestCase):
    def test_additive_round_trip_preserves_existing_schema_data_and_deletes(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_targets = [target for target in latest if target[0] != "duel"]
        old_targets.append(("duel", "0002_duelunit"))
        submission_targets = [target for target in latest if target[0] != "duel"]
        submission_targets.append(("duel", "0003_duelsubmission"))
        old_state = executor.loader.project_state(old_targets)
        migration = executor.loader.get_migration("duel", "0003_duelsubmission")
        self.assertEqual(migration.dependencies, [("duel", "0002_duelunit")])
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.CreateModel)
        self.assertEqual(migration.operations[0].name, "DuelSubmission")
        table = "duel_duelsubmission"

        executor.migrate(old_targets)
        try:
            before_tables = set(connection.introspection.table_names())
            self.assertNotIn(table, before_tables)
            hub_model = old_state.apps.get_model("games_hub", "HubSession")
            step_model = old_state.apps.get_model("games_hub", "HubGameStep")
            party_model = old_state.apps.get_model("games_hub", "HubParticipant")
            instance_model = old_state.apps.get_model("duel", "DuelGameInstance")
            unit_model = old_state.apps.get_model("duel", "DuelUnit")
            hub = hub_model.objects.create(code="PRE-SUBMISSION")
            step = step_model.objects.create(session=hub, order=0, game_key="estimation")
            a = party_model.objects.create(session=hub, nickname="Party A", score_adjustment=7)
            b = party_model.objects.create(session=hub, nickname="Party B", score_adjustment=3)
            instance = instance_model.objects.create(
                hub_session=hub, hub_game_step=step, participant_a=a, participant_b=b,
                game_key="estimation", state_revision=9,
            )
            unit = unit_model.objects.create(game_instance=instance, number=1)
            snapshots = {
                model: list(model.objects.order_by("pk").values())
                for model in (hub_model, step_model, party_model, instance_model, unit_model)
            }
            with connection.cursor() as cursor:
                descriptions = {
                    model._meta.db_table: connection.introspection.get_table_description(
                        cursor, model._meta.db_table,
                    )
                    for model in snapshots
                }

            # New reverse FKs must not query the absent table before deployment.
            disposable_unit = DuelUnit.objects.create(game_instance_id=instance.pk, number=2)
            disposable_party = HubParticipant.objects.create(session_id=hub.pk, nickname="Disposable")
            with CaptureQueriesContext(connection) as queries:
                disposable_unit.delete()
                disposable_party.delete()
            self.assertTrue(all(table not in query["sql"] for query in queries))

            executor = MigrationExecutor(connection)
            executor.migrate(submission_targets)
            new_state = executor.loader.project_state(submission_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables | {table})
            self.assertEqual(
                {key: value for key, value in new_state.models.items() if key != ("duel", "duelsubmission")},
                old_state.models,
            )
            submission_model = new_state.apps.get_model("duel", "DuelSubmission")
            draft = submission_model.objects.create(
                unit_id=unit.pk, participant_id=a.pk, submission_kind="answer", payload={"value": 42},
            )
            self.assertEqual(draft.status, "draft")
            self.assertIsNone(draft.committed_at)
            self.assertIsNone(draft.acceptance_sequence)
            submission_model.objects.create(
                unit_id=unit.pk, participant_id=a.pk, submission_kind="answer", status="committed",
                payload={"value": 42}, committed_at=timezone.now(), acceptance_sequence=1,
            )
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
