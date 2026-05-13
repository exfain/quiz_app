from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("QuizGame", "0011_quizquestion_answer_label_1_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="quiz",
            name="tutorial_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="quiz",
            name="tutorial_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="quiz",
            name="tutorial_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
