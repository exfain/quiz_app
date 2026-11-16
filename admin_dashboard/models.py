from django.db import OperationalError, ProgrammingError, models
from django.core.validators import MinValueValidator, MaxValueValidator


class DashboardSettings(models.Model):
    """Global presentation settings and defaults copied into new sessions."""

    DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER = 75
    MIN_QUESTION_REVEAL_MS_PER_CHARACTER = 35
    MAX_QUESTION_REVEAL_MS_PER_CHARACTER = 180
    DEFAULT_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS = 300
    MIN_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS = 120
    MAX_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS = 1000

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    question_reveal_ms_per_character = models.PositiveSmallIntegerField(
        default=DEFAULT_QUESTION_REVEAL_MS_PER_CHARACTER,
    )
    quiz_mc_answer_reveal_stagger_ms = models.PositiveSmallIntegerField(
        default=DEFAULT_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS,
    )
    text_match_threshold = models.PositiveSmallIntegerField(
        default=90, validators=[MinValueValidator(65), MaxValueValidator(100)],
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

    @classmethod
    def quiz_mc_answer_reveal_stagger(cls):
        """Return the delay between Quick Quiz multiple-choice options."""
        try:
            return cls.normalize_quiz_mc_answer_reveal_stagger(
                cls.load().quiz_mc_answer_reveal_stagger_ms
            )
        except (OperationalError, ProgrammingError) as exc:
            message = str(exc)
            if (
                cls._meta.db_table not in message
                and 'quiz_mc_answer_reveal_stagger_ms' not in message
            ):
                raise
            return cls.DEFAULT_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS

    @classmethod
    def normalize_quiz_mc_answer_reveal_stagger(cls, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = cls.DEFAULT_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS
        return max(
            cls.MIN_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS,
            min(cls.MAX_QUIZ_MC_ANSWER_REVEAL_STAGGER_MS, value),
        )
