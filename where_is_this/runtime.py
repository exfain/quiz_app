"""One transactional live path for HTTP, host/player sockets and spectator."""
import math

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession
from .geo import web_mercator_lat_lng_to_norm, web_mercator_norm_to_lat_lng, validate_map_location
from .models import WhereAnswer, WhereParticipant, WhereQuiz, WhereSession


class ActionError(ValueError):
    pass


def pool(code):
    return {'hub_session_code': code or None}


def code_for(game):
    if game.active_hub_session_code:
        return game.active_hub_session_code
    step = HubGameStep.objects.filter(game_key='where', room_code=game.room_code,
        session__ended_at__isnull=True).order_by('-session__is_active', '-id').first()
    return step.session.code if step else ''


def locked(game):
    game = WhereQuiz.objects.select_for_update().get(pk=game.pk)
    session, _ = WhereSession.objects.get_or_create(quiz=game)
    session = WhereSession.objects.select_for_update().get(pk=session.pk)
    return game, session


def broadcast(game):
    layer = get_channel_layer()
    if layer:
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(
            f'where_{game.room_code}', {'type': 'where_updated'}))


def zones(count, width):
    try:
        if isinstance(count, bool) or float(count) != int(count):
            raise ValueError()
        count, width = int(count), float(width)
        if not 1 <= count <= 50 or not math.isfinite(width) or width <= 0 or count * width > 20040:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ActionError('Verwende 1 bis 50 Zonen mit positiver Breite in km; der Außenradius darf höchstens 20.040 km betragen.')
    return [{'min_distance_km': i * width, 'max_distance_km': (i + 1) * width,
             'points': count - i, 'label': f'Zone {i + 1}'} for i in range(count)]


def coordinates(data):
    if data.get('x_norm') is not None and data.get('y_norm') is not None:
        lat, lon = web_mercator_norm_to_lat_lng(data['x_norm'], data['y_norm'])
    else:
        lat, lon = float(data['latitude']), float(data['longitude'])
    x, y = web_mercator_lat_lng_to_norm(lat, lon)
    return {'user_latitude': lat, 'user_longitude': lon, 'x_norm': x, 'y_norm': y}


@transaction.atomic
def start(game, code=None):
    game, session = locked(game)
    code = code if code is not None else code_for(game)
    code = code or ''
    if game.started_at and game.active_hub_session_code != code and game.status in {'active', 'inactive'}:
        if HubSession.objects.filter(code=game.active_hub_session_code, ended_at__isnull=True).exists():
            raise ActionError('Dieser Spielraum wird noch von einer anderen Session verwendet.')
    if game.started_at and game.active_hub_session_code == code and game.status in {'active', 'inactive', 'completed'}:
        if game.status == 'inactive':
            return set_paused(game, False)
        if game.status == 'completed':
            raise ActionError('Dieses Spiel ist bereits beendet.')
        return False
    from games_hub.session_results import freeze_previous_results
    freeze_previous_results('where', game, code)
    if code:
        hub = HubSession.objects.get(code=code)
        for player in HubParticipant.objects.filter(session=hub, scoring_eligible=True, check_in_excluded_by_host=False):
            WhereParticipant.objects.get_or_create(quiz=game, name=player.nickname, **pool(code))
    game.status, game.started_at, game.ended_at = 'active', timezone.now(), None
    game.active_hub_session_code = code
    game.paused_at = game.current_question = game.question_start_time = None
    game.save()
    session.question_rounds, session.pending_answers = [], {}
    session.current_question_number = session.total_questions_sent = 0
    session.is_question_active, session.question_end_time = False, None
    session.save()
    from games_hub.authoritative_state import reset_question_flow
    reset_question_flow(game_key='where', room_code=game.room_code, session_code=code, mode='legacy_immediate')
    broadcast(game)
    return True


