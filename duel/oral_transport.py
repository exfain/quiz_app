"""Oral duel commands over the existing authenticated JSON HTTP boundary."""
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_GET
from django.views.decorators.vary import vary_on_headers
from .views import _context, _body, _errors, _snapshot
from .controllers import oral
from .models import DuelOralRound, ReceiptStatus


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def unit_api(request, session_code, instance_id, unit_id, role, game_key):
    if role not in {"host", "participant", "spectator"} or (role == "spectator" and request.method != "GET"):
        raise Http404
    instance, actor = _context(request, session_code, instance_id, unit_id, role, game_key=game_key)
    if request.GET:
        raise ValueError("Projection parameters are not accepted.")
    result = None
    if request.method == "POST":
        data = _body(request); action = data.get("action")
        allowed = {"action", "client_action_id"}
        options = dict(instance_id=instance.pk, unit_id=unit_id, actor=actor, client_action_id=data.get("client_action_id"))
        if role == "participant":
            if action != "buzz":raise PermissionDenied
            operation = oral.buzz
        elif action == "start":
            operation = oral.start_round
        elif action == "judge":
            operation = oral.judge; allowed |= {"buzz_id", "verdict"}
            options.update(buzz_id=data.get("buzz_id"), verdict=data.get("verdict"))
        elif action in {"show_result", "complete"}:
            operation = oral.advance; options["action"] = action
        else:
            raise ValueError("Invalid oral command.")
        if set(data) != allowed:raise ValueError("Unexpected command data.")
        result = operation(**options)
    response = _snapshot(instance, unit_id, actor, role)
    if result:response["action"] = {"status": result.status.value, "replayed": result.replayed,
        "state_revision": result.state_revision, "code": result.data.get("code")}
    return JsonResponse(response,status=409 if result and result.status == ReceiptStatus.REJECTED else 200)


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_GET
@_errors
def image(request, session_code, instance_id, unit_id, role):
    if role not in {"host", "participant", "spectator"} or request.GET:raise Http404
    instance, actor = _context(request, session_code, instance_id, unit_id, role, game_key="who_that")
    state = oral.get_state(instance_id=instance.pk,unit_id=unit_id,actor=actor)
    if not state.get("has_image"):raise Http404
    response = HttpResponse(bytes(DuelOralRound.objects.get(unit_id=unit_id).image),content_type="image/png")
    response["X-Content-Type-Options"] = "nosniff"
    return response
