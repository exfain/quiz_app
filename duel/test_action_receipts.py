import hashlib
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from games_hub.models import HubGameStep, HubParticipant, HubSession

from .models import (
    DuelActionReceipt, DuelGameInstance, DuelScoreEntry, DuelSubmission, DuelUnit,
    ReceiptActorType, ReceiptStatus, SubmissionKind,
)


class DuelActionReceiptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.host = get_user_model().objects.create(username="receipt-host")
        cls.hub = HubSession.objects.create(code="RECEIPT-HUB", creator=cls.host)
        cls.step = HubGameStep.objects.create(session=cls.hub, order=0, game_key="estimation")
        cls.party_a = HubParticipant.objects.create(session=cls.hub, nickname="Party A", score_adjustment=7)
        cls.party_b = HubParticipant.objects.create(session=cls.hub, nickname="Party B", score_adjustment=3)
        values = dict(
            hub_session=cls.hub, hub_game_step=cls.step, game_key="estimation",
            participant_a=cls.party_a, participant_b=cls.party_b,
        )
        cls.instance = DuelGameInstance.objects.create(**values)
        cls.other_instance = DuelGameInstance.objects.create(**values)
        cls.unit = DuelUnit.objects.create(game_instance=cls.instance, number=1)
        cls.other_unit = DuelUnit.objects.create(game_instance=cls.other_instance, number=1)

    def receipt(self, **overrides):
        values = dict(
            game_instance=self.instance, unit=self.unit, actor_type=ReceiptActorType.PARTICIPANT,
            actor_key=str(self.party_a.pk), client_action_id=str(uuid4()), action_type="duel.submit",
            request_fingerprint=hashlib.sha256(b'answer A').hexdigest(), status=ReceiptStatus.APPLIED,
            result={"code": "accepted"},
        )
        values.update(overrides)
        return DuelActionReceipt(**values)

    def assert_invalid_in_model_and_database(self, **overrides):
        item = self.receipt(**overrides)
        with self.assertRaises(ValidationError):
            item.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            item.save(force_insert=True)

    def test_applied_receipt_round_trips(self):
        item = self.receipt()
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertIsInstance(item.pk, UUID)
        self.assertEqual(item.status, ReceiptStatus.APPLIED)
        self.assertEqual(item.actor_type, ReceiptActorType.PARTICIPANT)
        self.assertEqual(item.actor_key, str(self.party_a.pk))
        self.assertEqual(item.game_instance_id, self.instance.pk)
        self.assertEqual(item.unit_id, self.unit.pk)
        self.assertEqual(item.result, {"code": "accepted"})
        self.assertIsNotNone(item.created_at)

    def test_rejected_receipt_round_trips(self):
        item = self.receipt(status=ReceiptStatus.REJECTED, result={"code": "invalid_phase"})
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.status, "rejected")
        self.assertEqual(item.result, {"code": "invalid_phase"})

    def test_duplicate_id_for_same_actor_and_instance_rejected(self):
        original = self.receipt()
        original.save()
        self.assert_invalid_in_model_and_database(client_action_id=original.client_action_id)

    def test_distinct_action_ids_for_same_actor_allowed(self):
        first, second = self.receipt(), self.receipt()
        first.save()
        second.full_clean()
        second.save()
        self.assertNotEqual(first.client_action_id, second.client_action_id)

    def test_same_action_id_for_different_participants_allowed(self):
        first = self.receipt()
        first.save()
        second = self.receipt(actor_key=str(self.party_b.pk), client_action_id=first.client_action_id)
        second.full_clean()
        second.save()
        self.assertEqual(DuelActionReceipt.objects.count(), 2)

    def test_actor_type_namespaces_equal_host_and_participant_keys(self):
        first = self.receipt()
        first.save()
        # IDs from two identity tables may have identical string representations.
        second = self.receipt(
            actor_type=ReceiptActorType.HOST, actor_key=first.actor_key,
            client_action_id=first.client_action_id, action_type="duel.reveal_solution",
        )
        second.full_clean()
        second.save()
        self.assertEqual(DuelActionReceipt.objects.count(), 2)

    def test_distinct_host_identities_are_not_one_shared_host_key(self):
        first = self.receipt(actor_type=ReceiptActorType.HOST, actor_key=str(self.host.pk))
        first.save()
        other_host = get_user_model().objects.create(username="other-receipt-host")
        second = self.receipt(
            actor_type=ReceiptActorType.HOST, actor_key=str(other_host.pk), client_action_id=first.client_action_id,
        )
        second.full_clean()
        second.save()
        self.assertEqual(DuelActionReceipt.objects.count(), 2)

    def test_same_action_id_in_different_instances_allowed(self):
        first = self.receipt()
        first.save()
        second = self.receipt(
            game_instance=self.other_instance, unit=self.other_unit, client_action_id=first.client_action_id,
        )
        second.full_clean()
        second.save()

    def test_instance_required(self):
        self.assert_invalid_in_model_and_database(game_instance=None)

    def test_foreign_unit_rejected_by_model_validation(self):
        with self.assertRaisesMessage(ValidationError, "unit must belong"):
            self.receipt(unit=self.other_unit).full_clean()

    def test_cached_unit_cannot_spoof_stored_instance(self):
        self.other_unit.game_instance = self.instance
        with self.assertRaisesMessage(ValidationError, "unit must belong"):
            self.receipt(unit=self.other_unit).full_clean()

    def test_unit_optional(self):
        item = self.receipt(unit=None)
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertIsNone(item.unit_id)

    def test_invalid_actor_types_rejected_by_choices_and_database(self):
        for value in ("spectator", "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.receipt(actor_type=value).full_clean(validate_constraints=False)
                self.assert_invalid_in_model_and_database(actor_type=value)

    def test_invalid_or_pending_status_rejected_by_choices_and_database(self):
        for value in ("pending", "unknown", "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.receipt(status=value).full_clean(validate_constraints=False)
                self.assert_invalid_in_model_and_database(status=value)

    def test_status_has_no_implicit_success_default(self):
        field = DuelActionReceipt._meta.get_field("status")
        self.assertFalse(field.has_default())
        self.assertEqual(set(ReceiptStatus.values), {"applied", "rejected"})
        self.assert_invalid_in_model_and_database(status=field.get_default())

    def test_empty_identifiers_rejected_by_model_and_database(self):
        for name in ("client_action_id", "actor_key", "action_type"):
            for value in ("", None):
                with self.subTest(field=name, value=value):
                    self.assert_invalid_in_model_and_database(**{name: value})

    def test_whitespace_identifiers_rejected_by_model_validation(self):
        for name in ("client_action_id", "actor_key", "action_type"):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                self.receipt(**{name: " \t\n"}).full_clean()

    def test_action_id_has_no_generated_default(self):
        self.assertFalse(DuelActionReceipt._meta.get_field("client_action_id").has_default())

    def test_existing_uuid_strings_and_opaque_action_ids_round_trip(self):
        for action_id in (str(uuid4()), "XYZ"):
            item = self.receipt(client_action_id=action_id)
            item.full_clean()
            item.save()
            item.refresh_from_db()
            self.assertEqual(item.client_action_id, action_id)

    def test_fingerprint_rejected_by_validator_and_database(self):
        for value in ("", None, "a" * 63, "a" * 65, "g" * 64, " " * 64, "a" * 64 + "\n"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.receipt(request_fingerprint=value).full_clean(validate_constraints=False)
                self.assert_invalid_in_model_and_database(request_fingerprint=value)

    def test_valid_sha256_fingerprints_preserved_including_hex_case(self):
        digest = hashlib.sha256(b'canonical request').hexdigest()
        for value in (digest, digest.upper()):
            item = self.receipt(request_fingerprint=value)
            item.full_clean()
            item.save()
            item.refresh_from_db()
            self.assertEqual(item.request_fingerprint, value)

    def test_result_json_round_trips_without_fetching_game_state(self):
        result = {"code": "accepted", "revision": 7, "details": [True, None, {"message": "Gr\u00fc\u00dfe"}]}
        item = self.receipt(result=result)
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.result, result)

    def test_default_result_is_not_shared_between_receipts(self):
        first, second = DuelActionReceipt(), DuelActionReceipt()
        first.result["code"] = "accepted"
        self.assertEqual(second.result, {})

    def test_conflicting_fingerprint_cannot_create_second_receipt(self):
        original = self.receipt()
        original.save()
        before = DuelActionReceipt.objects.filter(pk=original.pk).values().get()
        self.assert_invalid_in_model_and_database(
            client_action_id=original.client_action_id, request_fingerprint=hashlib.sha256(b'answer B').hexdigest(),
            result={"code": "different"},
        )
        self.assertEqual(DuelActionReceipt.objects.filter(pk=original.pk).values().get(), before)

    def test_changed_action_type_cannot_bypass_action_identity(self):
        original = self.receipt()
        original.save()
        self.assert_invalid_in_model_and_database(client_action_id=original.client_action_id, action_type="duel.buzz")

    def test_changed_unit_cannot_bypass_action_identity(self):
        original = self.receipt()
        original.save()
        unit = DuelUnit.objects.create(game_instance=self.instance, number=2)
        self.assert_invalid_in_model_and_database(client_action_id=original.client_action_id, unit=unit)

    def test_changed_status_cannot_bypass_action_identity(self):
        original = self.receipt()
        original.save()
        self.assert_invalid_in_model_and_database(client_action_id=original.client_action_id, status=ReceiptStatus.REJECTED)

    def test_same_fingerprint_for_distinct_action_ids_allowed(self):
        first = self.receipt()
        first.save()
        second = self.receipt(request_fingerprint=first.request_fingerprint)
        second.full_clean()
        second.save()

    def test_uuid_unique_in_database(self):
        first = self.receipt()
        first.save()
        self.assertNotEqual(first.pk, self.receipt().pk)
        self.assert_invalid_in_model_and_database(id=first.pk)

    def test_foreign_keys_reject_nonexistent_targets(self):
        for field, value in (("unit_id", 99999999), ("game_instance_id", UUID(int=1))):
            with self.subTest(field=field):
                item = self.receipt(**{field: value})
                with self.assertRaises(ValidationError):
                    item.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    item.save()
                    connection.check_constraints()

    def test_database_constraints_cannot_be_bypassed_by_bulk_update(self):
        item = self.receipt()
        item.save()
        for overrides in (
            {"request_fingerprint": "not-a-digest"}, {"status": "pending"}, {"actor_type": "spectator"},
            {"actor_key": ""}, {"client_action_id": ""}, {"action_type": ""},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DuelActionReceipt.objects.filter(pk=item.pk).update(**overrides)

    def test_duplicate_action_identity_rejected_on_bulk_update(self):
        original, other = self.receipt(), self.receipt()
        original.save()
        other.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DuelActionReceipt.objects.filter(pk=other.pk).update(client_action_id=original.client_action_id)

    def test_receipt_does_not_change_scores_rounds_matches_or_submissions(self):
        DuelScoreEntry.objects.create(
            game_instance=self.instance, unit=self.unit, participant=self.party_a, delta=1, source_key="original",
        )
        DuelSubmission.objects.create(
            unit=self.unit, participant=self.party_a, submission_kind=SubmissionKind.ANSWER, payload={"value": 42},
        )
        parents = (DuelGameInstance, DuelUnit, DuelSubmission, DuelScoreEntry, HubSession, HubGameStep, HubParticipant)
        before = {model: list(model.objects.order_by("pk").values()) for model in parents}
        for status in ReceiptStatus:
            self.receipt(status=status).save()
        for model, rows in before.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), rows)
