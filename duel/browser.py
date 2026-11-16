"""Browser orchestration/projections, exclusively for planned DUEL sessions."""
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_headers
from Estimation.models import EstimationQuiz
from QuizGame.models import Quiz
from games_hub.models import HubSession
from games_hub.host_permissions import user_can_manage_hub_session
from games_hub.lobby_join import join_lobby_participant
from games_hub.check_in import get_check_in_state
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.game_intro import serialize_game_intro
from .models import DuelGameInstance
from .match_service import get_match_snapshot
from .hub import prepare_question, save_plan
from .views import _errors, _context, _body, _snapshot
from .oral_material import ORAL_GAMES
from .turn_material import TURN_GAMES
from .input_material import INPUT_GAMES
from .control_games import CONTROL_GAMES


def _session(code, *, duel_only=True):
    return get_object_or_404(HubSession, code=code, **({'session_type': 'duel'} if duel_only else {}))


def _host(request, session):
    if not user_can_manage_hub_session(request.user, session):
        raise PermissionDenied


@never_cache
@ensure_csrf_cookie
def session_page(request, session, role):
    if role == "host":
        _host(request, session)
    return render(request, "duel/session.html", {"session": session, "role": role})


@never_cache
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def session_api(request, session_code):
    session = _session(session_code)
    if request.method == "POST":
        data = _body(request)
        if data.get("action") == "join":
            if set(data) - {"action", "nickname", "rejoin_token"}:
                raise ValueError("Ungültige Teilnehmerdaten.")
            result = join_lobby_participant(session.code, data.get("nickname"), data.get("rejoin_token", ""))
            return JsonResponse(result, status=200 if result.get("success") else 409)
        _host(request, session)
        if data.get("action") == "activate" and set(data) == {"action"}:
            HubSession.activate_exclusive(session.code)
            return JsonResponse({"success": True})
        if data.get("action") == "save_plan" and set(data) == {"action", "configs"}:
            try:
                save_plan(request.user, session, data["configs"])
            except (ValueError, ValidationError) as exc:
                return JsonResponse({"success": False, "error": str(exc)}, status=400)
            return JsonResponse({"success": True})
        raise ValueError("Unbekannte Aktion.")
    # Public lobby metadata, like the existing Hub lobby. No answers/solutions.
    running = session.duel_instances.filter(Q(match_status="in_progress") | Q(game_key="quiz", match_status="completed",
        units__quiz_round_state__phase="show_result") | Q(game_key__in=ORAL_GAMES, match_status="completed",
        units__oral_round__phase="show_result") | Q(game_key__in=TURN_GAMES, match_status="completed",
        units__turn_round__phase="show_result") | Q(game_key__in=INPUT_GAMES, match_status="completed", units__input_round__phase="show_result") | Q(game_key__in=CONTROL_GAMES, match_status="completed", units__control_round__phase="show_result")).order_by("-created_at").first()
    page_name = "duel:match_page" if running and running.game_key == "estimation" else f"duel:{running.game_key}_match_page" if running else None
    steps = [{"id": step.pk, "title": step.title or (CONTROL_GAMES[step.game_key][1] if step.game_key in CONTROL_GAMES else INPUT_GAMES[step.game_key][1] if step.game_key in INPUT_GAMES else TURN_GAMES[step.game_key][1] if step.game_key in TURN_GAMES else ORAL_GAMES[step.game_key][1] if step.game_key in ORAL_GAMES else "Quick Quiz" if step.game_key == "quiz" else "Estimation"), "game_key": step.game_key,
              "room_code": step.room_code, "config": step.duel_match_config,
              "status": next(iter(step.duel_instances.values_list("match_status", flat=True)), "planned")}
             for step in session.steps.order_by("order", "pk")]
    return JsonResponse({"active": session.is_active, "steps": steps, "check_in": get_check_in_state(session),
        "match_url": reverse(page_name, args=[session.code, running.pk, "participant"]) if running else None,
        "spectator_url": reverse(page_name, args=[session.code, running.pk, "spectator"]) if running else None,
        "participants": list(session.participants.order_by("pk").values("nickname"))})


