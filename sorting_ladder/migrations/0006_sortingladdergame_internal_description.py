from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sorting_ladder', '0005_sortingquestion_starting_item'),
    ]

    operations = [
        migrations.AddField(
            model_name='sortingladdergame',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
