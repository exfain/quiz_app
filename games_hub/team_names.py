"""The same character policy for lobby and direct game joins."""
import json
import re
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import render


ALLOWED_TEAM_NAME = re.compile(r'[A-Za-zÄÖÜäöüß0-9 _-]+\Z')
ALLOWED_CHARACTERS_MESSAGE = (
    'Erlaubt sind Buchstaben A–Z und a–z, ÄÖÜäöüß, Ziffern 0–9, '
    'Leerzeichen, Bindestrich (-) und Unterstrich (_).'
)


def character_error(name):
    if name and not ALLOWED_TEAM_NAME.fullmatch(name):
        return ALLOWED_CHARACTERS_MESSAGE
    return None


def validate_join_name(game_key, template):
    """Reject new invalid identities while preserving existing saved teams."""
    def decorate(view):
        @wraps(view)
        def checked(request, *args, **kwargs):
            if request.method == 'POST':
                is_json = request.content_type == 'application/json'
                try:
                    data = json.loads(request.body or '{}') if is_json else request.POST
                except (ValueError, UnicodeError):
                    data = {}
                if isinstance(data, dict) or hasattr(data, 'get'):
                    name = str(data.get('participant_name') or data.get('name') or '').strip(' ')
                    error = character_error(name)
                    if error:
                        from .models import HubParticipant, HubSession
                        code = data.get('hub_session') or data.get('hub_session_code') or ''
                        model = HubSession._get_game_model_map()[game_key]
                        game = model.objects.filter(room_code=data.get('room_code')).first()
                        existing = bool(game and game.participants.filter(
                            name=name, hub_session_code=code or None,
                        ).exists())
                        # Some game models use '' rather than NULL standalone.
                        existing = existing or bool(game and game.participants.filter(
                            name=name, hub_session_code=code or '',
                        ).exists())
                        legacy_hub = bool(code and HubParticipant.objects.filter(
                            session__code=code, nickname=name,
                        ).exists())
                        if not existing and not legacy_hub:
                            if is_json:
                                return JsonResponse({'success': False, 'code': 'invalid_name_characters', 'error': error}, status=400)
                            return render(request, template, {'error': error}, status=400)
            return view(request, *args, **kwargs)
        return checked
    return decorate
