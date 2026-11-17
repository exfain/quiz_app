import json
import logging
from functools import wraps

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from games_hub.host_permissions import authorize_game_host
from games_hub.lobby_join import REJOIN_TOKEN_SALT
from games_hub.models import HubParticipant
from .models import Run, Enrollment
from .models import TargetGame
from games_hub.models import HubGameStep
from .service import ActionError, perform, snapshot

logger = logging.getLogger(__name__)


def json_errors(view):
    @wraps(view)
    def call(*args, **kwargs):
        try: return view(*args, **kwargs)
        except Http404: return JsonResponse({'success':False,'error':'Spiel nicht gefunden.'},status=404)
        except PermissionDenied: return JsonResponse({'success':False,'error':'Teilnahme oder Hostberechtigung nicht bestätigt.'},status=403)
        except ActionError as exc: return JsonResponse({'success':False,'error':str(exc)},status=409)
        except (ValueError, TypeError, ValidationError) as exc: return JsonResponse({'success':False,'error':str(exc)},status=400)
        except Exception:
            logger.exception('Zielansage-Transport fehlgeschlagen')
            return JsonResponse({'success':False,'error':'Der Spielstand konnte nicht verarbeitet werden.'},status=500)
    return call


def context(request, room, code, role, nickname=None):
    run=get_object_or_404(Run.objects.select_related('game','step__session'),game__room_code=room,step__session__code=code)
    if run.step.session.session_type!='multiplayer' or run.step.game_format!='standard':raise Http404
    participant=None
    if role=='host':
        if not authorize_game_host(request.user,'target',room,code).allowed: raise PermissionDenied
    elif role=='participant':
        header=request.headers.get('Authorization','').split()
        if len(header)!=2 or header[0].lower()!='bearer':raise PermissionDenied
        try:identity=signing.loads(header[1],salt=REJOIN_TOKEN_SALT)
        except signing.BadSignature as exc:raise PermissionDenied from exc
        if (type(identity) is not dict or set(identity)!={'session_id','participant_id'}
                or any(type(v) is not int for v in identity.values()) or identity.get('session_id')!=run.step.session_id):raise PermissionDenied
        participant=HubParticipant.objects.filter(pk=identity.get('participant_id'),session_id=run.step.session_id).first()
        if not participant or nickname is not None and participant.nickname!=nickname or not run.teams.filter(participant=participant).exists():raise PermissionDenied
    elif role!='spectator':raise PermissionDenied
    return run,participant


@never_cache
@ensure_csrf_cookie
@require_http_methods(['GET'])
def page(request, room, role, nickname=None):
    code=request.GET.get('hub_session','')
    step=get_object_or_404(HubGameStep.objects.select_related('session'),game_key='target',room_code=room,session__code=code)
    if step.session.session_type!='multiplayer' or step.game_format!='standard':raise Http404
    run=Run.objects.filter(step=step).first()
    if role=='host':
        if not authorize_game_host(request.user,'target',room,code).allowed:raise PermissionDenied
    elif run is None:raise Http404
    if role not in {'host','participant','spectator'}:raise PermissionDenied
    response=render(request,'target_game/host.html' if role=='host' else 'target_game/play.html',{'session':step.session,'role':role,'nickname':nickname,
        'room':room,'started':run is not None,
        'api_url':reverse('target_game:api',args=[code,room,role])})
    if role=='spectator':response['X-Frame-Options']='SAMEORIGIN'
    return response


@json_errors
@require_http_methods(['POST'])
def join(request):
    data=json.loads(request.body)
    run,participant=context(request,data.get('room_code'),data.get('hub_session'),'participant',data.get('participant_name'))
    if (not run.completed_at and run.game.status=='active' and run.step.session.is_active
            and run.game.active_hub_session_code==run.step.session.code):
        Enrollment.objects.filter(run=run,participant=participant).update(is_active=True)
    return JsonResponse({'success':True})


@never_cache
@json_errors
@require_http_methods(['GET','POST'])
def api(request, code, room, role):
    run,participant=context(request,room,code,role)
    result=None
    if request.method=='POST':
        if role=='spectator':raise PermissionDenied
        if request.content_type!='application/json' or len(request.body)>16384:raise ValueError('Ungültige Anfrage.')
        data=json.loads(request.body,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Ungültige Zahl.')))
        if type(data) is not dict or set(data)-{'action','client_action_id','round_id','previous_round_id','task_id','target','text','answer_id','solution_index'}:
            raise ValueError('Ungültige Aktionsdaten.')
        action=data.pop('action',None);action_id=data.pop('client_action_id',None)
        if not isinstance(action,str):raise ValueError('Aktion fehlt.')
        if role=='participant' and action not in {'ready','goal','answer','return'}:raise PermissionDenied
        if action=='return':
            from .service import leave_for_lobby
            if not participant:raise PermissionDenied
            leave_for_lobby(run.pk,participant)
            result={'success':True}
        else:result=perform(run.pk,actor=request.user,participant=participant,action_id=action_id,action=action,payload=data)
    data=snapshot(run.pk,role,participant);data['action']=result
    return JsonResponse(data,status=409 if result and not result['success'] else 200)
