"""Pause and termination for Assign and Sorting Ladder only."""
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .authoritative_state import get_runtime_state, reset_question_flow
from .models import GameRuntimeState
from .quiz_pause import session_code_for


def broadcast(quiz, game_key, event_type, **payload):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    group = {'assign': 'assign', 'sorting_ladder': 'sortingladder'}[game_key]
    layer = get_channel_layer()
    if layer:
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(f'{group}_{quiz.room_code}', {'type': event_type, **payload}))


@transaction.atomic
def set_paused(quiz, game_key, paused, *, at=None):
    if game_key not in {'assign', 'sorting_ladder'}:
        raise ValueError('Unsupported game.')
    quiz = type(quiz).objects.select_for_update().get(pk=quiz.pk)
    if quiz.status != ('active' if paused else 'inactive'):
        return False
    now = at or timezone.now()
    runtime = get_runtime_state(game_key, quiz.room_code, session_code_for(quiz, game_key))
    runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
    snapshot = dict(runtime.public_snapshot or {})
    if paused:
        snapshot['_paused_at'] = now.isoformat()
    else:
        paused_at = parse_datetime(snapshot.pop('_paused_at', '') or '')
        delta = max(now - (paused_at or now), timezone.timedelta(0))
        for name in ('question_presented_at', 'content_revealed_at', 'starts_at', 'ends_at'):
            if getattr(runtime, name):
                setattr(runtime, name, getattr(runtime, name) + delta)
        session = getattr(quiz, 'session', None)
        if session:
            session = type(session).objects.select_for_update().get(pk=session.pk)
            for name in ('question_end_time', 'round_start_time', 'round_end_time'):
                value = getattr(session, name, None)
                if value:
                    setattr(session, name, value + delta)
            session.save()
        if game_key == 'assign':
            from Assign.runtime import current_set_runtime
            current = current_set_runtime(quiz.room_code, session_code_for(quiz, game_key), lock=True)
            if current:
                current.round_started_at += delta
                current.round_ends_at += delta
                current.save(update_fields=['round_started_at', 'round_ends_at', 'updated_at'])
            if quiz.question_start_time:
                quiz.question_start_time += delta
    runtime.public_snapshot = snapshot
    runtime.state_revision += 1
    runtime.context_revision = runtime.state_revision
    runtime.save()
    quiz.status = 'inactive' if paused else 'active'
    quiz.save()
    broadcast(quiz, game_key, 'quiz_inactive' if paused else 'quiz_resumed')
    return True


@transaction.atomic
def end_game(quiz, game_key):
    quiz = type(quiz).objects.select_for_update().get(pk=quiz.pk)
    session = getattr(quiz, 'session', None)
    if game_key == 'assign':
        from Assign.runtime import current_set_runtime, persist_final_answers
        current = current_set_runtime(quiz.room_code, session_code_for(quiz, game_key), lock=True)
        if current:
            persist_final_answers(current)
            current.participant_round_states.filter(evaluated_at__isnull=True).update(user_match={}, is_locked=False, locked_at=None)
            current.phase = current.PHASE_ENDED
            current.ended_at = timezone.now()
            current.save(update_fields=['phase', 'ended_at', 'updated_at'])
        if session:
            session.is_question_active = False
            session.question_end_time = None
            session.save()
        quiz.question_start_time = None
    elif game_key == 'sorting_ladder':
        quiz.pending_round_selections.all().delete()
        if session:
            session.is_round_active = False
            session.round_start_time = session.round_end_time = None
            session.active_element = None
            session.shuffled_item_ids = ''
            session.placed_elements.clear()
            session.save()
    else:
        raise ValueError('Unsupported game.')
    quiz.current_question = None
    quiz.status = 'completed'
    quiz.ended_at = timezone.now()
    quiz.save()
    reset_question_flow(game_key=game_key, room_code=quiz.room_code, session_code=session_code_for(quiz, game_key), mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE)
    return quiz
