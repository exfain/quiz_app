from django.db import transaction
from django.utils import timezone

from .models import HubParticipant, HubReadinessCheck, HubReadinessParticipant, HubSession


def _serialize_dt(value):
    return value.isoformat() if value else None


def _eligible_participants(session):
    qs = HubParticipant.objects.filter(session=session, is_active=True)
    if session.check_in_completed:
        qs = qs.filter(scoring_eligible=True, check_in_excluded_by_host=False)
    else:
        qs = qs.filter(check_in_excluded_by_host=False)
    return qs.order_by('joined_at', 'nickname')


def _serialize_check(check):
    if not check:
        return {
            'readiness_check': {'active': False, 'id': None},
            'readiness_participants': [],
            'readiness_counts': {'total': 0, 'ready': 0, 'pending': 0},
        }

    rows = list(
        check.participant_states.select_related('participant')
        .order_by('ready_at', 'participant__joined_at', 'participant__nickname')
    )
    participants = [
        {
            'id': row.participant_id,
            'nickname': row.participant.nickname,
            'ready': row.ready_at is not None,
            'ready_at': _serialize_dt(row.ready_at),
        }
        for row in rows
    ]
    ready_count = sum(1 for participant in participants if participant['ready'])
    return {
        'readiness_check': {
            'active': check.active,
            'id': check.id,
            'started_at': _serialize_dt(check.started_at),
            'ended_at': _serialize_dt(check.ended_at),
        },
        'readiness_participants': participants,
        'readiness_counts': {
            'total': len(participants),
            'ready': ready_count,
            'pending': len(participants) - ready_count,
        },
    }


def get_readiness_state(session):
    check = (
        HubReadinessCheck.objects
        .filter(session=session, active=True, ended_at__isnull=True)
        .prefetch_related('participant_states__participant')
        .order_by('-started_at')
        .first()
    )
    return _serialize_check(check)


@transaction.atomic
def start_readiness_check(session):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    from .active_game_guard import _iter_session_games
    from .lobby_return_flow import get_session_lobby_presence
    from .session_results import step_status

    if session.ended_at:
        return {'success': False, 'error': 'Die Session ist bereits beendet.'}
    if session.session_type == 'duel' and (
        session.duel_instances.filter(match_status__in=['not_started', 'in_progress']).exists()
        or session.duel_instances.filter(units__estimation_round_state__phase='show_result').exists()
    ):
        return {'success': False, 'error': 'Schließe zuerst das laufende Duell und dessen Präsentation ab.'}
    if any(step_status(step, game) == 'active' for step, game in _iter_session_games(session)):
        return {
            'success': False,
            'error': 'Der Bereitschaftscheck ist nur zwischen Spielen möglich. Beende zuerst das laufende Spiel und hole alle Teilnehmer in die Lobby zurück.',
        }
    if not get_session_lobby_presence(session.code)['all_in_lobby']:
        return {
            'success': False,
            'error': 'Noch nicht alle Teilnehmer sind in der Lobby. Hole sie zuerst zurück, bevor du den Bereitschaftscheck startest.',
        }
    HubReadinessCheck.objects.select_for_update().filter(
        session=session,
        active=True,
        ended_at__isnull=True,
    ).update(active=False, ended_at=timezone.now())

    check = HubReadinessCheck.objects.create(session=session, active=True)
    participants = list(_eligible_participants(session).select_for_update())
    HubReadinessParticipant.objects.bulk_create([
        HubReadinessParticipant(readiness_check=check, participant=participant)
        for participant in participants
    ])
    return {'success': True, **get_readiness_state(session)}


@transaction.atomic
def mark_participant_ready(session, nickname):
    nickname = (nickname or '').strip()
    if not nickname:
        return {'success': False, 'error': 'Teilnehmername fehlt.'}

    session = HubSession.objects.select_for_update().get(pk=session.pk)
    check = (
        HubReadinessCheck.objects.select_for_update()
        .filter(session=session, active=True, ended_at__isnull=True)
        .first()
    )
    if not check:
        return {'success': False, 'error': 'Es läuft kein Bereitschaftscheck.'}

    participant = HubParticipant.objects.filter(session=session, nickname=nickname).first()
    if not participant:
        return {'success': False, 'error': 'Teilnehmer nicht gefunden.'}

    state = HubReadinessParticipant.objects.select_for_update().filter(
        readiness_check=check,
        participant=participant,
    ).first()
    if not state:
        return {'success': False, 'error': 'Teilnehmer ist nicht Teil dieses Bereitschaftschecks.'}

    if not state.ready_at:
        state.ready_at = timezone.now()
        state.save(update_fields=['ready_at', 'updated_at'])
    return {'success': True, **get_readiness_state(session)}


@transaction.atomic
def end_readiness_check(session, force=False):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    check = (
        HubReadinessCheck.objects.select_for_update()
        .filter(session=session, active=True, ended_at__isnull=True)
        .first()
    )
    if not check:
        return {'success': True, **get_readiness_state(session)}

    state = _serialize_check(check)
    pending = state['readiness_counts']['pending']
    if pending and not force:
        return {
            'success': False,
            'requires_confirmation': True,
            'error': 'Noch nicht alle Teilnehmer haben ihre Bereitschaft bestätigt.',
            **state,
        }

    check.active = False
    check.ended_at = timezone.now()
    check.save(update_fields=['active', 'ended_at', 'updated_at'])
    return {'success': True, **get_readiness_state(session)}
