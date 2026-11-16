"""Persisted pause/resume for Wer weiss mehr."""
from functools import wraps
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from games_hub.authoritative_state import get_runtime_state, _retry_sqlite_locked
from games_hub.models import GameRuntimeState
from games_hub.quiz_pause import session_code_for


def retry_database_write(operation):
    """Retry only a rolled-back SQLite transaction, leaving other errors intact."""
    @wraps(operation)
    def run(*args, **kwargs):
        return _retry_sqlite_locked(lambda: operation(*args, **kwargs))
    return run


@retry_database_write
@transaction.atomic
def set_paused(quiz, paused, *, at=None):
    quiz = type(quiz).objects.select_for_update().get(pk=quiz.pk)
    if quiz.status != ('active' if paused else 'inactive'):
        return False
    session = getattr(quiz, 'session', None)
    code = session.hub_session_code if session and session.hub_session_code else session_code_for(quiz, 'wer_weiss_mehr')
    runtime = get_runtime_state('wer_weiss_mehr', quiz.room_code, code)
    runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
    snapshot = dict(runtime.public_snapshot or {})
    now = at or timezone.now()
    if paused:
        snapshot['_paused_at'] = now.isoformat()
    else:
        paused_at = parse_datetime(snapshot.pop('_paused_at', '') or '')
        delta = max(now - (paused_at or now), timezone.timedelta(0))
        for name in ('question_presented_at', 'content_revealed_at', 'starts_at', 'ends_at'):
            value = getattr(runtime, name)
            if value:
                setattr(runtime, name, value + delta)
        if session:
            session = type(session).objects.select_for_update().get(pk=session.pk)
            for name in ('round_start_time', 'round_end_time'):
                value = getattr(session, name)
                if value:
                    setattr(session, name, value + delta)
            session.save(update_fields=['round_start_time', 'round_end_time'])
            if session.round_start_time:
                quiz.rounds.filter(question_id=quiz.current_question_id, set_number=session.current_set_number,
                    round_number=session.current_round).update(started_at=session.round_start_time)
    runtime.public_snapshot = snapshot
    runtime.state_revision += 1
    runtime.context_revision = runtime.state_revision
    runtime.save()
    quiz.status = 'inactive' if paused else 'active'
    quiz.save(update_fields=['status'])
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    layer = get_channel_layer()
    if layer:
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(
            f'werweissmehr_{quiz.room_code}', {'type': 'state_updated', 'payload': {'hub_session_code': code}}))
    return True
