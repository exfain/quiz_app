import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_lying', '0012_whoquiz_tutorial_question'),
    ]

    operations = [
        migrations.AddField(
            model_name='whosession',
            name='recently_ended_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='whosession',
            name='recently_ended_question',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='who_is_lying.whoquestion',
            ),
        ),
    ]
