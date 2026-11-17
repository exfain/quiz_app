"""Owner-scoped content editor in the existing dashboard."""
from django import forms
from django.shortcuts import get_object_or_404,redirect,render
from django.urls import reverse
from django.db.models.deletion import ProtectedError
from django.views.decorators.http import require_http_methods
from admin_dashboard.views import admin_required
from .models import TargetGame,Task,validate_solutions


class GameForm(forms.ModelForm):
    class Meta:
        model=TargetGame
        fields=['title','scoring_mode','answer_seconds']
        labels={'title':'Spieltitel','scoring_mode':'Wertung','answer_seconds':'Gemeinsame Antwortzeit (Sekunden)'}


class TaskForm(forms.ModelForm):
    answer_lines=forms.CharField(label='Lösungen: eine pro Zeile, alternative Schreibweisen mit | trennen',widget=forms.Textarea)
    class Meta:
        model=Task
        fields=['question_text','is_active']
        labels={'question_text':'Aufgabe','is_active':'Aktiv'}

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        if self.instance.pk:self.fields['answer_lines'].initial='\n'.join(' | '.join([o['text'],*o['aliases']]) for o in self.instance.solutions)

    def clean_answer_lines(self):
        options=[]
        for line in self.cleaned_data['answer_lines'].splitlines():
            if not line.strip():continue
            parts=[p.strip() for p in line.split('|')]
            options.append({'text':parts[0],'aliases':parts[1:]})
        validate_solutions(options)
        return options


@admin_required
@require_http_methods(['GET','POST'])
def manage(request):
    games=TargetGame.objects.all() if request.user.is_superuser else TargetGame.objects.filter(creator=request.user)
    game=get_object_or_404(games,pk=request.GET['game']) if request.GET.get('game') else None
    game_form=GameForm(instance=game);task_form=TaskForm();error=None
    if request.method=='POST':
        action=request.POST.get('action')
        if game and game.status!='waiting':error='Ein gestartetes Spiel kann hier nicht mehr verändert werden.'
        elif action in {'create_game','save_game'}:
            game_form=GameForm(request.POST,instance=game if action=='save_game' else None)
            if game_form.is_valid():
                game=game_form.save(commit=False)
                if not game.pk:game.creator=request.user
                game.save()
                return redirect(reverse('target_game:manage')+f'?game={game.pk}')
            error='Bitte Spieltitel, Wertung und eine positive Antwortdauer prüfen.'
        elif game and action in {'save_task','delete_task'}:
            task=get_object_or_404(Task,game=game,pk=request.POST['task_id']) if request.POST.get('task_id') else None
            if action=='delete_task':
                if task:
                    try:task.delete()
                    except ProtectedError:error='Diese Aufgabe wurde bereits gespielt. Deaktiviere sie für zukünftige Runden.'
            else:
                task_form=TaskForm(request.POST,instance=task)
                if task_form.is_valid():
                    task=task_form.save(commit=False);task.game=game;task.solutions=task_form.cleaned_data['answer_lines'];task.full_clean();task.save()
                else:error='Bitte Aufgabe und Lösungen prüfen.'
            if not error:return redirect(reverse('target_game:manage')+f'?game={game.pk}')
        else:error='Ungültige Verwaltungsaktion.'
    return render(request,'target_game/manage.html',{'games':games.order_by('-created_at'),'selected':game,'game_form':game_form,
        'task_form':task_form,'task_forms':[(t,TaskForm(instance=t)) for t in game.tasks.order_by('pk')] if game else [],'error':error},status=400 if error else 200)
