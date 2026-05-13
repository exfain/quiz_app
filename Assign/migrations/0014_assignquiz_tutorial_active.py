from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Assign', '0013_assignquiz_tutorial_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignquiz',
            name='tutorial_active',
            field=models.BooleanField(default=False),
        ),
    ]
