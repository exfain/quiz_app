from django.db import migrations, models


def preserve_scores(apps, schema_editor):
    Participant = apps.get_model('sorting_ladder', 'SortingLadderParticipant')
    Submission = apps.get_model('sorting_ladder', 'RoundSubmission')
    Tutorial = apps.get_model('games_hub', 'HubGameUnitTutorialRuntime')
    alias = schema_editor.connection.alias
    for participant in Participant.objects.using(alias).select_related('quiz').iterator():
        excluded = set(Tutorial.objects.using(alias).filter(requested=True,
            game_step__game_key='sorting_ladder', game_step__room_code=participant.quiz.room_code,
            session__code=participant.hub_session_code).values_list('tutorial_question_id', flat=True))
        excluded.add(participant.quiz.tutorial_question_id)
        answers = Submission.objects.using(alias).filter(participant_id=participant.pk)
        answers.filter(question_id__in=[pk for pk in excluded if pk]).update(is_tutorial=True)
        total = answers.filter(is_correct=True, is_tutorial=False).count()
        Participant.objects.using(alias).filter(pk=participant.pk).update(score_adjustment=participant.total_score - total)


class Migration(migrations.Migration):
    dependencies = [('sorting_ladder', '0015_recalculate_one_point_round_scores'), ('games_hub', '0014_hubgameunittutorialruntime')]
    operations = [
        migrations.AddField(model_name='roundsubmission', name='set_number', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='roundsubmission', name='is_tutorial', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='sortingpendingroundselection', name='set_number', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='sortingladderparticipant', name='score_adjustment', field=models.IntegerField(default=0)),
        migrations.AddField(model_name='sortingladdersession', name='current_set_number', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='sortingladdersession', name='question_rounds', field=models.JSONField(default=list, blank=True)),
        migrations.RemoveConstraint(model_name='roundsubmission', name='unique_sorting_round_submission'),
        migrations.AddConstraint(model_name='roundsubmission', constraint=models.UniqueConstraint(fields=('quiz', 'participant', 'question', 'set_number', 'round_number'), name='unique_sorting_round_submission')),
        migrations.RemoveConstraint(model_name='sortingpendingroundselection', name='unique_sorting_pending_round_selection'),
        migrations.AddConstraint(model_name='sortingpendingroundselection', constraint=models.UniqueConstraint(fields=('quiz', 'participant', 'question', 'set_number', 'round_number'), name='unique_sorting_pending_round_selection')),
        migrations.RunPython(preserve_scores, migrations.RunPython.noop),
    ]
