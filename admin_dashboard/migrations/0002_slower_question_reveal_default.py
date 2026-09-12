from django.db import migrations, models


def update_previous_default(apps, schema_editor):
    dashboard_settings = apps.get_model('admin_dashboard', 'DashboardSettings')
    dashboard_settings.objects.filter(
        question_reveal_ms_per_character=40,
    ).update(question_reveal_ms_per_character=60)


class Migration(migrations.Migration):
    dependencies = [
        ('admin_dashboard', '0001_dashboardsettings'),
    ]

    operations = [
        migrations.RunPython(update_previous_default, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='dashboardsettings',
            name='question_reveal_ms_per_character',
            field=models.PositiveSmallIntegerField(default=60),
        ),
    ]
