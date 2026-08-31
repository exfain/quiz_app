from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    GameRuntimeState,
    HubGameStep,
    HubParticipant,
    HubSession,
    HubSocketConnection,
    ProcessedClientAction,
)


QUESTION_PRESENTATION_DELAY_MS = 1000


@dataclass(frozen=True)
class QuestionFlowCapabilities:
    initial_phase: str = GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
    uses_prompt_phase: bool = True
    uses_content_phase: bool = True
    auto_open_answering_after_presentation: bool = False


DEFAULT_QUESTION_FLOW_CAPABILITIES = QuestionFlowCapabilities()
QUESTION_FLOW_CAPABILITIES = {
    'assign': QuestionFlowCapabilities(),
    'sorting_ladder': QuestionFlowCapabilities(),
    'clue_rush': QuestionFlowCapabilities(
        uses_content_phase=False,
    ),
    'blackjack': QuestionFlowCapabilities(
        uses_content_phase=False,
    ),
    'who': QuestionFlowCapabilities(
        uses_content_phase=False,
    ),
    'who_that': QuestionFlowCapabilities(
        initial_phase=GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN,
        uses_prompt_phase=False,
        uses_content_phase=False,
        auto_open_answering_after_presentation=True,
    ),
    'estimation': QuestionFlowCapabilities(
        uses_content_phase=False,
    ),
    'wer_weiss_mehr': QuestionFlowCapabilities(
        uses_content_phase=False,
    ),
    'wann_war_das': QuestionFlowCapabilities(
        uses_content_phase=False,
    ),
}


def get_question_flow_capabilities(game_key: str) -> QuestionFlowCapabilities:
    return QUESTION_FLOW_CAPABILITIES.get(
        _text(game_key),
        DEFAULT_QUESTION_FLOW_CAPABILITIES,
    )


def _question_visible_at(runtime: GameRuntimeState):
    if (
        runtime.question_flow_mode
        != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        or not runtime.question_presented_at
    ):
        return None
    return runtime.question_presented_at + timedelta(
        milliseconds=QUESTION_PRESENTATION_DELAY_MS
    )


def question_content_is_visible(snapshot: dict, *, at=None) -> bool:
    if snapshot.get('question_flow_mode') != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE:
        return bool(snapshot.get('current_question_id'))
    if snapshot.get('question_phase') not in {
        GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE,
        GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN,
    }:
        return False
    visible_at = _aware_datetime(snapshot.get('question_visible_at'))
    return not visible_at or (_aware_datetime(at) or timezone.now()) >= visible_at


def _presence_ttl_seconds() -> int:
    return int(getattr(settings, 'SOCKET_PRESENCE_TTL_SECONDS', 90))


def _retry_sqlite_locked(operation):
    """Retry a complete presence write after SQLite lock-upgrade conflicts."""
    delays = (0.02, 0.05, 0.1, 0.2)
    for attempt in range(len(delays) + 1):
        try:
            return operation()
        except OperationalError as exc:
            is_sqlite_lock = (
                connection.vendor == 'sqlite'
                and 'locked' in str(exc).lower()
            )
            if not is_sqlite_lock or attempt >= len(delays):
                raise
            time.sleep(delays[attempt])


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _contains_path(payload: dict, path: tuple[str, ...]) -> bool:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return True


def _first(payload: dict, *paths: tuple[str, ...]):
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if value is not None:
            return value
    return None


def _aware_datetime(value: Any):
    if not value:
        return None
    if hasattr(value, 'tzinfo'):
        parsed = value
    else:
        parsed = parse_datetime(str(value))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _json_fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _phase_from_event_type(event_type: Any) -> str:
    return {
        'question_started': 'question_active',
        'round_started': 'round_active',
        'question_ended': 'question_result',
        'round_ended': 'round_result',
        'quiz_started': 'active',
        'game_started': 'active',
        'quiz_ended': 'completed',
        'game_ended': 'completed',
        'solution_revealed': 'revealed',
    }.get(_text(event_type), '')


def _stronger_live_phase(*phases: Any) -> str:
    priorities = {
        '': -1,
        'waiting': 0,
        'ready': 0,
        'inactive': 0,
        'active': 1,
        'question_active': 2,
        'round_active': 2,
    }
    normalized = [_text(phase) for phase in phases if _text(phase)]
    if not normalized:
        return ''
    return max(normalized, key=lambda phase: priorities.get(phase, 1))


def _persisted_game_context(runtime: GameRuntimeState) -> dict:
    model = HubSession._get_game_model_map().get(runtime.game_key)
    if not model:
        return {}
    game = model.objects.filter(room_code=runtime.room_code).first()
    if not game:
        return {}
    session = None
    try:
        session = game.session
    except (AttributeError, ObjectDoesNotExist):
        session = None

    current_question_id = getattr(game, 'current_question_id', None)
    current_round_id = None
    for source, fields in (
        (session, ('current_round', 'current_round_index', 'current_question_number')),
        (game, ('current_round_number', 'current_question_number')),
    ):
        if not source:
            continue
        current_round_id = next(
            (getattr(source, field) for field in fields if getattr(source, field, None) is not None),
            None,
        )
        if current_round_id is not None:
            break

    starts_at = next(
        (
            getattr(source, field)
            for source, field in (
                (game, 'question_started_at'),
                (game, 'question_start_time'),
                (session, 'round_start_time'),
                (game, 'started_at'),
            )
            if source is not None and getattr(source, field, None)
        ),
        None,
    )
    ends_at = next(
        (
            getattr(session, field)
            for field in ('question_end_time', 'round_end_time')
            if session is not None and getattr(session, field, None)
        ),
        None,
    )
    phase = getattr(session, 'phase', None) or getattr(game, 'question_state', None)
    if not phase:
        phase = getattr(game, 'status', '')
        is_active = bool(
            getattr(session, 'is_question_active', False)
            or getattr(session, 'is_round_active', False)
            or current_question_id
        )
        if phase == 'active' and is_active:
            phase = 'question_active'

    current_set_id = None
    get_set_number = getattr(game, 'get_current_set_number', None)
    if callable(get_set_number):
        try:
            current_set_id = get_set_number()
        except (TypeError, ValueError):
            current_set_id = None
    return {
        'game_instance_id': str(game.pk),
        'phase': _text(phase),
        'current_question_id': _text(current_question_id),
        'current_round_id': _text(current_round_id),
        'current_set_id': _text(current_set_id),
        'starts_at': starts_at,
        'ends_at': ends_at,
    }


