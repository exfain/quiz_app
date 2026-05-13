from django.apps import AppConfig


class SortingLadderConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sorting_ladder'

    def ready(self):  # noqa: D401
        # Import signal handlers for SortingLadder models
        from . import signals  # noqa: F401
