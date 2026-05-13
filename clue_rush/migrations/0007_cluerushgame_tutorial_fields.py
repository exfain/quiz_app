from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clue_rush", "0006_remove_participant_question_from_cluequestion"),
    ]

    operations = [
        migrations.AddField(
            model_name="cluerushgame",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="cluerushgame",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="cluerushgame",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
