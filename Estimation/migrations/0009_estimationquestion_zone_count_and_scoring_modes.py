from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Estimation', '0008_estimationquiz_question_order'),
    ]

    operations = [
        migrations.AddField(
            model_name='estimationquestion',
            name='zone_count',
            field=models.PositiveIntegerField(
                default=5,
                help_text='Number of scoring zones; also the maximum score in zone mode',
                validators=[MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name='estimationquiz',
            name='scoring_mode',
            field=models.CharField(
                choices=[
                    ('zones', 'Zone Mode'),
                    ('rank', 'Ranking Mode'),
                    ('tolerance', 'Zone Mode (legacy)'),
                ],
                default='tolerance',
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name='estimationquestion',
            name='max_points',
            field=models.PositiveIntegerField(
                default=100,
                help_text='Legacy maximum points value',
            ),
        ),
        migrations.AlterField(
            model_name='estimationquestion',
            name='tolerance_percentage',
            field=models.FloatField(
                default=10.0,
                help_text='Zone width as percentage deviation',
            ),
        ),
    ]
