import json
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from games_hub.active_game_guard import resolve_session_game_activation_for_room
from games_hub.lobby_return_flow import ensure_session_players_ready_for_game_start_for_room

from .models import WerWeissMehrGame, WerWeissMehrParticipant, WerWeissMehrSession
from .services import (
    apply_manual_correction,
    build_game_state,
    clear_current_set,
    end_current_round,
    finish_set,
    start_set,
    start_next_round_after_review,
    store_pending_input,
    submit_answer,
)


logger = logging.getLogger(__name__)


def join_view(request):
    if request.method == 'GET':
        return render(request, 'wer_weiss_mehr/join.html')

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    participant_name = (data.get('participant_name') or '').strip()
    room_code = (data.get('room_code') or '').strip()
    hub_session = (data.get('hub_session') or '').strip() or None

    if not participant_name or not room_code:
        return JsonResponse({'success': False, 'error': 'Name and room code are required.'}, status=400)
    if len(participant_name) > 100:
        return JsonResponse({'success': False, 'error': 'Name must be 100 characters or less.'}, status=400)

    quiz = WerWeissMehrGame.objects.filter(room_code=room_code).first()
    if not quiz:
        return JsonResponse({'success': False, 'error': 'Game not found.'}, status=404)
    if quiz.status not in ['waiting', 'active', 'inactive']:
        return JsonResponse({'success': False, 'error': 'This game is no longer accepting participants.'}, status=400)

    participant, _ = WerWeissMehrParticipant.objects.get_or_create(
        quiz=quiz,
        name=participant_name,
        hub_session_code=hub_session,
        defaults={'is_active': True},
    )
    if not participant.is_active:
        participant.is_active = True
        participant.save(update_fields=['is_active'])

    return JsonResponse({'success': True, 'participant_id': participant.id, 'game_status': quiz.status})


def check_room_code(request, room_code):
    quiz = WerWeissMehrGame.objects.filter(room_code=room_code).first()
    if not quiz:
        return JsonResponse({'success': False, 'error': 'Invalid room code.'}, status=404)
    return JsonResponse({
        'success': True,
        'quiz': {
            'id': quiz.id,
            'title': quiz.title,
            'room_code': quiz.room_code,
            'status': quiz.get_status_display(),
            'participant_count': quiz.participants.filter(is_active=True).count(),
            'max_participants': quiz.max_participants,
        },
    })


def play(request, room_code, participant_name):
    session_code = request.GET.get('hub_session') or None
    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    participant = WerWeissMehrParticipant.objects.filter(
        quiz=quiz,
        name=participant_name,
        hub_session_code=session_code,
    ).first()
    if not participant:
        return redirect('wer_weiss_mehr:join')

    participant.is_active = True
    participant.last_activity = timezone.now()
    participant.save(update_fields=['is_active', 'last_activity'])

    return render(request, 'wer_weiss_mehr/play.html', {
        'quiz': quiz,
        'participant': participant,
        'hub_session': session_code or '',
    })


def state(request, room_code):
    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    participant_name = request.GET.get('participant_name') or None
    hub_session = request.GET.get('hub_session') or None
    return JsonResponse(build_game_state(quiz, hub_session_code=hub_session, participant_name=participant_name))


