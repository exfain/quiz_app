from django.db.models.signals import pre_save
from django.dispatch import receiver

from .models import HubSession, HubParticipant, HubGameStep


def _mark_unsynced(instance):
    if hasattr(instance, "synced"):
        instance.synced = False


@receiver(pre_save, sender=HubSession)
@receiver(pre_save, sender=HubParticipant)
@receiver(pre_save, sender=HubGameStep)
def mark_games_hub_models_unsynced(sender, instance, **kwargs):  # noqa: D401
    _mark_unsynced(instance)
