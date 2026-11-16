"""Session-scoped text grading; numerical values never receive fuzzy credit."""
import re
from rapidfuzz.fuzz import ratio


def default_text_match_threshold():
    from admin_dashboard.models import DashboardSettings
    return DashboardSettings.load().text_match_threshold


def validate_threshold(value):
    if isinstance(value, bool):
        raise ValueError('Die Textübereinstimmung muss eine ganze Zahl zwischen 65 und 100 sein.')
    try:
        result = int(value)
        if str(result) != str(value).strip() or not 65 <= result <= 100:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Die Textübereinstimmung muss eine ganze Zahl zwischen 65 und 100 sein.')
    return result


def threshold_for(participant):
    from .models import HubSession
    code = participant.hub_session_code
    if not code:
        return 100  # Standalone games retain their existing exact-answer policy.
    return HubSession.objects.filter(code=code).values_list('text_match_threshold', flat=True).first() or 100


def normalize(value):
    return ' '.join(str(value or '').casefold().split())


NUMBER = re.compile(r'(?<!\w)[+-]?\d+(?:[.,]\d+)*|\d+(?:[.,]\d+)*')


def similarity(value, expected, *, normalizer=normalize):
    raw_value, raw_expected = str(value or ''), str(expected or '')
    left, right = normalizer(raw_value), normalizer(raw_expected)
    if not left or not right or NUMBER.findall(raw_value) != NUMBER.findall(raw_expected):
        return 0.0
    if left == right:
        return 100.0
    if not any(char.isalpha() for char in raw_value) or not any(char.isalpha() for char in raw_expected):
        return 0.0
    return ratio(left, right)


def matches(value, expected, *, threshold=100, normalizer=normalize):
    return similarity(value, expected, normalizer=normalizer) >= threshold


def settings_locked(session):
    if session.ended_at:
        return True
    from .active_game_guard import _iter_session_games
    from .session_results import step_status
    return any(step_status(step, game) == 'active' or
               (step_status(step, game) == 'inactive' and game.started_at)
               for step, game in _iter_session_games(session))
