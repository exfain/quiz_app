from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("who_is_that", "0008_remove_whothatquestion_difficulty"),
    ]

    operations = [
        migrations.AlterField(
            model_name="whothatquestion",
            name="points",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Points awarded for correct answer",
            ),
        ),
    ]
