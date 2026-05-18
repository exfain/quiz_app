from django.db import migrations


def backfill_clueanswer_submission_state(apps, schema_editor):
    ClueAnswer = apps.get_model('clue_rush', 'ClueAnswer')

    updates = []
    for answer in ClueAnswer.objects.select_related('question').all().iterator():
        changed = False

        inferred_auto_is_correct = bool(answer.is_correct)
        if answer.auto_is_correct != inferred_auto_is_correct:
            answer.auto_is_correct = inferred_auto_is_correct
            changed = True

        if not answer.is_manually_corrected:
            # Historical answers predate manual correction support.
            # Keep them as automatic results unless explicitly changed later.
            pass

        total_clues = answer.total_clues_at_submission or answer.question.clues.count()
        if answer.total_clues_at_submission != total_clues:
            answer.total_clues_at_submission = total_clues
            changed = True

        if answer.submitted_clue_number <= 0 and total_clues > 0 and answer.is_correct and answer.points_earned > 0:
            inferred_clue_number = total_clues - int(answer.points_earned) + 1
            inferred_clue_number = max(1, min(total_clues, inferred_clue_number))
            if answer.submitted_clue_number != inferred_clue_number:
                answer.submitted_clue_number = inferred_clue_number
                changed = True

        if changed:
            updates.append(answer)

    if updates:
        ClueAnswer.objects.bulk_update(
            updates,
            ['auto_is_correct', 'total_clues_at_submission', 'submitted_clue_number'],
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clue_rush', '0009_clueanswer_submission_state_and_manual_override'),
    ]

    operations = [
        migrations.RunPython(
            backfill_clueanswer_submission_state,
            noop_reverse,
        ),
    ]
