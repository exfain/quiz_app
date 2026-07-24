from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sorting_ladder', '0011_sortingladdergame_tutorial_question'),
    ]

    operations = [
        migrations.AddField(
            model_name='sortingladdersession',
            name='reveal_state',
            field=models.CharField(
                choices=[
                    ('active', 'Rounds active'),
                    ('awaiting_reveal', 'Waiting for reveal'),
                    ('revealed', 'Solution revealed'),
                ],
                default='active',
                max_length=24,
            ),
        ),
    ]
