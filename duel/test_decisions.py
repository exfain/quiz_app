from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .models import (
    DecisionSubjectType, DecisionVerdict, DuelActionReceipt, DuelDecision,
    DuelGameInstance, DuelScoreEntry, DuelSubmission, DuelUnit, SubmissionKind,
)


class DuelDecisionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.host = get_user_model().objects.create(username="decision-host")
        cls.hub = HubSession.objects.create(code="DECISION-HUB", creator=cls.host)
        cls.step = HubGameStep.objects.create(session=cls.hub, order=0, game_key="clue_rush")
        cls.party_a = HubParticipant.objects.create(session=cls.hub, nickname="Party A", score_adjustment=7)
        cls.party_b = HubParticipant.objects.create(session=cls.hub, nickname="Party B", score_adjustment=3)
        values = dict(hub_session=cls.hub, hub_game_step=cls.step, game_key="clue_rush",
                      participant_a=cls.party_a, participant_b=cls.party_b)
        cls.instance = DuelGameInstance.objects.create(**values)
        cls.other_instance = DuelGameInstance.objects.create(**values)
        cls.unit = DuelUnit.objects.create(game_instance=cls.instance, number=1)
        cls.next_unit = DuelUnit.objects.create(game_instance=cls.instance, number=2)
        cls.other_unit = DuelUnit.objects.create(game_instance=cls.other_instance, number=1)

    def decision(self, **overrides):
        values = dict(game_instance=self.instance, unit=self.unit,
                      subject_type=DecisionSubjectType.BUZZ, subject_key="buzz-ABC",
                      verdict=DecisionVerdict.CORRECT, host_key=str(self.host.pk))
        values.update(overrides)
        return DuelDecision(**values)

    def assert_invalid_in_model_and_database(self, **overrides):
        item = self.decision(**overrides)
        with self.assertRaises(ValidationError):
            item.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            item.save(force_insert=True)

    def test_correct_decision_round_trips_with_server_timestamp_default(self):
        before = timezone.now()
        item = self.decision()
        after = timezone.now()
        self.assertLessEqual(before, item.decided_at)
        self.assertLessEqual(item.decided_at, after)
        timestamp = item.decided_at
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertIsInstance(item.pk, UUID)
        self.assertEqual(item.verdict, "correct")
        self.assertEqual(item.game_instance_id, self.instance.pk)
        self.assertEqual(item.unit_id, self.unit.pk)
        self.assertEqual(item.subject_type, "buzz")
        self.assertEqual(item.subject_key, "buzz-ABC")
        self.assertEqual(item.host_key, str(self.host.pk))
        self.assertEqual(item.decided_at, timestamp)

    def test_incorrect_decision_round_trips(self):
        item = self.decision(verdict=DecisionVerdict.INCORRECT)
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.verdict, "incorrect")

    def test_verdict_has_no_implicit_correct_default(self):
        field = DuelDecision._meta.get_field("verdict")
        self.assertFalse(field.has_default())
        self.assertEqual(set(DecisionVerdict.values), {"correct", "incorrect"})
        self.assert_invalid_in_model_and_database(verdict=field.get_default())

    def test_invalid_verdict_rejected_by_choices_and_database(self):
        for value in ("pending", "draw", "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.decision(verdict=value).full_clean(validate_constraints=False)
                self.assert_invalid_in_model_and_database(verdict=value)

    def test_instance_is_required(self):
        self.assert_invalid_in_model_and_database(game_instance=None)

    def test_unit_is_required(self):
        self.assert_invalid_in_model_and_database(unit=None)

    def test_foreign_unit_rejected_by_model_validation(self):
        with self.assertRaisesMessage(ValidationError, "unit must belong"):
            self.decision(unit=self.other_unit).full_clean()

    def test_cached_unit_cannot_spoof_stored_instance(self):
        self.other_unit.game_instance = self.instance
        with self.assertRaisesMessage(ValidationError, "unit must belong"):
            self.decision(unit=self.other_unit).full_clean()

    def test_empty_or_missing_host_key_rejected_in_model_and_database(self):
        for value in ("", None):
            with self.subTest(value=value):
                self.assert_invalid_in_model_and_database(host_key=value)

    def test_empty_or_missing_subject_key_rejected_in_model_and_database(self):
        for value in ("", None):
            with self.subTest(value=value):
                self.assert_invalid_in_model_and_database(subject_key=value)

    def test_whitespace_keys_rejected_by_model_validation(self):
        for name in ("host_key", "subject_key"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.decision(**{name: " \t\n"}).full_clean()

    def test_invalid_subject_type_rejected_by_choices_and_database(self):
        for value in ("participant", "question", "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.decision(subject_type=value).full_clean(validate_constraints=False)
                self.assert_invalid_in_model_and_database(subject_type=value)

    def test_duplicate_attempt_rejected_without_overwriting_original(self):
        original = self.decision()
        original.save()
        before = DuelDecision.objects.filter(pk=original.pk).values().get()
        self.assert_invalid_in_model_and_database(verdict=DecisionVerdict.INCORRECT)
        self.assertEqual(DuelDecision.objects.filter(pk=original.pk).values().get(), before)
        self.assertEqual(DuelDecision.objects.count(), 1)

    def test_different_host_cannot_bypass_attempt_uniqueness(self):
        self.decision().save()
        other_host = get_user_model().objects.create(username="other-decision-host")
        self.assert_invalid_in_model_and_database(host_key=str(other_host.pk))

    def test_two_attempts_in_same_unit_can_be_evaluated_independently(self):
        first = self.decision(subject_key="buzz-ABC", verdict=DecisionVerdict.INCORRECT)
        first.save()
        second = self.decision(subject_key="buzz-DEF", verdict=DecisionVerdict.CORRECT)
        second.full_clean()
        second.save()
        self.assertEqual(set(self.unit.decisions.values_list("subject_key", "verdict")), {
            ("buzz-ABC", "incorrect"), ("buzz-DEF", "correct"),
        })

    def test_different_units_may_reuse_attempt_key(self):
        self.decision().save()
        item = self.decision(unit=self.next_unit)
        item.full_clean()
        item.save()
        self.assertEqual(DuelDecision.objects.count(), 2)

    def test_different_instances_may_reuse_attempt_key(self):
        self.decision().save()
        item = self.decision(game_instance=self.other_instance, unit=self.other_unit)
        item.full_clean()
        item.save()
        self.assertEqual(DuelDecision.objects.count(), 2)

    def test_subject_type_namespaces_attempt_key(self):
        for subject_type in DecisionSubjectType:
            item = self.decision(subject_type=subject_type)
            item.full_clean()
            item.save()
        self.assertEqual(DuelDecision.objects.count(), 3)

    def test_submission_uuid_can_be_preserved_as_opaque_subject_key(self):
        submission = DuelSubmission.objects.create(
            unit=self.unit, participant=self.party_a, submission_kind=SubmissionKind.ANSWER,
            payload={"option_id": "A"},
        )
        item = self.decision(subject_type=DecisionSubjectType.SUBMISSION, subject_key=str(submission.pk))
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.subject_key, str(submission.pk))

    def test_timestamp_is_required_in_model_and_database(self):
        self.assert_invalid_in_model_and_database(decided_at=None)

    def test_invalid_timestamp_string_reports_validation_error(self):
        with self.assertRaises(ValidationError):
            self.decision(decided_at="not-a-date").full_clean()

    def test_uuid_unique_in_database(self):
        first = self.decision()
        first.save()
        self.assertNotEqual(first.pk, self.decision().pk)
        self.assert_invalid_in_model_and_database(id=first.pk, subject_key="different-attempt")

    def test_nonexistent_foreign_keys_rejected(self):
        for field, value in (("unit_id", 99999999), ("game_instance_id", uuid4())):
            with self.subTest(field=field):
                item = self.decision(**{field: value})
                with self.assertRaises(ValidationError):
                    item.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    item.save()
                    connection.check_constraints()

    def test_invalid_unit_id_reports_validation_error(self):
        with self.assertRaises(ValidationError):
            self.decision(unit_id="not-an-id").full_clean()

    def test_database_constraints_also_apply_to_bulk_updates(self):
        item = self.decision()
        item.save()
        for overrides in ({"verdict": "pending"}, {"subject_type": "participant"},
                          {"subject_key": ""}, {"host_key": ""}, {"decided_at": None}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DuelDecision.objects.filter(pk=item.pk).update(**overrides)

    def test_attempt_uniqueness_also_applies_to_bulk_updates(self):
        self.decision().save()
        other = self.decision(subject_key="buzz-DEF")
        other.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DuelDecision.objects.filter(pk=other.pk).update(subject_key="buzz-ABC")

    def test_neither_verdict_creates_points_or_changes_any_existing_state(self):
        DuelScoreEntry.objects.create(
            game_instance=self.instance, unit=self.unit, participant=self.party_a,
            delta=7, source_key="existing",
        )
        DuelSubmission.objects.create(
            unit=self.unit, participant=self.party_b, submission_kind=SubmissionKind.ANSWER, payload={"value": 42},
        )
        models = (DuelGameInstance, DuelUnit, DuelSubmission, DuelScoreEntry, DuelActionReceipt,
                  HubSession, HubGameStep, HubParticipant)
        before = {model: list(model.objects.order_by("pk").values()) for model in models}
        for verdict in DecisionVerdict:
            item = self.decision(verdict=verdict, subject_key=f"attempt-{verdict.value}")
            item.full_clean()
            item.save()
            for model, rows in before.items():
                self.assertEqual(list(model.objects.order_by("pk").values()), rows)
