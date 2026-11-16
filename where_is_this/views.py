from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db import transaction
import json
from .models import WhereQuiz, WhereQuestion, WhereParticipant
from games_hub.authoritative_state import validate_and_reserve_action
from games_hub.team_names import validate_join_name
from games_hub.models import HubParticipant, HubGameStep
from .identity import issue_player_token, valid_player_token
from . import runtime as r


@validate_join_name('where', 'where_is_this/join.html')
def where_join_view(request):
    if request.method != 'POST':
        return render(request, 'where_is_this/join.html')
    try:
        data = json.loads(request.body)
        code = str(data.get('hub_session') or '')
        name = str(data.get('participant_name') or '').strip()
        if not name or len(name) > 50:
            raise r.ActionError('Bitte einen Teamnamen mit 1 bis 50 Zeichen eingeben.')
        game = get_object_or_404(WhereQuiz, room_code=data.get('room_code'))
        if game.status not in {'waiting', 'active', 'inactive'}:
            raise r.ActionError('Dieses Spiel ist bereits beendet.')
        if code and (not HubGameStep.objects.filter(session__code=code, game_key='where', room_code=game.room_code).exists()
                     or not HubParticipant.objects.filter(session__code=code, nickname=name).exists()):
            raise r.ActionError('Bitte zuerst der Session in der Lobby beitreten.')
        with transaction.atomic():
            game = WhereQuiz.objects.select_for_update().get(pk=game.pk)
            participant = game.participants.filter(name=name, **r.pool(code)).first()
            if not participant:
                if game.participants.filter(**r.pool(code)).count() >= game.max_participants:
                    raise r.ActionError('Das Spiel ist voll.')
                participant = WhereParticipant.objects.create(quiz=game, name=name, **r.pool(code))
        return JsonResponse({'success': True, 'participant_id': participant.pk, 'quiz_status': game.status})
    except (ValueError, TypeError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


def check_room_code(request, room_code):
    game = WhereQuiz.objects.filter(room_code=room_code).first()
    return JsonResponse({'success': bool(game), 'exists': bool(game), 'quiz_status': game.status if game else None})


def participant_for(request, room_code, participant_name, *, token=False):
    game = get_object_or_404(WhereQuiz, room_code=room_code)
    participant = get_object_or_404(WhereParticipant, quiz=game, name=participant_name, **r.pool(request.GET.get('hub_session')))
    if token and not valid_player_token(request.GET.get('player_token'), participant):
        raise PermissionError('Teilnahme nicht best?tigt.')
    return game, participant


def where_play(request, room_code, participant_name):
    game, participant = participant_for(request, room_code, participant_name)
    participant.is_active = True
    participant.save(update_fields=['is_active', 'last_activity', 'updated_at'])
    return render(request, 'where_is_this/play.html', {'quiz': game, 'participant': participant,
        'hub_session': participant.hub_session_code or '', 'player_token': issue_player_token(participant), 'where_config': {'room': game.room_code, 'code': participant.hub_session_code or '', 'name': participant.name, 'token': issue_player_token(participant), 'host': False}})


def where_result(request, room_code, participant_name):
    return where_play(request, room_code, participant_name)


@require_POST
def submit_answer(request, room_code, participant_name):
    return _write_input(request, room_code, participant_name, confirm=True)


@require_POST
def pending_input(request, room_code, participant_name):
    return _write_input(request, room_code, participant_name, confirm=False)


def _write_input(request, room_code, participant_name, *, confirm):
    try:
        game, participant = participant_for(request, room_code, participant_name, token=True)
        data = json.loads(request.body)
        if confirm:
            decision = validate_and_reserve_action(game_key='where', room_code=room_code,
                session_code=participant.hub_session_code, participant_name=participant.name,
                action_type='participant_submit_answer', action=data)
            if not decision.accepted:
                return JsonResponse({'success': False, 'code': decision.code, 'error': decision.message}, status=409)
        point = r.save_input(game, participant, data, confirm=confirm)
        return JsonResponse({'success': True, **point})
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)
    except (ValueError, TypeError, KeyError) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


def get_quiz_status(request, room_code, participant_name):
    try:
        game, participant = participant_for(request, room_code, participant_name, token=True)
        return JsonResponse({'success': True, **r.state(game, participant.hub_session_code or '', participant)})
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)


@require_POST
def leave_quiz(request, room_code, participant_name):
    try:
        game, participant = participant_for(request, room_code, participant_name, token=True)
        participant.is_active = False
        participant.save(update_fields=['is_active', 'updated_at'])
        return JsonResponse({'success': True})
    except PermissionError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=403)


def api_quiz_participants(request, room_code):
    game = get_object_or_404(WhereQuiz, room_code=room_code)
    state = r.state(game, request.GET.get('hub_session') or r.code_for(game))
    return JsonResponse({'success': True, 'participants': state['participants'], 'count': len(state['participants'])})


def api_quiz_leaderboard(request, room_code):
    response = api_quiz_participants(request, room_code)
    data = json.loads(response.content)
    return JsonResponse({'success': True, 'leaderboard': data['participants']})


def get_quiz_statistics(quiz):
    return {'participant_count': quiz.get_participant_count(), 'total_answers': quiz.where_answers.count()}
