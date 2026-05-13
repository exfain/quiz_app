from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import (
    WhereQuiz,
    WhereQuestion,
    WhereParticipant,
    WhereAnswer,
    WhereSession,
)


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=WhereQuiz)
@receiver(pre_save, sender=WhereQuestion)
@receiver(pre_save, sender=WhereParticipant)
@receiver(pre_save, sender=WhereAnswer)
@receiver(pre_save, sender=WhereSession)
def mark_where_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
