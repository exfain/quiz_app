from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Estimation", "0013_estimationquiz_tutorial_active"),
    ]

    operations = [
        migrations.AddField(
            model_name="estimationsession",
            name="pending_answers",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
