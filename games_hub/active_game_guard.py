from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from .models import HubGameParticipantSnapshot, HubGameStep, HubSession


CHECK_IN_REQUIRED_MESSAGE = (
    'Bitte schließe zuerst den Teilnehmer-Check-in ab, '
    'damit die Teilnehmerzahl für die Sessionwertung fixiert wird.'
)


def get_game_model_map():
    from QuizGame.models import Quiz as QuizGameModel
    from Assign.models import AssignQuiz
    from Estimation.models import EstimationQuiz
    from where_is_this.models import WhereQuiz
    from who_is_lying.models import WhoQuiz
    from who_is_that.models import WhoThatQuiz
    from black_jack_quiz.models import BlackJackQuiz
    from clue_rush.models import ClueRushGame
    from sorting_ladder.models import SortingLadderGame
    from wer_weiss_mehr.models import WerWeissMehrGame

    return {
        'quiz': QuizGameModel,
        'assign': AssignQuiz,
        'estimation': EstimationQuiz,
        'where': WhereQuiz,
        'who': WhoQuiz,
        'who_that': WhoThatQuiz,
        'blackjack': BlackJackQuiz,
        'clue_rush': ClueRushGame,
        'sorting_ladder': SortingLadderGame,
        'wer_weiss_mehr': WerWeissMehrGame,
    }


def _get_relevant_step(game_key: str, room_code: str) -> HubGameStep | None:
    qs = HubGameStep.objects.select_related('session').filter(game_key=game_key, room_code=room_code)
    active = qs.filter(session__is_active=True, session__ended_at__isnull=True).order_by('-id').first()
    if active:
        return active
    available = qs.filter(session__ended_at__isnull=True).order_by('-id').first()
    return available or qs.order_by('-id').first()


def _serialize_game(step: HubGameStep, game) -> dict[str, Any]:
    title = getattr(game, 'title', None) or step.title or step.get_game_key_display()
    return {
        'game_key': step.game_key,
        'room_code': step.room_code,
        'title': title,
        'display_name': title,
        'type_name': step.get_game_key_display(),
    }


def is_game_routable_for_hub_auto_redirect(game) -> bool:
    """Only route participants to games that have actually been started."""
    if not game or getattr(game, 'status', None) != 'active':
        return False

    if hasattr(game, 'started_at') and getattr(game, 'started_at', None) is None:
        return False

    return True


def _set_game_status(game, status: str):
    update_fields = ['status']
    game.status = status
    if status == 'completed' and hasattr(game, 'ended_at'):
        game.ended_at = timezone.now()
        update_fields.append('ended_at')
    game.save(update_fields=update_fields)


def _activate_target_game(game_key: str, game):
    if game_key == 'wer_weiss_mehr' and hasattr(game, 'start_quiz'):
        game.start_quiz()
        return
    _set_game_status(game, 'active')


def _end_game_cleanly(game):
    session = getattr(game, 'session', None)
    if session:
        if hasattr(session, 'end_current_question'):
            session.end_current_question()
        elif hasattr(session, 'end_round'):
            session.end_round()

    if hasattr(game, 'end_quiz'):
        game.end_quiz()
    else:
        _set_game_status(game, 'completed')


def _validate_first_game_check_in(session: HubSession) -> dict[str, Any] | None:
    """Before the first game starts, the official scoring pool must be fixed."""
    if session.has_started_game():
        return None

    if session.check_in_status != HubSession.CHECK_IN_COMPLETED:
        return {
            'success': False,
            'check_in_required': True,
            'check_in_status': session.check_in_status,
            'locked_participant_count': session.locked_participant_count,
            'error': CHECK_IN_REQUIRED_MESSAGE,
        }

    if not session.locked_participant_count:
        return {
            'success': False,
            'check_in_required': True,
            'check_in_status': session.check_in_status,
            'locked_participant_count': session.locked_participant_count,
            'error': 'Der Check-in ist abgeschlossen, aber es ist kein offizieller Teilnehmer eingecheckt.',
        }

    return None


def _iter_session_games(session: HubSession):
    model_map = get_game_model_map()
    for step in session.steps.exclude(room_code='').order_by('order'):
        model = model_map.get(step.game_key)
        if not model:
            continue
        game = model.objects.filter(room_code=step.room_code).first()
        if not game:
            continue
        yield step, game


def resolve_session_game_activation(
    session_code: str,
    target_game_key: str,
    target_room_code: str,
    action: str | None = None,
    check_only: bool = False,
) -> dict[str, Any]:
    if not session_code:
        return {'success': True}

    with transaction.atomic():
        session = HubSession.objects.select_for_update().filter(code=session_code).first()
        if not session or session.ended_at:
            return {'success': False, 'error': 'Session nicht gefunden oder bereits beendet.'}
        if not session.is_active:
            return {'success': False, 'error': 'Diese Session ist aktuell inaktiv. Aktiviere zuerst die Session.'}

        target_step = session.steps.filter(game_key=target_game_key, room_code=target_room_code).first()
        if not target_step:
            return {'success': True}

        check_in_error = _validate_first_game_check_in(session)
        if check_in_error:
            return check_in_error

        target_game = None
        conflicts: list[tuple[HubGameStep, Any]] = []
        for step, game in _iter_session_games(session):
            if step.game_key == target_game_key and step.room_code == target_room_code:
                target_game = game
                continue
            if getattr(game, 'status', None) == 'active':
                conflicts.append((step, game))

        if target_game and getattr(target_game, 'status', None) == 'completed':
            return {'success': False, 'error': 'Dieses Spiel ist bereits beendet.'}

        if conflicts and action not in ('end', 'inactive'):
            active_step, active_game = conflicts[0]
            return {
                'success': False,
                'conflict': True,
                'active_game': _serialize_game(active_step, active_game),
            }

        if conflicts:
            primary_step, primary_game = conflicts[0]
            if action == 'end':
                _end_game_cleanly(primary_game)
            else:
                _set_game_status(primary_game, 'inactive')

            for extra_step, extra_game in conflicts[1:]:
                _set_game_status(extra_game, 'inactive')

        if check_only:
            return {
                'success': True,
                'active_game': _serialize_game(target_step, target_game) if target_game else None,
            }

        target_needs_activation = (
            target_game
            and (
                getattr(target_game, 'status', None) != 'active'
                or (
                    target_game_key == 'wer_weiss_mehr'
                    and getattr(target_game, 'started_at', None) is None
                )
            )
        )
        if target_game:
            HubGameParticipantSnapshot.create_for_step(target_step)
        if target_needs_activation:
            _activate_target_game(target_game_key, target_game)

        return {
            'success': True,
            'active_game': _serialize_game(target_step, target_game) if target_game else None,
        }


def resolve_session_game_activation_for_room(
    game_key: str,
    room_code: str,
    action: str | None = None,
    check_only: bool = False,
    session_code: str | None = None,
) -> dict[str, Any]:
    if session_code:
        return resolve_session_game_activation(
            session_code,
            game_key,
            room_code,
            action=action,
            check_only=check_only,
        )
    step = _get_relevant_step(game_key, room_code)
    if not step:
        return {'success': True}
    return resolve_session_game_activation(
        step.session.code,
        game_key,
        room_code,
        action=action,
        check_only=check_only,
    )
