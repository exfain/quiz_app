from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import (
    EstimationQuiz,
    EstimationQuestion,
    EstimationParticipant,
    EstimationAnswer,
    EstimationSession,
)


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=EstimationQuiz)
@receiver(pre_save, sender=EstimationQuestion)
@receiver(pre_save, sender=EstimationParticipant)
@receiver(pre_save, sender=EstimationAnswer)
@receiver(pre_save, sender=EstimationSession)
def mark_estimation_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
