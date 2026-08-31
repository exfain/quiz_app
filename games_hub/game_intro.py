from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import HubGameStep


GAME_INTRO_DURATION_MS = 8000

GAME_INTRO_TITLES = {
    'quiz': 'QUICK QUIZ',
    'assign': 'ASSIGN',
    'estimation': 'ESTIMATION',
    'where': 'WHERE IS THIS',
    'who': 'WHO IS LYING',
    'who_that': 'WHO IS THAT',
    'blackjack': 'BLACK JACK',
    'sorting_ladder': 'SORTING LADDER',
    'clue_rush': 'CLUE RUSH',
    'wer_weiss_mehr': 'WER WEISS MEHR',
    'buzzer': 'BUZZER',
    'host_points': 'HOST-PUNKTEVERGABE',
    'wann_war_das': 'WANN WAR DAS',
}


def serialize_game_intro(step: HubGameStep, *, server_now=None) -> dict:
    server_now = server_now or timezone.now()
    started_at = step.intro_started_at
    ends_at = step.intro_ends_at
    active = bool(started_at and ends_at and started_at <= server_now < ends_at)
    game_title = (step.title or '').strip() or GAME_INTRO_TITLES.get(
        step.game_key,
        step.get_game_key_display(),
    )
    return {
        'intro_active': active,
        'intro_started_at': started_at.isoformat() if started_at else None,
        'intro_ends_at': ends_at.isoformat() if ends_at else None,
        'server_now': server_now.isoformat(),
        'state_revision': int(step.intro_state_revision or 0),
        'game_number': int(step.order) + 1,
        'game_title': game_title.upper(),
        'game_key': step.game_key,
        'game_instance_id': f'{step.game_key}:{step.room_code}:{step.pk}',
    }


def serialize_game_step(step: HubGameStep, *, server_now=None) -> dict:
    return {
        'index': step.order,
        'order': step.order,
        'game_key': step.game_key,
        'room_code': step.room_code,
        'title': step.title,
        'intro': serialize_game_intro(step, server_now=server_now),
    }


def start_game_intro(session_code: str, game_key: str, room_code: str) -> dict | None:
    server_now = timezone.now()
    step = (
        HubGameStep.objects
        .filter(
            session__code=session_code,
            game_key=game_key,
            room_code=room_code,
        )
        .order_by('order', 'pk')
        .first()
    )
    if not step:
        return None

    HubGameStep.objects.filter(
        pk=step.pk,
        intro_started_at__isnull=True,
    ).update(
        intro_started_at=server_now,
        intro_ends_at=server_now + timedelta(milliseconds=GAME_INTRO_DURATION_MS),
        intro_state_revision=F('intro_state_revision') + 1,
    )
    step.refresh_from_db(
        fields=['intro_started_at', 'intro_ends_at', 'intro_state_revision']
    )
    return serialize_game_step(step, server_now=server_now)


def get_game_step(session_code: str, game_key: str, room_code: str) -> dict | None:
    step = (
        HubGameStep.objects
        .filter(
            session__code=session_code,
            game_key=game_key,
            room_code=room_code,
        )
        .order_by('order', 'pk')
        .first()
    )
    return serialize_game_step(step) if step else None
