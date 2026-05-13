from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('games_hub', '0004_hubparticipant_score_adjustment_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='GameVote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('participant_nickname', models.CharField(max_length=50)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='votes', to='games_hub.hubsession')),
                ('step', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='votes', to='games_hub.hubgamestep')),
            ],
            options={
                'unique_together': {('session', 'participant_nickname')},
            },
        ),
    ]
