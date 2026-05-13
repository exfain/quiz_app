from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('QuizGame', '0007_quizbundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='quiz',
            name='internal_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
