from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('games_hub', '0017_alter_hubgamestep_game_key'),
    ]

    operations = [
        migrations.CreateModel(
            name='HubReadinessCheck',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('synced', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('active', models.BooleanField(default=True)),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='readiness_checks', to='games_hub.hubsession')),
            ],
            options={
                'ordering': ['-started_at'],
            },
        ),
        migrations.CreateModel(
            name='HubReadinessParticipant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('synced', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('ready_at', models.DateTimeField(blank=True, null=True)),
                ('readiness_check', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='participant_states', to='games_hub.hubreadinesscheck')),
                ('participant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='readiness_states', to='games_hub.hubparticipant')),
            ],
            options={
                'ordering': ['ready_at', 'participant__joined_at', 'participant__nickname'],
                'unique_together': {('readiness_check', 'participant')},
            },
        ),
    ]
