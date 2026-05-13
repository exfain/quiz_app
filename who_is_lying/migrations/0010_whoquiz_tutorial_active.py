from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_lying', '0009_whoquiz_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='whoquiz',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