@transaction.atomic
def send_question(game, question, *, time_limit=None, tutorial=False):
    game, session = locked(game)
    if game.status != 'active' or session.is_question_active:
        raise ActionError('Beende zuerst die laufende Frage beziehungsweise starte das Spiel.')
    if game.selected_questions.exists() and not game.selected_questions.filter(pk=question.pk).exists():
        raise ActionError('Diese Frage gehört nicht zum Spiel.')
    question.full_clean()
    limit = int(time_limit or question.time_limit)
    if not 1 <= limit <= 86400:
        raise ActionError('Die Antwortzeit muss zwischen 1 und 86.400 Sekunden liegen.')
    game.current_question, game.question_start_time = question, timezone.now()
    game.save(update_fields=['current_question', 'question_start_time', 'updated_at'])
    participants = list(game.participants.filter(**pool(game.active_hub_session_code)).values_list('pk', flat=True))
    config = zones(game.zone_count, game.zone_width_km) if game.zone_count else question.get_effective_distance_zones()
    history = list(session.question_rounds)
    # Unknown legacy slots preserve numbering without inventing old answers.
    history.extend([{'question_id': None, 'revealed': True}] * max(0, session.current_question_number - len(history)))
    history.append({'question_id': question.pk, 'revealed': False, 'participants': participants,
                    'question': {'id': question.pk, 'text': question.question_text, 'question_text': question.question_text,
                        'map_type': question.map_type, 'image_url': question.image.url if question.image else None,
                        'correct_location': {'latitude': question.correct_latitude, 'longitude': question.correct_longitude},
                        'explanation': question.explanation or ''},
                    'scoring_mode': game.get_effective_scoring_mode(), 'zones': config, 'tutorial': tutorial,
                    'max_points': len(participants) if game.scoring_mode == 'rank' else max((z['points'] for z in config), default=0)})
    session.question_rounds = history
    session.current_question_number = session.total_questions_sent = len(history)
    session.is_question_active = True
    session.question_end_time = game.question_start_time + timezone.timedelta(seconds=limit)
    session.pending_answers = {}
    session.save()
    broadcast(game)
    return True


def current_answers(game, session):
    return game.where_answers.filter(round_number=session.current_question_number,
        participant__hub_session_code=game.active_hub_session_code or None)


@transaction.atomic
def save_input(game, participant, data, *, confirm=False):
    game, session = locked(game)
    if game.active_hub_session_code != (participant.hub_session_code or ''):
        raise ActionError('Diese Teilnahme gehört zu einem anderen Spieldurchlauf.')
    if game.status != 'active' or not session.is_question_active or not game.current_question_id:
        raise ActionError('Es ist keine Antwortabgabe geöffnet.')
    if str(data.get('question_id')) != str(game.current_question_id) or str(data.get('round_id')) != str(session.current_question_number):
        raise ActionError('Die Markierung gehört zu einer früheren Runde.')
    if participant.pk not in session.question_rounds[-1].get('participants', []):
        raise ActionError('Du kannst ab der nächsten Frage mitspielen.')
    now = timezone.now()
    if not session.question_end_time or now >= session.question_end_time:
        raise ActionError('Die Antwortzeit ist abgelaufen.')
    if current_answers(game, session).filter(participant=participant).exists():
        raise ActionError('Die Markierung ist bereits eingeloggt.')
    point = coordinates(data)
    map_type = session.question_rounds[-1].get('question', {}).get('map_type', game.current_question.map_type)
    validate_map_location(map_type, point['user_latitude'], point['user_longitude'])
    if confirm:
        WhereAnswer.objects.create(quiz=game, question=game.current_question, participant=participant,
            round_number=session.current_question_number, time_taken=max(0, (now - game.question_start_time).total_seconds()), **point)
    pending = dict(session.pending_answers)
    if confirm:
        pending.pop(str(participant.pk), None)
    else:
        pending[str(participant.pk)] = point
    session.pending_answers = pending
    session.save(update_fields=['pending_answers', 'updated_at'])
    broadcast(game)
    return point


