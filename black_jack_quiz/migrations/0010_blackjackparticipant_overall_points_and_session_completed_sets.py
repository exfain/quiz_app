from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('black_jack_quiz', '0009_blackjackquiz_scoring_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='blackjackparticipant',
            name='overall_points',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='blackjacksession',
            name='completed_sets_count',
            field=models.IntegerField(default=0),
        ),
    ]
