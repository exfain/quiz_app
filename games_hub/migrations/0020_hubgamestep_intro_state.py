from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('games_hub', '0019_authoritative_runtime_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='hubgamestep',
            name='intro_ends_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='hubgamestep',
            name='intro_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='hubgamestep',
            name='intro_state_revision',
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
