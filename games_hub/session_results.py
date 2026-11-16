"""Freeze completed game results before a reusable room changes its run."""
from django.db import transaction
from django.utils import timezone

from .models import HubGameStep


def step_status(step, game):
    if step.game_format == 'tournament':
        run = getattr(step, 'tournament', None)
        return 'completed' if run and run.status == 'completed' else 'active' if run else 'waiting'
    if step.game_format == 'duel':
        from duel.encounters import presentation_finished
        instance = step.duel_instances.order_by('-created_at').first()
        if instance is None: return 'waiting'
        return 'completed' if instance.match_status == 'completed' and presentation_finished(instance) else 'active'
    if (step.result_snapshot or {}).get('status') == 'completed':
        return 'completed'
    if not game:
        return ''
    # Older ended sessions have no archived JSON yet. Their own participant
    # pool still proves that this step ran, regardless of a later room reuse.
    if step.session.ended_at and (step.participant_snapshots.exists() or
            game.participants.filter(hub_session_code=step.session.code).exists()):
        return 'completed'
    code = getattr(game, 'active_hub_session_code', '')
    if code and code != step.session.code:
        return 'waiting'
    return game.status


@transaction.atomic
def freeze_step_result(step, game):
    step = HubGameStep.objects.select_for_update().get(pk=step.pk)
    if step.game_format in {'duel', 'tournament'}:
        return
    if step.result_snapshot or not game:
        return
    from .views import get_game_participant_data
    step.result_snapshot = {
        'status': 'completed', 'title': game.title,
        'scores': get_game_participant_data(step.session, type(game), game.participants.model,
            game.room_code, step.game_key),
        'completed_at': timezone.now().isoformat(),
    }
    step.save(update_fields=['result_snapshot', 'updated_at'])


def freeze_previous_results(game_key, game, next_session_code):
    if not game:
        return
    for step in HubGameStep.objects.select_related('session').filter(
            game_key=game_key, room_code=game.room_code).exclude(session__code=next_session_code):
        owns_run = getattr(game, 'active_hub_session_code', None) == step.session.code
        ended = step.session.ended_at is not None
        if (owns_run and game.status == 'completed' or ended) and (
                step.participant_snapshots.exists() or game.participants.filter(hub_session_code=step.session.code).exists()):
            freeze_step_result(step, game)
