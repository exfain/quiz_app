from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('admin_dashboard', '0003_slower_question_reveal_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='dashboardsettings',
            name='quiz_mc_answer_reveal_stagger_ms',
            field=models.PositiveSmallIntegerField(default=300),
        ),
    ]
