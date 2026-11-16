import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, migrations, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant
from duel.contracts import GameFormat, SessionType, validate_game_format
from duel.models import DuelGameInstance, DuelUnit
from games_website.services import restore_all_models_from_supabase, sync_all_models_to_supabase

from .models import HubGameStep, HubParticipant, HubSession


class HubGameFormatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.host = User.objects.create_superuser("format-host", "", "pw")
        cls.hub = HubSession.objects.create(code="FORMAT-HUB", is_active=False)
        cls.game = Quiz.objects.create(
            creator=cls.host, title="Existing game", room_code="FMTQUIZ",
            status="active", started_at=timezone.now(),
        )
        cls.step = HubGameStep.objects.create(
            session=cls.hub, order=0, game_key="quiz", room_code=cls.game.room_code,
        )
        parties = [HubParticipant.objects.create(session=cls.hub, nickname=name)
                   for name in ("Alice", "Bobby")]
        for party in parties:
            QuizParticipant.objects.create(
                quiz=cls.game, name=party.nickname, hub_session_code=cls.hub.code,
                total_score=11,
            )
        instance = DuelGameInstance.objects.create(
            hub_session=cls.hub, hub_game_step=cls.step,
            participant_a=parties[0], participant_b=parties[1], game_key="quiz",
        )
        DuelUnit.objects.create(game_instance=instance, number=1)

    def test_default_is_standard_and_round_trips(self):
        step = HubGameStep(session=self.hub, order=1, game_key="estimation")
        step.full_clean()
        step.save()
        step.refresh_from_db()
        self.assertEqual(step.game_format, GameFormat.STANDARD.value)
        self.assertEqual(step.game_key, "estimation")

    def test_each_explicit_format_round_trips_independently_of_game_key(self):
        for index, kind in enumerate(GameFormat, 1):
            with self.subTest(kind=kind):
                step = HubGameStep(
                    session=self.hub, order=index, game_key="estimation", game_format=kind.value,
                )
                step.full_clean()
                step.save()
                step.refresh_from_db()
                self.assertEqual(step.game_format, kind.value)
                self.assertEqual(step.game_key, "estimation")

    def test_choices_and_both_defaults_come_from_contract(self):
        field = HubGameStep._meta.get_field("game_format")
        self.assertEqual({value for value, _ in field.choices}, {kind.value for kind in GameFormat})
        self.assertEqual(field.get_default(), GameFormat.STANDARD.value)
        self.assertEqual(field.db_default, GameFormat.STANDARD.value)
        self.assertFalse(field.null)

    def test_database_default_supports_inserts_omitting_the_new_column(self):
        # Copy an existing row using only the old columns, with a new order.
        # The source's DUEL value must not leak into the omitted column.
        HubGameStep.objects.filter(pk=self.step.pk).update(game_format=GameFormat.DUEL.value)
        quote = connection.ops.quote_name
        table = quote(HubGameStep._meta.db_table)
        columns = ", ".join(quote(field.column) for field in HubGameStep._meta.local_concrete_fields
                            if not field.primary_key and field.name not in {"game_format", "order"})
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table} ({columns}, {quote('order')}) "
                f"SELECT {columns}, %s FROM {table} WHERE {quote('id')} = %s",
                [12, self.step.pk],
            )
        self.assertEqual(HubGameStep.objects.get(session=self.hub, order=12).game_format, "standard")

    def test_invalid_and_null_formats_fail_choices_and_database_validation(self):
        for value in ("", "unknown", "multiplayer", "final_duel", "STANDARD", None):
            with self.subTest(value=value):
                step = HubGameStep(session=self.hub, order=1, game_key="estimation", game_format=value)
                with self.assertRaises(ValidationError) as error:
                    step.full_clean(validate_constraints=False)
                self.assertIn("game_format", error.exception.message_dict)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    step.save()

    def test_database_constraint_guards_bulk_updates(self):
        for value in ("invalid", "", None):
            with self.subTest(value=value):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    HubGameStep.objects.filter(pk=self.step.pk).update(game_format=value)
        self.step.refresh_from_db()
        self.assertEqual(self.step.game_format, GameFormat.STANDARD.value)

    def test_duel_session_temporarily_allows_standard_metadata_without_starting(self):
        hub = HubSession.objects.create(code="FMT-DUEL", session_type=SessionType.DUEL.value, is_active=False)
        step = HubGameStep.objects.create(session=hub, order=0, game_key="estimation")
        step.full_clean()
        self.assertEqual(step.game_format, GameFormat.STANDARD.value)
        with self.assertRaises(ValueError):
            validate_game_format(SessionType(hub.session_type), GameFormat(step.game_format))
        hub.refresh_from_db()
        self.assertFalse(hub.is_active)
        self.assertIsNone(hub.started_at)
        self.assertFalse(hub.duel_instances.exists())

    def test_format_writes_do_not_change_parents_game_state_or_points(self):
        snapshots = {
            model: list(model.objects.order_by("pk").values())
            for model in (HubSession, HubParticipant, Quiz, QuizParticipant, DuelGameInstance, DuelUnit)
        }
        for kind in GameFormat:
            self.step.game_format = kind.value
            self.step.full_clean()
            self.step.save()
        for model, before in snapshots.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), before)

    def test_existing_session_creation_with_game_selection_keeps_standard(self):
        game = Quiz.objects.create(creator=self.host, title="Planning source", room_code="FMTPLAN")
        self.client.force_login(self.host)
        response = self.client.post(reverse("games_hub:create_session"), {
            "name": "Existing planning", "code": "FMT-CREATE",
            "games_order": json.dumps([{"game_key": "quiz", "room_code": game.room_code}]),
        })
        self.assertEqual(response.status_code, 302)
        hub = HubSession.objects.get(code="FMT-CREATE")
        step = hub.steps.get()
        self.assertEqual(hub.session_type, SessionType.MULTIPLAYER.value)
        self.assertEqual(step.game_format, GameFormat.STANDARD.value)
        self.assertEqual(step.game_key, "quiz")
        self.assertEqual(step.room_code, game.room_code)
        self.assertFalse(hub.duel_instances.exists())

    def test_format_selection_is_absent_from_both_admin_interfaces(self):
        request = RequestFactory().get("/admin/")
        request.user = self.host
        form = admin.site._registry[HubGameStep].get_form(request)
        self.assertNotIn("game_format", form.base_fields)
        self.assertIn("game_key", form.base_fields)
        self.client.force_login(self.host)
        response = self.client.get(reverse("games_hub:create_session"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="game_format"')


class HubGameFormatSyncTests(TestCase):
    databases = {"default", "supabase"}

    def configs(self):
        return [SimpleNamespace(get_models=lambda: [HubGameStep]), apps.get_app_config("duel")]

    def parent_sessions(self):
        hub = HubSession.objects.create(code="FMT-TRANSPORT")
        HubSession.objects.using("supabase").create(pk=hub.pk, code=hub.code)
        return hub

    def test_sync_includes_default_standard_and_all_explicit_formats(self):
        hub = self.parent_sessions()
        steps = []
        for index, kind in enumerate(GameFormat):
            values = {} if kind is GameFormat.STANDARD else {"game_format": kind.value}
            steps.append(HubGameStep.objects.create(
                session=hub, order=index, game_key="estimation", title=f"Step {index}", **values,
            ))
        output, errors = StringIO(), StringIO()
        with patch("games_website.services.apps.get_app_configs", return_value=self.configs()):
            result = sync_all_models_to_supabase(stdout=output, stderr=errors)
        self.assertEqual(result, (4, ["HubGameStep"]))
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("DuelGameInstance", output.getvalue())
        for source in steps:
            saved = HubGameStep.objects.using("supabase").get(pk=source.pk)
            self.assertEqual(saved.game_format, source.game_format)
            self.assertEqual(saved.game_key, source.game_key)
            self.assertEqual(saved.session_id, hub.pk)
            self.assertEqual(saved.title, source.title)
            source.refresh_from_db()
            self.assertTrue(source.synced)

    def test_restore_includes_formats_and_retains_existing_step_fields(self):
        hub = self.parent_sessions()
        for index, kind in enumerate(GameFormat):
            HubGameStep.objects.using("supabase").create(
                pk=100 + index, session_id=hub.pk, order=index, game_key="estimation",
                game_format=kind.value, title=f"Restored {index}",
                result_snapshot={"existing": index},
            )
            HubGameStep.objects.create(
                pk=100 + index, session=hub, order=index, game_key="estimation", title="Old local name",
            )
        output, errors = StringIO(), StringIO()
        with patch("games_website.services.apps.get_app_configs", return_value=self.configs()):
            result = restore_all_models_from_supabase(stdout=output, stderr=errors)
        self.assertEqual(result, (4, ["HubGameStep"]))
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("DuelUnit", output.getvalue())
        for index, kind in enumerate(GameFormat):
            saved = HubGameStep.objects.get(pk=100 + index)
            self.assertEqual(saved.game_format, kind.value)
            self.assertEqual(saved.game_key, "estimation")
            self.assertEqual(saved.title, f"Restored {index}")
            self.assertEqual(saved.result_snapshot, {"existing": index})


class HubGameFormatMigrationTests(TransactionTestCase):
    def test_backfill_and_rollback_preserve_steps_sessions_games_and_duels(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_targets = [target for target in latest if target[0] != "games_hub"]
        new_targets = [*old_targets, ("games_hub", "0026_hubgamestep_game_format")]
        old_targets.append(("games_hub", "0025_hubsession_session_type"))
        old_state = executor.loader.project_state(old_targets)
        migration = executor.loader.get_migration("games_hub", "0026_hubgamestep_game_format")
        self.assertEqual([type(operation) for operation in migration.operations], [
            migrations.AddField, migrations.AddConstraint,
        ])
        self.assertTrue(all(operation.model_name == "hubgamestep" for operation in migration.operations))
        self.assertEqual(migration.operations[0].name, "game_format")
        executor.migrate(old_targets)
        try:
            before_tables = set(connection.introspection.table_names())
            old_hub = old_state.apps.get_model("games_hub", "HubSession")
            old_step = old_state.apps.get_model("games_hub", "HubGameStep")
            old_party = old_state.apps.get_model("games_hub", "HubParticipant")
            old_instance = old_state.apps.get_model("duel", "DuelGameInstance")
            old_unit = old_state.apps.get_model("duel", "DuelUnit")
            old_user = old_state.apps.get_model("auth", "User")
            old_quiz = old_state.apps.get_model("QuizGame", "Quiz")
            old_player = old_state.apps.get_model("QuizGame", "QuizParticipant")
            host = old_user.objects.create(username="legacy-format-host")
            hub = old_hub.objects.create(code="FMT-LEGACY", started_at=timezone.now(), games_weight=4)
            duel_hub = old_hub.objects.create(code="FMT-OLD-DUEL", session_type="duel", is_active=False)
            game = old_quiz.objects.create(creator_id=host.pk, title="Existing active game", room_code="LEGACYFMT",
                status="active", started_at=timezone.now())
            step = old_step.objects.create(session=hub, order=0, game_key="quiz", room_code=game.room_code,
                result_snapshot={"existing": 7}, intro_state_revision=3)
            old_step.objects.create(session=hub, order=1, game_key="estimation", title="Next game")
            old_step.objects.create(session=duel_hub, order=0, game_key="estimation")
            a = old_party.objects.create(session=hub, nickname="Alice", score_adjustment=7)
            b = old_party.objects.create(session=hub, nickname="Bobby", score_adjustment=3)
            old_player.objects.create(quiz_id=game.pk, name="Alice", total_score=19, hub_session_code=hub.code)
            instance = old_instance.objects.create(hub_session_id=hub.pk, hub_game_step_id=step.pk,
                participant_a_id=a.pk, participant_b_id=b.pk, game_key="quiz", state_revision=9)
            old_unit.objects.create(game_instance_id=instance.pk, number=1)
            snapshots = {
                model: list(model.objects.order_by("pk").values())
                for model in (old_hub, old_step, old_party, old_instance, old_unit, old_quiz, old_player)
            }

            executor = MigrationExecutor(connection)
            executor.migrate(new_targets)
            new_state = executor.loader.project_state(new_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables)
            self.assertEqual(
                {key: value for key, value in new_state.models.items() if key != ("games_hub", "hubgamestep")},
                {key: value for key, value in old_state.models.items() if key != ("games_hub", "hubgamestep")},
            )
            migrated = new_state.apps.get_model("games_hub", "HubGameStep")
            rows = list(migrated.objects.order_by("pk").values())
            self.assertTrue(all(row["game_format"] == GameFormat.STANDARD.value for row in rows))
            self.assertEqual(
                [{key: value for key, value in row.items() if key != "game_format"} for row in rows],
                snapshots[old_step],
            )
            for model, before in snapshots.items():
                if model is not old_step:
                    self.assertEqual(list(model.objects.order_by("pk").values()), before)

            MigrationExecutor(connection).migrate(old_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables)
            for model, before in snapshots.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), before)
        finally:
            MigrationExecutor(connection).migrate(latest)
