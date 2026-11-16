from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('who_is_lying', '0013_persist_recently_ended_question')]
    operations = [
        migrations.AddField(
            model_name='whosession', name='pending_answers',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
