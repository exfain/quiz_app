from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_lying', '0007_whoquiz_internal_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='whoquiz',
            name='question_order',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