@never_cache
@ensure_csrf_cookie
@_errors
def match_page(request, session_code, instance_id, role, game_key="estimation"):
    if role not in ("host", "participant", "spectator"):
        raise Http404
    session = _session(session_code, duel_only=False)
    get_object_or_404(DuelGameInstance, pk=instance_id, hub_session=session, game_key=game_key)
    if role == "host":
        _context(request, session_code, instance_id, None, role, game_key=game_key)
    template = "duel/control.html" if game_key in CONTROL_GAMES else "duel/inputs.html" if game_key in INPUT_GAMES else "duel/turns.html" if game_key in TURN_GAMES else "duel/oral.html" if game_key in ORAL_GAMES else "duel/quiz.html" if game_key == "quiz" else "duel/estimation.html"
    from .tournaments import pair_for, page_url as tournament_url
    pair = pair_for(instance_id)
    return render(request, template, {"session": session, "instance_id": instance_id, "role": role,
        'tournament_url': tournament_url(pair.tournament_set.tournament, role) if pair else None,
        'tournament_api': reverse('duel:tournament_api', args=[session_code, pair.tournament_set.tournament_id, role]) if pair else None,
        'tournament_number': pair.tournament_set.number if pair else None,
        'tournament_duration': pair.tournament_set.selection.get('duration_seconds', '') if pair else '',
        "game_key": game_key, "title": CONTROL_GAMES[game_key][1] if game_key in CONTROL_GAMES else INPUT_GAMES[game_key][1] if game_key in INPUT_GAMES else TURN_GAMES[game_key][1] if game_key in TURN_GAMES else ORAL_GAMES[game_key][1] if game_key in ORAL_GAMES else "",
        "match_api_url": reverse("duel:match_api" if game_key == "estimation" else f"duel:{game_key}_match_api",args=[session_code,instance_id,role])})


@never_cache
@vary_on_headers("Authorization", "Cookie")
@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
@_errors
def match_api(request, session_code, instance_id, role, game_key="estimation"):
    if role not in ("host", "participant", "spectator"):
        raise Http404
    _session(session_code, duel_only=False)
    if game_key != "estimation" and request.GET:
        raise ValueError("No caller-selected projection parameters.")
    instance, actor = _context(request, session_code, instance_id, None, role, game_key=game_key)
    result = None
    if request.method == "POST":
        if role != "host":
            raise PermissionDenied
        data = _body(request)
        if set(data) == {'action', 'client_action_id'} and data['action'] == 'finish_draw':
            from .match_service import finish_exhausted_match
            result = finish_exhausted_match(instance_id=instance.pk, actor=actor, client_action_id=data['client_action_id'])
        elif set(data) != {"action", "question_id", "previous_unit_id", "client_action_id"} or data["action"] != "prepare":
            raise ValueError("Ungültige Vorbereitung.")
        else:
            result = prepare_question(instance_id=instance.pk, actor=actor, client_action_id=data["client_action_id"],
                question_id=data["question_id"], previous_unit_id=data["previous_unit_id"])
    def read_snapshot():
        # One read transaction: same revision/phase and score visibility, no writes.
        with transaction.atomic():
            instance = get_object_or_404(DuelGameInstance.objects.select_for_update().select_related(
                "hub_game_step", "participant_a", "participant_b"), pk=instance_id)
            unit = instance.units.order_by("-number").first()
            state = _snapshot(instance, unit.pk, actor, role)["snapshot"] if unit else None
            if state and state["phase"] == "prepared" and role != "host":
                state.pop("question", None)
            # Hide the current match tally until publication of the current result.
            # The next unit retains earlier published points, without leaking its own.
            tally = get_match_snapshot(instance.pk) if (not state or not state.get('tournament_waiting')) and (game_key in {*INPUT_GAMES, *CONTROL_GAMES} or not state or state["phase"] in ("show_result", "completed", "prepared")) else None
            data = {"instance_id": str(instance.pk), "role": role, "state_revision": instance.state_revision,
                "match_status": instance.match_status, "unit": state, "match": tally,
                "names": {"a": instance.participant_a.nickname, "b": instance.participant_b.nickname},
                "intro": serialize_game_intro(instance.hub_game_step),
                "unit_api": reverse("duel:" + game_key + "_" + role, args=[session_code, instance.pk, unit.pk]) if unit else None}
            if game_key != "estimation":
                data["parties"] = {"a": instance.participant_a_id, "b": instance.participant_b_id}
            if role == "host":
                from .material_supply import exhausted
                data['can_finish_draw'] = bool(instance.match_status == 'in_progress' and state and state['phase'] == 'completed'
                    and instance.hub_game_step.game_format != 'tournament' and exhausted(instance))
                from .material_supply import choices
                data['questions'] = choices(instance)
            if result:
                data["action"] = {"status": result.status.value, "code": result.data.get("code"), "replayed": result.replayed}
            return JsonResponse(data, status=409 if result and result.status.value == "rejected" else 200)

    return _retry_sqlite_locked(read_snapshot)
