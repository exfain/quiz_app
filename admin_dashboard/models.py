from django.db import OperationalError, ProgrammingError, models


class DashboardSettings(models.Model):
    """Global presentation settings shared by all sessions."""

    DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER = 75
    MIN_QUESTION_REVEAL_MS_PER_CHARACTER = 35
    MAX_QUESTION_REVEAL_MS_PER_CHARACTER = 180

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    question_reveal_ms_per_character = models.PositiveSmallIntegerField(
        default=DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER,
    )

    @classmethod
    def load(cls):
        settings, _ = cls.objects.get_or_create(singleton_id=1)
        return settings

    @classmethod
    def question_reveal_speed(cls):
        """Keep question presentation available during a rolling schema update."""
        try:
            return cls.normalize_question_reveal_speed(
                cls.load().question_reveal_ms_per_character
            )
        except (OperationalError, ProgrammingError) as exc:
            if cls._meta.db_table not in str(exc):
                raise
            return cls.DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER

    @classmethod
    def normalize_question_reveal_speed(cls, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = cls.DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER
        return max(
            cls.MIN_QUESTION_REVEAL_MS_PER_CHARACTER,
            min(cls.MAX_QUESTION_REVEAL_MS_PER_CHARACTER, value),
        )
