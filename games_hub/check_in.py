from django.db import transaction
from django.utils import timezone

from .models import HubParticipant, HubSession


def _serialize_dt(value):
    return value.isoformat() if value else None


def _serialize_participant(participant, session=None):
    joined_after_completed_check_in = bool(
        session
        and session.check_in_completed_at
        and participant.joined_at
        and participant.joined_at > session.check_in_completed_at
    )
    if participant.check_in_excluded_by_host:
        official_status = 'excluded'
    elif session and session.check_in_completed:
        if participant.scoring_eligible:
            official_status = 'official'
        elif joined_after_completed_check_in:
            official_status = 'late'
        else:
            official_status = 'not_official'
    elif participant.checked_in_at:
        official_status = 'checked_in'
    else:
        official_status = 'pending'

    return {
        'id': participant.id,
        'nickname': participant.nickname,
        'is_active': participant.is_active,
        'last_seen': _serialize_dt(participant.last_seen),
        'left_permanently_at': _serialize_dt(participant.left_permanently_at),
        'checked_in': participant.checked_in_at is not None,
        'checked_in_at': _serialize_dt(participant.checked_in_at),
        'scoring_eligible': participant.scoring_eligible,
        'excluded_by_host': participant.check_in_excluded_by_host,
        'joined_after_completed_check_in': joined_after_completed_check_in,
        'official_status': official_status,
    }


def get_check_in_state(session):
    participants = list(session.participants.order_by('joined_at', 'nickname'))
    checked_in_count = sum(
        1
        for participant in participants
        if participant.checked_in_at and not participant.check_in_excluded_by_host
    )
    eligible_count = sum(
        1
        for participant in participants
        if participant.scoring_eligible and not participant.check_in_excluded_by_host
    )
    return {
        'check_in': {
            'status': session.check_in_status,
            'started_at': _serialize_dt(session.check_in_started_at),
            'completed_at': _serialize_dt(session.check_in_completed_at),
            'locked_participant_count': session.locked_participant_count,
            'locked': session.check_in_locked,
        },
        'participants': [_serialize_participant(participant, session=session) for participant in participants],
        'counts': {
            'total_participants': len(participants),
            'checked_in': checked_in_count,
            'eligible': eligible_count,
            'not_checked_in': len([
                participant
                for participant in participants
                if not participant.checked_in_at and not participant.check_in_excluded_by_host
            ]),
            'excluded': len([
                participant
                for participant in participants
                if participant.check_in_excluded_by_host
            ]),
        },
    }


def _assert_check_in_editable(session):
    if session.check_in_locked:
        return 'Der Check-in kann nach Start des ersten Spiels nicht mehr geändert werden.'
    if session.ended_at:
        return 'Der Check-in kann für eine beendete Session nicht geändert werden.'
    return None


@transaction.atomic
def start_session_check_in(session):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    error = _assert_check_in_editable(session)
    if error:
        return {'success': False, 'error': error}
    if session.check_in_status == HubSession.CHECK_IN_COMPLETED:
        return {'success': False, 'error': 'Der Check-in ist bereits abgeschlossen.'}

    update_fields = ['check_in_status', 'check_in_started_at', 'check_in_completed_at', 'locked_participant_count']
    if session.check_in_status == HubSession.CHECK_IN_NOT_STARTED:
        session.participants.update(
            checked_in_at=None,
            scoring_eligible=False,
            check_in_excluded_by_host=False,
        )
    session.check_in_status = HubSession.CHECK_IN_OPEN
    if not session.check_in_started_at:
        session.check_in_started_at = timezone.now()
    session.check_in_completed_at = None
    session.locked_participant_count = None
    session.save(update_fields=update_fields + ['updated_at'])
    return {'success': True, **get_check_in_state(session)}


