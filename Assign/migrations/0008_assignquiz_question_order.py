from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Assign', '0007_assignquiz_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignquiz',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
