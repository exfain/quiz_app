from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import Quiz, QuizQuestion, QuizParticipant, QuizAnswer, QuizSession


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=Quiz)
@receiver(pre_save, sender=QuizQuestion)
@receiver(pre_save, sender=QuizParticipant)
@receiver(pre_save, sender=QuizAnswer)
@receiver(pre_save, sender=QuizSession)
def mark_quizgame_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    # Any create/update in QuizGame marks the row as needing re-sync.
    _mark_unsynced(instance)
