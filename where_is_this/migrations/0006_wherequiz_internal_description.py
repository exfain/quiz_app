from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('where_is_this', '0005_wherebundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='wherequiz',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
