"""Planning-only match metadata, with the existing framework-free rules."""
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from duel.matches import MatchConfig, MatchMode
from duel.models import DuelGameInstance, DuelSubmission, DuelScoreEntry
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from .models import HubGameStep, HubParticipant, HubSession


class DuelMatchPlanTests(TestCase):
    def setUp(self):
        self.hub = HubSession.objects.create(code="DUEL-PLAN", session_type="duel")

    def step(self, config=None, **kwargs):
        values = dict(session=self.hub, order=0, game_key="estimation")
        if config is not None:
            values["duel_match_config"] = config
        return HubGameStep(**(values | kwargs))

    def test_existing_creation_is_unconfigured_and_has_no_implicit_match_rule(self):
        step = self.step()
        step.full_clean()
        step.save()
        step.refresh_from_db()
        self.assertEqual(step.duel_match_config, {})
        self.assertEqual(step.game_format, "standard")
        with self.assertRaises(ValueError):
            step.get_duel_match_config()
        self.assertFalse(DuelGameInstance.objects.exists())

    def test_best_of_one_three_five_seven_round_trip_through_existing_contract(self):
        for n in (1, 3, 5, 7):
            with self.subTest(n=n):
                step = self.step({"mode": "best_of_n", "best_of": n}, order=n)
                step.full_clean()
                step.save()
                step.refresh_from_db()
                self.assertEqual(step.get_duel_match_config(), MatchConfig(MatchMode.BEST_OF_N, best_of=n))
                self.assertEqual(step.get_duel_match_config().required_wins, (n+1)//2)

    def test_target_score_round_trip_without_changing_scoring_or_session(self):
        before = HubSession.objects.values().get(pk=self.hub.pk)
        step = self.step({"mode": "target_score", "target_score": 10})
        step.full_clean()
        step.save()
        step.refresh_from_db()
        self.assertEqual(step.get_duel_match_config(), MatchConfig(MatchMode.TARGET_SCORE, target_score=10))
        self.assertEqual(HubSession.objects.values().get(pk=self.hub.pk), before)
        self.assertFalse(DuelSubmission.objects.exists())
        self.assertFalse(DuelScoreEntry.objects.exists())
        self.assertFalse(DuelGameInstance.objects.exists())

    def test_invalid_shape_modes_thresholds_and_ambiguous_rules_fail_full_clean(self):
        invalid = [None, [], "", False, 0, [1], "best_of_n",
            {"mode": "unknown"}, {"mode": "best_of_n"},
            *[{"mode": "best_of_n", "best_of": n} for n in (0, -1, 2, True, 3.0, "3")],
            *[{"mode": "target_score", "target_score": n} for n in (0, -1, True, 10.0, "10")],
            {"mode": "target_score"}, {"mode": "best_of_n", "best_of": 3, "target_score": 10},
            {"mode": "target_score", "target_score": 10, "best_of": 3},
            {"mode": "best_of_n", "best_of": 3, "extra": 1}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError) as raised:
                step = self.step()
                step.duel_match_config = value
                step.full_clean()
            self.assertIn("duel_match_config", raised.exception.message_dict)

    def test_unconfigured_defaults_are_independent_dicts(self):
        first, second = self.step(), self.step(order=1)
        first.duel_match_config["mode"] = "unknown"
        self.assertEqual(second.duel_match_config, {})

    def test_configuration_is_isolated_between_slots_and_never_selects_a_format(self):
        first = self.step({"mode": "best_of_n", "best_of": 7})
        first.save()
        second = self.step({"mode": "target_score", "target_score": 10}, order=1)
        second.save()
        first.duel_match_config = {"mode": "best_of_n", "best_of": 3}
        first.full_clean()
        first.save()
        second.refresh_from_db()
        self.assertEqual(second.duel_match_config, {"mode": "target_score", "target_score": 10})
        self.assertEqual((first.game_format, second.game_format), ("standard", "standard"))

    def test_database_default_allows_legacy_inserts_omitting_new_column(self):
        source = self.step({"mode": "best_of_n", "best_of": 7})
        source.save()
        quote = connection.ops.quote_name
        table = quote(HubGameStep._meta.db_table)
        columns = ", ".join(quote(f.column) for f in HubGameStep._meta.local_concrete_fields
            if not f.primary_key and f.name not in {"order", "duel_match_config"})
        with connection.cursor() as cursor:
            cursor.execute(f"INSERT INTO {table} ({columns}, {quote('order')}) "
                f"SELECT {columns}, %s FROM {table} WHERE {quote('id')} = %s", [1, source.pk])
        restored = HubGameStep.objects.get(session=self.hub, order=1)
        self.assertEqual(restored.duel_match_config, {})
        self.assertEqual(restored.game_format, "standard")

    def test_sql_null_is_rejected_without_claiming_save_performs_full_validation(self):
        step = self.step()
        step.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            HubGameStep.objects.filter(pk=step.pk).update(duel_match_config=None)
        # Cross-field JSON semantics deliberately use full_clean/the future
        # locked start path, not a save override or database-specific JSON SQL.
        step.duel_match_config = {"mode": "unknown"}
        step.save()
        step.refresh_from_db()
        with self.assertRaises(ValueError):
            step.get_duel_match_config()

    def test_config_does_not_expand_generic_admin_or_create_participant_identities(self):
        host = User.objects.create_superuser("plan-admin", "", "pw")
        request = RequestFactory().get("/admin/")
        request.user = host
        form = admin.site._registry[HubGameStep].get_form(request)
        self.assertNotIn("duel_match_config", form.base_fields)
        self.assertNotIn("game_format", form.base_fields)
        self.assertFalse(HubParticipant.objects.exists())

    def test_normal_session_with_two_participants_still_uses_multiplayer_hub_start(self):
        from Estimation.models import EstimationQuiz
        host = User.objects.create_user("plan-multiplayer-host")
        self.hub.session_type = "multiplayer"
        self.hub.creator = host
        self.hub.check_in_status = "completed"
        self.hub.locked_participant_count = 2
        self.hub.save()
        for name in ("Party A", "Party B"):
            HubParticipant.objects.create(session=self.hub, nickname=name,
                scoring_eligible=True, checked_in_at=timezone.now())
        quiz = EstimationQuiz.objects.create(creator=host, status="waiting")
        step = self.step({"mode": "best_of_n", "best_of": 7}, room_code=quiz.room_code)
        step.save()
        self.client.force_login(host)
        response = self.client.post(reverse("games_hub:activate_session_game", args=[self.hub.code]),
            data=json.dumps({"game_key": "estimation", "room_code": quiz.room_code}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        quiz.refresh_from_db()
        step.refresh_from_db()
        self.assertEqual(quiz.status, "active")
        self.assertIsNone(quiz.question_start_time)
        self.assertEqual(step.game_format, "standard")
        self.assertEqual(step.duel_match_config, {"mode": "best_of_n", "best_of": 7})
        self.assertFalse(DuelGameInstance.objects.exists())


class DuelMatchPlanSyncTests(TestCase):
    databases = {"default", "supabase"}

    def setUp(self):
        self.hub = HubSession.objects.create(code="PLAN-SYNC")
        HubSession.objects.using("supabase").create(pk=self.hub.pk, code=self.hub.code)
        self.configs = [SimpleNamespace(get_models=lambda: [HubGameStep]), apps.get_app_config("duel")]

    def test_sync_transports_empty_best_of_and_target_without_discovering_duel_tables(self):
        plans = [{}, {"mode": "best_of_n", "best_of": 7}, {"mode": "target_score", "target_score": 10}]
        for order, plan in enumerate(plans):
            HubGameStep.objects.create(session=self.hub, order=order, game_key="estimation", duel_match_config=plan)
        errors = StringIO()
        with patch("games_website.services.apps.get_app_configs", return_value=self.configs):
            result = sync_all_models_to_supabase(stderr=errors, stdout=StringIO())
        self.assertEqual(result, (3, ["HubGameStep"]))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(list(HubGameStep.objects.using("supabase").order_by("order").values_list("duel_match_config", flat=True)), plans)

    def test_restore_preserves_explicit_plans_and_keeps_legacy_rows_unconfigured(self):
        plans = [{}, {"mode": "best_of_n", "best_of": 5}, {"mode": "target_score", "target_score": 8}]
        for order, plan in enumerate(plans):
            values = {"duel_match_config": plan} if plan else {}
            HubGameStep.objects.using("supabase").create(session_id=self.hub.pk,
                order=order, game_key="estimation", **values)
        errors = StringIO()
        with patch("games_website.services.apps.get_app_configs", return_value=self.configs):
            result = restore_all_models_from_supabase(stderr=errors, stdout=StringIO())
        self.assertEqual(result, (3, ["HubGameStep"]))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(list(HubGameStep.objects.order_by("order").values_list("duel_match_config", flat=True)), plans)
        self.assertFalse(DuelGameInstance.objects.exists())