def resolve_runtime_identity(game_key: str, room_code: str, session_code: str | None = None) -> dict:
    session = None
    step = None
    if session_code:
        session = HubSession.objects.filter(code=session_code).first()
    if session:
        step = (
            HubGameStep.objects
            .filter(session=session, game_key=game_key, room_code=room_code)
            .order_by('order', 'id')
            .first()
        )
    if not step:
        step = (
            HubGameStep.objects
            .select_related('session')
            .filter(game_key=game_key, room_code=room_code, session__ended_at__isnull=True)
            .order_by('-session__started_at', '-id')
            .first()
        )
        if step:
            session = step.session

    game_instance_id = ''
    model = HubSession._get_game_model_map().get(game_key)
    if model:
        game = model.objects.filter(room_code=room_code).only('pk').first()
        if game:
            game_instance_id = str(game.pk)

    if step:
        identity_key = f'hub:{step.session_id}:step:{step.pk}:game:{game_instance_id or room_code}'
    elif session:
        identity_key = f'hub:{session.pk}:{game_key}:{game_instance_id or room_code}'
    else:
        identity_key = f'standalone:{game_key}:{game_instance_id or room_code}'
    return {
        'identity_key': identity_key,
        'session': session,
        'game_step': step,
        'game_key': game_key,
        'room_code': room_code,
        'game_instance_id': game_instance_id,
    }


def get_runtime_state(game_key: str, room_code: str, session_code: str | None = None) -> GameRuntimeState:
    identity = resolve_runtime_identity(game_key, room_code, session_code)
    runtime, _ = GameRuntimeState.objects.get_or_create(
        identity_key=identity['identity_key'],
        defaults=identity,
    )
    updates = {}
    for field in ('session', 'game_step', 'game_instance_id'):
        value = identity[field]
        current = getattr(runtime, field)
        current_id = getattr(runtime, f'{field}_id', None)
        value_id = getattr(value, 'pk', None)
        if field == 'game_instance_id':
            if current != value:
                updates[field] = value
        elif current_id != value_id:
            updates[field] = value
    if updates:
        GameRuntimeState.objects.filter(pk=runtime.pk).update(**updates)
        runtime.refresh_from_db()
    return runtime


def _question_lifecycle_snapshot(
    runtime: GameRuntimeState,
    *,
    at=None,
    context: dict | None = None,
) -> dict:
    now = at or timezone.now()
    context = context or {}
    current_question_id = _text(
        context.get('current_question_id', runtime.current_question_id)
    )
    starts_at = (
        _aware_datetime(context.get('starts_at'))
        if 'starts_at' in context
        else runtime.starts_at
    )
    ends_at = (
        _aware_datetime(context.get('ends_at'))
        if 'ends_at' in context
        else runtime.ends_at
    )
    manual = (
        runtime.question_flow_mode
        == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
    )
    if manual:
        question_phase = runtime.question_phase or None
        presented_at = runtime.question_presented_at
        content_revealed_at = runtime.content_revealed_at
        visible_at = _question_visible_at(runtime)
    else:
        inactive_phases = {'completed', 'ended', 'inactive', 'revealed'}
        general_phase = _text(context.get('phase', runtime.phase))
        question_phase = (
            GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
            if current_question_id and general_phase not in inactive_phases
            else None
        )
        presented_at = starts_at if question_phase else None
        content_revealed_at = starts_at if question_phase else None
        visible_at = None

    answering_allowed = bool(
        question_phase == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
        and (not starts_at or now >= starts_at)
        and (not ends_at or now < ends_at)
    )
    remaining_answer_time = None
    if answering_allowed and ends_at:
        remaining_answer_time = max(
            0,
            int((ends_at - now).total_seconds() + 0.999),
        )
    return {
        'question_flow_mode': runtime.question_flow_mode,
        'question_phase': question_phase,
        'question_presented_at': (
            presented_at.isoformat() if presented_at else None
        ),
        'question_visible_at': visible_at.isoformat() if visible_at else None,
        'content_revealed_at': (
            content_revealed_at.isoformat() if content_revealed_at else None
        ),
        'answering_started_at': starts_at.isoformat() if starts_at else None,
        'answering_deadline_at': ends_at.isoformat() if ends_at else None,
        'answering_allowed': answering_allowed,
        'timer_running': bool(
            answering_allowed and ends_at and ends_at > now
        ),
        'remaining_answer_time': remaining_answer_time,
    }


