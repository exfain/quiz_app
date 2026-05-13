from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import AssignQuiz, AssignQuestion, AssignParticipant, AssignAnswer, AssignSession


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=AssignQuiz)
@receiver(pre_save, sender=AssignQuestion)
@receiver(pre_save, sender=AssignParticipant)
@receiver(pre_save, sender=AssignAnswer)
@receiver(pre_save, sender=AssignSession)
def mark_assign_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
