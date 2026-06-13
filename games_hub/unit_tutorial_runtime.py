from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import HubGameUnitTutorialRuntime, HubSession
from .tutorial_runtime import get_relevant_game_step


TUTORIAL_QUESTION_MISSING_MESSAGE = 'F\u00fcr dieses Spiel wurde keine Tutorialfrage festgelegt.'
TUTORIAL_SET_MISSING_MESSAGE = 'F\u00fcr dieses Spiel wurde kein Tutorialset festgelegt.'


def _get_game(game_key: str, room_code: str):
    model = HubSession._get_game_model_map().get(game_key)
    if not model:
        return None
    return model.objects.filter(room_code=room_code).first()


def _get_configured_tutorial_question_ids(game_key: str, game):
    if not game:
        return []
    if game_key == 'blackjack' and hasattr(game, 'get_tutorial_set_question_ids'):
        return list(game.get_tutorial_set_question_ids(active_only=True))
    question_id = getattr(game, 'tutorial_question_id', None)
    return [question_id] if question_id else []


def _get_configured_tutorial_question_id(game_key: str, game):
    question_ids = _get_configured_tutorial_question_ids(game_key, game)
    return question_ids[0] if question_ids else None


def get_scorebox_excluded_tutorial_question_ids(game_key: str, room_code: str, session_code: str | None = None):
    game = _get_game(game_key, room_code)
    excluded_ids = {
        int(question_id)
        for question_id in _get_configured_tutorial_question_ids(game_key, game)
        if question_id
    }
    state = get_unit_tutorial_state(game_key, room_code, session_code)
    if state.get('requested') and state.get('tutorial_question_id'):
        try:
            excluded_ids.add(int(state['tutorial_question_id']))
        except (TypeError, ValueError):
            pass
    return excluded_ids


def validate_unit_tutorial_request(game_key: str, room_code: str, play_tutorial: bool):
    if not play_tutorial:
        return {'success': True}

    game = _get_game(game_key, room_code)
    if _get_configured_tutorial_question_id(game_key, game):
        return {'success': True}

    return {
        'success': False,
        'type': 'tutorial_question_missing',
        'message': TUTORIAL_SET_MISSING_MESSAGE if game_key == 'blackjack' else TUTORIAL_QUESTION_MISSING_MESSAGE,
    }


@transaction.atomic
def prepare_unit_tutorial_runtime(
    game_key: str,
    room_code: str,
    session_code: str | None,
    play_tutorial: bool,
    *,
    validate: bool = True,
):
    if validate:
        validation = validate_unit_tutorial_request(game_key, room_code, play_tutorial)
        if not validation.get('success'):
            return validation

    step = get_relevant_game_step(game_key, room_code, session_code)
    game = _get_game(game_key, room_code)
    tutorial_question_id = _get_configured_tutorial_question_id(game_key, game)
    requested = bool(play_tutorial and tutorial_question_id)
    now = timezone.now() if requested else None

    if not step:
        return {
            'success': True,
            'requested': requested,
            'tutorial_question_id': tutorial_question_id if requested else None,
        }

    runtime, _ = HubGameUnitTutorialRuntime.objects.select_for_update().get_or_create(
        game_step=step,
        defaults={'session': step.session},
    )
    runtime.session = step.session
    runtime.requested = requested
    runtime.tutorial_question_id = tutorial_question_id if requested else None
    runtime.tutorial_has_been_played = False
    runtime.current_unit_is_tutorial = False
    runtime.requested_at = now
    runtime.started_at = None
    runtime.completed_at = None
    runtime.save(update_fields=[
        'session',
        'requested',
        'tutorial_question_id',
        'tutorial_has_been_played',
        'current_unit_is_tutorial',
        'requested_at',
        'started_at',
        'completed_at',
    ])

    return {
        'success': True,
        'requested': runtime.requested,
        'tutorial_question_id': runtime.tutorial_question_id,
        'tutorial_has_been_played': runtime.tutorial_has_been_played,
        'current_unit_is_tutorial': runtime.current_unit_is_tutorial,
    }


def _serialize_runtime(runtime):
    if not runtime:
        return {
            'requested': False,
            'tutorial_question_id': None,
            'tutorial_has_been_played': False,
            'current_unit_is_tutorial': False,
        }
    return {
        'requested': runtime.requested,
        'tutorial_question_id': runtime.tutorial_question_id,
        'tutorial_has_been_played': runtime.tutorial_has_been_played,
        'current_unit_is_tutorial': runtime.current_unit_is_tutorial,
    }


def get_unit_tutorial_state(game_key: str, room_code: str, session_code: str | None):
    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return _serialize_runtime(None)
    runtime = HubGameUnitTutorialRuntime.objects.filter(game_step=step).first()
    return _serialize_runtime(runtime)


@transaction.atomic
def start_unit_tutorial_if_needed(game_key: str, room_code: str, session_code: str | None):
    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return {'is_tutorial_round': False, 'tutorial_question_id': None}

    runtime = (
        HubGameUnitTutorialRuntime.objects
        .select_for_update()
        .filter(game_step=step, requested=True, tutorial_has_been_played=False)
        .first()
    )
    if not runtime or not runtime.tutorial_question_id:
        return {'is_tutorial_round': False, 'tutorial_question_id': None}

    runtime.current_unit_is_tutorial = True
    runtime.started_at = runtime.started_at or timezone.now()
    runtime.save(update_fields=['current_unit_is_tutorial', 'started_at'])
    return {
        'is_tutorial_round': True,
        'tutorial_question_id': runtime.tutorial_question_id,
    }


@transaction.atomic
def finish_current_unit_tutorial(game_key: str, room_code: str, session_code: str | None):
    step = get_relevant_game_step(game_key, room_code, session_code)
    if not step:
        return {'is_tutorial_round': False, 'tutorial_question_id': None}

    runtime = (
        HubGameUnitTutorialRuntime.objects
        .select_for_update()
        .filter(game_step=step, current_unit_is_tutorial=True)
        .first()
    )
    if not runtime:
        return {'is_tutorial_round': False, 'tutorial_question_id': None}

    runtime.current_unit_is_tutorial = False
    runtime.tutorial_has_been_played = True
    runtime.completed_at = runtime.completed_at or timezone.now()
    runtime.save(update_fields=[
        'current_unit_is_tutorial',
        'tutorial_has_been_played',
        'completed_at',
    ])
    return {
        'is_tutorial_round': True,
        'tutorial_question_id': runtime.tutorial_question_id,
    }


def is_unit_tutorial_question(game_key: str, room_code: str, session_code: str | None, question_id):
    if question_id is None:
        return False
    state = get_unit_tutorial_state(game_key, room_code, session_code)
    if not state.get('requested'):
        return False
    if str(state.get('tutorial_question_id') or '') != str(question_id):
        return False
    return bool(state.get('current_unit_is_tutorial') or state.get('tutorial_has_been_played'))


def is_current_unit_tutorial_question(game_key: str, room_code: str, session_code: str | None, question_id):
    if question_id is None:
        return False
    state = get_unit_tutorial_state(game_key, room_code, session_code)
    if not state.get('current_unit_is_tutorial'):
        return False
    return str(state.get('tutorial_question_id') or '') == str(question_id)