def _normalize_snapshot(payload: dict, runtime: GameRuntimeState) -> tuple[dict, dict, dict]:
    game = _mapping(payload.get('game'))
    question = _mapping(payload.get('question'))
    round_state = _mapping(payload.get('round'))
    timer = _mapping(payload.get('timer'))
    participant = _mapping(payload.get('participant_state') or payload.get('participant'))

    explicit_phase = _first(
        payload,
        ('phase',),
        ('question_state',),
        ('game', 'phase'),
        ('game', 'status'),
        ('game_status',),
        ('status',),
    )
    event_type = _text(payload.get('event_type') or payload.get('type'))
    event_phase = _phase_from_event_type(event_type)
    question_id = _first(
        payload,
        ('current_question_id',),
        ('question_id',),
        ('question', 'id'),
        ('question', 'question_id'),
    )
    round_id = _first(
        payload,
        ('current_round_id',),
        ('round_id',),
        ('round', 'id'),
        ('round', 'number'),
        ('current_round',),
        ('round_number',),
    )
    set_id = _first(
        payload,
        ('current_set_id',),
        ('set_id',),
        ('set_number',),
        ('current_set',),
        ('question', 'set_number'),
    )
    starts_at = _first(
        payload,
        ('starts_at',),
        ('timer', 'started_at'),
        ('timer', 'starts_at'),
        ('question', 'starts_at'),
        ('question', 'question_start_time'),
        ('question_start_time',),
    )
    ends_at = _first(
        payload,
        ('ends_at',),
        ('timer', 'ends_at'),
        ('question', 'ends_at'),
        ('question', 'question_end_time'),
        ('question_end_time',),
        ('round_end_time',),
    )
    reveal_value = _first(
        payload,
        ('revealed',),
        ('reveal_visible',),
        ('question', 'revealed'),
    )
    reveal = (
        bool(reveal_value)
        if reveal_value is not None
        else bool((runtime.public_snapshot or {}).get('revealed'))
    )

    persisted = _persisted_game_context(runtime)
    question_context_supplied = any(
        _contains_path(payload, path)
        for path in (
            ('current_question_id',),
            ('question_id',),
            ('question',),
        )
    )
    round_context_supplied = any(
        _contains_path(payload, path)
        for path in (
            ('current_round_id',),
            ('round_id',),
            ('round',),
            ('current_round',),
            ('round_number',),
        )
    )
    set_context_supplied = any(
        _contains_path(payload, path)
        for path in (
            ('current_set_id',),
            ('set_id',),
            ('set_number',),
            ('current_set',),
        )
    )
    starts_context_supplied = any(
        _contains_path(payload, path)
        for path in (
            ('starts_at',),
            ('timer', 'started_at'),
            ('timer', 'starts_at'),
            ('question', 'starts_at'),
            ('question', 'question_start_time'),
            ('question_start_time',),
        )
    )
    ends_context_supplied = any(
        _contains_path(payload, path)
        for path in (
            ('ends_at',),
            ('timer', 'ends_at'),
            ('question', 'ends_at'),
            ('question', 'question_end_time'),
            ('question_end_time',),
            ('round_end_time',),
        )
    )
    if event_phase in {'question_result', 'round_result', 'revealed', 'completed'}:
        phase = explicit_phase or event_phase
    elif event_type in {'question_started', 'round_started'}:
        phase = explicit_phase or persisted.get('phase') or event_phase
    else:
        phase = explicit_phase or _stronger_live_phase(
            runtime.phase,
            persisted.get('phase'),
            event_phase,
        )
    manual_three_phase = (
        runtime.question_flow_mode
        == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
    )
    context_starts_at = _aware_datetime(
        runtime.starts_at
        if manual_three_phase
        else (
            starts_at
            if starts_context_supplied
            else (runtime.starts_at or persisted.get('starts_at'))
        )
    )
    context_ends_at = _aware_datetime(
        runtime.ends_at
        if manual_three_phase
        else (
            ends_at
            if ends_context_supplied
            else (runtime.ends_at or persisted.get('ends_at'))
        )
    )
    context = {
        'phase': _text(phase),
        'current_question_id': (
            runtime.current_question_id
            if manual_three_phase
            else (
                _text(question_id)
                if question_context_supplied
                else (
                    runtime.current_question_id
                    or persisted.get('current_question_id', '')
                )
            )
        ),
        'current_round_id': (
            _text(round_id)
            if round_context_supplied
            else (
                runtime.current_round_id
                or persisted.get('current_round_id', '')
            )
        ),
        'current_set_id': (
            _text(set_id)
            if set_context_supplied
            else (
                runtime.current_set_id
                or persisted.get('current_set_id', '')
            )
        ),
        'starts_at': context_starts_at.isoformat() if context_starts_at else '',
        'ends_at': context_ends_at.isoformat() if context_ends_at else '',
    }
    display = {
        **context,
        'reveal': reveal,
        'public_state': payload.get('_revision_state') or {},
    }
    answer_locked = bool(
        participant.get('answer_locked')
        or participant.get('locked')
        or participant.get('has_submitted')
        or payload.get('answer_submitted')
        or _text(payload.get('type')) == 'answer_submitted'
    )
    standard = {
        'state_revision': runtime.state_revision,
        'server_now': timezone.now().isoformat(),
        'game_id': runtime.game_instance_id or None,
        'session_id': runtime.session_id,
        'game_key': runtime.game_key,
        'room_code': runtime.room_code,
        'phase': context['phase'] or None,
        'current_question_id': context['current_question_id'] or None,
        'current_round_id': context['current_round_id'] or None,
        'current_set_id': context['current_set_id'] or None,
        'question': question or None,
        'answer_options': (
            question.get('answers')
            or question.get('options')
            or payload.get('answer_options')
            or payload.get('items')
        ),
        'starts_at': (
            _aware_datetime(context['starts_at']).isoformat()
            if _aware_datetime(context['starts_at'])
            else None
        ),
        'ends_at': (
            _aware_datetime(context['ends_at']).isoformat()
            if _aware_datetime(context['ends_at'])
            else None
        ),
        'remaining_seconds': _first(payload, ('remaining_seconds',), ('timer', 'remaining_seconds')),
        'tutorial': payload.get('tutorial') or payload.get('tutorial_state'),
        'own_answer': _first(
            payload,
            ('own_answer',),
            ('participant_state', 'answer'),
            ('participant', 'answer'),
        ),
        'answer_locked': answer_locked,
        'revealed': reveal,
        'result': payload.get('result') or payload.get('scorebox'),
        'next_participant_state': _first(
            payload,
            ('next_participant_state',),
            ('participant_state', 'next_state'),
        ),
    }
    standard.update(_question_lifecycle_snapshot(runtime, context=context))
    if (
        manual_three_phase
        and runtime.question_phase == GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
    ):
        if standard['question']:
            standard['question'] = dict(standard['question'])
            for field in ('answers', 'options', 'items'):
                standard['question'].pop(field, None)
        standard['answer_options'] = None
    return context, display, standard


