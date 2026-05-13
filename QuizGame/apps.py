from django.apps import AppConfig


class QuizgameConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'QuizGame'

    def ready(self):  # noqa: D401
        # Import signal handlers for QuizGame models
        from . import signals  # noqa: F401
