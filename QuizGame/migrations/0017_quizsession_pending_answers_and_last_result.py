from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("QuizGame", "0016_quiz_tutorial_question"),
    ]

    operations = [
        migrations.AddField(
            model_name="quizsession",
            name="pending_answers",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="quizsession",
            name="last_question_result",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
