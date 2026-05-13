from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('QuizGame', '0005_quiz_synced_quiz_updated_at_quizanswer_synced_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='quizparticipant',
            name='tutorial_completed',
            field=models.BooleanField(default=False),
        ),
    ]
