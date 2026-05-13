from django.apps import AppConfig


class AssignConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Assign'

    def ready(self):  # noqa: D401
        from . import signals  # noqa: F401