@transaction.atomic
def observe_snapshot(
    game_key: str,
    room_code: str,
    payload: dict,
    session_code: str | None = None,
) -> dict:
    runtime = get_runtime_state(game_key, room_code, session_code)
    runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
    context, display, standard = _normalize_snapshot(payload, runtime)
    context_fingerprint = _json_fingerprint(context)
    display_fingerprint = _json_fingerprint(display)
    context_changed = context_fingerprint != runtime.context_fingerprint
    display_changed = display_fingerprint != runtime.display_fingerprint
    previous_question_id = _text(runtime.current_question_id)
    if context_changed or display_changed:
        runtime.state_revision += 1
    if context_changed:
        runtime.context_revision = runtime.state_revision

    runtime.phase = context['phase']
    runtime.current_question_id = context['current_question_id']
    runtime.current_round_id = context['current_round_id']
    runtime.current_set_id = context['current_set_id']
    runtime.starts_at = _aware_datetime(context['starts_at'])
    runtime.ends_at = _aware_datetime(context['ends_at'])
    runtime.context_fingerprint = context_fingerprint
    runtime.display_fingerprint = display_fingerprint
    standard['state_revision'] = runtime.state_revision
    public_snapshot = dict(runtime.public_snapshot or {})
    for field, value in standard.items():
        if value is not None and field not in {
            'server_now',
            'remaining_seconds',
            'remaining_answer_time',
            'question',
            'answer_options',
        }:
            public_snapshot[field] = value
    revision_state = payload.get('_revision_state') or {}
    public_question = revision_state.get('question', standard.get('question'))
    public_answer_options = revision_state.get('answer_options', standard.get('answer_options'))
    if context['current_question_id'] != previous_question_id:
        public_snapshot['question'] = public_question
        public_snapshot['answer_options'] = public_answer_options
    elif public_question is not None:
        public_snapshot['question'] = public_question
        public_snapshot['answer_options'] = public_answer_options
    for field in (
        'state_revision',
        'game_id',
        'session_id',
        'game_key',
        'room_code',
        'phase',
        'current_question_id',
        'current_round_id',
        'current_set_id',
        'starts_at',
        'ends_at',
        'revealed',
        'question_flow_mode',
        'question_phase',
        'question_presented_at',
        'question_visible_at',
        'content_revealed_at',
        'answering_started_at',
        'answering_deadline_at',
        'answering_allowed',
        'timer_running',
    ):
        public_snapshot[field] = standard.get(field)
    for participant_field in (
        'own_answer',
        'answer_locked',
        'result',
        'next_participant_state',
    ):
        public_snapshot.pop(participant_field, None)
    public_snapshot_changed = public_snapshot != (runtime.public_snapshot or {})
    runtime.public_snapshot = public_snapshot
    if context_changed or display_changed or public_snapshot_changed:
        runtime.save(update_fields=[
            'state_revision',
            'context_revision',
            'phase',
            'current_question_id',
            'current_round_id',
            'current_set_id',
            'starts_at',
            'ends_at',
            'context_fingerprint',
            'display_fingerprint',
            'public_snapshot',
            'updated_at',
        ])
    return standard


def _runtime_snapshot_payload(runtime: GameRuntimeState, *, at=None) -> dict:
    now = at or timezone.now()
    snapshot = dict(runtime.public_snapshot or {})
    snapshot.update({
        'state_revision': runtime.state_revision,
        'server_now': now.isoformat(),
        'game_id': runtime.game_instance_id or None,
        'session_id': runtime.session_id,
        'game_key': runtime.game_key,
        'room_code': runtime.room_code,
        'phase': runtime.phase or None,
        'current_question_id': runtime.current_question_id or None,
        'current_round_id': runtime.current_round_id or None,
        'current_set_id': runtime.current_set_id or None,
        'starts_at': runtime.starts_at.isoformat() if runtime.starts_at else None,
        'ends_at': runtime.ends_at.isoformat() if runtime.ends_at else None,
    })
    snapshot.update(_question_lifecycle_snapshot(runtime, at=now))
    if runtime.ends_at:
        snapshot['remaining_seconds'] = max(
            0,
            int((runtime.ends_at - now).total_seconds() + 0.999),
        )
    else:
        snapshot['remaining_seconds'] = None
    return snapshot