@require_POST
def participant_pending_input(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = WerWeissMehrGame.objects.filter(room_code=room_code).first()
    if not quiz:
        return JsonResponse({'success': False, 'error': 'Game not found.'}, status=404)
    participant_name = (data.get('participant_name') or data.get('name') or '').strip()
    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    participant = _get_participant(quiz, participant_name, hub_session)
    if not participant:
        return JsonResponse({'success': False, 'error': 'Participant not found.'}, status=404)
    if quiz.status != 'active':
        return JsonResponse({'success': False, 'error': 'Das Spiel ist nicht aktiv.'}, status=400)

    pending = store_pending_input(quiz, participant, data.get('answer_text') or '')
    return JsonResponse({'success': True, 'pending_saved': bool(pending)})


@require_POST
def participant_submit_answer(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = WerWeissMehrGame.objects.filter(room_code=room_code).first()
    if not quiz:
        return JsonResponse({'success': False, 'error': 'Game not found.'}, status=404)
    participant_name = (data.get('participant_name') or data.get('name') or '').strip()
    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    participant = _get_participant(quiz, participant_name, hub_session)
    if not participant:
        return JsonResponse({'success': False, 'error': 'Participant not found.'}, status=404)

    try:
        submit_answer(quiz, participant, data.get('answer_text') or '')
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Failed to submit Wer weiss mehr answer for %s in %s', participant_name, room_code)
        return JsonResponse({
            'success': False,
            'error': 'Antwort konnte nicht eingeloggt werden.',
            'details': str(exc),
        }, status=500)

    quiz.refresh_from_db()
    state_payload = build_game_state(quiz, hub_session_code=hub_session, participant_name=participant.name)
    _broadcast_state_updated(quiz, hub_session)
    return JsonResponse(state_payload)


@login_required
@require_POST
def start_game(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    lobby_ready = ensure_session_players_ready_for_game_start_for_room(
        'wer_weiss_mehr',
        quiz.room_code,
        hub_session,
    )
    if not lobby_ready.get('allowed', True):
        return JsonResponse({
            'success': False,
            'error': lobby_ready.get('message') or 'Noch nicht alle Teilnehmer sind in der Lobby.',
            'not_in_lobby_count': lobby_ready.get('not_in_lobby_count', 0),
            'participants_not_in_lobby': lobby_ready.get('participants_not_in_lobby', []),
        }, status=409)

    activation = resolve_session_game_activation_for_room(
        'wer_weiss_mehr',
        quiz.room_code,
        session_code=hub_session,
    )
    if not activation.get('success'):
        return JsonResponse({
            'success': False,
            'error': activation.get('message') or activation.get('error') or 'Unable to start this game.',
            'active_game': activation.get('active_game'),
            'conflict': activation.get('conflict', False),
        }, status=409 if activation.get('conflict') else 400)

    quiz.refresh_from_db()
    quiz.start_quiz()
    WerWeissMehrSession.objects.get_or_create(quiz=quiz)
    state_payload = build_game_state(quiz, hub_session_code=hub_session)
    _broadcast_game_started(quiz, hub_session)
    return JsonResponse(state_payload)


@login_required
@require_POST
def start_game_set(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    if quiz.status != 'active' or quiz.started_at is None:
        return JsonResponse({'success': False, 'error': 'Das Spiel wurde noch nicht gestartet.'}, status=400)

    try:
        question_id = int(data.get('question_id'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Set-ID fehlt oder ist ungueltig.'}, status=400)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    try:
        start_set(
            quiz,
            question_id,
            hub_session_code=hub_session,
            time_limit_seconds=data.get('time_limit_seconds'),
        )
    except Exception as exc:  # pylint: disable=broad-except
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    quiz.refresh_from_db()
    state_payload = build_game_state(quiz, hub_session_code=hub_session)
    _broadcast_state_updated(quiz, hub_session)
    _broadcast_hub_event(quiz, hub_session, 'question_started', {'question_id': question_id})
    return JsonResponse(state_payload)


@login_required
@require_POST
def end_round(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    try:
        round_state = end_current_round(quiz)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Failed to end Wer weiss mehr round for %s', room_code)
        return JsonResponse({
            'success': False,
            'error': 'Die Runde konnte nicht beendet werden.',
            'details': str(exc),
        }, status=500)

    if not round_state:
        return JsonResponse({'success': False, 'error': 'Aktuell laeuft keine Runde.'}, status=400)

    quiz.refresh_from_db()
    state_payload = build_game_state(quiz, hub_session_code=hub_session)
    _broadcast_state_updated(quiz, hub_session)
    _broadcast_hub_event(quiz, hub_session, 'round_ended')
    return JsonResponse(state_payload)


@login_required
@require_POST
def next_round(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    try:
        start_next_round_after_review(quiz)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Failed to start next Wer weiss mehr round for %s', room_code)
        return JsonResponse({
            'success': False,
            'error': 'Die naechste Runde konnte nicht gestartet werden.',
            'details': str(exc),
        }, status=500)

    quiz.refresh_from_db()
    state_payload = build_game_state(quiz, hub_session_code=hub_session)
    _broadcast_state_updated(quiz, hub_session)
    _broadcast_hub_event(quiz, hub_session, 'round_started')
    return JsonResponse(state_payload)


@login_required
@require_POST
def finish_current_set(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    try:
        finish_set(quiz)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Failed to finish Wer weiss mehr set for %s', room_code)
        return JsonResponse({
            'success': False,
            'error': 'Das Set konnte nicht beendet werden.',
            'details': str(exc),
        }, status=500)

    quiz.refresh_from_db()
    state_payload = build_game_state(quiz, hub_session_code=hub_session)
    _broadcast_state_updated(quiz, hub_session)
    _broadcast_hub_event(quiz, hub_session, 'question_ended')
    return JsonResponse(state_payload)


@login_required
@require_POST
def clear_set_selection(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    try:
        clear_current_set(quiz)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Failed to return Wer weiss mehr host to set selection for %s', room_code)
        return JsonResponse({
            'success': False,
            'error': 'Die Setauswahl konnte nicht geladen werden.',
            'details': str(exc),
        }, status=500)

    quiz.refresh_from_db()
    state_payload = build_game_state(quiz, hub_session_code=hub_session)
    _broadcast_state_updated(quiz, hub_session)
    return JsonResponse(state_payload)


@login_required
@require_POST
def apply_correction(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    try:
        response_id = int(data.get('response_id'))
        target_answer_id = int(data.get('target_answer_id'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Antwort oder Zielantwort fehlt.'}, status=400)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    try:
        apply_manual_correction(quiz, response_id, target_answer_id)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Failed to apply Wer weiss mehr correction for %s', room_code)
        return JsonResponse({
            'success': False,
            'error': 'Die Korrektur konnte nicht uebernommen werden.',
            'details': str(exc),
        }, status=500)

    quiz.refresh_from_db()
    state_payload = build_game_state(quiz, hub_session_code=hub_session)
    _broadcast_state_updated(quiz, hub_session)
    return JsonResponse(state_payload)


@require_POST
def end_game(request, room_code):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'error': 'Authentication required.'}, status=401)

    quiz = WerWeissMehrGame.objects.filter(room_code=room_code).first()
    if not quiz:
        return JsonResponse({'success': False, 'error': 'Game not found.'}, status=404)
    if not request.user.is_superuser and quiz.creator != request.user:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)

    hub_session = (data.get('hub_session') or data.get('hub_session_code') or '').strip() or None
    try:
        quiz.end_quiz()
        quiz.refresh_from_db()
        final_scores_qs = quiz.participants.all()
        if hub_session is not None:
            final_scores_qs = final_scores_qs.filter(hub_session_code=hub_session)
        final_scores = list(final_scores_qs.order_by('-total_score', 'name').values('name', 'total_score'))
        state_payload = build_game_state(quiz, hub_session_code=hub_session)
        state_payload['final_scores'] = final_scores

        _broadcast_game_ended(quiz, hub_session, final_scores)
        return JsonResponse(state_payload)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Failed to end Wer weiss mehr game %s', room_code)
        return JsonResponse({
            'success': False,
            'error': 'Das Spiel konnte nicht beendet werden.',
            'details': str(exc),
        }, status=500)


def _broadcast_game_started(quiz, hub_session):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    room_group = f'werweissmehr_{quiz.room_code}'
    async_to_sync(channel_layer.group_send)(room_group, {
        'type': 'quiz_started',
        'message': 'Wer weiß mehr wurde gestartet.',
    })
    async_to_sync(channel_layer.group_send)(room_group, {
        'type': 'state_updated',
        'payload': {'hub_session_code': hub_session},
    })

    if not hub_session:
        return

    step = {
        'index': -1,
        'order': -1,
        'game_key': 'wer_weiss_mehr',
        'room_code': quiz.room_code,
        'title': quiz.title,
    }
    async_to_sync(channel_layer.group_send)(f'hub_{hub_session}', {
        'type': 'hub_event',
        'event': {
            'type': 'quiz_started',
            'game_key': 'wer_weiss_mehr',
            'room_code': quiz.room_code,
            'title': quiz.title,
        },
    })
    async_to_sync(channel_layer.group_send)(f'hub_{hub_session}', {
        'type': 'navigate',
        'step': step,
    })


def _broadcast_state_updated(quiz, hub_session):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(f'werweissmehr_{quiz.room_code}', {
        'type': 'state_updated',
        'payload': {'hub_session_code': hub_session},
    })


def _broadcast_game_ended(quiz, hub_session, final_scores):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    room_group = f'werweissmehr_{quiz.room_code}'
    async_to_sync(channel_layer.group_send)(room_group, {
        'type': 'quiz_ended',
        'message': 'Wer weiss mehr wurde beendet.',
        'final_scores': final_scores,
    })
    async_to_sync(channel_layer.group_send)(room_group, {
        'type': 'state_updated',
        'payload': {'hub_session_code': hub_session},
    })
    _broadcast_hub_event(quiz, hub_session, 'quiz_ended', {'final_scores': final_scores})


def _broadcast_hub_event(quiz, hub_session, event_type, extra_payload=None):
    if not hub_session:
        return
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    payload = {
        'type': event_type,
        'game_key': 'wer_weiss_mehr',
        'room_code': quiz.room_code,
        'title': quiz.title,
    }
    if extra_payload:
        payload.update(extra_payload)
    async_to_sync(channel_layer.group_send)(f'hub_{hub_session}', {
        'type': 'hub_event',
        'event': payload,
    })


def _get_participant(quiz, participant_name, hub_session):
    if not participant_name:
        return None
    return WerWeissMehrParticipant.objects.filter(
        quiz=quiz,
        name=participant_name,
        hub_session_code=hub_session,
    ).first()


def leave_game(request, room_code, participant_name):
    session_code = request.GET.get('hub_session') or None
    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    participant = get_object_or_404(
        WerWeissMehrParticipant,
        quiz=quiz,
        name=participant_name,
        hub_session_code=session_code,
    )
    participant.is_active = False
    participant.save(update_fields=['is_active'])
    return JsonResponse({'success': True})


def api_participants(request, room_code):
    hub_session = request.GET.get('hub_session') or None
    quiz = get_object_or_404(WerWeissMehrGame, room_code=room_code)
    participants = quiz.participants.filter(is_active=True)
    if hub_session is not None:
        participants = participants.filter(hub_session_code=hub_session)
    return JsonResponse({
        'success': True,
        'participants': [
            {
                'id': participant.id,
                'name': participant.name,
                'total_score': participant.total_score,
            }
            for participant in participants.order_by('-total_score', 'name')
        ],
    })
