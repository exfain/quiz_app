from datetime import timedelta
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

from duel.contracts import SessionType
from games_website.services import restore_all_models_from_supabase, sync_all_models_to_supabase

from .lobby_join import join_lobby_participant
from .models import HubGameStep, HubParticipant, HubSession
from .migration_test_utils import targets_before


class HubSessionTypeTests(TestCase):
    def test_default_is_multiplayer_and_round_trips(self):
        session = HubSession.objects.create(code="TYPE-DEFAULT")
        session.full_clean()
        session.refresh_from_db()
        self.assertEqual(session.session_type, SessionType.MULTIPLAYER.value)

    def test_both_explicit_types_round_trip(self):
        for index, kind in enumerate(SessionType):
            with self.subTest(kind=kind):
                session = HubSession(code=f"TYPE-{index}", session_type=kind.value)
                session.full_clean()
                session.save()
                session.refresh_from_db()
                self.assertEqual(session.session_type, kind.value)
                self.assertIsNone(session.started_at)
                self.assertFalse(session.duel_instances.exists())

    def test_field_choices_and_default_come_from_contract(self):
        field = HubSession._meta.get_field("session_type")
        self.assertEqual({value for value, _ in field.choices}, {kind.value for kind in SessionType})
        self.assertEqual(field.get_default(), SessionType.MULTIPLAYER.value)
        self.assertFalse(field.null)

    def test_invalid_values_fail_choices_validation_and_database_constraint(self):
        for value in ("unknown", "", "standard", "two_party", "DUEL", None):
            with self.subTest(value=value):
                session = HubSession(code="TYPE-INVALID", session_type=value)
                # Disable constraint validation to verify Choices independently.
                with self.assertRaises(ValidationError) as error:
                    session.full_clean(validate_constraints=False)
                self.assertIn("session_type", error.exception.message_dict)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    session.save()

    def test_constraint_also_guards_bulk_updates(self):
        session = HubSession.objects.create(code="TYPE-BULK")
        for value in ("invalid", "", None):
            with self.subTest(value=value):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    HubSession.objects.filter(pk=session.pk).update(session_type=value)
        session.refresh_from_db()
        self.assertEqual(session.session_type, SessionType.MULTIPLAYER.value)

    def test_existing_creation_endpoint_works_without_type_parameter(self):
        user = User.objects.create_user("session-type-host", is_staff=True)
        self.client.force_login(user)
        response = self.client.post(reverse("games_hub:create_session"), {
            "name": "Existing planning", "code": "TYPE-CREATE", "games_order": "[]",
        })
        self.assertEqual(response.status_code, 302)
        session = HubSession.objects.get(code="TYPE-CREATE")
        self.assertEqual(session.session_type, SessionType.MULTIPLAYER.value)
        self.assertEqual(session.creator_id, user.pk)
        self.assertFalse(session.is_active)
        self.assertIsNone(session.started_at)

    def test_multiplayer_stays_unlimited_and_duel_has_two_reserved_identities(self):
        for index, kind in enumerate(SessionType):
            with self.subTest(kind=kind):
                session = HubSession.objects.create(code=f"TYPE-JOIN-{index}", session_type=kind.value)
                for name in ("Alice", "Bobby"):
                    self.assertTrue(join_lobby_participant(session.code, name)["success"])
                third = join_lobby_participant(session.code, "Carla")
                self.assertEqual(third['success'], kind is SessionType.MULTIPLAYER)
                self.assertEqual(session.participants.count(), 3 if kind is SessionType.MULTIPLAYER else 2)
                self.assertFalse(session.duel_instances.exists())

    def test_generic_admin_does_not_implicitly_expose_type_selection(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser("type-admin", "", "pw")
        form = admin.site._registry[HubSession].get_form(request)
        self.assertNotIn("session_type", form.base_fields)
        self.assertIn("name", form.base_fields)


class HubSessionTypeSyncTests(TestCase):
    """Exercise the real sync services against two isolated, migrated DBs."""

    databases = {"default", "supabase"}

    def configs(self):
        # Keep the existing HubSession serialization and real duel opt-out;
        # unrelated models are outside this focused transport regression test.
        return [SimpleNamespace(get_models=lambda: [HubSession]), apps.get_app_config("duel")]

    def test_sync_transports_both_types_without_touching_duel_models(self):
        sessions = [HubSession.objects.create(
            code=f"TYPE-SYNC-{index}", session_type=kind.value,
            name=f"Existing name {index}", games_weight=3.5,
        ) for index, kind in enumerate(SessionType)]
        output, errors = StringIO(), StringIO()
        with patch("games_website.services.apps.get_app_configs", return_value=self.configs()):
            result = sync_all_models_to_supabase(stdout=output, stderr=errors)
        self.assertEqual(result, (2, ["HubSession"]))
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("DuelGameInstance", output.getvalue())
        for source in sessions:
            saved = HubSession.objects.using("supabase").get(pk=source.pk)
            self.assertEqual(saved.session_type, source.session_type)
            self.assertEqual(saved.name, source.name)
            self.assertEqual(saved.games_weight, source.games_weight)
            source.refresh_from_db()
            self.assertTrue(source.synced)

    def test_restore_transports_both_types_and_preserves_other_fields(self):
        for index, kind in enumerate(SessionType, 1):
            HubSession.objects.using("supabase").create(
                pk=100 + index, code=f"TYPE-RESTORE-{index}", session_type=kind.value,
                name=f"Restored name {index}", games_weight=4.5,
            )
            HubSession.objects.create(
                pk=100 + index, code=f"TYPE-RESTORE-{index}", name="Stale local name",
            )
        output, errors = StringIO(), StringIO()
        with patch("games_website.services.apps.get_app_configs", return_value=self.configs()):
            result = restore_all_models_from_supabase(stdout=output, stderr=errors)
        self.assertEqual(result, (2, ["HubSession"]))
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("DuelUnit", output.getvalue())
        for index, kind in enumerate(SessionType, 1):
            saved = HubSession.objects.get(pk=100 + index)
            self.assertEqual(saved.session_type, kind.value)
            self.assertEqual(saved.name, f"Restored name {index}")
            self.assertEqual(saved.games_weight, 4.5)


class HubSessionTypeMigrationTests(TransactionTestCase):
    def test_existing_sessions_become_multiplayer_and_rollback_preserves_data(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_targets = targets_before(executor.loader, ("games_hub", "0025_hubsession_session_type"))
        new_targets = [target for target in old_targets if target[0] != "games_hub"]
        new_targets.append(("games_hub", "0025_hubsession_session_type"))
        old_state = executor.loader.project_state(old_targets)
        migration = executor.loader.get_migration("games_hub", "0025_hubsession_session_type")
        self.assertEqual([type(operation) for operation in migration.operations], [
            migrations.AddField, migrations.AddConstraint,
        ])
        self.assertTrue(all(operation.model_name == "hubsession" for operation in migration.operations))
        self.assertEqual(migration.operations[0].name, "session_type")

        executor.migrate(old_targets)
        try:
            before_tables = set(connection.introspection.table_names())
            old_hub = old_state.apps.get_model("games_hub", "HubSession")
            old_step = old_state.apps.get_model("games_hub", "HubGameStep")
            old_party = old_state.apps.get_model("games_hub", "HubParticipant")
            old_instance = old_state.apps.get_model("duel", "DuelGameInstance")
            old_unit = old_state.apps.get_model("duel", "DuelUnit")
            now = timezone.now()
            sessions = [
                old_hub.objects.create(code="TYPE-OLD-WAIT", name="Waiting", is_active=False),
                old_hub.objects.create(code="TYPE-OLD-LIVE", name="Running", started_at=now,
                    current_step_index=3, games_weight=4, text_match_threshold=75),
                old_hub.objects.create(code="TYPE-OLD-END", name="Ended", is_active=False,
                    started_at=now - timedelta(hours=1), ended_at=now),
            ]
            step = old_step.objects.create(session=sessions[1], order=0, game_key="estimation")
            a = old_party.objects.create(session=sessions[1], nickname="Alice", score_adjustment=7)
            b = old_party.objects.create(session=sessions[1], nickname="Bobby", score_adjustment=3)
            instance = old_instance.objects.create(
                hub_session_id=sessions[1].pk, hub_game_step_id=step.pk,
                participant_a_id=a.pk, participant_b_id=b.pk, game_key="estimation",
                state_revision=9, rule_config={"existing_setting": 2},
            )
            old_unit.objects.create(game_instance_id=instance.pk, number=1)
            snapshots = {
                model: list(model.objects.order_by("pk").values())
                for model in (old_hub, old_step, old_party, old_instance, old_unit)
            }

            executor = MigrationExecutor(connection)
            executor.migrate(new_targets)
            new_state = executor.loader.project_state(new_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables)
            self.assertEqual(
                {key: value for key, value in new_state.models.items() if key != ("games_hub", "hubsession")},
                {key: value for key, value in old_state.models.items() if key != ("games_hub", "hubsession")},
            )
            migrated = new_state.apps.get_model("games_hub", "HubSession")
            rows = list(migrated.objects.order_by("pk").values())
            self.assertTrue(all(row["session_type"] == SessionType.MULTIPLAYER.value for row in rows))
            self.assertEqual(
                [{key: value for key, value in row.items() if key != "session_type"} for row in rows],
                snapshots[old_hub],
            )
            for model, before in snapshots.items():
                if model is not old_hub:
                    self.assertEqual(list(model.objects.order_by("pk").values()), before)

            MigrationExecutor(connection).migrate(old_targets)
            self.assertEqual(set(connection.introspection.table_names()), before_tables)
            for model, before in snapshots.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), before)
        finally:
            MigrationExecutor(connection).migrate(latest)
