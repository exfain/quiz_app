from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("who_is_lying", "0008_whoquiz_question_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="whoquiz",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="whoquiz",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="whoquiz",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
