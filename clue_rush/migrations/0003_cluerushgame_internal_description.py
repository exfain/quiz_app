from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clue_rush', '0002_clue_synced_clue_updated_at_clueanswer_synced_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='cluerushgame',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
