from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('black_jack_quiz', '0006_remove_difficulty_and_hint'),
    ]

    operations = [
        migrations.AddField(
            model_name='blackjackquiz',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
