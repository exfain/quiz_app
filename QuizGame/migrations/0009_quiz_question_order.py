from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('QuizGame', '0008_quiz_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='quiz',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
