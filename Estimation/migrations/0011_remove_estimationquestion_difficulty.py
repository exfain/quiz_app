from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('Estimation', '0010_estimationquestion_use_manual_points'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='estimationquestion',
            name='difficulty',
        ),
    ]