def current_snapshot(game_key: str, room_code: str, session_code: str | None = None) -> dict:
    runtime = get_runtime_state(game_key, room_code, session_code)
    return _runtime_snapshot_payload(runtime)


def attach_snapshot_metadata(
    payload: dict,
    *,
    game_key: str,
    room_code: str,
    session_code: str | None = None,
) -> dict:
    result = dict(payload)
    revision_state = result.pop('_revision_state', None)
    observed_payload = dict(result)
    if revision_state is not None:
        observed_payload['_revision_state'] = revision_state
    standard = _retry_sqlite_locked(
        lambda: observe_snapshot(
            game_key,
            room_code,
            observed_payload,
            session_code,
        )
    )
    for field in (
        'state_revision',
        'server_now',
        'game_id',
        'session_id',
        'game_key',
        'room_code',
        'phase',
        'current_question_id',
        'current_round_id',
        'current_set_id',
        'starts_at',
        'ends_at',
        'question_flow_mode',
        'question_phase',
        'question_presented_at',
        'question_visible_at',
        'content_revealed_at',
        'answering_started_at',
        'answering_deadline_at',
        'answering_allowed',
        'timer_running',
        'remaining_answer_time',
    ):
        if standard.get(field) is not None or field in {
            'question_phase',
            'question_presented_at',
            'question_visible_at',
            'content_revealed_at',
            'answering_started_at',
            'answering_deadline_at',
            'remaining_answer_time',
        }:
            result[field] = standard[field]
    return result


@dataclass(frozen=True)
class QuestionPhaseDecision:
    accepted: bool
    code: str
    message: str
    runtime_state_id: int | None = None
    state_revision: int | None = None
    snapshot: dict | None = None
    duplicate: bool = False


