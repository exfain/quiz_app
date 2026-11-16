"""Participant capabilities scoped to one quiz, participant and hub."""
from django.core import signing

SALT = 'blackjack.player.v1'


def issue_player_token(participant):
    return signing.dumps({'quiz': participant.quiz_id, 'participant': participant.pk,
                         'hub': participant.hub_session_code or ''}, salt=SALT)


def valid_player_token(token, participant):
    try:
        identity = signing.loads(token or '', salt=SALT)
    except (signing.BadSignature, TypeError, ValueError):
        return False
    return identity == {'quiz': participant.quiz_id, 'participant': participant.pk,
                        'hub': participant.hub_session_code or ''}
