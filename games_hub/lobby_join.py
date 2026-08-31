import time

from django.core import signing
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from .models import HubParticipant, HubSession


MIN_NICKNAME_LENGTH = 3
REJOIN_TOKEN_SALT = 'games_hub.lobby_rejoin.v1'


def normalize_nickname(value):
    return str(value or '').strip()


def nickname_validation_error(value):
    nickname = normalize_nickname(value)
    if not nickname:
        return 'name_required', 'Name eingeben'
    if len(nickname) < MIN_NICKNAME_LENGTH:
        return 'name_too_short', 'Der Name ist zu kurz.'
    if len(nickname) > HubParticipant._meta.get_field('nickname').max_length:
        return 'name_too_long', 'Der Name ist zu lang.'
    return None


def issue_rejoin_token(participant):
    return signing.dumps(
        {
            'session_id': participant.session_id,
            'participant_id': participant.pk,
        },
        salt=REJOIN_TOKEN_SALT,
        compress=True,
    )


def _token_matches_participant(token, participant):
    if not token:
        return False
    try:
        payload = signing.loads(token, salt=REJOIN_TOKEN_SALT)
    except signing.BadSignature:
        return False
    return (
        payload.get('session_id') == participant.session_id
        and payload.get('participant_id') == participant.pk
    )


def check_nickname_availability(session_code, nickname, rejoin_token=''):
    nickname = normalize_nickname(nickname)
    validation_error = nickname_validation_error(nickname)
    if validation_error:
        code, message = validation_error
        return {'status': 'invalid', 'code': code, 'message': message, 'nickname': nickname}

    session = HubSession.objects.filter(code=session_code).first()
    if not session or session.ended_at:
        return {
            'status': 'invalid',
            'code': 'session_unavailable',
            'message': 'Diese Session ist nicht verfügbar.',
            'nickname': nickname,
        }

    participant = (
        HubParticipant.objects
        .filter(session=session, nickname__iexact=nickname)
        .order_by('id')
        .first()
    )
    if not participant:
        return {'status': 'available', 'nickname': nickname}
    if _token_matches_participant(rejoin_token, participant):
        return {'status': 'rejoin', 'nickname': participant.nickname}
    return {
        'status': 'taken',
        'code': 'nickname_taken',
        'message': 'Dieser Name ist bereits vergeben.',
        'nickname': participant.nickname,
    }


def _join_once(session_code, nickname, rejoin_token):
    with transaction.atomic():
        session = (
            HubSession.objects
            .select_for_update()
            .filter(code=session_code)
            .first()
        )
        if not session or session.ended_at:
            return {
                'success': False,
                'code': 'session_unavailable',
                'message': 'Diese Session ist nicht verfügbar.',
            }

        participant = (
            HubParticipant.objects
            .filter(session=session, nickname__iexact=nickname)
            .order_by('id')
            .first()
        )
        if participant:
            if not _token_matches_participant(rejoin_token, participant):
                return {
                    'success': False,
                    'code': 'nickname_taken',
                    'message': 'Dieser Name ist bereits vergeben.',
                }
            HubParticipant.objects.filter(pk=participant.pk).update(
                is_active=True,
                last_seen=timezone.now(),
            )
            participant.is_active = True
            return {
                'success': True,
                'participant_id': participant.pk,
                'nickname': participant.nickname,
                'rejoined': True,
                'rejoin_token': issue_rejoin_token(participant),
            }

        participant = HubParticipant.objects.create(
            session=session,
            nickname=nickname,
            is_active=True,
            last_seen=timezone.now(),
        )
        return {
            'success': True,
            'participant_id': participant.pk,
            'nickname': participant.nickname,
            'rejoined': False,
            'rejoin_token': issue_rejoin_token(participant),
        }


def join_lobby_participant(session_code, nickname, rejoin_token=''):
    nickname = normalize_nickname(nickname)
    validation_error = nickname_validation_error(nickname)
    if validation_error:
        code, message = validation_error
        return {'success': False, 'code': code, 'message': message}

    delays = (0.02, 0.05, 0.1, 0.2)
    for attempt in range(len(delays) + 1):
        try:
            return _join_once(session_code, nickname, rejoin_token)
        except IntegrityError:
            availability = check_nickname_availability(
                session_code,
                nickname,
                rejoin_token,
            )
            if availability['status'] == 'rejoin':
                continue
            return {
                'success': False,
                'code': 'nickname_taken',
                'message': 'Dieser Name ist bereits vergeben.',
            }
        except OperationalError as exc:
            is_sqlite_lock = connection.vendor == 'sqlite' and 'locked' in str(exc).lower()
            if not is_sqlite_lock or attempt == len(delays):
                raise
            time.sleep(delays[attempt])

    return {
        'success': False,
        'code': 'join_failed',
        'message': 'Der Beitritt ist derzeit nicht möglich.',
    }
