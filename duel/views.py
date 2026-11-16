"""Narrow JSON transport for already provisioned Estimation duel units.

Host identity comes from Django authentication plus the existing game/session
permission checks. Participant identity comes ONLY from the existing signed
lobby rejoin credential (Authorization: Bearer ...), not from a request ID.
The spectator route follows the Hub's public session-code, read-only policy;
it never authenticates a participant, registers presence or permits commands.

GET and post-command snapshots use the same stored-phase projection. No raw
receipt result, material context or shared Multiplayer snapshot is serialized.
There is no push channel, provisioning, Hub activation, automatic deadline
reconciliation or new rule for missing answers in this adapter.
"""
import json
import logging
from functools import wraps
from uuid import UUID

from django.core import signing
from django.core.exceptions import BadRequest, PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods
from django.views.decorators.vary import vary_on_headers

from games_hub.host_permissions import authorize_game_host
from games_hub.lobby_join import REJOIN_TOKEN_SALT
from games_hub.models import HubParticipant

from .commands import ActorContext, ActionConflict, CommandRejected
from .contracts import GameFormat, SessionType, validate_game_format
from .controllers.estimation import commit_estimate, get_estimation_state
from .estimation_questions import get_estimation_question_context
from .estimation_rounds import (
    start_estimation_round, show_estimation_answers, show_estimation_solution,
    show_estimation_result, complete_estimation_round,
)
from .match_service import MatchIntegrityError
from .models import DuelGameInstance, DuelUnit, ReceiptActorType, ReceiptStatus
from .oral_material import ORAL_GAMES
from .turn_material import TURN_GAMES
from .input_material import INPUT_GAMES
from .control_games import CONTROL_GAMES

logger = logging.getLogger(__name__)


def _error(code, status):
    return JsonResponse({"error": {"code": code}}, status=status)


