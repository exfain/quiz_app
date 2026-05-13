from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import (
    ClueRushGame,
    ClueRushParticipant,
    ClueQuestion,
    Clue,
    ClueAnswer,
    ClueRushSession,
)


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=ClueRushGame)
@receiver(pre_save, sender=ClueRushParticipant)
@receiver(pre_save, sender=ClueQuestion)
@receiver(pre_save, sender=Clue)
@receiver(pre_save, sender=ClueAnswer)
@receiver(pre_save, sender=ClueRushSession)
def mark_clue_rush_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
