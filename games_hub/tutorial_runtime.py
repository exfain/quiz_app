from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import (
    HubGameParticipantSnapshot,
    HubGameStep,
    HubGameTutorialAcknowledgement,
    HubGameTutorialRuntime,
)


TUTORIAL_ACK_WARNING_MESSAGE = 'Nicht alle Teilnehmer haben die Erläuterung bestätigt'


def get_relevant_game_step(game_key: str, room_code: str, session_code: str | None = None):
    steps = HubGameStep.objects.select_related('session').filter(
        game_key=game_key,
        room_code=room_code,
    )
    if session_code:
        return steps.filter(session__code=session_code).first()

    active = steps.filter(
        session__is_active=True,
        session__ended_at__isnull=True,
    ).order_by('-id').first()
    if active:
        return active
    return steps.filter(session__ended_at__isnull=True).order_by('-id').first() or steps.order_by('-id').first()


def _active_snapshots_for_runtime(runtime):
    return runtime.game_step.participant_snapshots.filter(
        active_player=True,
        included_in_scoring=True,
    ).select_related('participant').order_by('participant__joined_at', 'participant__nickname')


def _serialize_progress(runtime):
    snapshots = list(_active_snapshots_for_runtime(runtime))
    acknowledged_ids = set(
        runtime.acknowledgements.filter(snapshot__in=snapshots).values_list('snapshot_id', flat=True)
    )
    participants = [
        {
            'name': snapshot.participant.nickname,
            'completed': snapshot.id in acknowledged_ids,
        }
        for snapshot in snapshots
    ]
    total = len(participants)
    completed = sum(1 for participant in participants if participant['completed'])
    return {
        'active': runtime.active,
        'completed': completed,
        'total': total,
        'all_done': completed >= total and total > 0,
        'participants': participants,
    }


@transaction.atomic
def activate_tutorial_runtime(game_key: str, room_code: str, session_code: str | None, game, show_tutorial: bool):
    should_show = bool(show_tutorial and getattr(game, 'tutorial_enabled', False) and getattr(game, 'tutorial_text', ''))
    step = get_relevant_game_step(game_key, room_code, session_code)

    game.tutorial_active = should_show
    game.save(update_fields=['tutorial_active'])

    if not step:
        return {
            'game_title': getattr(game, 'title', ''),
            'tutorial_title': getattr(game, 'tutorial_title', '') or 'Spielerläuterung',
            'tutorial_text': getattr(game, 'tutorial_text', ''),
            'official_participants': [],
        } if should_show else None

    runtime, _ = HubGameTutorialRuntime.objects.select_for_update().get_or_create(
        game_step=step,
        defaults={'session': step.session},
    )
    runtime.session = step.session
    runtime.active = should_show
    runtime.title = getattr(game, 'tutorial_title', '') or 'Spielerläuterung'
    runtime.text = getattr(game, 'tutorial_text', '') if should_show else ''
    runtime.started_at = timezone.now() if should_show else None
    runtime.completed_at = None
    runtime.save(update_fields=[
        'session',
        'active',
        'title',
        'text',
        'started_at',
        'completed_at',
    ])
    runtime.acknowledgements.all().delete()

    if not should_show:
        return None

    HubGameParticipantSnapshot.create_for_step(step)
    progress = _serialize_progress(runtime)
    return {
        'game_title': getattr(game, 'title', '') or step.title or step.get_game_key_display(),
        'tutorial_title': runtime.title,
        'tutorial_text': runtime.text,
        'official_participants': [participant['name'] for participant in progress['participants']],
        **progress,
    }


def deactivate_tutorial_runtime(game_key: str, room_code: str, session_code: str | None, game=None):
    if game is not None:
        game.tutorial_active = False
        game.save(update_fields=['tutorial_active'])

    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return
    HubGameTutorialRuntime.objects.filter(game_step=step).update(active=False)


def get_tutorial_start_warning(game_key: str, room_code: str, session_code: str | None):
    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return None

    runtime = HubGameTutorialRuntime.objects.filter(game_step=step, active=True).first()
    if not runtime or not runtime.text:
        return None

    progress = _serialize_progress(runtime)
    if progress['total'] <= progress['completed']:
        return None

    return {
        'message': TUTORIAL_ACK_WARNING_MESSAGE,
        **progress,
    }


@transaction.atomic
def force_close_tutorial_runtime(game_key: str, room_code: str, session_code: str | None, game=None):
    if game is not None:
        game.tutorial_active = False
        game.save(update_fields=['tutorial_active'])

    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return False

    runtime = HubGameTutorialRuntime.objects.select_for_update().filter(game_step=step, active=True).first()
    if not runtime:
        return False

    runtime.active = False
    runtime.completed_at = runtime.completed_at or timezone.now()
    runtime.save(update_fields=['active', 'completed_at'])
    return True


def get_tutorial_payload(game_key: str, room_code: str, session_code: str | None, participant_name: str | None = None):
    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return None

    runtime = HubGameTutorialRuntime.objects.filter(game_step=step, active=True).first()
    if not runtime or not runtime.text:
        return None

    progress = _serialize_progress(runtime)
    official_names = [participant['name'] for participant in progress['participants']]
    if participant_name:
        if participant_name not in official_names:
            return None
        snapshot = runtime.game_step.participant_snapshots.filter(
            active_player=True,
            included_in_scoring=True,
            participant__nickname=participant_name,
        ).first()
        if snapshot and runtime.acknowledgements.filter(snapshot=snapshot).exists():
            return None

    return {
        'game_title': step.title or runtime.title,
        'tutorial_title': runtime.title or 'Spielerläuterung',
        'tutorial_text': runtime.text,
        'official_participants': official_names,
        **progress,
    }


@transaction.atomic
def mark_tutorial_completed(game_key: str, room_code: str, session_code: str | None, participant_name: str):
    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step or not participant_name:
        return {'completed': 0, 'total': 0, 'all_done': False, 'participants': []}

    runtime = HubGameTutorialRuntime.objects.select_for_update().filter(game_step=step, active=True).first()
    if not runtime:
        return {'completed': 0, 'total': 0, 'all_done': False, 'participants': []}

    snapshot = runtime.game_step.participant_snapshots.select_related('participant').filter(
        active_player=True,
        included_in_scoring=True,
        participant__nickname=participant_name,
    ).first()
    if snapshot:
        HubGameTutorialAcknowledgement.objects.get_or_create(
            runtime=runtime,
            snapshot=snapshot,
            defaults={'acknowledged_at': timezone.now()},
        )

    progress = _serialize_progress(runtime)
    if progress['all_done'] and runtime.completed_at is None:
        runtime.completed_at = timezone.now()
        runtime.save(update_fields=['completed_at'])
    return progress


def get_tutorial_progress(game_key: str, room_code: str, session_code: str | None):
    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return {'completed': 0, 'total': 0, 'all_done': False, 'participants': []}
    runtime = HubGameTutorialRuntime.objects.filter(game_step=step).first()
    if not runtime:
        return {'completed': 0, 'total': 0, 'all_done': False, 'participants': []}
    return _serialize_progress(runtime)
