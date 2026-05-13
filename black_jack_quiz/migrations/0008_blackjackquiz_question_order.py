from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('black_jack_quiz', '0007_blackjackquiz_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='blackjackquiz',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
