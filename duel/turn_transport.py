"""Authenticated host-only turn commands; all roles share stored projections."""
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_headers
from .views import _context, _body, _errors, _snapshot
from .controllers import turns
from .models import ReceiptStatus


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def unit_api(request, session_code, instance_id, unit_id, role, game_key):
    if role not in {"host", "participant", "spectator"}: raise Http404
    instance, actor = _context(request, session_code, instance_id, unit_id, role, game_key=game_key)
    if request.GET: raise ValueError("No caller-selected projection parameters.")
    result = None
    if request.method == "POST":
        if role != "host": raise PermissionDenied
        data = _body(request); action = data.get("action")
        allowed = {"action", "client_action_id"}
        options = dict(instance_id=instance.pk, unit_id=unit_id, actor=actor, client_action_id=data.get("client_action_id"))
        if action == "start":
            operation = turns.start_round; allowed.add("starting_participant_id")
            options["starting_participant_id"] = data.get("starting_participant_id")
        elif action == "record":
            operation = turns.record_turn; allowed |= {"expected_turn", "selection"}
            options.update(expected_turn=data.get("expected_turn"), selection=data.get("selection"))
        elif action in {"show_result", "complete"}:
            operation = turns.advance; options["action"] = action
        else: raise ValueError("Invalid turn command.")
        if set(data) != allowed: raise ValueError("Unexpected command fields.")
        result = operation(**options)
    response = _snapshot(instance, unit_id, actor, role)
    if result:
        response["action"] = {"status": result.status.value, "replayed": result.replayed,
            "state_revision": result.state_revision, "code": result.data.get("code")}
    return JsonResponse(response, status=409 if result and result.status == ReceiptStatus.REJECTED else 200)
