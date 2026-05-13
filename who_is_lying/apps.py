from django.apps import AppConfig


class WhoIsLyingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'who_is_lying'

    def ready(self):  # noqa: D401
        from . import signals  # noqa: F401