def configure_question_flow(
    *,
    game_key: str,
    room_code: str,
    session_code: str | None,
    mode: str,
) -> dict:
    valid_modes = {
        GameRuntimeState.QUESTION_FLOW_LEGACY_IMMEDIATE,
        GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
    }
    if mode not in valid_modes:
        raise ValueError('Unknown question flow mode.')
    with transaction.atomic():
        runtime = get_runtime_state(game_key, room_code, session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        if runtime.question_flow_mode == mode:
            return _runtime_snapshot_payload(runtime)
        if runtime.current_question_id or runtime.question_phase:
            raise ValueError('Question flow mode cannot change during an active question.')
        runtime.question_flow_mode = mode
        runtime.question_phase = ''
        runtime.question_presented_at = None
        runtime.content_revealed_at = None
        runtime.starts_at = None
        runtime.ends_at = None
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        snapshot = _runtime_snapshot_payload(runtime)
        runtime.public_snapshot = snapshot
        runtime.save(update_fields=[
            'question_flow_mode',
            'question_phase',
            'question_presented_at',
            'content_revealed_at',
            'starts_at',
            'ends_at',
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        return snapshot


def reset_question_flow(
    *,
    game_key: str,
    room_code: str,
    session_code: str | None,
    mode: str,
) -> dict:
    """Start a new game run with an empty, explicitly configured question flow."""
    valid_modes = {
        GameRuntimeState.QUESTION_FLOW_LEGACY_IMMEDIATE,
        GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
    }
    if mode not in valid_modes:
        raise ValueError('Unknown question flow mode.')
    with transaction.atomic():
        runtime = get_runtime_state(game_key, room_code, session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        runtime.question_flow_mode = mode
        runtime.question_phase = ''
        runtime.current_question_id = ''
        runtime.question_presented_at = None
        runtime.content_revealed_at = None
        runtime.starts_at = None
        runtime.ends_at = None
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        snapshot = dict(runtime.public_snapshot or {})
        snapshot.pop('question', None)
        snapshot.pop('answer_options', None)
        snapshot.pop('answer_duration_seconds', None)
        snapshot['revealed'] = False
        runtime.public_snapshot = snapshot
        runtime.public_snapshot = _runtime_snapshot_payload(runtime)
        runtime.save(update_fields=[
            'question_flow_mode',
            'question_phase',
            'current_question_id',
            'question_presented_at',
            'content_revealed_at',
            'starts_at',
            'ends_at',
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        return dict(runtime.public_snapshot)


def _question_phase_action_context(action: dict):
    client_action_id = action.get('client_action_id')
    if not client_action_id:
        return None, None
    try:
        return uuid.UUID(str(client_action_id)), int(action.get('state_revision'))
    except (TypeError, ValueError, AttributeError):
        return None, None


def _transition_question_phase(
    *,
    game_key: str,
    room_code: str,
    session_code: str | None,
    action_type: str,
    action: dict,
    answer_duration_seconds: int | float | None = None,
    at=None,
) -> QuestionPhaseDecision:
    action_id, action_revision = _question_phase_action_context(action)
    if action_id is None:
        return QuestionPhaseDecision(
            False,
            'invalid_action_context',
            'Aktionskontext ist ungueltig.',
        )
    with transaction.atomic():
        runtime = get_runtime_state(game_key, room_code, session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        capabilities = get_question_flow_capabilities(runtime.game_key)
        participant_key = '__host_question_phase__'
        previous_action = ProcessedClientAction.objects.filter(
            runtime_state=runtime,
            participant_key=participant_key,
            client_action_id=action_id,
        ).first()
        if previous_action:
            if previous_action.action_type != action_type:
                return QuestionPhaseDecision(
                    False,
                    'invalid_action_context',
                    'Die Action-ID wurde bereits fuer eine andere Aktion verwendet.',
                    runtime.pk,
                    runtime.state_revision,
                )
            return QuestionPhaseDecision(
                previous_action.status == ProcessedClientAction.STATUS_ACCEPTED,
                'accepted' if previous_action.status == ProcessedClientAction.STATUS_ACCEPTED else 'rejected',
                '',
                runtime.pk,
                runtime.state_revision,
                _runtime_snapshot_payload(runtime),
                True,
            )
        if (
            runtime.question_flow_mode
            != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        ):
            return QuestionPhaseDecision(
                False,
                'unsupported_question_flow',
                'Der manuelle Fragenablauf ist fuer dieses Spiel nicht aktiviert.',
                runtime.pk,
                runtime.state_revision,
            )
        if (
            action_revision < runtime.context_revision
            or action_revision > runtime.state_revision
        ):
            return QuestionPhaseDecision(
                False,
                'stale_action',
                'Der Spielzustand hat sich geaendert.',
                runtime.pk,
                runtime.state_revision,
            )
        supplied_game_id = _text(action.get('game_id'))
        if supplied_game_id and supplied_game_id != runtime.game_instance_id:
            return QuestionPhaseDecision(
                False,
                'stale_action',
                'Die Aktion gehoert zu einer anderen Spielinstanz.',
                runtime.pk,
                runtime.state_revision,
            )
        question_id = _text(action.get('question_id'))
        if not question_id:
            return QuestionPhaseDecision(
                False,
                'invalid_action_context',
                'question_id fehlt.',
                runtime.pk,
                runtime.state_revision,
            )
        if action_type != 'present_question' and question_id != runtime.current_question_id:
            return QuestionPhaseDecision(
                False,
                'stale_action',
                'Die Aktion gehoert nicht zur aktuellen Frage.',
                runtime.pk,
                runtime.state_revision,
            )

        if (
            action_type == 'reveal_question_content'
            and (
                not capabilities.uses_prompt_phase
                or not capabilities.uses_content_phase
            )
        ):
            return QuestionPhaseDecision(
                False,
                'invalid_phase',
                'Dieses Spiel besitzt keine separate Inhaltsfreigabe.',
                runtime.pk,
                runtime.state_revision,
            )
        if (
            action_type == 'open_answering'
            and capabilities.auto_open_answering_after_presentation
        ):
            return QuestionPhaseDecision(
                False,
                'invalid_phase',
                'Die Antwortphase startet fuer dieses Spiel automatisch.',
                runtime.pk,
                runtime.state_revision,
            )

        target_phase = {
            'present_question': capabilities.initial_phase,
            'reveal_question_content': GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE,
            'open_answering': GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN,
        }.get(action_type)
        if not target_phase:
            return QuestionPhaseDecision(
                False,
                'invalid_action_context',
                'Unbekannte Fragenaktion.',
                runtime.pk,
                runtime.state_revision,
            )

        current_phase = runtime.question_phase
        idempotent = current_phase == target_phase and question_id == runtime.current_question_id
        expected_phase = {
            'present_question': '',
            'reveal_question_content': GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
            'open_answering': (
                GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE
                if capabilities.uses_content_phase
                else GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
            ),
        }[action_type]
        compatible_source_phases = {expected_phase}
        if action_type == 'open_answering' and not capabilities.uses_content_phase:
            # Development sessions created before a phase path is simplified may
            # still persist the now-skipped content phase.
            compatible_source_phases.add(GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE)
        if not idempotent and current_phase not in compatible_source_phases:
            return QuestionPhaseDecision(
                False,
                'invalid_phase',
                'Dieser Phasenwechsel ist nicht erlaubt.',
                runtime.pk,
                runtime.state_revision,
            )
        if (
            action_type == 'present_question'
            and runtime.current_question_id
            and runtime.current_question_id != question_id
        ):
            return QuestionPhaseDecision(
                False,
                'invalid_phase',
                'Die vorherige Frage ist noch nicht abgeschlossen.',
                runtime.pk,
                runtime.state_revision,
            )

        transition_at = _aware_datetime(at) or timezone.now()
        if (
            action_type == 'reveal_question_content'
            and not idempotent
            and (
                not runtime.question_presented_at
                or transition_at < _question_visible_at(runtime)
            )
        ):
            return QuestionPhaseDecision(
                False,
                'question_not_visible',
                'Die Frage ist fuer Teilnehmer noch nicht sichtbar.',
                runtime.pk,
                runtime.state_revision,
                _runtime_snapshot_payload(runtime, at=transition_at),
            )
        if (
            action_type == 'open_answering'
            and not idempotent
            and (
                not capabilities.uses_prompt_phase
                or not capabilities.uses_content_phase
            )
            and (
                not runtime.question_presented_at
                or transition_at < _question_visible_at(runtime)
            )
        ):
            return QuestionPhaseDecision(
                False,
                'question_not_visible',
                'Die Frage ist fuer Teilnehmer noch nicht sichtbar.',
                runtime.pk,
                runtime.state_revision,
                _runtime_snapshot_payload(runtime, at=transition_at),
            )
        if (
            action_type == 'open_answering'
            or (
                action_type == 'present_question'
                and capabilities.auto_open_answering_after_presentation
            )
        ) and not idempotent:
            duration_source = answer_duration_seconds
            if duration_source is None:
                duration_source = (runtime.public_snapshot or {}).get(
                    'answer_duration_seconds'
                )
            try:
                duration = float(duration_source)
            except (TypeError, ValueError):
                duration = 0
            if duration <= 0:
                return QuestionPhaseDecision(
                    False,
                    'invalid_action_context',
                    'Die Antwortdauer muss groesser als null sein.',
                    runtime.pk,
                    runtime.state_revision,
                )

        if not idempotent:
            runtime.question_phase = target_phase
            if action_type == 'present_question':
                runtime.current_question_id = question_id
                runtime.question_presented_at = transition_at
                runtime.content_revealed_at = (
                    _question_visible_at(runtime)
                    if capabilities.initial_phase
                    == GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE
                    else None
                )
                if capabilities.auto_open_answering_after_presentation:
                    runtime.starts_at = _question_visible_at(runtime)
                    runtime.ends_at = runtime.starts_at + timedelta(seconds=duration)
                else:
                    runtime.starts_at = None
                    runtime.ends_at = None
                runtime.public_snapshot = dict(runtime.public_snapshot or {})
                runtime.public_snapshot['question'] = None
                runtime.public_snapshot['answer_options'] = None
                runtime.public_snapshot['revealed'] = False
                if answer_duration_seconds is not None:
                    try:
                        prepared_duration = float(answer_duration_seconds)
                    except (TypeError, ValueError):
                        prepared_duration = 0
                    if prepared_duration <= 0:
                        return QuestionPhaseDecision(
                            False,
                            'invalid_action_context',
                            'Die Antwortdauer muss groesser als null sein.',
                            runtime.pk,
                            runtime.state_revision,
                        )
                    runtime.public_snapshot['answer_duration_seconds'] = prepared_duration
            elif action_type == 'reveal_question_content':
                runtime.content_revealed_at = transition_at
            else:
                runtime.starts_at = transition_at
                runtime.ends_at = transition_at + timedelta(seconds=duration)
            runtime.state_revision += 1
            runtime.context_revision = runtime.state_revision

        snapshot = _runtime_snapshot_payload(runtime, at=transition_at)
        runtime.public_snapshot = snapshot
        runtime.save(update_fields=[
            'question_phase',
            'current_question_id',
            'question_presented_at',
            'content_revealed_at',
            'starts_at',
            'ends_at',
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        processed_action = ProcessedClientAction.objects.create(
            runtime_state=runtime,
            participant_key=participant_key,
            client_action_id=action_id,
            action_type=action_type,
            state_revision=action_revision,
            game_instance_id=supplied_game_id,
            question_id=question_id,
            status=ProcessedClientAction.STATUS_ACCEPTED,
            response_payload=snapshot,
            completed_at=transition_at,
        )
        return QuestionPhaseDecision(
            True,
            'accepted',
            '',
            runtime.pk,
            runtime.state_revision,
            processed_action.response_payload,
            idempotent,
        )


def present_question(**kwargs) -> QuestionPhaseDecision:
    return _transition_question_phase(action_type='present_question', **kwargs)


def reveal_question_content(**kwargs) -> QuestionPhaseDecision:
    return _transition_question_phase(action_type='reveal_question_content', **kwargs)


def open_answering(**kwargs) -> QuestionPhaseDecision:
    return _transition_question_phase(action_type='open_answering', **kwargs)


def finish_question_flow(
    *,
    game_key: str,
    room_code: str,
    session_code: str | None,
    question_id,
) -> dict:
    """Clear a completed manual question without creating another host action."""
    with transaction.atomic():
        runtime = get_runtime_state(game_key, room_code, session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        if (
            runtime.question_flow_mode
            != GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
        ):
            return _runtime_snapshot_payload(runtime)
        if _text(question_id) != runtime.current_question_id:
            raise ValueError('The completed question is not the current question.')
        if (
            runtime.question_phase
            != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
        ):
            raise ValueError('The question cannot be completed before answering opens.')

        runtime.question_phase = ''
        runtime.current_question_id = ''
        runtime.question_presented_at = None
        runtime.content_revealed_at = None
        runtime.starts_at = None
        runtime.ends_at = None
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        snapshot = dict(runtime.public_snapshot or {})
        snapshot.pop('question', None)
        snapshot.pop('answer_options', None)
        snapshot.pop('answer_duration_seconds', None)
        snapshot['revealed'] = False
        runtime.public_snapshot = snapshot
        runtime.public_snapshot = _runtime_snapshot_payload(runtime)
        runtime.save(update_fields=[
            'question_phase',
            'current_question_id',
            'question_presented_at',
            'content_revealed_at',
            'starts_at',
            'ends_at',
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])
        return dict(runtime.public_snapshot)


@dataclass(frozen=True)
class ActionDecision:
    accepted: bool
    code: str
    message: str
    runtime_state_id: int | None = None
    received_at: str | None = None
    time_taken: float | None = None


def validate_and_reserve_action(
    *,
    game_key: str,
    room_code: str,
    session_code: str | None,
    participant_name: str,
    action_type: str,
    action: dict,
    allow_inactive: bool = False,
) -> ActionDecision:
    client_action_id = action.get('client_action_id')
    if not client_action_id:
        return ActionDecision(False, 'invalid_action_context', 'client_action_id fehlt.')
    try:
        normalized_action_id = uuid.UUID(str(client_action_id))
        action_revision = int(action.get('state_revision'))
    except (TypeError, ValueError, AttributeError):
        return ActionDecision(False, 'invalid_action_context', 'Aktionskontext ist ungueltig.')

    with transaction.atomic():
        runtime = get_runtime_state(game_key, room_code, session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        received_at = timezone.now()
        if action_revision < runtime.context_revision or action_revision > runtime.state_revision:
            return ActionDecision(False, 'stale_action', 'Der Spielzustand hat sich geaendert.', runtime.pk)
        supplied_game_id = _text(action.get('game_id'))
        if runtime.game_instance_id and supplied_game_id != runtime.game_instance_id:
            return ActionDecision(False, 'stale_action', 'Die Aktion gehoert zu einer anderen Spielinstanz.', runtime.pk)
        for supplied_name, current_value in (
            ('question_id', runtime.current_question_id),
            ('round_id', runtime.current_round_id),
            ('set_id', runtime.current_set_id),
        ):
            supplied_value = _text(action.get(supplied_name))
            if current_value and supplied_value != current_value:
                return ActionDecision(False, 'stale_action', 'Die Aktion gehoert nicht zum aktuellen Kontext.', runtime.pk)
        if (
            runtime.question_flow_mode
            == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
            and (
                runtime.question_phase
                != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
                or (runtime.starts_at and received_at < runtime.starts_at)
            )
        ):
            return ActionDecision(
                False,
                'invalid_phase',
                'Antworten sind noch nicht freigegeben.',
                runtime.pk,
            )
        if runtime.ends_at and received_at >= runtime.ends_at:
            return ActionDecision(False, 'deadline_expired', 'Die Zeit fuer diese Aktion ist abgelaufen.', runtime.pk)
        blocked_phases = {'completed', 'ended', 'revealed'}
        if not allow_inactive:
            blocked_phases.add('inactive')
        if runtime.phase in blocked_phases:
            return ActionDecision(False, 'invalid_phase', 'Die Aktion ist in dieser Phase nicht erlaubt.', runtime.pk)

        participant = None
        if runtime.session_id and participant_name:
            participant = HubParticipant.objects.filter(
                session_id=runtime.session_id,
                nickname=participant_name,
            ).first()
        participant_key = str(participant.pk) if participant else participant_name.strip().casefold()
        try:
            ProcessedClientAction.objects.create(
                runtime_state=runtime,
                participant=participant,
                participant_key=participant_key,
                client_action_id=normalized_action_id,
                action_type=action_type,
                state_revision=action_revision,
                game_instance_id=supplied_game_id,
                question_id=_text(action.get('question_id')),
                round_id=_text(action.get('round_id')),
                set_id=_text(action.get('set_id')),
            )
        except IntegrityError:
            return ActionDecision(False, 'already_submitted', 'Diese Aktion wurde bereits verarbeitet.', runtime.pk)
    time_taken = None
    if runtime.starts_at:
        time_taken = max(0.0, (received_at - runtime.starts_at).total_seconds())
    return ActionDecision(
        True,
        'accepted',
        '',
        runtime.pk,
        received_at.isoformat(),
        time_taken,
    )


def register_socket_connection(
    *,
    channel_name: str,
    session_code: str,
    participant_name: str,
    scope_kind: str,
    game_key: str = '',
    room_code: str = '',
) -> HubSocketConnection | None:
    session = HubSession.objects.filter(code=session_code).first()
    if not session or not participant_name:
        return None
    participant = HubParticipant.objects.filter(
        session=session,
        nickname=participant_name,
    ).first()
    if not participant:
        return None
    now = timezone.now()

    def persist_connection():
        with transaction.atomic():
            socket_connection, _ = HubSocketConnection.objects.update_or_create(
                channel_name=channel_name,
                defaults={
                    'session': session,
                    'participant': participant,
                    'scope_kind': scope_kind,
                    'game_key': game_key,
                    'room_code': room_code,
                    'connected_at': now,
                    'last_seen': now,
                    'disconnected_at': None,
                },
            )
            HubParticipant.objects.filter(pk=participant.pk).update(last_seen=now)
            return socket_connection

    return _retry_sqlite_locked(persist_connection)


def touch_socket_connection(channel_name: str) -> bool:
    now = timezone.now()

    def persist_heartbeat():
        with transaction.atomic():
            updated = HubSocketConnection.objects.filter(
                channel_name=channel_name,
                disconnected_at__isnull=True,
            ).update(last_seen=now)
            if updated:
                participant_id = (
                    HubSocketConnection.objects
                    .filter(channel_name=channel_name)
                    .values_list('participant_id', flat=True)
                    .first()
                )
                HubParticipant.objects.filter(pk=participant_id).update(last_seen=now)
            return bool(updated)

    return _retry_sqlite_locked(persist_heartbeat)


def disconnect_socket_connection(channel_name: str) -> bool:
    def persist_disconnect():
        return bool(
            HubSocketConnection.objects
            .filter(channel_name=channel_name, disconnected_at__isnull=True)
            .update(disconnected_at=timezone.now())
        )

    return _retry_sqlite_locked(persist_disconnect)


def connected_participant_ids(session: HubSession, at=None) -> set[int]:
    cutoff = (at or timezone.now()) - timedelta(seconds=_presence_ttl_seconds())
    return set(
        HubSocketConnection.objects.filter(
            session=session,
            disconnected_at__isnull=True,
            last_seen__gte=cutoff,
        ).values_list('participant_id', flat=True)
    )


def participant_is_connected(participant: HubParticipant, at=None) -> bool:
    cutoff = (at or timezone.now()) - timedelta(seconds=_presence_ttl_seconds())
    return HubSocketConnection.objects.filter(
        participant=participant,
        disconnected_at__isnull=True,
        last_seen__gte=cutoff,
    ).exists()


def expire_stale_connections(at=None) -> int:
    cutoff = (at or timezone.now()) - timedelta(seconds=_presence_ttl_seconds())
    return HubSocketConnection.objects.filter(
        disconnected_at__isnull=True,
        last_seen__lt=cutoff,
    ).update(disconnected_at=at or timezone.now())
