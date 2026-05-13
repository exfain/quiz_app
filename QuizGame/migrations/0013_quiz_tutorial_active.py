from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('QuizGame', '0012_quiz_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='quiz',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
