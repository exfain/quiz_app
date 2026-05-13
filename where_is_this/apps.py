from django.apps import AppConfig


class WhereIsThisConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'where_is_this'

    def ready(self):  # noqa: D401
        from . import signals  # noqa: F401
