from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("black_jack_quiz", "0012_blackjacksession_set_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="blackjackquiz",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="blackjackquiz",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="blackjackquiz",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
