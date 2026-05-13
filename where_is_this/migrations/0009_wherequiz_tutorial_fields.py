from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("where_is_this", "0008_remove_wherequestion_difficulty"),
    ]

    operations = [
        migrations.AddField(
            model_name="wherequiz",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="wherequiz",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="wherequiz",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
