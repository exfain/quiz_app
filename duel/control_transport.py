"""Role-checked HTTP commands; never accepts client roles, clocks or sequences."""
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_headers
from .views import _context, _body, _errors, _snapshot
from .controllers import control


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def unit_api(request, session_code, instance_id, unit_id, role, game_key):
    if role not in {"host", "participant", "spectator"}: raise Http404
    instance, actor = _context(request, session_code, instance_id, unit_id, role, game_key=game_key)
    if request.GET: raise ValueError("No client-selected projection parameters.")
    result = None
    if request.method == "POST":
        if role == "spectator": raise PermissionDenied
        data = _body(request); action = data.get("action")
        allowed = {"action", "client_action_id"}
        options = dict(instance_id=instance.pk, unit_id=unit_id, actor=actor, client_action_id=data.get("client_action_id"))
        if role == "participant":
            if game_key == "bid" and action == "bid":
                operation = control.commit_bid; allowed.add("payload"); options["payload"] = data.get("payload")
            elif game_key == "buzzer" and action == "buzz": operation = control.buzz
            else: raise PermissionDenied
        elif action == "start": operation = control.start_round
        elif action in {"increment", "judge", "open", "adjust", "end_round", "complete"}:
            operation = control.host_action; options["action"] = action
            extra = {"increment": {"expected_counter"}, "judge": {"buzz_id", "verdict"}, "adjust": {"participant_id", "delta", "expected_counter"}}.get(action, set())
            allowed |= extra; options.update({key: data.get(key) for key in extra})
        else: raise ValueError("Invalid action.")
        if set(data) != allowed: raise ValueError("Unexpected fields.")
        result = operation(**options)
    response = _snapshot(instance, unit_id, actor, role)
    if result: response["action"] = {"status": result.status.value, "code": result.data.get("code"), "replayed": result.replayed, "state_revision": result.state_revision}
    return JsonResponse(response, status=409 if result and result.status.value == "rejected" else 200)
