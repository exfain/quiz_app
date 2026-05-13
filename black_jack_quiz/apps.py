from django.apps import AppConfig


class BlackJackQuizConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'black_jack_quiz'

    def ready(self):  # noqa: D401
        from . import signals  # noqa: F401
