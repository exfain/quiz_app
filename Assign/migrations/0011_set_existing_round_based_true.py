from django.db import migrations


def set_round_based_true(apps, schema_editor):
    AssignQuiz = apps.get_model('Assign', 'AssignQuiz')
    AssignQuiz.objects.filter(round_based=False).update(round_based=True)


class Migration(migrations.Migration):

    dependencies = [
        ('Assign', '0010_round_based_default_true'),
    ]

    operations = [
        migrations.RunPython(set_round_based_true, migrations.RunPython.noop),
    ]
