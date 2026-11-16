"""Role-authenticated Quick Quiz adapter over the existing JSON transport."""
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_headers

from .controllers.quiz import start_quiz_round, commit_quiz_answer, buzz, judge_quiz_buzz, advance_quiz_phase
from .models import QuizPhase, ReceiptStatus
from .views import _errors, _context, _body, _snapshot


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def unit_api(request, session_code, instance_id, unit_id, role):
    if role not in ("host", "participant", "spectator") or (role == "spectator" and request.method != "GET"):
        raise Http404
    instance, actor = _context(request, session_code, instance_id, unit_id, role, game_key="quiz")
    if request.GET:
        raise ValueError("No caller-selected projection parameters.")
    result = None
    if request.method == "POST":
        data = _body(request)
        action = data.get("action")
        allowed = {"action", "client_action_id"}
        options = dict(instance_id=instance.pk, unit_id=unit_id, actor=actor, client_action_id=data.get("client_action_id"))
        if role == "participant":
            if action == "answer":
                allowed.add("payload")
                operation = commit_quiz_answer
                options["payload"] = data.get("payload")
            elif action == "buzz":
                operation = buzz
            else:
                from django.core.exceptions import PermissionDenied
                raise PermissionDenied
        else:
            targets = {"show_answers": QuizPhase.SHOW_ANSWERS, "show_solution": QuizPhase.SHOW_SOLUTION,
                       "show_result": QuizPhase.SHOW_RESULT, "complete": QuizPhase.COMPLETED}
            if action == "start":
                operation = start_quiz_round
            elif action == "judge":
                allowed |= {"buzz_id", "verdict"}
                operation = judge_quiz_buzz
                options.update(buzz_id=data.get("buzz_id"), verdict=data.get("verdict"))
            elif action in targets:
                operation = advance_quiz_phase
                options["target_phase"] = targets[action]
            else:
                raise ValueError("Invalid host action.")
        if set(data) != allowed:
            raise ValueError("Unexpected action data.")
        result = operation(**options)
    response = _snapshot(instance, unit_id, actor, role)
    if result:
        response["action"] = {"status": result.status.value, "code": result.data.get("code"),
                              "replayed": result.replayed, "state_revision": result.state_revision}
    return JsonResponse(response, status=409 if result and result.status == ReceiptStatus.REJECTED else 200)
