import django.db.models.deletion
from django.db import migrations, models


def preserve_rounds(apps, schema_editor):
    Game = apps.get_model('wann_war_das', 'WannWarDasGame')
    Answer = apps.get_model('wann_war_das', 'WannWarDasAnswer')
    Question = apps.get_model('wann_war_das', 'WannWarDasQuestion')
    alias = schema_editor.connection.alias
    for game in Game.objects.using(alias).all().iterator():
        answers = list(Answer.objects.using(alias).filter(quiz=game,
            hub_session_code=game.active_hub_session_code).order_by('submitted_at', 'id'))
        ids = list(dict.fromkeys(a.question_id for a in answers))
        if game.current_question_id:
            ids = [pk for pk in ids if pk != game.current_question_id] + [game.current_question_id]
        questions = {q.id: q for q in Question.objects.using(alias).filter(id__in=ids)}
        history = []
        for pk in ids:
            tutorial = (game.current_question_is_tutorial if pk == game.current_question_id
                        else all(a.is_tutorial for a in answers if a.question_id == pk))
            history.append({'question_id': pk, 'is_tutorial': tutorial,
                'revealed': pk != game.current_question_id or game.question_state in {'revealed', 'ended'},
                'max_points': questions[pk].max_points})
        missing = max(0, game.current_question_number - sum(not r['is_tutorial'] for r in history))
        history = [{'question_id': None, 'is_tutorial': False, 'revealed': True, 'max_points': 0} for _ in range(missing)] + history
        Game.objects.using(alias).filter(pk=game.pk).update(question_rounds=history)
        for number, entry in enumerate(history, 1):
            if entry['question_id']:
                Answer.objects.using(alias).filter(quiz=game, question_id=entry['question_id'],
                    hub_session_code=game.active_hub_session_code).update(round_number=number)


class Migration(migrations.Migration):
    dependencies = [('wann_war_das', '0001_initial')]
    operations = [
        migrations.AddField('wannwardasgame', 'paused_at', models.DateTimeField(blank=True, null=True)),
        migrations.AddField('wannwardasgame', 'question_rounds', models.JSONField(blank=True, default=list)),
        migrations.AddField('wannwardasanswer', 'round_number', models.PositiveIntegerField(default=0)),
        migrations.AlterUniqueTogether('wannwardasanswer', {('quiz', 'participant', 'question', 'round_number', 'hub_session_code')}),
        migrations.RunPython(preserve_rounds, migrations.RunPython.noop),
        migrations.CreateModel(name='WannWarDasPendingInput', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('synced', models.BooleanField(default=False)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('hub_session_code', models.CharField(blank=True, default='', max_length=16)),
            ('round_number', models.PositiveIntegerField(default=0)),
            ('raw_answer', models.CharField(blank=True, default='', max_length=120)),
            ('quiz', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pending_inputs', to='wann_war_das.wannwardasgame')),
            ('participant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pending_inputs', to='wann_war_das.wannwardasparticipant')),
            ('question', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='wann_war_das.wannwardasquestion')),
        ], options={'unique_together': {('quiz', 'participant', 'round_number', 'hub_session_code')}}),
    ]
