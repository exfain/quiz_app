from django.db import migrations, models
from django.db.models import Sum


def preserve_scores(apps, schema_editor):
    Participant = apps.get_model('who_is_that', 'WhoThatParticipant')
    Answer = apps.get_model('who_is_that', 'WhoThatAnswer')
    alias = schema_editor.connection.alias
    totals = dict(Answer.objects.using(alias).values('participant_id').annotate(
        points=Sum('points_earned')).values_list('participant_id', 'points'))
    for participant in Participant.objects.using(alias).iterator():
        Participant.objects.using(alias).filter(pk=participant.pk).update(
            score_adjustment=participant.total_score - (totals.get(participant.pk) or 0))


class Migration(migrations.Migration):
    dependencies = [('who_is_that', '0014_whothatquiz_tutorial_question')]
    operations = [
        migrations.AddField(model_name='whothatanswer', name='round_number',
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='whothatanswer', name='is_tutorial',
                            field=models.BooleanField(default=False)),
        migrations.AddField(model_name='whothatsession', name='question_rounds',
                            field=models.JSONField(default=list, blank=True)),
        migrations.AddField(model_name='whothatparticipant', name='score_adjustment',
                            field=models.IntegerField(default=0)),
        migrations.AlterUniqueTogether(name='whothatanswer',
            unique_together={('quiz', 'participant', 'question', 'round_number')}),
        migrations.RunPython(preserve_scores, migrations.RunPython.noop),
    ]
