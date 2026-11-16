"""Bridge for supported planned DUEL slots; no Multiplayer start calls."""
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from django.db import transaction
from django.db.models import Q
from django.urls import reverse

from games_hub.host_permissions import authorize_game_host, user_can_manage_hub_session
from games_hub.models import HubGameStep, HubSession
from games_hub.game_intro import start_game_intro
from .commands import ActorContext, CommandRejected, HandlerResult, execute_action
from .contracts import MatchStatus, SessionType
from .estimation_questions import bind_estimation_question_in_action
from .match_service import start_match_in_action, get_match_snapshot
from .models import DuelGameInstance, DuelUnit, ReceiptActorType, ReceiptStatus
from .oral_material import ORAL_GAMES
from .turn_material import TURN_GAMES
from .input_material import INPUT_GAMES
from .control_games import CONTROL_GAMES


def slot_identity(step):
    # This start operation is one run of a planned slot. Replaying a finished
    # slot never restarts it. A future explicit restart needs a NEW run identity.
    return uuid5(NAMESPACE_URL, f"quizmaster:duel:{step.game_key}:{step.session_id}:{step.pk}")


def start_slot(*, user, session_code, step_id, participant_ids=None, match_config=None):
    step = HubGameStep.objects.select_related("session").get(pk=step_id, session__code=session_code)
    instance_id = slot_identity(step)

    def initialize():
        session = HubSession.objects.select_for_update().get(pk=step.session_id)
        locked = HubGameStep.objects.select_for_update().get(pk=step.pk, session=session)
        permission = authorize_game_host(user, locked.game_key, locked.room_code, session.code)
        if not permission.allowed:
            raise CommandRejected("unauthorized", "Keine Berechtigung für diesen Spielslot.")
        manual = participant_ids is not None
        if ((manual and session.session_type != 'multiplayer') or (not manual and session.session_type != SessionType.DUEL.value)
                or locked.game_key not in {"estimation", "quiz", *ORAL_GAMES, *TURN_GAMES, *INPUT_GAMES, *CONTROL_GAMES}):
            raise CommandRejected("unsupported_duel", "Dieser Spieltyp ist noch nicht für 2-Spieler-Sessions freigegeben.")
        existing = DuelGameInstance.objects.filter(pk=instance_id).first()
        if existing:
            if manual and (participant_ids != [existing.participant_a_id, existing.participant_b_id] or match_config != locked.duel_match_config):
                raise CommandRejected('pairing_frozen', 'Paarung und Matchkonfiguration sind bereits festgelegt.')
            return
        if not session.is_active or session.ended_at:
            raise CommandRejected("session_inactive", "Aktiviere zuerst diese Session.")
        from .tournaments import active_tournament
        if active_tournament(session): raise CommandRejected('active_tournament', 'Beende zuerst das laufende Turnier.')
        if session.readiness_checks.filter(active=True, ended_at__isnull=True).exists():
            raise CommandRejected("readiness_active", "Beende zuerst den Bereitschaftscheck.")
        if not session.check_in_completed:
            raise CommandRejected("check_in_required", "Schließe zuerst den Teilnehmer-Check-in ab.")
        if manual:
            if (not session.started_at or type(participant_ids) is not list or len(participant_ids) != 2
                    or any(type(pk) is not int for pk in participant_ids) or participant_ids[0] == participant_ids[1]):
                raise CommandRejected('invalid_parties', 'Wähle zwei verschiedene anwesende Teilnehmer der laufenden Session.')
            rows = {p.pk: p for p in session.participants.select_for_update().filter(pk__in=participant_ids,
                is_active=True, left_permanently_at__isnull=True, check_in_excluded_by_host=False)}
            parties = [rows[pk] for pk in participant_ids if pk in rows]
            from games_hub.active_game_guard import _iter_session_games
            if any(game.status == 'active' for _, game in _iter_session_games(session)):
                raise CommandRejected('active_multiplayer', 'Beende zuerst das laufende Mehrspielerspiel.')
            if locked.result_snapshot or locked.intro_started_at:
                raise CommandRejected('slot_already_started', 'Dieser Spielslot wurde bereits gestartet.')
            locked.duel_match_config = match_config
        else:
            parties = list(session.get_official_participants().select_for_update().filter(
                left_permanently_at__isnull=True, checked_in_at__isnull=False).order_by("pk"))
        if len(parties) != 2 or (not manual and (session.participants.count() != 2 or session.locked_participant_count != 2)):
            raise CommandRejected("invalid_parties", "Für dieses Duell müssen genau zwei gültige Teilnehmer eingecheckt sein.")
        try:
            from .control_games import validate_config
            config = locked.get_duel_match_config()
        except (ValueError, TypeError) as exc:
            raise CommandRejected("match_not_configured", "Wähle im Spielplan Best of N oder eine Zielpunktzahl und speichere die Konfiguration.") from exc
        validate_config(locked.game_key, config)
        if DuelGameInstance.objects.filter(hub_session=session, hub_game_step__game_format='duel').filter(
                Q(match_status__in=["not_started", "in_progress"]) | Q(game_key="quiz", match_status="completed",
                    units__quiz_round_state__phase="show_result") | Q(game_key__in=ORAL_GAMES, match_status="completed",
                    units__oral_round__phase="show_result") | Q(game_key__in=TURN_GAMES, match_status="completed",
                    units__turn_round__phase="show_result") | Q(game_key__in=INPUT_GAMES, match_status="completed", units__input_round__phase="show_result") | Q(game_key__in=CONTROL_GAMES, match_status="completed", units__control_round__phase="show_result")).exists():
            raise CommandRejected("active_match", "Schließe zuerst das laufende Duell ab.")
        locked.game_format = "duel"
        locked.save(update_fields=["game_format", "duel_match_config"])
        instance = DuelGameInstance(id=instance_id, hub_session=session, hub_game_step=locked,
            participant_a=parties[0], participant_b=parties[1], game_key=locked.game_key)
        instance.full_clean()
        instance.save(force_insert=True)

    def handler(context):
        locked = HubGameStep.objects.select_for_update().select_related("session").get(pk=step_id)
        result = start_match_in_action(context, locked.get_duel_match_config())
        if result.status == ReceiptStatus.APPLIED:
            session = locked.session
            session.current_step_index = list(session.steps.order_by("order", "pk").values_list("pk", flat=True)).index(locked.pk)
            session.save(update_fields=["current_step_index"])
            start_game_intro(session.code, locked.game_key, locked.room_code, step_id=locked.pk)
        return result

    result = execute_action(instance_id=instance_id, actor=ActorContext(ReceiptActorType.HOST, user.pk),
        client_action_id=f"hub-slot:{step_id}:start", action_type="duel.hub.start",
        payload={"step_id": step_id, **({'participant_ids': participant_ids, 'config': match_config} if participant_ids is not None else {})}, handler=handler, _initialize_instance=initialize)
    if result.status == ReceiptStatus.REJECTED and result.data.get("code") != "match_already_started":
        raise CommandRejected(result.data.get("code", "start_failed"), "Das Duell konnte nicht gestartet werden.")
    return {"success": True, "duel": True, "instance_id": str(instance_id),
            "host_url": reverse("duel:match_page" if step.game_key == "estimation" else f"duel:{step.game_key}_match_page", args=[session_code, instance_id, "host"])}


