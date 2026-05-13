from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Assign", "0012_remove_round_based"),
    ]

    operations = [
        migrations.AddField(
            model_name="assignquiz",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="assignquiz",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="assignquiz",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
