from django.db import models

class SyncBase(models.Model):
    synced = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
