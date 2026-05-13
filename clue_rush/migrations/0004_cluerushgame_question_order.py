from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clue_rush', '0003_cluerushgame_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='cluerushgame',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
