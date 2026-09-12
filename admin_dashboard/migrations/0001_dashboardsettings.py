from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='DashboardSettings',
            fields=[
                (
                    'singleton_id',
                    models.PositiveSmallIntegerField(
                        default=1,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    'question_reveal_ms_per_character',
                    models.PositiveSmallIntegerField(default=40),
                ),
            ],
        ),
    ]
