from django.apps import AppConfig


class DuelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "duel"

    # Runtime data stays local until a dedicated sync/restore policy exists.
    supabase_sync_enabled = False
