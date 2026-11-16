"""Persisted pause/resume for Quick Quiz, Who Is Lying and Who Is That."""
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .authoritative_state import get_runtime_state
from .models import GameRuntimeState, HubGameStep


def _broadcast_after_commit(game_key, room_code, event_type):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    layer = get_channel_layer()
    if layer:
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(
            f'{game_key}_{room_code}', {'type': event_type},
        ))


def session_code_for(quiz, game_key):
    step = HubGameStep.objects.filter(game_key=game_key, room_code=quiz.room_code,
        session__ended_at__isnull=True).select_related('session').order_by('-id').first()
    return step.session.code if step else None


@transaction.atomic
def pause(quiz, game_key, session_code=None, *, at=None):
    if game_key not in {'quiz', 'who', 'who_that', 'estimation'}:
        raise ValueError('Unsupported game pause.')
    quiz = type(quiz).objects.select_for_update().get(pk=quiz.pk)
    if quiz.status != 'active':
        return False
    runtime = get_runtime_state(game_key, quiz.room_code, session_code or session_code_for(quiz, game_key))
    runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
    snapshot = dict(runtime.public_snapshot or {})
    snapshot['_paused_at'] = (at or timezone.now()).isoformat()
    runtime.public_snapshot = snapshot
    runtime.state_revision += 1
    runtime.context_revision = runtime.state_revision
    runtime.save(update_fields=['public_snapshot', 'state_revision', 'context_revision', 'updated_at'])
    quiz.status = 'inactive'
    quiz.save(update_fields=['status', 'updated_at'])
    _broadcast_after_commit(game_key, quiz.room_code, 'quiz_inactive')
    return True


@transaction.atomic
def resume(quiz, game_key, session_code=None, *, at=None):
    quiz = type(quiz).objects.select_for_update().get(pk=quiz.pk)
    if quiz.status != 'inactive':
        return False
    runtime = get_runtime_state(game_key, quiz.room_code, session_code or session_code_for(quiz, game_key))
    runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
    snapshot = dict(runtime.public_snapshot or {})
    paused_at = parse_datetime(snapshot.pop('_paused_at', '') or '')
    if paused_at:
        delta = max((at or timezone.now()) - paused_at, timezone.timedelta(0))
        for name in ('question_presented_at', 'content_revealed_at', 'starts_at', 'ends_at'):
            value = getattr(runtime, name)
            if value:
                setattr(runtime, name, value + delta)
        session = getattr(quiz, 'session', None)
        if session:
            session = type(session).objects.select_for_update().get(pk=session.pk)
            old_start = quiz.question_start_time.isoformat() if quiz.question_start_time else None
            if quiz.question_start_time:
                quiz.question_start_time += delta
            if session.question_end_time:
                session.question_end_time += delta
            pending = dict(session.pending_answers or {})
            for entry in pending.values():
                if entry.get('question_started_at') == old_start and old_start:
                    entry['question_started_at'] = quiz.question_start_time.isoformat()
                updated_at = parse_datetime(entry.get('updated_at') or '')
                if updated_at:
                    entry['updated_at'] = (updated_at + delta).isoformat()
            session.pending_answers = pending
            session.save(update_fields=['question_end_time', 'pending_answers', 'updated_at'])
    runtime.public_snapshot = snapshot
    runtime.state_revision += 1
    runtime.context_revision = runtime.state_revision
    runtime.save(update_fields=['question_presented_at', 'content_revealed_at', 'starts_at', 'ends_at',
                               'public_snapshot', 'state_revision', 'context_revision', 'updated_at'])
    quiz.status = 'active'
    quiz.ended_at = None
    quiz.save(update_fields=['status', 'ended_at', 'question_start_time', 'updated_at'])
    _broadcast_after_commit(game_key, quiz.room_code, 'quiz_resumed')
    return True
