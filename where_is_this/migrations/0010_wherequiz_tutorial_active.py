from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('where_is_this', '0009_wherequiz_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='wherequiz',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
