from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('black_jack_quiz', '0011_blackjackquiz_question_sets'),
    ]

    operations = [
        migrations.AddField(
            model_name='blackjacksession',
            name='asked_question_ids',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='blackjacksession',
            name='finalized_set_numbers',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='blackjacksession',
            name='selected_set_number',
            field=models.IntegerField(default=1),
        ),
    ]
