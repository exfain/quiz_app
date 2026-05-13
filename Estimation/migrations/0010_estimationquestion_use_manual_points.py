from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Estimation', '0009_estimationquestion_zone_count_and_scoring_modes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='estimationquestion',
            name='max_points',
            field=models.PositiveIntegerField(
                default=100,
                help_text='Manual maximum points value for zone scoring',
            ),
        ),
        migrations.AddField(
            model_name='estimationquestion',
            name='use_manual_points',
            field=models.BooleanField(
                default=False,
                help_text='Use max_points as the inner-zone maximum instead of zone_count',
            ),
        ),
    ]
