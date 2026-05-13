from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sorting_ladder', '0008_sortingladdergame_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='sortingladdergame',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
