"""Bind participant transports to the player page that opened them."""
from django.core import signing

SALT = 'wer_weiss_mehr.player.v1'


def issue_player_token(participant):
    return signing.dumps({'quiz': participant.quiz_id, 'participant': participant.pk,
        'hub': participant.hub_session_code or ''}, salt=SALT, compress=True)


def valid_player_token(token, participant):
    try:
        identity = signing.loads(token or '', salt=SALT)
    except (signing.BadSignature, TypeError, ValueError):
        return False
    return identity == {'quiz': participant.quiz_id, 'participant': participant.pk,
                        'hub': participant.hub_session_code or ''}
