from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Estimation', '0007_estimationquiz_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='estimationquiz',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
