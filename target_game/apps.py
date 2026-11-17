from django.apps import AppConfig


class TargetGameConfig(AppConfig):
    name = 'target_game'
    default_auto_field = 'django.db.models.BigAutoField'
    # Session runtime/answers must never participate in content restore.
    supabase_sync_enabled = False
