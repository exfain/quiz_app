from datetime import timedelta
from uuid import UUID, uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .contracts import DuelInstanceContext, MatchStatus
from .models import DuelGameInstance, DuelUnit, UnitStatus


class DuelUnitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hub = HubSession.objects.create(code="UNIT-HUB")
        cls.step = HubGameStep.objects.create(session=cls.hub, order=0, game_key="estimation")
        cls.party_a = HubParticipant.objects.create(session=cls.hub, nickname="Party A")
        cls.party_b = HubParticipant.objects.create(session=cls.hub, nickname="Party B")
        values = dict(
            hub_session=cls.hub, hub_game_step=cls.step, game_key="estimation",
            participant_a=cls.party_a, participant_b=cls.party_b,
        )
        cls.instance = DuelGameInstance.objects.create(**values)
        cls.other_instance = DuelGameInstance.objects.create(**values)

    def unit(self, **overrides):
        values = dict(game_instance=self.instance, number=1)
        values.update(overrides)
        return DuelUnit(**values)

    def assert_invalid_in_model_and_database(self, **overrides):
        unit = self.unit(**overrides)
        with self.assertRaises(ValidationError):
            unit.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            unit.save()

    def test_valid_unit_round_trips_with_prepared_default_and_integer_contract_id(self):
        unit = self.unit()
        self.assertIsInstance(unit.uuid, UUID)
        self.assertEqual(unit.status, UnitStatus.PREPARED)
        unit.full_clean()
        unit.save()
        loaded = DuelUnit.objects.get(uuid=unit.uuid)
        self.assertIsInstance(loaded.pk, int)
        self.assertEqual(loaded.game_instance_id, self.instance.pk)
        self.assertEqual(loaded.number, 1)
        self.assertEqual(loaded.status, "prepared")
        self.assertIsNotNone(loaded.created_at)
        self.assertIsNotNone(loaded.updated_at)
        self.assertIsNone(loaded.started_at)
        self.assertIsNone(loaded.completed_at)
        context = DuelInstanceContext(
            hub_session_id=self.hub.pk, hub_game_step_id=self.step.pk,
            duel_instance_id=self.instance.pk, current_unit_id=loaded.pk,
            game_key="estimation", participant_a_id=self.party_a.pk,
            participant_b_id=self.party_b.pk,
        )
        self.assertEqual(context.current_unit_id, loaded.pk)

    def test_multiple_units_are_numbered_within_their_instance(self):
        for number in (3, 1, 2):
            unit = self.unit(number=number)
            unit.full_clean()
            unit.save()
        self.assertEqual(list(self.instance.units.values_list("number", flat=True)), [1, 2, 3])

    def test_each_instance_can_have_unit_one(self):
        first = self.unit()
        first.save()
        second = self.unit(game_instance=self.other_instance)
        second.full_clean()
        second.save()
        self.assertEqual(first.number, second.number)
        self.assertNotEqual(first.uuid, second.uuid)

    def test_duplicate_number_within_instance_is_rejected(self):
        self.unit().save()
        self.assert_invalid_in_model_and_database()

    def test_duplicate_uuid_is_rejected(self):
        first = self.unit()
        first.save()
        self.assert_invalid_in_model_and_database(number=2, uuid=first.uuid)

    def test_zero_and_negative_numbers_are_rejected(self):
        for number in (0, -1):
            with self.subTest(number=number):
                self.assert_invalid_in_model_and_database(number=number)

    def test_invalid_status_is_rejected(self):
        for status in ("", "unknown", "in_progress", "buzz_accepted"):
            with self.subTest(status=status):
                self.assert_invalid_in_model_and_database(status=status)

    def test_instance_is_required(self):
        self.assert_invalid_in_model_and_database(game_instance=None)

    def test_nonexistent_instance_fails_validation_and_fk_constraint(self):
        unit = self.unit(game_instance_id=uuid4())
        with self.assertRaises(ValidationError) as error:
            unit.full_clean()
        self.assertIn("game_instance", error.exception.message_dict)
        with self.assertRaises(IntegrityError), transaction.atomic():
            unit.save()
            connection.check_constraints(table_names=[DuelUnit._meta.db_table])

    def test_repeated_occurrences_have_independent_identity(self):
        # Material binding is deliberately deferred. Occurrences in the same
        # instance already have separate IDs, so no question identity is reused
        # as the unit key and no once-per-material constraint is introduced.
        units = [self.unit(number=number) for number in (1, 2, 3)]
        for unit in units:
            unit.full_clean()
            unit.save()
        self.assertEqual(len({unit.uuid for unit in units}), 3)
        self.assertEqual(len({unit.pk for unit in units}), 3)
        self.assertEqual(self.instance.units.count(), 3)

    def test_unit_status_does_not_have_to_match_parent_status(self):
        for number, status in enumerate(UnitStatus, 1):
            unit = self.unit(number=number, status=status.value)
            unit.full_clean()
            unit.save()
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.NOT_STARTED.value)

    def test_unit_create_and_update_leave_parent_and_hub_rows_unchanged(self):
        snapshots = {
            model: list(model.objects.order_by("pk").values())
            for model in (DuelGameInstance, HubSession, HubGameStep, HubParticipant)
        }
        unit = self.unit()
        unit.save()
        unit.status = UnitStatus.COMPLETED.value
        unit.save()
        for model, rows in snapshots.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), rows)

    def test_optional_timestamps_and_equal_timestamps_are_valid(self):
        now = timezone.now()
        for number, values in enumerate((
            {}, {"started_at": now}, {"completed_at": now},
            {"started_at": now, "completed_at": now},
            {"started_at": now, "completed_at": now + timedelta(seconds=1)},
        ), 1):
            with self.subTest(values=values):
                unit = self.unit(number=number, **values)
                unit.full_clean()
                unit.save()
                unit.refresh_from_db()
                self.assertEqual(unit.started_at, values.get("started_at"))
                self.assertEqual(unit.completed_at, values.get("completed_at"))

    def test_completion_before_start_is_rejected(self):
        now = timezone.now()
        self.assert_invalid_in_model_and_database(
            started_at=now, completed_at=now - timedelta(seconds=1),
        )

    def test_invalid_timestamp_strings_report_field_validation_errors(self):
        for name in ("started_at", "completed_at"):
            with self.subTest(field=name):
                values = {"started_at": timezone.now(), "completed_at": timezone.now()}
                values[name] = "not-a-date"
                with self.assertRaises(ValidationError) as error:
                    self.unit(**values).full_clean()
                self.assertIn(name, error.exception.message_dict)

    def test_created_timestamp_is_preserved_and_updated_timestamp_advances(self):
        unit = self.unit()
        unit.save()
        created_at, updated_at = unit.created_at, unit.updated_at
        unit.started_at = timezone.now()
        unit.full_clean()
        unit.save()
        unit.refresh_from_db()
        self.assertEqual(unit.created_at, created_at)
        self.assertGreaterEqual(unit.updated_at, updated_at)

    def test_database_constraints_also_apply_to_bulk_updates(self):
        now = timezone.now()
        first = self.unit()
        first.save()
        second = self.unit(number=2)
        second.save()
        for changes in (
            {"number": 0}, {"number": -1}, {"number": 1},
            {"status": "unknown"}, {"game_instance_id": None},
            {"uuid": first.uuid},
            {"started_at": now, "completed_at": now - timedelta(seconds=1)},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DuelUnit.objects.filter(pk=second.pk).update(**changes)
        second.refresh_from_db()
        self.assertEqual(second.number, 2)
        self.assertEqual(second.status, UnitStatus.PREPARED)