def _errors(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except Http404:
            return _error("not_found", 404)
        except PermissionDenied:
            return _error("unauthorized", 403)
        except ActionConflict as exc:
            return _error(exc.code, 409)
        except CommandRejected as exc:
            return _error(exc.code, 409)
        except (BadRequest, ValueError, TypeError, RecursionError):
            return _error("invalid_request", 400)
        except (ValidationError, MatchIntegrityError):
            # Never turn integrity failures into debug pages containing material.
            return _error("invalid_state", 409)
        except Exception:
            # Keep technical tracebacks in server logs, including when DEBUG is
            # enabled locally. A failed post-commit snapshot is recovered by
            # retrying the same action ID, never by repeating the mutation.
            logger.exception("Duel transport failed")
            return _error("internal_error", 500)
    return wrapped


def _context(request, session_code, instance_id, unit_id, role, *, game_key="estimation"):
    instance = get_object_or_404(DuelGameInstance.objects.select_related(
        "hub_session", "hub_game_step"), pk=instance_id, hub_session__code=session_code)
    session, step = instance.hub_session, instance.hub_game_step
    if step.game_format == GameFormat.TOURNAMENT.value:
        from .tournaments import pair_for, SUPPORTED
        pair = pair_for(instance.pk)
        if session.session_type != 'multiplayer' or game_key not in SUPPORTED or pair is None or pair.tournament_set.tournament.step_id != step.pk:
            raise Http404
    elif step.game_format != GameFormat.DUEL.value:
        raise Http404
    if (game_key not in {"estimation", "quiz", *ORAL_GAMES, *TURN_GAMES, *INPUT_GAMES, *CONTROL_GAMES} or instance.game_key != game_key or step.game_key != game_key
            or step.session_id != session.pk):
        raise Http404
    try:
        validate_game_format(SessionType(session.session_type), GameFormat(step.game_format))
    except ValueError as exc:
        raise Http404 from exc
    if unit_id is not None:
        get_object_or_404(DuelUnit, pk=unit_id, game_instance=instance)
    instance.full_clean()
    if role == "host":
        permission = authorize_game_host(request.user, game_key, step.room_code, session.code)
        if not permission.allowed:
            raise PermissionDenied
        actor = ActorContext(ReceiptActorType.HOST, request.user.pk)
    elif role == "participant":
        # Reuse the exact credential issued/verified by the existing lobby.
        # Only its verified payload supplies the identity; request participant
        # and actor IDs are deliberately absent from the input protocol.
        header = request.headers.get("Authorization", "").split()
        if len(header) != 2 or header[0].lower() != "bearer":
            raise PermissionDenied
        try:
            identity = signing.loads(header[1], salt=REJOIN_TOKEN_SALT)
        except signing.BadSignature as exc:
            raise PermissionDenied from exc
        if (type(identity) is not dict or set(identity) != {"session_id", "participant_id"}
                or any(type(identity[key]) is not int for key in identity)
                or identity["session_id"] != session.pk
                or identity["participant_id"] not in (instance.participant_a_id, instance.participant_b_id)):
            raise PermissionDenied
        participant = HubParticipant.objects.filter(pk=identity["participant_id"], session=session).first()
        if participant is None:
            raise PermissionDenied
        actor = ActorContext(ReceiptActorType.PARTICIPANT, participant.pk)
    else:
        actor = None  # Public spectator; never a participant or host assertion.
    if request.method == "POST" and session.ended_at is not None:
        raise CommandRejected("session_closed", "This session has ended.")
    if game_key != "estimation" and request.method == "POST" and not session.is_active:
        raise CommandRejected("session_closed", "This session is not active.")
    return instance, actor


def _body(request):
    if request.content_type != "application/json" or len(request.body) > 65536:
        raise BadRequest
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BadRequest
            result[key] = value
        return result
    def invalid_constant(value):
        raise BadRequest
    data = json.loads(request.body, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    if type(data) is not dict:
        raise BadRequest
    return data


def _snapshot_unfiltered(instance, unit_id, actor, role):
    if instance.game_key in CONTROL_GAMES:
        from .controllers.control import get_state
        return {"role": role, "snapshot": get_state(instance_id=instance.pk, unit_id=unit_id, actor=actor)}
    if instance.game_key in INPUT_GAMES:
        from .controllers.inputs import get_state
        from django.urls import reverse
        state = get_state(instance_id=instance.pk, unit_id=unit_id, actor=actor)
        if state.get("has_image"):
            state["image_url"] = reverse("duel:input_image", args=[instance.hub_session.code, instance.pk, unit_id, role])
        return {"role": role, "snapshot": state}
    if instance.game_key in TURN_GAMES:
        from .controllers.turns import get_state
        return {"role": role, "snapshot": get_state(instance_id=instance.pk, unit_id=unit_id, actor=actor)}
    if instance.game_key in ORAL_GAMES:
        from .controllers.oral import get_state
        from django.urls import reverse
        state = get_state(instance_id=instance.pk, unit_id=unit_id, actor=actor)
        if state.get("has_image"):
            state["image_url"] = reverse("duel:oral_image", args=[instance.hub_session.code, instance.pk, unit_id, role])
        return {"role": role, "snapshot": state}
    if instance.game_key == "quiz":
        from .controllers.quiz import get_quiz_state
        return {"role": role, "snapshot": get_quiz_state(instance_id=instance.pk, unit_id=unit_id, actor=actor)}
    return {"role": role, "snapshot": get_estimation_state(
        instance_id=instance.pk, unit_id=unit_id, actor=actor)}


def _snapshot(instance, unit_id, actor, role):
    response = _snapshot_unfiltered(instance, unit_id, actor, role)
    from .tournaments import project
    response['snapshot'] = project(instance, response['snapshot'], role)
    return response


def _dispatch(request, session_code, instance_id, unit_id, *, role):
    instance, actor = _context(request, session_code, instance_id, unit_id, role)
    # No caller-selectable phase, role, material or participant ID, including GET.
    if request.GET:
        raise BadRequest
    if request.method == "GET":
        return JsonResponse(_snapshot(instance, unit_id, actor, role))
    data = _body(request)
    common = {"client_action_id", "expected_revision"}
    if role == "participant":
        if set(data) - common - {"payload", "draft_id"} or "client_action_id" not in data:
            raise BadRequest
        if data.get("draft_id") is not None and type(data["draft_id"]) is not str:
            raise BadRequest
        draft_id = UUID(data["draft_id"]) if data.get("draft_id") is not None else None
        result = commit_estimate(question=get_estimation_question_context(unit_id), actor=actor,
            client_action_id=data["client_action_id"], payload=data.get("payload"), draft_id=draft_id,
            expected_revision=data.get("expected_revision"))
    else:
        operations = {"start": start_estimation_round, "show_answers": show_estimation_answers,
            "show_solution": show_estimation_solution, "show_result": show_estimation_result,
            "complete": complete_estimation_round}
        action = data.get("action")
        if type(action) is not str or action not in operations or "client_action_id" not in data:
            raise BadRequest
        allowed = common | {"action"} | ({"duration_seconds"} if action == "start" else set())
        if set(data) - allowed or (action == "start" and "duration_seconds" not in data):
            raise BadRequest
        options = {"duration_seconds": data["duration_seconds"]} if action == "start" else {}
        result = operations[action](instance_id=instance.pk, unit_id=unit_id, actor=actor,
            client_action_id=data["client_action_id"], expected_revision=data.get("expected_revision"), **options)
    response = _snapshot(instance, unit_id, actor, role)
    # Do not expose arbitrary stored handler data. Revalidate visibility through
    # the current snapshot even on a replay after a phase change. The original
    # receipt revision remains distinguishable from the snapshot revision.
    response["action"] = {"status": result.status.value, "replayed": result.replayed,
        "state_revision": result.state_revision}
    for key in ("code", "submission_id", "committed_at", "acceptance_sequence"):
        if key in result.data:
            response["action"][key] = result.data[key]
    return JsonResponse(response, status=200 if result.status == ReceiptStatus.APPLIED else 409)


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def estimation_host(request, session_code, instance_id, unit_id):
    return _dispatch(request, session_code, instance_id, unit_id, role="host")


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def estimation_participant(request, session_code, instance_id, unit_id):
    return _dispatch(request, session_code, instance_id, unit_id, role="participant")


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_GET
@_errors
def estimation_spectator(request, session_code, instance_id, unit_id):
    return _dispatch(request, session_code, instance_id, unit_id, role="spectator")
