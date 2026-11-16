from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from games_hub.models import HubParticipant

from .models import DuelGameInstance, DuelUnit


class DuelScoreEntryMigrationTests(TransactionTestCase):
    def test_score_migration_round_trip_only_adds_journal_and_preserves_data(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        non_duel = [target for target in latest if target[0] != "duel"]
        old_targets = [*non_duel, ("duel", "0003_duelsubmission")]
        score_targets = [*non_duel, ("duel", "0004_duelscoreentry")]
        old_state = executor.loader.project_state(old_targets)
        migration = executor.loader.get_migration("duel", "0004_duelscoreentry")
        self.assertEqual(migration.dependencies, [("duel", "0003_duelsubmission")])
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.CreateModel)
        self.assertEqual(migration.operations[0].name, "DuelScoreEntry")
        table = "duel_duelscoreentry"

        executor.migrate(old_targets)
        try:
            before_tables = set(connection.introspection.table_names())
            self.assertNotIn(table, before_tables)
            hub_model = old_state.apps.get_model("games_hub", "HubSession")
            step_model = old_state.apps.get_model("games_hub", "HubGameStep")
            party_model = old_state.apps.get_model("games_hub", "HubParticipant")
            instance_model = old_state.apps.get_model("duel", "DuelGameInstance")
            unit_model = old_state.apps.get_model("duel", "DuelUnit")
            submission_model = old_state.apps.get_model("duel", "DuelSubmission")
            hub = hub_model.objects.create(code="PRE-SCORE")
            step = step_model.objects.create(session=hub, order=0, game_key="estimation")
            a = party_model.objects.create(session=hub, nickname="Party A", score_adjustment=7)
            b = party_model.objects.create(session=hub, nickname="Party B", score_adjustment=3)
            values = dict(
                hub_session_id=hub.pk, hub_game_step_id=step.pk, participant_a_id=a.pk,
                participant_b_id=b.pk, game_key="estimation", state_revision=9,
            )
            instance = instance_model.objects.create(**values)
            unit = unit_model.objects.create(game_instance_id=instance.pk, number=1)
            submission_model.objects.create(
                unit_id=unit.pk, participant_id=a.pk, submission_kind="answer", payload={"value": 42},
            )
            # Include existing Multiplayer game/score data in preservation checks.
            user_model = old_state.apps.get_model("auth", "User")
            quiz_model = old_state.apps.get_model("QuizGame", "Quiz")
            player_model = old_state.apps.get_model("QuizGame", "QuizParticipant")
            host = user_model.objects.create(username="score-migration-host")
            game = quiz_model.objects.create(creator_id=host.pk, title="Unchanged quiz", room_code="SCOREOLD")
            player_model.objects.create(quiz_id=game.pk, name="Party A", total_score=19, hub_session_code=hub.code)
            existing = (hub_model, step_model, party_model, instance_model, unit_model,
                        submission_model, user_model, quiz_model, player_model)
            snapshots = {model: list(model.objects.order_by("pk").values()) for model in existing}
            with connection.cursor() as cursor:
                descriptions = {
                    model._meta.db_table: connection.introspection.get_table_description(cursor, model._meta.db_table)
                    for model in existing
                }

            # Registration must not add queries to an absent score table.
            historical = instance_model.objects.create(**values)
            disposable_instance = DuelGameInstance(pk=historical.pk, **values)
            disposable_unit = DuelUnit.objects.create(game_instance_id=instance.pk, number=2)
            disposable_party = HubParticipant.objects.create(session_id=hub.pk, nickname="Disposable")
            with CaptureQueriesContext(connection) as queries:
                disposable_instance.delete()
                disposable_unit.delete()
                disposable_party.delete()
            self.assertTrue(all(table not in query["sql"] for query in queries))

            executor = MigrationExecutor(connection)
            executor.migrate(score_targets)
            new_state = executor.loader.project_state(score_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables | {table})
            self.assertEqual(
                {key: value for key, value in new_state.models.items() if key != ("duel", "duelscoreentry")},
                old_state.models,
            )
            score_model = new_state.apps.get_model("duel", "DuelScoreEntry")
            score_model.objects.create(
                game_instance_id=instance.pk, unit_id=unit.pk, participant_id=a.pk,
                delta=1, source_key="unit-1-result",
            )
            correction = score_model.objects.create(
                game_instance_id=instance.pk, participant_id=a.pk, delta=-1, source_key="correction-1",
            )
            self.assertIsNone(correction.unit_id)
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
