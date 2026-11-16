from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from games_hub.models import HubSession, HubGameStep, HubParticipant
from .contracts import MatchStatus, RoundOutcome
from .models import DuelGameInstance, DuelMatchOutcome, DuelUnit, DuelUnitOutcome, DuelScoreEntry


class OutcomeFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hub = HubSession.objects.create(code="OUTCOME-MODEL")
        cls.step = HubGameStep.objects.create(session=cls.hub, game_key="estimation", order=0)
        cls.a = HubParticipant.objects.create(session=cls.hub, nickname="Party A")
        cls.b = HubParticipant.objects.create(session=cls.hub, nickname="Party B")
        cls.third = HubParticipant.objects.create(session=cls.hub, nickname="Third")
        cls.instance = DuelGameInstance.objects.create(
            hub_session=cls.hub, hub_game_step=cls.step, participant_a=cls.a, participant_b=cls.b,
            game_key="estimation", match_status=MatchStatus.IN_PROGRESS.value,
        )
        cls.unit = DuelUnit.objects.create(game_instance=cls.instance, number=1)


class UnitOutcomeModelTests(OutcomeFixture):
    def item(self, **kwargs):
        return DuelUnitOutcome(**(dict(unit=self.unit, outcome=RoundOutcome.PARTY_A_WINS.value, winner=self.a) | kwargs))

    def invalid(self, **kwargs):
        item = self.item(**kwargs)
        with self.assertRaises(ValidationError):
            item.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            item.save(force_insert=True)

    def test_all_final_results_round_trip(self):
        for number, (outcome, winner) in enumerate(((RoundOutcome.PARTY_A_WINS, self.a),
                (RoundOutcome.PARTY_B_WINS, self.b), (RoundOutcome.DRAW, None), (RoundOutcome.NO_WINNER, None)), 1):
            unit = DuelUnit.objects.create(game_instance=self.instance, number=number + 1)
            item = self.item(unit=unit, outcome=outcome.value, winner=winner)
            item.full_clean()
            item.save()
            item.refresh_from_db()
            self.assertEqual(item.outcome, outcome.value)
            self.assertEqual(item.winner, winner)
            self.assertIsNotNone(item.completed_at)

    def test_one_result_per_unit_is_database_enforced(self):
        self.item().save()
        self.invalid(outcome=RoundOutcome.PARTY_B_WINS.value, winner=self.b)

    def test_winner_cannot_be_third_party_or_wrong_side(self):
        for winner in (self.third, self.b):
            with self.subTest(winner=winner.pk), self.assertRaises(ValidationError):
                self.item(winner=winner).full_clean()

    def test_nonfinal_unknown_and_null_results_rejected(self):
        for outcome in (RoundOutcome.UNDECIDED.value, "unknown", "", None):
            with self.subTest(outcome=outcome):
                self.invalid(outcome=outcome)

    def test_winner_null_consistency_is_database_enforced(self):
        self.invalid(winner=None)
        self.invalid(outcome=RoundOutcome.DRAW.value)
        self.invalid(outcome=RoundOutcome.NO_WINNER.value)

    def test_unit_and_timestamp_required(self):
        self.invalid(unit=None)
        self.invalid(completed_at=None)

    def test_uuid_unique(self):
        first = self.item()
        first.save()
        other = DuelUnit.objects.create(game_instance=self.instance, number=2)
        self.invalid(id=first.pk, unit=other)

    def test_other_instance_winner_not_allowed(self):
        hub = HubSession.objects.create(code="OTHER-OUTCOME")
        party = HubParticipant.objects.create(session=hub, nickname="Foreign")
        with self.assertRaises(ValidationError):
            self.item(winner=party).full_clean()

    def test_outcome_save_never_awards_points_or_changes_parent_lifecycle(self):
        before = (self.instance.match_status, self.instance.state_revision, self.unit.status)
        self.item().save()
        self.instance.refresh_from_db()
        self.unit.refresh_from_db()
        self.assertEqual(before, (self.instance.match_status, self.instance.state_revision, self.unit.status))
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_bulk_update_cannot_set_pending_or_remove_winner(self):
        item = self.item()
        item.save()
        for changes in ({"winner": None}, {"outcome": "undecided"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                DuelUnitOutcome.objects.filter(pk=item.pk).update(**changes)


class MatchOutcomeModelTests(OutcomeFixture):
    def item(self, **kwargs):
        return DuelMatchOutcome(**(dict(game_instance=self.instance, winner=self.a, mode="best_of_n", threshold=3) | kwargs))

    def invalid(self, **kwargs):
        item = self.item(**kwargs)
        with self.assertRaises(ValidationError):
            item.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            item.save(force_insert=True)

    def test_completion_round_trip_does_not_change_parent_or_scores(self):
        item = self.item()
        item.full_clean()
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.winner_id, self.a.pk)
        self.assertEqual(item.threshold, 3)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.match_status, MatchStatus.IN_PROGRESS.value)
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_only_one_completion_per_instance(self):
        self.item().save()
        self.invalid(winner=self.b)

    def test_foreign_winner_rejected_by_model(self):
        with self.assertRaises(ValidationError):
            self.item(winner=self.third).full_clean()

    def test_required_fields_and_mode_constraints(self):
        for overrides in ({"game_instance": None}, {"winner": None}, {"mode": "unknown"},
                          {"threshold": 0}, {"threshold": -1}, {"completed_at": None}):
            with self.subTest(overrides=overrides):
                self.invalid(**overrides)

    def test_even_best_of_rejected_by_model_validation(self):
        with self.assertRaises(ValidationError):
            self.item(threshold=2).full_clean()

    def test_target_rule_snapshot_can_be_stored(self):
        item = self.item(mode="target_score", threshold=10)
        item.full_clean()
        item.save()
        self.assertEqual(DuelMatchOutcome.objects.get().threshold, 10)

    def test_uuid_uniqueness_across_instances(self):
        first = self.item()
        first.save()
        other = DuelGameInstance.objects.create(hub_session=self.hub, hub_game_step=self.step,
            participant_a=self.a, participant_b=self.b, game_key="estimation")
        self.invalid(id=first.pk, game_instance=other)
