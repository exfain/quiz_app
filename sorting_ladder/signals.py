from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import SortingLadderGame, SortingLadderParticipant, SortingLadderSession, SortingQuestion, SortingItem


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=SortingLadderGame)
@receiver(pre_save, sender=SortingLadderParticipant)
@receiver(pre_save, sender=SortingLadderSession)
@receiver(pre_save, sender=SortingQuestion)
@receiver(pre_save, sender=SortingItem)
def mark_quizgame_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    # Any create/update in QuizGame marks the row as needing re-sync.
    _mark_unsynced(instance)
