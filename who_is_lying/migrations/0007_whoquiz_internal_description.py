from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_lying', '0006_whobundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='whoquiz',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
