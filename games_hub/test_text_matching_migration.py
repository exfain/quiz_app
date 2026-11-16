from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class TextMatchingMigrationTests(TransactionTestCase):
    def test_existing_sessions_keep_exact_policy_and_new_sessions_inherit_default(self):
        old = [('games_hub', '0023_hubgamestep_result_snapshot'),
               ('admin_dashboard', '0004_dashboardsettings_quiz_mc_answer_reveal_stagger_ms')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            session = apps.get_model('games_hub', 'HubSession').objects.create(code='OLDTEXT')
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
        from .models import HubSession
        from admin_dashboard.models import DashboardSettings
        saved = HubSession.objects.get(pk=session.pk)
        self.assertEqual(saved.text_match_threshold, 100)
        self.assertEqual(DashboardSettings.load().text_match_threshold, 90)
        self.assertEqual(HubSession.objects.create(code='NEWTEXT').text_match_threshold, 90)
