from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.test import SimpleTestCase

from games_website.services import (
    restore_all_models_from_supabase,
    sync_all_models_to_supabase,
)

from .models import DuelActionReceipt, DuelDecision, DuelMatchOutcome, DuelScoreEntry, DuelSubmission, DuelUnit, DuelUnitOutcome


class DuelSupabaseExclusionTests(SimpleTestCase):
    def test_match_result_models_are_excluded_from_sync_and_restore(self):
        config = apps.get_app_config("duel")
        self.assertTrue({DuelUnitOutcome, DuelMatchOutcome}.issubset(set(config.get_models())))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))

    def test_registered_decision_is_excluded_from_sync_and_restore(self):
        config = apps.get_app_config("duel")
        self.assertIn(DuelDecision, list(config.get_models()))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__):
                with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                    self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))

    def test_registered_receipt_is_excluded_from_sync_and_restore(self):
        config = apps.get_app_config("duel")
        self.assertIn(DuelActionReceipt, list(config.get_models()))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__):
                with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                    self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))

    def test_registered_score_entry_is_excluded_from_sync_and_restore(self):
        config = apps.get_app_config("duel")
        self.assertIn(DuelScoreEntry, list(config.get_models()))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__):
                with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                    self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))

    def test_registered_submission_is_excluded_from_sync_and_restore(self):
        config = apps.get_app_config("duel")
        self.assertIn(DuelSubmission, list(config.get_models()))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__):
                with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                    self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))

    def test_registered_unit_is_excluded_from_sync_and_restore(self):
        config = apps.get_app_config("duel")
        self.assertIn(DuelUnit, list(config.get_models()))
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__):
                errors = StringIO()
                with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                    self.assertEqual(service(stdout=StringIO(), stderr=errors), (0, []))
                self.assertEqual(errors.getvalue(), "")

    def test_duel_is_skipped_before_model_discovery_in_both_directions(self):
        config = apps.get_app_config("duel")
        self.assertFalse(config.supabase_sync_enabled)
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__):
                with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                    with patch.object(config, "get_models", side_effect=AssertionError("Duel must not be inspected")):
                        self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))

    def test_existing_apps_without_flag_are_still_processed(self):
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with self.subTest(service=service.__name__):
                manager = MagicMock()
                manager.all.return_value.count.return_value = 0
                manager.values_list.return_value = []
                remote = manager.using.return_value
                remote.all.return_value.count.return_value = 0
                remote.values_list.return_value = []
                legacy_model = type("LegacyModel", (), {
                    "_meta": SimpleNamespace(abstract=False, managed=True, fields=[]),
                    "objects": manager,
                })
                config = SimpleNamespace(get_models=lambda: [legacy_model])
                output, errors = StringIO(), StringIO()
                with patch("games_website.services.apps.get_app_configs", return_value=[config]):
                    self.assertEqual(service(stdout=output, stderr=errors), (0, []))
                self.assertIn("LegacyModel", output.getvalue())
                self.assertEqual(errors.getvalue(), "")
                manager.using.assert_any_call("supabase")
                remote.values_list.assert_any_call("id", flat=True)
                if service is sync_all_models_to_supabase:
                    manager.all.assert_called_once_with()
                    manager.values_list.assert_called_once_with("id", flat=True)
                else:
                    remote.all.assert_called_once_with()
                    manager.using.assert_any_call("default")
