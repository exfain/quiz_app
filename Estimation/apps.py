from django.apps import AppConfig


class EstimationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Estimation'

    def ready(self):  # noqa: D401
        from . import signals  # noqa: F401
