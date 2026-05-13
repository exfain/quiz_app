from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Estimation', '0012_estimationquiz_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='estimationquiz',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