def prepare_question(*, instance_id, actor, client_action_id, question_id, previous_unit_id=None):
    if actor.actor_type != ReceiptActorType.HOST:
        raise CommandRejected("host_required", "Nur der Host darf Fragen vorbereiten.")
    if type(question_id) is not int or question_id <= 0:
        raise ValueError("Eine Frage muss ausgewählt werden.")
    game_key = DuelGameInstance.objects.get(pk=instance_id).game_key
    def handler(context):
        # Old simultaneous-goal records acquire their persisted boundary on the
        # next authorized write; snapshots remain read-only.
        if get_match_snapshot(context.instance.pk)['evaluation'] == 'sudden_death' and context.instance.tiebreak_unit_id is None:
            from .match_service import reconcile_match_in_action
            reconcile_match_in_action(context)
        last = context.instance.units.order_by("-number").first()
        if (last.pk if last else None) != previous_unit_id:
            return HandlerResult(ReceiptStatus.REJECTED, {"code": "stale_unit"})
        if last:
            state = getattr(last, "control_round" if game_key in CONTROL_GAMES else "input_round" if game_key in INPUT_GAMES else "turn_round" if game_key in TURN_GAMES else "oral_round" if game_key in ORAL_GAMES else "quiz_round_state" if game_key == "quiz" else "estimation_round_state", None)
            if state is None or state.phase != "completed":
                return HandlerResult(ReceiptStatus.REJECTED, {"code": "unit_not_finished"})
        unit = DuelUnit(game_instance=context.instance, number=last.number+1 if last else 1)
        unit.full_clean()
        unit.save()
        if game_key in CONTROL_GAMES:
            from .controllers.control import bind_control_in_action
            return bind_control_in_action(replace(context, unit=unit), question_id)
        if game_key in INPUT_GAMES:
            from .controllers.inputs import bind_input_question_in_action
            return bind_input_question_in_action(replace(context, unit=unit), question_id)
        if game_key in TURN_GAMES:
            from .controllers.turns import bind_turn_question_in_action
            return bind_turn_question_in_action(replace(context, unit=unit), question_id)
        if game_key in ORAL_GAMES:
            from .controllers.oral import bind_oral_question_in_action
            return bind_oral_question_in_action(replace(context, unit=unit), question_id)
        if game_key == "quiz":
            from .controllers.quiz import bind_quiz_question_in_action
            return bind_quiz_question_in_action(replace(context, unit=unit), question_id)
        return bind_estimation_question_in_action(replace(context, unit=unit), question_id)
    return execute_action(instance_id=instance_id, actor=actor, client_action_id=client_action_id,
        action_type=f"duel.{game_key}.prepare_question", payload={"question_id": question_id, "previous_unit_id": previous_unit_id}, handler=handler)


