from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import (
    BlackJackQuiz,
    BlackJackQuestion,
    BlackJackParticipant,
    BlackJackAnswer,
    BlackJackSession,
)


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=BlackJackQuiz)
@receiver(pre_save, sender=BlackJackQuestion)
@receiver(pre_save, sender=BlackJackParticipant)
@receiver(pre_save, sender=BlackJackAnswer)
@receiver(pre_save, sender=BlackJackSession)
def mark_blackjack_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
