from django.db import migrations, models


def assign_round_numbers(apps, schema_editor):
    RoundSubmission = apps.get_model('sorting_ladder', 'RoundSubmission')
    groups = (
        RoundSubmission.objects
        .values_list('quiz_id', 'participant_id', 'question_id')
        .distinct()
    )
    for quiz_id, participant_id, question_id in groups.iterator():
        submissions = list(
            RoundSubmission.objects.filter(
                quiz_id=quiz_id,
                participant_id=participant_id,
                question_id=question_id,
            ).order_by('submitted_at', 'id')
        )
        used_rounds = set()
        next_fallback = 1
        for submission in submissions:
            elements = submission.all_elements if isinstance(submission.all_elements, list) else []
            candidate = max(len(elements) - 1, 1) if elements else next_fallback
            if candidate in used_rounds:
                submission.delete()
                continue
            submission.round_number = candidate
            submission.save(update_fields=['round_number'])
            used_rounds.add(candidate)
            while next_fallback in used_rounds:
                next_fallback += 1


class Migration(migrations.Migration):

    dependencies = [
        ('sorting_ladder', '0013_persist_pending_round_selection'),
    ]

    operations = [
        migrations.AddField(
            model_name='roundsubmission',
            name='round_number',
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.RunPython(assign_round_numbers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='roundsubmission',
            name='round_number',
            field=models.PositiveIntegerField(),
        ),
        migrations.AddConstraint(
            model_name='roundsubmission',
            constraint=models.UniqueConstraint(
                fields=('quiz', 'participant', 'question', 'round_number'),
                name='unique_sorting_round_submission',
            ),
        ),
    ]
