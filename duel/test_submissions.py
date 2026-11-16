from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .models import (
    DuelGameInstance, DuelSubmission, DuelUnit, SubmissionKind, SubmissionStatus,
)


class DuelSubmissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hub = HubSession.objects.create(code="SUBMISSION")
        cls.step = HubGameStep.objects.create(session=cls.hub, order=0, game_key="estimation")
        cls.party_a = HubParticipant.objects.create(session=cls.hub, nickname="Party A", score_adjustment=7)
        cls.party_b = HubParticipant.objects.create(session=cls.hub, nickname="Party B", score_adjustment=3)
        cls.third = HubParticipant.objects.create(session=cls.hub, nickname="Third")
        cls.instance = DuelGameInstance.objects.create(
            hub_session=cls.hub, hub_game_step=cls.step, game_key="estimation",
            participant_a=cls.party_a, participant_b=cls.party_b,
        )
        cls.unit = DuelUnit.objects.create(game_instance=cls.instance, number=1)
        cls.other_unit = DuelUnit.objects.create(game_instance=cls.instance, number=2)

    def submission(self, **overrides):
        values = dict(unit=self.unit, participant=self.party_a, submission_kind=SubmissionKind.ANSWER)
        values.update(overrides)
        return DuelSubmission(**values)

    def committed(self, **overrides):
        values = dict(status=SubmissionStatus.COMMITTED, committed_at=timezone.now(), acceptance_sequence=1)
        values.update(overrides)
        return self.submission(**values)

    def assert_invalid(self, submission):
        with self.assertRaises(ValidationError):
            submission.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            submission.save(force_insert=True)

    def test_valid_draft_default_round_trips(self):
        item = self.submission(payload={"value": 42})
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertIsInstance(item.pk, UUID)
        self.assertEqual(item.status, SubmissionStatus.DRAFT)
        self.assertIsNone(item.committed_at)
        self.assertIsNone(item.acceptance_sequence)
        self.assertIsNotNone(item.created_at)
        self.assertIsNotNone(item.updated_at)
        self.assertEqual(item.payload, {"value": 42})

    def test_valid_committed_round_trips_without_allocating_values(self):
        item = self.committed(payload={"option_id": "A"})
        accepted_at = item.committed_at
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.status, SubmissionStatus.COMMITTED)
        self.assertEqual(item.committed_at, accepted_at)
        self.assertEqual(item.acceptance_sequence, 1)
        self.assertEqual(item.payload, {"option_id": "A"})

    def test_two_parties_can_commit_in_same_unit(self):
        for participant, sequence in ((self.party_a, 1), (self.party_b, 2)):
            item = self.committed(participant=participant, acceptance_sequence=sequence)
            item.full_clean()
            item.save()
        self.assertEqual(self.unit.submissions.count(), 2)

    def test_duplicate_committed_answer_rejected_and_original_preserved(self):
        original = self.committed(payload={"value": 8})
        original.save()
        snapshot = DuelSubmission.objects.get(pk=original.pk).__dict__.copy()
        self.assert_invalid(self.committed(acceptance_sequence=2, payload={"value": 9}))
        loaded = DuelSubmission.objects.get(pk=original.pk)
        self.assertEqual(loaded.payload, snapshot["payload"])
        self.assertEqual(loaded.committed_at, snapshot["committed_at"])
        self.assertEqual(loaded.acceptance_sequence, snapshot["acceptance_sequence"])

    def test_answer_and_bid_are_distinct(self):
        self.committed().save()
        item = self.committed(submission_kind=SubmissionKind.BID, acceptance_sequence=2)
        item.full_clean()
        item.save()
        self.assertEqual(set(self.unit.submissions.values_list("submission_kind", flat=True)), {"answer", "bid"})

    def test_multiple_drafts_do_not_block_commit_or_other_drafts(self):
        self.submission().save()
        self.submission().save()
        item = self.committed()
        item.full_clean()
        item.save()
        draft = self.submission()
        draft.full_clean()
        draft.save()
        self.assertEqual(self.unit.submissions.count(), 4)

    def test_third_party_rejected_by_model_validation(self):
        with self.assertRaisesMessage(ValidationError, "Only a party"):
            self.submission(participant=self.third).full_clean()

    def test_same_nickname_in_other_session_is_not_identity(self):
        other_hub = HubSession.objects.create(code="OTHER-SUB")
        other = HubParticipant.objects.create(session=other_hub, nickname=self.party_a.nickname)
        with self.assertRaisesMessage(ValidationError, "Only a party"):
            self.submission(participant=other).full_clean()

    def test_membership_uses_stored_relationship_not_cached_objects(self):
        self.unit.game_instance.participant_a = self.third
        with self.assertRaisesMessage(ValidationError, "Only a party"):
            self.submission(participant=self.third).full_clean()

    def test_unit_required(self):
        self.assert_invalid(self.submission(unit=None))

    def test_participant_required(self):
        self.assert_invalid(self.submission(participant=None))

    def test_nonexistent_foreign_keys_rejected(self):
        for field in ("unit_id", "participant_id"):
            with self.subTest(field=field):
                item = self.submission(**{field: 99999999})
                with self.assertRaises(ValidationError):
                    item.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    item.save()
                    connection.check_constraints()

    def test_invalid_status_rejected_by_choices_and_database(self):
        for value in ("unknown", "", None):
            with self.subTest(value=value):
                item = self.submission(status=value)
                with self.assertRaises(ValidationError):
                    item.full_clean(validate_constraints=False)
                self.assert_invalid(item)

    def test_invalid_kind_rejected_by_choices_and_database(self):
        for value in ("unknown", "", "buzzer", "host", None):
            with self.subTest(value=value):
                item = self.submission(submission_kind=value)
                with self.assertRaises(ValidationError):
                    item.full_clean(validate_constraints=False)
                self.assert_invalid(item)

    def test_draft_cannot_have_acceptance_sequence(self):
        self.assert_invalid(self.submission(acceptance_sequence=1))

    def test_draft_cannot_have_committed_timestamp(self):
        self.assert_invalid(self.submission(committed_at=timezone.now()))

    def test_committed_requires_timestamp(self):
        self.assert_invalid(self.committed(committed_at=None))

    def test_committed_requires_sequence(self):
        self.assert_invalid(self.committed(acceptance_sequence=None))

    def test_sequence_must_be_positive(self):
        for value in (0, -1):
            with self.subTest(value=value):
                self.assert_invalid(self.committed(acceptance_sequence=value))

    def test_sequence_unique_within_unit_across_parties_and_kinds(self):
        self.committed().save()
        self.assert_invalid(self.committed(participant=self.party_b, submission_kind=SubmissionKind.BID))

    def test_same_sequence_in_different_units_allowed(self):
        self.committed().save()
        item = self.committed(unit=self.other_unit)
        item.full_clean()
        item.save()
        self.assertEqual(DuelSubmission.objects.filter(acceptance_sequence=1).count(), 2)

    def test_equal_bids_preserve_explicit_server_order_not_party_order(self):
        timestamp = timezone.now()
        for party, sequence in ((self.party_a, 2), (self.party_b, 1)):
            item = self.committed(
                participant=party, acceptance_sequence=sequence, committed_at=timestamp,
                submission_kind=SubmissionKind.BID, payload={"value": 8},
            )
            item.full_clean()
            item.save()
        self.assertEqual(
            list(self.unit.submissions.order_by("acceptance_sequence").values_list("participant_id", flat=True)),
            [self.party_b.pk, self.party_a.pk],
        )

    def test_payload_examples_round_trip_without_interpretation(self):
        for payload in (
            {"value": 42}, {"latitude": 52.5, "longitude": 13.4},
            {"option_id": "A"}, {"value": 8},
            {"pairs": [{"left": "A", "right": "B"}], "text": "Gr\u00fc\u00dfe"},
        ):
            with self.subTest(payload=payload):
                item = self.submission(payload=payload)
                item.full_clean()
                item.save()
                item.refresh_from_db()
                self.assertEqual(item.payload, payload)

    def test_default_payload_not_shared(self):
        first, second = self.submission(), self.submission()
        first.payload["value"] = 42
        self.assertEqual(second.payload, {})

    def test_uuid_unique(self):
        first = self.submission()
        first.save()
        self.assertNotEqual(first.pk, self.submission().pk)
        self.assert_invalid(self.submission(id=first.pk))

    def test_draft_can_be_updated_and_committed_with_explicit_values(self):
        item = self.submission(payload={"value": 1})
        item.save()
        created_at, updated_at = item.created_at, item.updated_at
        item.payload = {"value": 2}
        item.status = SubmissionStatus.COMMITTED
        item.committed_at = timezone.now()
        item.acceptance_sequence = 1
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.payload, {"value": 2})
        self.assertEqual(item.created_at, created_at)
        self.assertGreaterEqual(item.updated_at, updated_at)

    def test_bulk_update_cannot_bypass_database_state_constraint(self):
        draft = self.submission()
        draft.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DuelSubmission.objects.filter(pk=draft.pk).update(status=SubmissionStatus.COMMITTED)
        draft.refresh_from_db()
        self.assertEqual(draft.status, SubmissionStatus.DRAFT)

    def test_replayed_material_in_another_unit_can_receive_new_answer(self):
        # No uniqueness on question/material, session, instance or participant alone.
        self.committed(payload={"option_id": "A"}).save()
        self.committed(unit=self.other_unit, payload={"option_id": "A"}).save()
        self.assertEqual(DuelSubmission.objects.count(), 2)

    def test_submission_does_not_change_parents_or_multiplayer_scores(self):
        models = (DuelGameInstance, DuelUnit, HubSession, HubGameStep, HubParticipant)
        before = {model: list(model.objects.order_by("pk").values()) for model in models}
        self.committed().save()
        for model, rows in before.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), rows)

