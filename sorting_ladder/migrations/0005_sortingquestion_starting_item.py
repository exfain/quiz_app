from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('sorting_ladder', '0004_sortingbundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='sortingquestion',
            name='starting_item',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='starting_in_topics',
                to='sorting_ladder.sortingitem',
            ),
        ),
    ]
