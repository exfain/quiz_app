from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_that', '0005_whothatbundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='whothatquiz',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
