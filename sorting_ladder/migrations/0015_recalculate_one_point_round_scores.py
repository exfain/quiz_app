from django.db import migrations, models


def recalculate_sorting_ladder_scores(apps, schema_editor):
    Participant = apps.get_model('sorting_ladder', 'SortingLadderParticipant')
    Participant.objects.update(total_score=models.F('rounds_survived'))


class Migration(migrations.Migration):

    dependencies = [
        ('sorting_ladder', '0014_roundsubmission_round_number'),
    ]

    operations = [
        migrations.RunPython(
            recalculate_sorting_ladder_scores,
            migrations.RunPython.noop,
        ),
    ]
