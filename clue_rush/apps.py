from django.apps import AppConfig


class ClueRushConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'clue_rush'

    def ready(self):  # noqa: D401
        from . import signals  # noqa: F401
