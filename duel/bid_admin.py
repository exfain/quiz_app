"""Small content editor inside the existing admin dashboard/authorization."""
from django import forms
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from admin_dashboard.views import admin_required
from .models import BidGame, BidTask


class TaskForm(forms.ModelForm):
    class Meta:
        model = BidTask
        fields = ["text", "bid_seconds", "answer_seconds", "is_active"]
        labels = {"text": "Aufgabe", "bid_seconds": "Gebotsdauer (Sekunden)", "answer_seconds": "Antwortdauer (Sekunden)", "is_active": "Aktiv"}


@admin_required
@require_http_methods(["GET", "POST"])
def manage(request):
    games = BidGame.objects.all() if request.user.is_superuser else BidGame.objects.filter(creator=request.user)
    game = get_object_or_404(games, pk=request.GET["game"]) if request.GET.get("game") else None
    form = TaskForm()
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_game":
            title = request.POST.get("title", "").strip()
            if not title or len(title) > 200: error = "Ein Spieltitel mit höchstens 200 Zeichen ist erforderlich."
            else:
                game = BidGame.objects.create(title=title, creator=request.user)
                return redirect(reverse("admin_dashboard:bid_management")+f"?game={game.pk}")
        elif action in {"save_task", "delete_task"} and game:
            task = get_object_or_404(BidTask, pk=request.POST["task_id"], game=game) if request.POST.get("task_id") else None
            if action == "delete_task":
                if task is None: error = "Eine Aufgabe muss ausgewählt werden."
                else: task.delete()
            else:
                form = TaskForm(request.POST, instance=task)
                if form.is_valid():
                    task = form.save(commit=False); task.game = game; task.full_clean(); task.save()
                else: error = "Bitte Aufgabe und positive Zeitdauern prüfen."
            if error is None: return redirect(reverse("admin_dashboard:bid_management")+f"?game={game.pk}")
        else: error = "Ungültige Verwaltungsaktion."
    task_forms = [(task, TaskForm(instance=task)) for task in game.tasks.order_by("pk")] if game else []
    return render(request, "admin_dashboard/bid_management.html", {"bid_games": games.order_by("-created_at"), "selected": game,
        "task_form": form, "task_forms": task_forms, "error": error}, status=400 if error else 200)
