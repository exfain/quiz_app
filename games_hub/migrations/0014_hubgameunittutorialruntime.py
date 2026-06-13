from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('games_hub', '0013_hubgametutorialruntime_and_acknowledgement'),
    ]

    operations = [
        migrations.CreateModel(
            name='HubGameUnitTutorialRuntime',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('synced', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('requested', models.BooleanField(default=False)),
                ('tutorial_question_id', models.PositiveIntegerField(blank=True, null=True)),
                ('tutorial_has_been_played', models.BooleanField(default=False)),
                ('current_unit_is_tutorial', models.BooleanField(default=False)),
                ('requested_at', models.DateTimeField(blank=True, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('game_step', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='unit_tutorial_runtime', to='games_hub.hubgamestep')),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='game_unit_tutorial_runtimes', to='games_hub.hubsession')),
            ],
            options={
                'ordering': ['game_step__order'],
            },
        ),
    ]
