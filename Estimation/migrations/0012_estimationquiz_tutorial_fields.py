from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Estimation", "0011_remove_estimationquestion_difficulty"),
    ]

    operations = [
        migrations.AddField(
            model_name="estimationquiz",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="estimationquiz",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="estimationquiz",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
