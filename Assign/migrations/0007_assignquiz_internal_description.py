from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('Assign', '0006_assignbundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignquiz',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