@transaction.atomic
def participant_check_in(session, nickname):
    nickname = (nickname or '').strip()
    if not nickname:
        return {'success': False, 'error': 'Teilnehmername fehlt.'}

    session = HubSession.objects.select_for_update().get(pk=session.pk)
    if session.check_in_status != HubSession.CHECK_IN_OPEN:
        return {'success': False, 'error': 'Der Check-in ist aktuell nicht geöffnet.'}

    participant, _ = HubParticipant.objects.select_for_update().get_or_create(
        session=session,
        nickname=nickname,
    )
    if participant.check_in_excluded_by_host:
        return {'success': False, 'error': 'Dieser Teilnehmer ist vom Check-in ausgeschlossen.'}

    now = timezone.now()
    participant.checked_in_at = participant.checked_in_at or now
    participant.scoring_eligible = True
    participant.is_active = True
    participant.last_seen = now
    participant.save(update_fields=[
        'checked_in_at',
        'scoring_eligible',
        'is_active',
        'last_seen',
        'updated_at',
    ])
    return {'success': True, **get_check_in_state(session)}


@transaction.atomic
def set_participant_check_in_state(session, participant_id=None, nickname=None, checked_in=None, excluded_by_host=None):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    error = _assert_check_in_editable(session)
    if error:
        return {'success': False, 'error': error}
    if session.check_in_status == HubSession.CHECK_IN_COMPLETED:
        return {'success': False, 'error': 'Der Check-in ist bereits abgeschlossen.'}

    qs = HubParticipant.objects.select_for_update().filter(session=session)
    if participant_id:
        participant = qs.filter(id=participant_id).first()
    else:
        participant = qs.filter(nickname=(nickname or '').strip()).first()
    if not participant:
        return {'success': False, 'error': 'Teilnehmer nicht gefunden.'}

    update_fields = []
    if excluded_by_host is not None:
        participant.check_in_excluded_by_host = bool(excluded_by_host)
        update_fields.append('check_in_excluded_by_host')
        if participant.check_in_excluded_by_host:
            participant.checked_in_at = None
            participant.scoring_eligible = False
            update_fields.extend(['checked_in_at', 'scoring_eligible'])

    if checked_in is not None and not participant.check_in_excluded_by_host:
        if checked_in:
            participant.checked_in_at = participant.checked_in_at or timezone.now()
            participant.scoring_eligible = True
        else:
            participant.checked_in_at = None
            participant.scoring_eligible = False
        update_fields.extend(['checked_in_at', 'scoring_eligible'])

    if update_fields:
        participant.save(update_fields=list(set(update_fields + ['updated_at'])))
    return {'success': True, **get_check_in_state(session)}


@transaction.atomic
def complete_session_check_in(session, allow_empty=False):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    error = _assert_check_in_editable(session)
    if error:
        return {'success': False, 'error': error}
    if session.check_in_status != HubSession.CHECK_IN_OPEN:
        return {'success': False, 'error': 'Der Check-in ist nicht geöffnet.'}

    eligible_qs = session.participants.select_for_update().filter(
        checked_in_at__isnull=False,
        check_in_excluded_by_host=False,
    )
    eligible_count = eligible_qs.count()
    if eligible_count == 0 and not allow_empty:
        return {'success': False, 'error': 'Es ist noch kein Teilnehmer eingecheckt.'}

    session.participants.select_for_update().update(scoring_eligible=False)
    eligible_qs.update(scoring_eligible=True)

    now = timezone.now()
    session.check_in_status = HubSession.CHECK_IN_COMPLETED
    session.check_in_completed_at = now
    session.locked_participant_count = eligible_count
    session.save(update_fields=[
        'check_in_status',
        'check_in_completed_at',
        'locked_participant_count',
        'updated_at',
    ])
    return {'success': True, **get_check_in_state(session)}


@transaction.atomic
def reset_session_check_in(session):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    error = _assert_check_in_editable(session)
    if error:
        return {'success': False, 'error': error}

    session.participants.select_for_update().update(
        checked_in_at=None,
        scoring_eligible=False,
        check_in_excluded_by_host=False,
    )
    session.check_in_status = HubSession.CHECK_IN_NOT_STARTED
    session.check_in_started_at = None
    session.check_in_completed_at = None
    session.locked_participant_count = None
    session.save(update_fields=[
        'check_in_status',
        'check_in_started_at',
        'check_in_completed_at',
        'locked_participant_count',
        'updated_at',
    ])
    return {'success': True, **get_check_in_state(session)}
