from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clue_rush', '0007_cluerushgame_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='cluerushgame',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