def save_plan(user, session, configs):
    """Server-side planning validation; no implicit defaults or runtime edits."""
    if not user_can_manage_hub_session(user, session) or session.session_type != "duel":
        raise CommandRejected("unauthorized", "Keine Berechtigung für diesen Duellspielplan.")
    with transaction.atomic():
        locked = HubSession.objects.select_for_update().get(pk=session.pk)
        steps = list(locked.steps.select_for_update().order_by("order", "pk"))
        if type(configs) is not dict or set(configs) != {str(step.pk) for step in steps}:
            raise ValueError("Der vollständige Spielplan muss gespeichert werden.")
        for step in steps:
            if step.game_key not in {"estimation", "quiz", *ORAL_GAMES, *TURN_GAMES, *INPUT_GAMES, *CONTROL_GAMES} or not authorize_game_host(user, step.game_key, step.room_code, session.code).allowed:
                raise ValueError("Der Spielplan enthält einen nicht unterstützten oder fremden Spieltyp.")
            if step.duel_instances.exists():
                if configs[str(step.pk)] != step.duel_match_config:
                    raise ValueError("Die Konfiguration eines gestarteten Slots ist eingefroren.")
                from .control_games import validate_config
                validate_config(step.game_key, step.get_duel_match_config())
                continue
            step.duel_match_config = configs[str(step.pk)]
            step.full_clean()
            from .control_games import validate_config
            validate_config(step.game_key, step.get_duel_match_config())
        for step in steps:
            step.save(update_fields=["duel_match_config"])
