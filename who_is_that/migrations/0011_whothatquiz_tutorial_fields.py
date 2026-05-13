from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("who_is_that", "0010_whothatsession_pending_answers"),
    ]

    operations = [
        migrations.AddField(
            model_name="whothatquiz",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="whothatquiz",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="whothatquiz",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
