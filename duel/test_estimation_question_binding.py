"""Persistent material authority, exact targets and unchanged source objects."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from Estimation.models import EstimationQuiz, EstimationQuestion
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase

from . import test_match_service as fixtures
from .commands import ActorContext, ActionConflict, CommandRejected
from .controllers.estimation import (
    RevealContext, RevealPhase, apply_estimation_result, commit_estimate,
    get_estimation_state, load_question_context,
)
from .estimation_questions import (
    bind_estimation_question, canonical_target, get_estimation_question_context, get_public_estimation_question,
)
from .match_service import get_match_snapshot
from .models import (
    DuelEstimationQuestionBinding as Binding, DuelGameInstance, DuelUnit, DuelActionReceipt,
    DuelSubmission, DuelScoreEntry, DuelUnitOutcome, DuelEstimationRoundState, ReceiptActorType, ReceiptStatus,
)
from .estimation_rounds import show_estimation_answers, show_estimation_solution


class BindingFixture(fixtures.MatchFixture):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("binding-material-host")
        self.quiz = EstimationQuiz.objects.create(creator=self.user)
        self.foreign_quiz = EstimationQuiz.objects.create(creator=self.user)
        self.source = EstimationQuestion.objects.create(created_by=self.user, question_text="Wie hoch?", correct_answer=100, unit="meters")
        self.alternative = EstimationQuestion.objects.create(created_by=self.user, question_text="Wie alt?", correct_answer=42, unit="years")
        self.foreign = EstimationQuestion.objects.create(created_by=self.user, question_text="Fremd?", correct_answer=7)
        self.quiz.selected_questions.add(self.source, self.alternative)
        self.foreign_quiz.selected_questions.add(self.foreign)
        self.step.room_code = self.quiz.room_code
        self.step.save(update_fields=["room_code"])
        self.unit.status = "prepared"
        self.unit.save(update_fields=["status"])

    def bind(self, **kwargs):
        return bind_estimation_question(**(dict(instance_id=self.instance.pk, unit_id=self.unit.pk,
            question_id=self.source.pk, actor=self.host, client_action_id=str(uuid4())) | kwargs))

    def prepared_unit(self, instance=None):
        instance = instance or self.instance
        return DuelUnit.objects.create(game_instance=instance, number=instance.units.count()+1)

    def model(self, **kwargs):
        return Binding(**(dict(unit=self.unit, quiz=self.quiz, question=self.source,
            question_text=self.source.question_text, target_value=canonical_target(self.source.correct_answer),
            unit_key=self.source.unit) | kwargs))

    def activate(self, unit=None):
        unit = unit or self.unit
        now = timezone.now()-timedelta(seconds=1)
        DuelUnit.objects.filter(pk=unit.pk).update(status="active", started_at=now)
        DuelEstimationRoundState.objects.filter(unit=unit).update(phase="answering", started_at=now,
            deadline=now+timedelta(hours=1), phase_changed_at=now)

    def answer(self, actor=None, value="95", question=None):
        return commit_estimate(question=question or get_estimation_question_context(self.unit.pk),
            actor=actor or self.actor, client_action_id=str(uuid4()), payload={"value": value})


class EstimationQuestionBindingTests(BindingFixture):
    def test_binding_round_trip_records_origin_and_frozen_fields(self):
        result = self.bind()
        self.assertEqual(result.status, ReceiptStatus.APPLIED)
        row = Binding.objects.get()
        self.assertEqual((row.unit_id, row.quiz_id, row.question_id), (self.unit.pk, self.quiz.pk, self.source.pk))
        self.assertEqual((row.question_text, row.target_value, row.unit_key), ("Wie hoch?", "100", "meters"))
        self.assertIsNotNone(row.created_at)
        context = get_estimation_question_context(self.unit.pk)
        self.assertEqual(context.instance_id, self.instance.pk)
        self.assertEqual(context.question_id, self.source.pk)
        self.assertEqual(context.question_text, "Wie hoch?")
        self.assertEqual(context.target, Decimal(100))
        self.assertEqual(context.unit, "meters")
        self.assertNotIn("target", result.data)
        self.assertNotIn("target_value", result.data)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.status, "prepared")
        self.assertEqual(result.state_revision, 1)

    def test_same_action_retries_after_source_edit_and_activation_preserve_everything(self):
        key = str(uuid4())
        first = self.bind(client_action_id=key)
        before = Binding.objects.values().get()
        self.activate()
        EstimationQuestion.objects.filter(pk=self.source.pk).update(question_text="Changed", correct_answer=999)
        retry = self.bind(client_action_id=key)
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.data, first.data)
        self.assertEqual(retry.state_revision, 1)
        self.assertEqual(Binding.objects.values().get(), before)
        self.assertEqual(DuelActionReceipt.objects.count(), 1)

    def test_new_action_same_binding_is_noop_with_original_identity_and_revision(self):
        original = self.bind()
        self.source.correct_answer = 900
        self.source.save(update_fields=["correct_answer"])
        repeat = self.bind()
        self.assertEqual(repeat.status, ReceiptStatus.REJECTED)
        self.assertEqual(repeat.data, {"code": "already_bound", "binding_id": original.data["binding_id"]})
        self.assertEqual(repeat.state_revision, 1)
        self.assertEqual(Binding.objects.get().target_value, "100")

    def test_conflicting_second_question_never_replaces_prepared_or_active_binding(self):
        self.bind()
        for active in (False, True):
            with self.subTest(active=active):
                if active:
                    self.activate()
                result = self.bind(question_id=self.alternative.pk)
                self.assertEqual(result.data["code"], "question_binding_conflict")
                self.assertEqual(Binding.objects.get().question_id, self.source.pk)
                self.assertEqual(result.state_revision, 1)

    def test_same_action_id_with_other_question_is_action_conflict(self):
        key = str(uuid4())
        self.bind(client_action_id=key)
        with self.assertRaises(ActionConflict):
            self.bind(client_action_id=key, question_id=self.alternative.pk)
        self.assertEqual(Binding.objects.count(), 1)

    def test_database_one_to_one_constraint_without_model_validation(self):
        self.bind()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            # Inner savepoint below keeps the connection usable for the outer block.
            with transaction.atomic():
                self.model().save(force_insert=True)
        self.assertEqual(Binding.objects.count(), 1)

    def test_database_decimal_text_constraints_without_full_clean(self):
        self.bind()
        for text in ("", "NaN", "Infinity", "1,5", "1e2", "01", "1.0", "-0", "2\n"):
            with self.subTest(text=text), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    Binding.objects.update(target_value=text)
        self.assertEqual(Binding.objects.get().target_value, "100")

    def test_database_rejects_empty_snapshot_text_or_unit(self):
        self.bind()
        for values in ({"question_text": ""}, {"unit_key": ""}):
            with self.subTest(values=values), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    Binding.objects.update(**values)

    def test_required_fks_are_validated_and_database_enforced(self):
        for field in ("unit", "quiz", "question"):
            with self.subTest(field=field):
                invalid = self.model(**{field: None})
                with self.assertRaises(ValidationError):
                    invalid.full_clean()
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        invalid.save(force_insert=True)
        self.assertFalse(Binding.objects.exists())

    def test_model_validation_checks_wrong_quiz_and_foreign_question(self):
        for values, field in (({"quiz": self.foreign_quiz}, "quiz"), ({"question": self.foreign}, "question")):
            with self.subTest(values=values), self.assertRaises(ValidationError) as raised:
                self.model(**values).full_clean()
            self.assertIn(field, raised.exception.message_dict)

    def test_model_validation_disallows_fabricated_snapshot_at_creation(self):
        for values in ({"question_text": "Fake"}, {"target_value": "999"}, {"unit_key": "years"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.model(**values).full_clean()

    def test_model_validation_rejects_editing_existing_snapshot_or_identity(self):
        self.bind()
        for values in ({"question_text": "Fake"}, {"target_value": "999"}, {"unit_key": "years"},
                       {"question_id": self.alternative.pk}, {"quiz_id": self.foreign_quiz.pk},
                       {"unit_id": self.other_unit.pk}):
            row = Binding.objects.get()
            for key, value in values.items():
                setattr(row, key, value)
            with self.subTest(values=values), self.assertRaises(ValidationError):
                row.full_clean()
        self.assertEqual(Binding.objects.get().target_value, "100")

    def test_foreign_unit_or_instance_cannot_bind(self):
        for kwargs in ({"unit_id": self.other_unit.pk}, {"instance_id": self.other_instance.pk}, {"instance_id": uuid4()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CommandRejected):
                self.bind(**kwargs)
        self.assertFalse(Binding.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_wrong_game_key_in_instance_or_step_is_rejected(self):
        self.instance.game_key = "buzzer"
        self.instance.save(update_fields=["game_key"])
        self.assertEqual(self.bind().data["code"], "not_estimation")
        self.instance.game_key = "estimation"
        self.instance.save(update_fields=["game_key"])
        self.step.game_key = "buzzer"
        self.step.save(update_fields=["game_key"])
        self.assertEqual(self.bind().data["code"], "not_estimation")
        self.assertFalse(Binding.objects.exists())

    def test_missing_quiz_missing_question_and_wrong_selection_fail_closed(self):
        self.assertEqual(self.bind(question_id=self.foreign.pk).data["code"], "question_not_in_quiz")
        self.assertEqual(self.bind(question_id=99999999).data["code"], "question_not_found")
        self.step.room_code = "NONE"
        self.step.save(update_fields=["room_code"])
        self.assertEqual(self.bind().data["code"], "quiz_not_found")
        self.assertFalse(Binding.objects.exists())

    def test_participants_cannot_create_a_binding(self):
        for participant in (self.party_a, self.third):
            with self.assertRaises(CommandRejected):
                self.bind(actor=ActorContext(ReceiptActorType.PARTICIPANT, participant.pk))
        self.assertFalse(Binding.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())

    def test_active_completed_or_already_started_prepared_unit_cannot_be_bound(self):
        for status, started, completed in (("active", None, None), ("completed", None, None),
                                          ("prepared", timezone.now(), None), ("prepared", None, timezone.now())):
            with self.subTest(status=status, started=started, completed=completed):
                DuelUnit.objects.filter(pk=self.unit.pk).update(status=status, started_at=started, completed_at=completed)
                self.assertEqual(self.bind().data["code"], "unit_not_prepared")
        self.assertFalse(Binding.objects.exists())

    def test_closed_matches_cannot_receive_binding(self):
        for status in ("completed", "aborted"):
            DuelGameInstance.objects.filter(pk=self.instance.pk).update(match_status=status)
            self.assertEqual(self.bind().data["code"], "match_closed")
        self.assertFalse(Binding.objects.exists())

    def test_binding_before_match_start_does_not_start_it(self):
        DuelGameInstance.objects.filter(pk=self.instance.pk).update(match_status="not_started")
        self.assertEqual(self.bind().status, ReceiptStatus.APPLIED)
        self.instance.refresh_from_db()
        self.unit.refresh_from_db()
        self.assertEqual(self.instance.match_status, "not_started")
        self.assertEqual(self.unit.status, "prepared")

    def test_existing_input_history_prevents_binding_a_reused_unit(self):
        DuelSubmission.objects.create(unit=self.unit, participant=self.party_a, submission_kind="answer", payload={"value": 95})
        self.assertEqual(self.bind().data["code"], "unit_has_history")
        self.assertFalse(Binding.objects.exists())

    def test_same_question_in_two_units_gets_independent_identity_and_new_snapshot(self):
        self.bind()
        second = self.prepared_unit()
        self.source.correct_answer = 200
        self.source.save(update_fields=["correct_answer"])
        self.assertEqual(self.bind(unit_id=second.pk).status, ReceiptStatus.APPLIED)
        a, b = get_estimation_question_context(self.unit.pk), get_estimation_question_context(second.pk)
        self.assertEqual(a.question_id, b.question_id)
        self.assertNotEqual(a.unit_id, b.unit_id)
        self.assertEqual((a.target, b.target), (Decimal(100), Decimal(200)))

    def test_new_instance_never_inherits_old_unit_binding(self):
        self.bind()
        with self.assertRaises(CommandRejected):
            get_estimation_question_context(self.other_unit.pk)
        self.assertEqual(self.bind(instance_id=self.other_instance.pk, unit_id=self.other_unit.pk).status, ReceiptStatus.APPLIED)
        self.assertEqual(Binding.objects.count(), 2)
        self.assertNotEqual(get_estimation_question_context(self.unit.pk).instance_id,
                            get_estimation_question_context(self.other_unit.pk).instance_id)

    def test_snapshot_survives_original_edit_and_selection_removal(self):
        self.bind()
        before = get_estimation_question_context(self.unit.pk)
        self.activate()
        before = get_estimation_question_context(self.unit.pk)  # persisted clock is now part of context
        EstimationQuestion.objects.filter(pk=self.source.pk).update(question_text="Geändert", correct_answer=999, unit="years")
        self.quiz.selected_questions.remove(self.source)
        after = get_estimation_question_context(self.unit.pk)
        self.assertEqual(after, before)
        Binding.objects.get().full_clean()  # historical snapshot is still valid
        self.assertEqual(load_question_context(instance_id=self.instance.pk, unit_id=self.unit.pk, question_id=self.source.pk), before)

    def test_all_finite_source_values_are_copied_without_added_precision_loss(self):
        for value in (-20.0, 0.0, -0.0, 0.1, 0.10000000000000002, 1.7976931348623157e308, 5e-324):
            with self.subTest(value=value):
                self.source.correct_answer = value
                self.source.save(update_fields=["correct_answer"])
                self.source.refresh_from_db()
                unit = self.prepared_unit()
                self.assertEqual(self.bind(unit_id=unit.pk).status, ReceiptStatus.APPLIED)
                context = get_estimation_question_context(unit.pk)
                self.assertEqual(context.target, Decimal(str(self.source.correct_answer)))
                self.assertIs(type(Binding.objects.get(unit=unit).target_value), str)

    def test_resolver_does_not_use_float_conversion_helper_for_decimal_target(self):
        with localcontext() as context:
            context.prec = 2
            self.source.correct_answer = 0.12345678901234566
            self.source.save(update_fields=["correct_answer"])
            with patch("duel.controllers.estimation.finite_number", side_effect=AssertionError("no float round-trip")):
                self.bind()
                target = get_estimation_question_context(self.unit.pk).target
        self.assertEqual(target, Decimal(str(self.source.correct_answer)))

    def test_nonfinite_source_target_rejected(self):
        for value in (float("inf"), -float("inf")):
            EstimationQuestion.objects.filter(pk=self.source.pk).update(correct_answer=value)
            self.assertEqual(self.bind().data["code"], "invalid_question_target")
        self.assertFalse(Binding.objects.exists())

    def test_decimal_magnitude_validation_is_consistent_before_and_after_storage(self):
        self.bind()
        self.activate()
        # float(text) rounds this out-of-range decimal down to float_info.max;
        # exact validation must reject it before committing, not fail at scoring.
        result = self.answer(value="1.7976931348623158e308")
        self.assertEqual(result.status, ReceiptStatus.REJECTED)
        self.assertEqual(result.data["code"], "invalid_estimate")
        self.assertFalse(DuelSubmission.objects.exists())

    def test_public_question_has_no_solution_answers_or_reveal_toggle(self):
        self.bind()
        data = get_public_estimation_question(self.unit.pk)
        self.assertEqual(data["question"], "Wie hoch?")
        self.assertEqual(data["unit"]["symbol"], "m")
        for key in ("target", "target_value", "answers", "own_answer", "points", "deviations"):
            self.assertNotIn(key, data)
        with self.assertRaises(TypeError):
            get_public_estimation_question(self.unit.pk, reveal=True)

    def test_unbound_unit_never_falls_back_to_source_question(self):
        for read in (lambda: get_estimation_question_context(self.unit.pk),
                     lambda: load_question_context(instance_id=self.instance.pk, unit_id=self.unit.pk, question_id=self.source.pk)):
            with self.assertRaises(CommandRejected) as raised:
                read()
            self.assertEqual(raised.exception.code, "question_not_bound")

    def test_reconstructed_context_drives_atomic_idempotent_estimation_result(self):
        self.bind()
        self.activate()
        self.answer()
        self.answer(ActorContext(ReceiptActorType.PARTICIPANT, self.party_b.pk), "120")
        question = get_estimation_question_context(self.unit.pk)
        key = str(uuid4())
        args = dict(reveal=RevealContext(question, RevealPhase.RESULT), actor=self.host, client_action_id=key)
        phase_args = dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.host)
        show_estimation_answers(**phase_args, client_action_id=str(uuid4()))
        show_estimation_solution(**phase_args, client_action_id=str(uuid4()))
        first, retry = apply_estimation_result(**args), apply_estimation_result(**args)
        self.assertEqual(first.data["points"], {"a": 1, "b": 0})
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.data, first.data)
        self.assertEqual(DuelScoreEntry.objects.count(), 2)
        self.assertEqual(DuelUnitOutcome.objects.count(), 1)
        state = get_estimation_state(reveal=RevealContext(get_estimation_question_context(self.unit.pk), RevealPhase.RESULT))
        self.assertEqual(state["target"]["value"], "100")
        self.assertEqual(state["match"]["party_a"]["round_wins"], 1)

    def test_forged_context_cannot_change_display_commit_or_result(self):
        self.bind()
        self.activate()
        question = get_estimation_question_context(self.unit.pk)
        for forged in (replace(question, target=Decimal(200)), replace(question, question_text="Fake"), replace(question, unit="years")):
            with self.subTest(forged=forged), self.assertRaises(CommandRejected):
                self.answer(question=forged)
        self.answer()
        self.answer(ActorContext(ReceiptActorType.PARTICIPANT, self.party_b.pk), "120")
        forged = RevealContext(replace(question, target=Decimal(200)), RevealPhase.RESULT)
        with self.assertRaises(CommandRejected):
            apply_estimation_result(reveal=forged, actor=self.host, client_action_id=str(uuid4()))
        with self.assertRaises(CommandRejected):
            get_estimation_state(reveal=RevealContext(replace(question, question_text="Fake"), RevealPhase.SOLUTION))
        self.assertFalse(DuelScoreEntry.objects.exists())

    def test_receipt_failure_rolls_back_binding_and_revision(self):
        with patch.object(DuelActionReceipt, "save", side_effect=IntegrityError("receipt failed")):
            with self.assertRaises(IntegrityError):
                self.bind()
        self.assertFalse(Binding.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.exists())
        self.assertEqual(get_match_snapshot(self.instance.pk)["state_revision"], 0)

    def test_binding_does_not_change_source_quiz_points_or_unit_status(self):
        source, quiz = EstimationQuestion.objects.values().get(pk=self.source.pk), EstimationQuiz.objects.values().get(pk=self.quiz.pk)
        self.bind()
        self.assertEqual(EstimationQuestion.objects.values().get(pk=self.source.pk), source)
        self.assertEqual(EstimationQuiz.objects.values().get(pk=self.quiz.pk), quiz)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelUnitOutcome.objects.exists())

    def test_new_model_inherits_existing_app_wide_supabase_exclusion(self):
        config = apps.get_app_config("duel")
        self.assertIn(Binding, list(config.get_models()))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__), patch("games_website.services.apps.get_app_configs", return_value=[config]):
                with patch.object(config, "get_models", side_effect=AssertionError("skip before discovery")):
                    self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))
