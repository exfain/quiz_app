from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('where_is_this', '0006_wherequiz_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='wherequiz',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
