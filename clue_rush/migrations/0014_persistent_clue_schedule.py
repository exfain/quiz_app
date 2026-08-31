from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clue_rush', '0013_cluerushgame_tutorial_question'),
    ]

    operations = [
        migrations.AddField(
            model_name='cluerushsession',
            name='answer_deadline',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='cluerushsession',
            name='clue_duration_override',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='cluerushsession',
            name='clue_schedule',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='cluerushsession',
            name='finalized_question_id',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='cluerushsession',
            name='question_finalized_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
