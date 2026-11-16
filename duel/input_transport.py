"""Existing authenticated JSON HTTP transport for device-input duel games."""
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_GET
from django.views.decorators.vary import vary_on_headers
from .views import _context, _body, _errors, _snapshot
from .controllers import inputs
from .models import DuelInputRound, ReceiptStatus


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
        if role == "spectator": raise PermissionDenied
        data = _body(request); action = data.get("action")
        allowed = {"action", "client_action_id", "question_index"}
        if type(data.get("question_index")) is not int or data["question_index"] < 0: raise ValueError("A concrete question is required.")
        options = dict(instance_id=instance.pk, unit_id=unit_id, actor=actor,
            client_action_id=data.get("client_action_id"), question_index=data["question_index"])
        if role == "participant":
            if action not in {"draft", "commit"}: raise PermissionDenied
            allowed.add("payload"); operation = inputs.submit
            options.update(payload=data.get("payload"), commit=action == "commit")
        elif action == "start": operation = inputs.start_round
        elif action in {"reveal", "show_answers", "show_solution", "evaluate", "next", "complete"}:
            operation = inputs.advance; options["action"] = action
        else: raise ValueError("Invalid host command.")
        if set(data) != allowed: raise ValueError("Unexpected command fields.")
        result = operation(**options)
    response = _snapshot(instance, unit_id, actor, role)
    if result:
        response["action"] = {"status": result.status.value, "replayed": result.replayed,
            "state_revision": result.state_revision, "code": result.data.get("code")}
    return JsonResponse(response, status=409 if result and result.status == ReceiptStatus.REJECTED else 200)


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_GET
@_errors
def image(request, session_code, instance_id, unit_id, role):
    if role not in {"host", "participant", "spectator"}: raise Http404
    instance, actor = _context(request, session_code, instance_id, unit_id, role, game_key="where")
    if request.GET: raise ValueError("No projection parameters.")
    state = inputs.get_state(instance_id=instance.pk, unit_id=unit_id, actor=actor)
    if not state.get("has_image"): raise Http404
    data = DuelInputRound.objects.only("image").get(unit_id=unit_id).image
    response = HttpResponse(bytes(data), content_type="image/png")
    response["X-Content-Type-Options"] = "nosniff"
    return response
