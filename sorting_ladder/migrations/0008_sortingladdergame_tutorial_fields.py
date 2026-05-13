from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sorting_ladder", "0007_sortingladdergame_question_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="sortingladdergame",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="sortingladdergame",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="sortingladdergame",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
