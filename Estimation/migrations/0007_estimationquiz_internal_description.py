from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Estimation', '0006_estimationbundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='estimationquiz',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
