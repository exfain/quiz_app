from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sorting_ladder', '0006_sortingladdergame_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='sortingladdergame',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