@transaction.atomic
def reveal(game):
    game, session = locked(game)
    if not session.is_question_active or not game.current_question_id:
        return False
    end = min(timezone.now(), session.question_end_time or timezone.now())
    spec = session.question_rounds[-1] if session.question_rounds else {}
    for key, point in session.pending_answers.items():
        player = game.participants.filter(pk=key, **pool(game.active_hub_session_code)).first()
        if player and player.pk in spec.get('participants', []):
            WhereAnswer.objects.get_or_create(quiz=game, participant=player, question=game.current_question,
                round_number=session.current_question_number, defaults={**point,
                    'time_taken': max(0, (end - game.question_start_time).total_seconds())})
    answers = list(current_answers(game, session).select_related('participant').order_by('distance_km', 'time_taken', 'submitted_at', 'id'))
    for rank, answer in enumerate(answers, 1):
        answer.scoring_mode = spec.get('scoring_mode', game.get_effective_scoring_mode())
        answer.rank_position = rank
        zone = next((z for z in spec.get('zones', game.current_question.get_effective_distance_zones())
                     if z['min_distance_km'] <= answer.distance_km < z['max_distance_km']), None)
        answer.zone_label = zone['label'] if zone else ''
        answer.points_earned = max(0, len(spec.get('participants', answers)) - rank + 1) if answer.scoring_mode == 'rank' else (zone['points'] if zone else 0)
        if spec.get('tutorial'):
            answer.points_earned = 0
        answer.save(update_fields=['scoring_mode', 'rank_position', 'zone_label', 'points_earned', 'updated_at'])
    session.is_question_active = False
    session.pending_answers = {}
    history = list(session.question_rounds)
    if history:
        history[-1] = {**history[-1], 'revealed': True}
    session.question_rounds = history
    session.save()
    broadcast(game)
    return True


@transaction.atomic
def end_game(game, status='completed'):
    game, session = locked(game)
    if game.status == status and game.ended_at:
        return False
    reveal(game)
    game.status, game.ended_at, game.paused_at = status, timezone.now(), None
    game.save(update_fields=['status', 'ended_at', 'paused_at', 'updated_at'])
    broadcast(game)
    return True


@transaction.atomic
def set_paused(game, paused):
    game, session = locked(game)
    if paused and game.status == 'active':
        game.status, game.paused_at = 'inactive', timezone.now()
    elif not paused and game.status == 'inactive':
        delta = timezone.now() - (game.paused_at or timezone.now())
        if game.question_start_time:
            game.question_start_time += delta
        if session.question_end_time:
            session.question_end_time += delta
        game.status, game.paused_at = 'active', None
        session.save(update_fields=['question_end_time', 'updated_at'])
    else:
        return False
    game.save(update_fields=['status', 'paused_at', 'question_start_time', 'updated_at'])
    from games_hub.authoritative_state import get_runtime_state
    runtime = get_runtime_state('where', game.room_code, game.active_hub_session_code)
    snapshot = dict(runtime.public_snapshot or {})
    if game.paused_at:
        snapshot['_paused_at'] = game.paused_at.isoformat()
    else:
        snapshot.pop('_paused_at', None)
    runtime.public_snapshot = snapshot
    runtime.state_revision += 1
    runtime.context_revision = runtime.state_revision
    runtime.save(update_fields=['public_snapshot', 'state_revision', 'context_revision', 'updated_at'])
    broadcast(game)
    return True


def answer_data(answer):
    return {'participant_name': answer.participant.name, 'participant_id': answer.participant_id,
            'latitude': answer.user_latitude, 'longitude': answer.user_longitude,
            'x_norm': answer.x_norm, 'y_norm': answer.y_norm, 'distance_km': answer.distance_km,
            'formatted_distance': answer.get_formatted_distance(), 'points_earned': answer.points_earned,
            'rank_position': answer.rank_position, 'zone_label': answer.zone_label}


