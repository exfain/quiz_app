from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Assign', '0016_assignquiz_tutorial_question'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignparticipant',
            name='eliminated_set_number',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='assignparticipant',
            name='elimination_reason',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
    ]
