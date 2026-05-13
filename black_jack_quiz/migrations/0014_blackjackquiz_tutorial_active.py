from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('black_jack_quiz', '0013_blackjackquiz_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='blackjackquiz',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
