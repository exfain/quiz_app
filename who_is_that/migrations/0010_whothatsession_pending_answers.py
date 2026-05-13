from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("who_is_that", "0009_alter_whothatquestion_points"),
    ]

    operations = [
        migrations.AddField(
            model_name="whothatsession",
            name="pending_answers",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