def state(game, code=None, participant=None, *, host=False, observe=True):
    game = WhereQuiz.objects.get(pk=game.pk)
    code = code if code is not None else code_for(game)
    session, _ = WhereSession.objects.get_or_create(quiz=game)
    mismatch = bool(game.started_at and (code or '') != game.active_hub_session_code)
    question = game.current_question if not mismatch else None
    spec = session.question_rounds[-1] if session.question_rounds and not mismatch else {}
    revealed = bool(question and not session.is_question_active)
    answers = list(current_answers(game, session).select_related('participant')) if question else []
    own = next((a for a in answers if participant and a.participant_id == participant.pk), None)
    question_data = {'id': question.pk, 'text': question.question_text, 'question_text': question.question_text,
                     'map_type': question.map_type, 'image_url': question.image.url if question.image else None} if question else None
    if spec.get('question') and question:
        question_data = dict(spec['question'])
        if not (host or revealed):
            question_data.pop('correct_location', None)
            question_data.pop('explanation', None)
    elif question_data and (host or revealed):
        question_data.update(correct_location={'latitude': question.correct_latitude, 'longitude': question.correct_longitude},
                             explanation=question.explanation or '')
    players = list(game.participants.filter(**pool(code)).order_by('-total_score', 'name'))
    scores = [{'name': p.name, 'score': p.total_score, 'total_score': p.total_score,
               'rank': 1 + sum(other.total_score > p.total_score for other in players)} for p in players]
    numbered_history = [(i, s) for i, s in enumerate(session.question_rounds, 1) if not s.get('tutorial')] if not mismatch else []
    history = [s for _, s in numbered_history]
    selected = list(game.selected_questions.values_list('pk', flat=True))
    seen = {s['question_id'] for s in history}
    total = len(history) + len(set(selected) - seen)
    now = game.paused_at or timezone.now()
    remaining = max(0, math.ceil((session.question_end_time - now).total_seconds())) if question and session.is_question_active and session.question_end_time else 0
    result = {'type': 'where_state', 'phase': 'reveal' if revealed else 'question_active' if question else 'waiting',
              'game': {'status': 'waiting' if mismatch else game.status}, 'quiz_status': 'waiting' if mismatch else game.status,
              'question': question_data, 'current_question_id': question.pk if question else None,
              'current_round_id': session.current_question_number if question else None,
              'starts_at': game.question_start_time.isoformat() if question and game.question_start_time else None,
              'ends_at': session.question_end_time.isoformat() if question and session.question_end_time else None,
              'revealed': revealed, 'progress': {'kind': 'question', 'current': len(history), 'total': total},
              'scoring_mode': spec.get('scoring_mode', game.scoring_mode), 'participants': scores,
              'response_count': len(answers), 'remaining_seconds': remaining,
              'can_answer': bool(participant and participant.pk in spec.get('participants', []) and game.status == 'active'
                                 and not mismatch and session.is_question_active and remaining > 0 and not own),
              'own_answer': {'x_norm': own.x_norm, 'y_norm': own.y_norm} if own else None,
              'pending_input': session.pending_answers.get(str(participant.pk)) if participant and not mismatch else None,
              'answers': [answer_data(a) for a in answers] if revealed or host else [],
              'zone_scoring': {'zones': spec.get('zones', question.get_effective_distance_zones() if question else [])} if (revealed or host) and spec.get('scoring_mode', game.scoring_mode) == 'zones' else None,
              'scoreboard': []}
    for number, (physical_number, row) in enumerate(numbered_history, 1):
        answer = game.where_answers.filter(participant=participant, round_number=physical_number).first() if participant else None
        result['scoreboard'].append({'number': number, 'question_id': row['question_id'],
            'points': answer.points_earned if answer and row.get('revealed') else 0 if row.get('revealed') else None,
            'max_points': row.get('max_points', 0)})
    if game.paused_at:
        result['_paused_at'] = game.paused_at.isoformat()
    if observe:
        from games_hub.authoritative_state import observe_snapshot
        public = {**result, 'question': dict(question_data) if question_data else None}
        if public['question'] and not revealed:
            public['question'].pop('correct_location', None)
            public['question'].pop('explanation', None)
        public.update(own_answer=None, pending_input=None, answers=result['answers'] if revealed else [])
        result.update(observe_snapshot('where', game.room_code, public, code))
        # The revision metadata cannot replace the authorized per-view DTO.
        result.update(question=question_data, own_answer={'x_norm': own.x_norm, 'y_norm': own.y_norm} if own else None)
    if code:
        from games_hub.game_intro import get_game_step
        step = get_game_step(code, 'where', game.room_code)
        if step:
            result['intro'] = step['intro']
    return result
