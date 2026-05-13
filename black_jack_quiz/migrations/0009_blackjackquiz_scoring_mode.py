from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("black_jack_quiz", "0008_blackjackquiz_question_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="blackjackquiz",
            name="scoring_mode",
            field=models.CharField(
                choices=[("simple", "Einfach"), ("rank", "Ranking")],
                default="simple",
                max_length=10,
            ),
        ),
    ]
