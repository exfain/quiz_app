from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.models import Sum
from django.test import TestCase

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .contracts import MatchStatus, RoundOutcome, RoundResult, ScoreChange
from .models import DuelGameInstance, DuelScoreEntry, DuelSubmission, DuelUnit, SubmissionKind


class DuelScoreEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hub = HubSession.objects.create(code="SCORE-HUB")
        cls.step = HubGameStep.objects.create(session=cls.hub, order=0, game_key="estimation")
        cls.party_a = HubParticipant.objects.create(session=cls.hub, nickname="Party A", score_adjustment=7)
        cls.party_b = HubParticipant.objects.create(session=cls.hub, nickname="Party B", score_adjustment=3)
        cls.third = HubParticipant.objects.create(session=cls.hub, nickname="Third")
        values = dict(
            hub_session=cls.hub, hub_game_step=cls.step, game_key="estimation",
            participant_a=cls.party_a, participant_b=cls.party_b,
        )
        cls.instance = DuelGameInstance.objects.create(**values)
        cls.other_instance = DuelGameInstance.objects.create(**values)
        cls.unit = DuelUnit.objects.create(game_instance=cls.instance, number=1)
        cls.other_unit = DuelUnit.objects.create(game_instance=cls.other_instance, number=1)

    def entry(self, **overrides):
        values = dict(
            game_instance=self.instance, unit=self.unit, participant=self.party_a,
            delta=1, source_key="unit-1-result",
        )
        values.update(overrides)
        return DuelScoreEntry(**values)

    def assert_invalid_in_model_and_database(self, **overrides):
        entry = self.entry(**overrides)
        with self.assertRaises(ValidationError):
            entry.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            entry.save(force_insert=True)

    def test_positive_point_booking_round_trips(self):
        entry = self.entry()
        entry.full_clean()
        entry.save()
        entry.refresh_from_db()
        self.assertIsInstance(entry.pk, UUID)
        self.assertEqual(entry.game_instance_id, self.instance.pk)
        self.assertEqual(entry.unit_id, self.unit.pk)
        self.assertEqual(entry.participant_id, self.party_a.pk)
        self.assertEqual(entry.delta, 1)
        self.assertEqual(entry.source_key, "unit-1-result")
        self.assertIsNotNone(entry.created_at)

    def test_negative_point_booking(self):
        entry = self.entry(delta=-1)
        entry.full_clean()
        entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.delta, -1)

    def test_zero_point_booking(self):
        entry = self.entry(delta=0)
        entry.full_clean()
        entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.delta, 0)

    def test_multiple_causes_for_same_party_are_allowed(self):
        for key in ("result-1", "result-2", "host-correction-1"):
            entry = self.entry(source_key=key)
            entry.full_clean()
            entry.save()
        self.assertEqual(self.instance.score_entries.count(), 3)

    def test_both_parties_can_receive_points(self):
        for party, key in ((self.party_a, "a-result"), (self.party_b, "b-result")):
            entry = self.entry(participant=party, source_key=key)
            entry.full_clean()
            entry.save()
        self.assertEqual(set(self.instance.score_entries.values_list("participant_id", flat=True)), {
            self.party_a.pk, self.party_b.pk,
        })

    def test_duplicate_cause_for_same_party_rejected_by_database(self):
        original = self.entry()
        original.save()
        # Changing the unit or delta must not bypass business-cause uniqueness.
        self.assert_invalid_in_model_and_database(unit=None, delta=-1)
        original.refresh_from_db()
        self.assertEqual(original.delta, 1)
        self.assertEqual(DuelScoreEntry.objects.count(), 1)

    def test_both_parties_may_share_same_business_cause(self):
        self.entry().save()
        entry = self.entry(participant=self.party_b)
        entry.full_clean()
        entry.save()
        self.assertEqual(DuelScoreEntry.objects.count(), 2)

    def test_same_cause_in_different_instances_is_allowed(self):
        self.entry().save()
        entry = self.entry(game_instance=self.other_instance, unit=self.other_unit)
        entry.full_clean()
        entry.save()
        self.assertEqual(DuelScoreEntry.objects.count(), 2)

    def test_third_party_rejected_by_model_validation(self):
        with self.assertRaisesMessage(ValidationError, "Only a party"):
            self.entry(participant=self.third).full_clean()

    def test_other_session_with_same_nickname_is_not_same_identity(self):
        hub = HubSession.objects.create(code="OTHER-SCORE-HUB")
        party = HubParticipant.objects.create(session=hub, nickname=self.party_a.nickname)
        with self.assertRaisesMessage(ValidationError, "Only a party"):
            self.entry(participant=party).full_clean()

    def test_foreign_unit_rejected_by_model_validation(self):
        with self.assertRaisesMessage(ValidationError, "unit must belong"):
            self.entry(unit=self.other_unit).full_clean()

    def test_cached_objects_cannot_spoof_stored_membership(self):
        self.instance.participant_a = self.third
        self.other_unit.game_instance = self.instance
        for overrides, message in ((dict(participant=self.third), "Only a party"),
                                   (dict(unit=self.other_unit), "unit must belong")):
            with self.subTest(overrides=overrides):
                with self.assertRaisesMessage(ValidationError, message):
                    self.entry(**overrides).full_clean()

    def test_unit_is_optional(self):
        entry = self.entry(unit=None)
        entry.full_clean()
        entry.save()
        entry.refresh_from_db()
        self.assertIsNone(entry.unit_id)

    def test_instance_is_required(self):
        self.assert_invalid_in_model_and_database(game_instance=None)

    def test_participant_is_required(self):
        self.assert_invalid_in_model_and_database(participant=None)

    def test_empty_source_key_rejected_by_model_and_database(self):
        self.assert_invalid_in_model_and_database(source_key="")

    def test_null_source_key_rejected_by_model_and_database(self):
        self.assert_invalid_in_model_and_database(source_key=None)

    def test_whitespace_only_source_key_rejected_by_model_validation(self):
        with self.assertRaises(ValidationError):
            self.entry(source_key=" \t\n").full_clean()

    def test_source_key_is_not_silently_rewritten(self):
        entry = self.entry(source_key="unit-1:result:party-A")
        entry.full_clean()
        entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.source_key, "unit-1:result:party-A")

    def test_signed_bookings_can_be_summed_per_party_and_instance(self):
        for index, delta in enumerate((1, 1, 1, -1, 0)):
            self.entry(delta=delta, source_key=f"result-{index}").save()
        self.entry(participant=self.party_b, delta=9).save()
        self.entry(game_instance=self.other_instance, unit=self.other_unit, delta=50).save()
        self.assertEqual(
            DuelScoreEntry.objects.filter(game_instance=self.instance, participant=self.party_a)
            .aggregate(total=Sum("delta"))["total"], 2,
        )

    def test_correction_is_a_new_booking_and_preserves_original(self):
        original = self.entry()
        original.save()
        before = DuelScoreEntry.objects.filter(pk=original.pk).values().get()
        correction = self.entry(delta=-1, unit=None, source_key="host-correction-1")
        correction.full_clean()
        correction.save()
        self.assertEqual(self.instance.score_entries.aggregate(total=Sum("delta"))["total"], 0)
        self.assertEqual(DuelScoreEntry.objects.filter(pk=original.pk).values().get(), before)

    def test_booking_does_not_change_match_status_or_infer_round_winner(self):
        for outcome in RoundOutcome:
            result = RoundResult(outcome)
            self.entry(delta=100, source_key=f"outcome-{outcome.value}").save()
            self.assertEqual(result.outcome, outcome)
        self.instance.refresh_from_db()
        self.unit.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.NOT_STARTED.value)
        self.assertEqual(self.unit.status, "prepared")
        self.assertEqual(self.instance.state_revision, 0)

    def test_contract_deltas_round_trip_including_large_signed_values(self):
        for index, delta in enumerate((2**40, -(2**40), 0)):
            change = ScoreChange(self.party_a.pk, delta)
            entry = self.entry(delta=change.delta, source_key=f"large-{index}")
            entry.full_clean()
            entry.save()
            entry.refresh_from_db()
            self.assertEqual(entry.delta, change.delta)

    def test_non_integer_inputs_rejected_before_django_coercion(self):
        for value in (1.5, 1.0, -0.5, True, False, "1", "invalid", Decimal("1.5")):
            with self.subTest(value=value):
                entry = self.entry(delta=value)
                with self.assertRaises(ValidationError):
                    entry.full_clean()

    def test_missing_delta_rejected_by_model_and_database(self):
        self.assert_invalid_in_model_and_database(delta=None)

    def test_database_rejects_fractional_or_nonnumeric_stored_delta(self):
        entry = self.entry()
        entry.save()
        # Explicit expression output types bypass IntegerField's normal Python
        # coercion, exercising the CHECK against actual database values.
        for value, field in ((1.5, models.FloatField()), (-0.5, models.FloatField()),
                             ("invalid", models.CharField())):
            with self.subTest(value=value):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DuelScoreEntry.objects.filter(pk=entry.pk).update(delta=models.Value(value, output_field=field))
        entry.refresh_from_db()
        self.assertEqual(entry.delta, 1)

    def test_database_rejects_duplicate_cause_on_bulk_update(self):
        self.entry().save()
        other = self.entry(source_key="other-result")
        other.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DuelScoreEntry.objects.filter(pk=other.pk).update(source_key="unit-1-result")

    def test_database_rejects_empty_source_on_bulk_update(self):
        entry = self.entry()
        entry.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DuelScoreEntry.objects.filter(pk=entry.pk).update(source_key="")

    def test_uuid_unique_in_database(self):
        first = self.entry()
        first.save()
        self.assertNotEqual(first.pk, self.entry().pk)
        self.assert_invalid_in_model_and_database(id=first.pk, source_key="other")

    def test_nonexistent_foreign_keys_rejected(self):
        for field, value in (("unit_id", 99999999), ("participant_id", 99999999),
                             ("game_instance_id", UUID(int=1))):
            with self.subTest(field=field):
                entry = self.entry(**{field: value})
                with self.assertRaises(ValidationError):
                    entry.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    entry.save()
                    connection.check_constraints()

    def test_booking_leaves_existing_duel_and_hub_data_unchanged(self):
        DuelSubmission.objects.create(
            unit=self.unit, participant=self.party_a, submission_kind=SubmissionKind.ANSWER,
            payload={"value": 42},
        )
        models_to_check = (DuelGameInstance, DuelUnit, DuelSubmission, HubSession, HubGameStep, HubParticipant)
        before = {model: list(model.objects.order_by("pk").values()) for model in models_to_check}
        self.entry().save()
        for model, rows in before.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), rows)
