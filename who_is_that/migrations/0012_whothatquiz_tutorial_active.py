from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_that', '0011_whothatquiz_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='whothatquiz',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
