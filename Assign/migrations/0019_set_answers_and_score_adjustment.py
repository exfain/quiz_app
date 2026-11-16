from django.db import migrations, models
from django.db.models import Sum


def preserve_scores(apps, schema_editor):
    Participant = apps.get_model('Assign', 'AssignParticipant')
    Answer = apps.get_model('Assign', 'AssignAnswer')
    Runtime = apps.get_model('Assign', 'AssignSetRuntime')
    Tutorial = apps.get_model('games_hub', 'HubGameUnitTutorialRuntime')
    alias = schema_editor.connection.alias
    for participant in Participant.objects.using(alias).select_related('quiz').iterator():
        excluded = set(Tutorial.objects.using(alias).filter(requested=True,
            game_step__game_key='assign', game_step__room_code=participant.quiz.room_code,
            session__code=participant.hub_session_code).values_list('tutorial_question_id', flat=True))
        excluded.add(participant.quiz.tutorial_question_id)
        answers = Answer.objects.using(alias).filter(participant_id=participant.pk)
        for answer in answers.iterator():
            runtime = Runtime.objects.using(alias).filter(quiz_id=participant.quiz_id, question_id=answer.question_id, hub_session_code=participant.hub_session_code or '').order_by('set_number').first()
            if runtime:
                Answer.objects.using(alias).filter(pk=answer.pk).update(set_number=runtime.set_number)
        answers.filter(question_id__in=[pk for pk in excluded if pk]).update(is_tutorial=True)
        total = answers.filter(is_tutorial=False).aggregate(total=Sum('points_earned'))['total'] or 0
        Participant.objects.using(alias).filter(pk=participant.pk).update(score_adjustment=participant.total_score - total)


class Migration(migrations.Migration):
    dependencies = [('Assign', '0018_assign_persistent_round_runtime'), ('games_hub', '0014_hubgameunittutorialruntime')]
    operations = [
        migrations.AddField(model_name='assignanswer', name='set_number', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='assignanswer', name='is_tutorial', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='assignparticipant', name='score_adjustment', field=models.IntegerField(default=0)),
        migrations.AlterUniqueTogether(name='assignanswer', unique_together={('quiz', 'participant', 'question', 'set_number')}),
        migrations.RunPython(preserve_scores, migrations.RunPython.noop),
    ]
