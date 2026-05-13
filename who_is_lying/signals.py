from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import (
    WhoQuiz,
    WhoQuestion,
    WhoParticipant,
    WhoAnswer,
    WhoSession,
)


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=WhoQuiz)
@receiver(pre_save, sender=WhoQuestion)
@receiver(pre_save, sender=WhoParticipant)
@receiver(pre_save, sender=WhoAnswer)
@receiver(pre_save, sender=WhoSession)
def mark_who_is_lying_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
