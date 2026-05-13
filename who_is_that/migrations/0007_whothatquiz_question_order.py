from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_that', '0006_whothatquiz_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='whothatquiz',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
