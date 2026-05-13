from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import (
    WhoThatQuiz,
    WhoThatQuestion,
    WhoThatParticipant,
    WhoThatAnswer,
    WhoThatSession,
)


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=WhoThatQuiz)
@receiver(pre_save, sender=WhoThatQuestion)
@receiver(pre_save, sender=WhoThatParticipant)
@receiver(pre_save, sender=WhoThatAnswer)
@receiver(pre_save, sender=WhoThatSession)
def mark_who_is_that_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
