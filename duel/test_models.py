from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .contracts import MatchStatus
from .models import DuelGameInstance


class DuelGameInstanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hub = HubSession.objects.create(code="DUEL-MODEL")
        cls.other_hub = HubSession.objects.create(code="OTHER-HUB")
        cls.step = HubGameStep.objects.create(
            session=cls.hub, order=0, game_key="estimation", room_code="SOURCE"
        )
        cls.party_a = HubParticipant.objects.create(session=cls.hub, nickname="Party A")
        cls.party_b = HubParticipant.objects.create(session=cls.hub, nickname="Party B")

    def instance(self, **overrides):
        values = dict(
            hub_session=self.hub, hub_game_step=self.step,
            participant_a=self.party_a, participant_b=self.party_b,
            game_key="estimation",
        )
        values.update(overrides)
        return DuelGameInstance(**values)

    def test_valid_instance_round_trips_with_defaults_and_timestamps(self):
        instance = self.instance(rule_config={"future_setting": 3})
        instance.full_clean()
        self.assertIsInstance(instance.pk, UUID)
        self.assertEqual(instance.state_revision, 0)
        self.assertEqual(instance.match_status, MatchStatus.NOT_STARTED.value)
        instance.save()
        loaded = DuelGameInstance.objects.get(pk=instance.pk)
        self.assertEqual(loaded.hub_session_id, self.hub.pk)
        self.assertEqual(loaded.hub_game_step_id, self.step.pk)
        self.assertEqual(loaded.participant_a_id, self.party_a.pk)
        self.assertEqual(loaded.participant_b_id, self.party_b.pk)
        self.assertEqual(loaded.game_key, "estimation")
        self.assertEqual(loaded.rule_config, {"future_setting": 3})
        self.assertEqual(loaded.state_revision, 0)
        self.assertIsNotNone(loaded.created_at)
        self.assertIsNotNone(loaded.updated_at)
        created_at, updated_at = loaded.created_at, loaded.updated_at
        loaded.rule_config = {"future_setting": 4}
        loaded.save()
        loaded.refresh_from_db()
        self.assertEqual(loaded.created_at, created_at)
        self.assertGreaterEqual(loaded.updated_at, updated_at)

    def test_same_step_accepts_multiple_distinct_instance_uuids(self):
        first, second = self.instance(), self.instance()
        first.full_clean()
        first.save()
        second.full_clean()
        second.save()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(DuelGameInstance.objects.filter(hub_game_step=self.step).count(), 2)

    def test_duplicate_uuid_is_rejected_by_database(self):
        first = self.instance()
        first.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.instance(id=first.pk).save(force_insert=True)

    def test_same_party_is_rejected_by_validation_and_database(self):
        instance = self.instance(participant_b=self.party_a)
        with self.assertRaises(ValidationError) as error:
            instance.full_clean()
        self.assertIn("participant_b", error.exception.message_dict)
        with self.assertRaises(IntegrityError), transaction.atomic():
            instance.save()

    def test_both_parties_are_required_in_validation_and_database(self):
        for name in ("participant_a", "participant_b"):
            with self.subTest(field=name):
                instance = self.instance(**{name: None})
                with self.assertRaises(ValidationError) as error:
                    instance.full_clean()
                self.assertIn(name, error.exception.message_dict)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    instance.save()

    def test_status_choices_come_from_contract_and_all_are_valid(self):
        field = DuelGameInstance._meta.get_field("match_status")
        self.assertEqual({value for value, _ in field.choices}, {s.value for s in MatchStatus})
        for status in MatchStatus:
            with self.subTest(status=status):
                instance = self.instance(match_status=status.value)
                instance.full_clean()
                instance.save()
                instance.refresh_from_db()
                self.assertEqual(instance.match_status, status.value)

    def test_invalid_status_is_rejected_by_validation_and_database(self):
        for status in ("unknown", "", "active"):
            with self.subTest(status=status):
                instance = self.instance(match_status=status)
                with self.assertRaises(ValidationError):
                    instance.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    instance.save()

    def test_database_constraints_also_cover_bulk_updates(self):
        instance = self.instance()
        instance.save()
        for changes in (
            {"match_status": "unknown"},
            {"participant_b_id": self.party_a.pk},
            {"state_revision": -1},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DuelGameInstance.objects.filter(pk=instance.pk).update(**changes)
        instance.refresh_from_db()
        self.assertEqual(instance.state_revision, 0)

    def test_foreign_session_is_reported_on_each_reference(self):
        instance = self.instance(hub_session=self.other_hub)
        with self.assertRaises(ValidationError) as error:
            instance.full_clean()
        self.assertEqual(
            set(error.exception.message_dict),
            {"hub_game_step", "participant_a", "participant_b"},
        )

    def test_foreign_step_is_reported(self):
        step = HubGameStep.objects.create(session=self.other_hub, order=0, game_key="estimation")
        with self.assertRaises(ValidationError) as error:
            self.instance(hub_game_step=step).full_clean()
        self.assertEqual(set(error.exception.message_dict), {"hub_game_step"})

    def test_each_foreign_party_is_reported(self):
        foreign = HubParticipant.objects.create(session=self.other_hub, nickname="Foreign")
        for name in ("participant_a", "participant_b"):
            with self.subTest(field=name):
                with self.assertRaises(ValidationError) as error:
                    self.instance(**{name: foreign}).full_clean()
                self.assertEqual(set(error.exception.message_dict), {name})

    def test_invalid_fk_is_a_validation_error_not_a_relation_lookup_exception(self):
        instance = self.instance(participant_a_id=2**40)
        with self.assertRaises(ValidationError) as error:
            instance.full_clean()
        self.assertIn("participant_a", error.exception.message_dict)

    def test_rule_config_is_an_object_with_an_independent_default(self):
        first, second = self.instance(), self.instance()
        first.rule_config["future_setting"] = 3
        self.assertEqual(second.rule_config, {})
        with self.assertRaises(ValidationError) as error:
            self.instance(rule_config=[]).full_clean()
        self.assertIn("rule_config", error.exception.message_dict)

    def test_validation_does_not_start_or_modify_multiplayer_data(self):
        before = {
            model: list(model.objects.order_by("pk").values())
            for model in (HubSession, HubGameStep, HubParticipant)
        }
        instance = self.instance()
        instance.full_clean()
        instance.save()
        for model, rows in before.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), rows)

    def test_validation_uses_persisted_session_membership(self):
        instance = self.instance()
        instance.participant_a.session = self.other_hub
        # An unsaved change to a cached related object must not replace DB truth.
        instance.full_clean()
        HubParticipant.objects.filter(pk=self.party_a.pk).update(session=self.other_hub)
        with self.assertRaises(ValidationError) as error:
            instance.full_clean()
        self.assertIn("participant_a", error.exception.message_dict)
