from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clue_rush', '0008_cluerushgame_tutorial_active'),
    ]

    operations = [
        migrations.AddField(
            model_name='clueanswer',
            name='auto_is_correct',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='clueanswer',
            name='is_manually_corrected',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='clueanswer',
            name='submitted_clue_number',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='clueanswer',
            name='total_clues_at_submission',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
