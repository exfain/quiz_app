from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('games_hub', '0022_hubsession_creator')]
    operations = [migrations.AddField('hubgamestep', 'result_snapshot', models.JSONField(blank=True, default=dict))]
