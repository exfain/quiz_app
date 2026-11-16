"""Only planned-slot metadata is added; old and duel data remain untouched."""
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DuelMatchPlanMigrationTests(TransactionTestCase):
    def test_additive_backfill_and_round_trip_preserve_every_other_model_and_data(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        others = [node for node in latest if node[0] != "games_hub"]
        before = [*others, ("games_hub", "0026_hubgamestep_game_format")]
        after = [*others, ("games_hub", "0027_hubgamestep_duel_match_config")]
        migration = executor.loader.get_migration("games_hub", "0027_hubgamestep_duel_match_config")
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], migrations.AddField)
        self.assertEqual(migration.operations[0].model_name, "hubgamestep")
        self.assertEqual(migration.operations[0].name, "duel_match_config")
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before)
            Hub, Step, Party = [old.apps.get_model("games_hub", name) for name in ("HubSession", "HubGameStep", "HubParticipant")]
            Instance, Unit = [old.apps.get_model("duel", name) for name in ("DuelGameInstance", "DuelUnit")]
            Quiz, Player = [old.apps.get_model("QuizGame", name) for name in ("Quiz", "QuizParticipant")]
            host = old.apps.get_model("auth", "User").objects.create(username="plan-migration")
            hub = Hub.objects.create(code="PLAN-MIGRATE", current_step_index=2)
            duel_hub = Hub.objects.create(code="PLAN-DUEL", session_type="duel")
            game = Quiz.objects.create(creator_id=host.pk, room_code="PLANQUIZ", status="active")
            step = Step.objects.create(session=hub, order=0, game_key="quiz", room_code=game.room_code,
                result_snapshot={"existing": 5})
            Step.objects.create(session=duel_hub, order=0, game_key="estimation", game_format="duel")
            a, b = [Party.objects.create(session=hub, nickname=name, score_adjustment=8) for name in ("A", "B")]
            Player.objects.create(quiz_id=game.pk, name="A", total_score=17, hub_session_code=hub.code)
            instance = Instance.objects.create(hub_session=hub, hub_game_step=step,
                participant_a=a, participant_b=b, game_key="quiz", rule_config={"existing": 7}, state_revision=4)
            Unit.objects.create(game_instance=instance, number=1)
            models = (Hub, Step, Party, Instance, Unit, Quiz, Player)
            data = {model: list(model.objects.order_by("pk").values()) for model in models}
            tables = set(connection.introspection.table_names())
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new = executor.loader.project_state(after)
            self.assertEqual(set(connection.introspection.table_names()), tables)
            self.assertEqual({k: v for k, v in new.models.items() if k != ("games_hub", "hubgamestep")},
                             {k: v for k, v in old.models.items() if k != ("games_hub", "hubgamestep")})
            PlannedStep = new.apps.get_model("games_hub", "HubGameStep")
            rows = list(PlannedStep.objects.order_by("pk").values())
            self.assertTrue(all(row["duel_match_config"] == {} for row in rows))
            self.assertEqual([{k: v for k, v in row.items() if k != "duel_match_config"} for row in rows], data[Step])
            for model in models:
                if model is not Step:
                    self.assertEqual(list(model.objects.order_by("pk").values()), data[model])
            PlannedStep.objects.filter(pk=step.pk).update(duel_match_config={"mode": "best_of_n", "best_of": 7})
            self.assertEqual(PlannedStep.objects.get(pk=step.pk).duel_match_config["best_of"], 7)
            MigrationExecutor(connection).migrate(before)
            for model in models:
                self.assertEqual(list(model.objects.order_by("pk").values()), data[model])
            MigrationExecutor(connection).migrate(after)
            self.assertTrue(all(value == {} for value in PlannedStep.objects.values_list("duel_match_config", flat=True)))
        finally:
            MigrationExecutor(connection).migrate(latest)
