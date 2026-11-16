"""Explicit worker entry point. Reconstructs all due clocks from persisted data."""
from django.utils import timezone
from .commands import ActorContext, CommandRejected
from .estimation_rounds import reconcile_estimation_deadline
from .models import DuelEstimationRoundState, ReceiptActorType


def reconcile_due_rounds():
    applied = 0
    rows = DuelEstimationRoundState.objects.filter(phase="answering", deadline__lte=timezone.now(),
        unit__game_instance__match_status="in_progress", unit__game_instance__game_key='estimation',
        unit__game_instance__hub_game_step__game_format__in=['duel','tournament']).select_related(
            "unit__game_instance__hub_session", "unit__estimation_question_binding__quiz")
    # Exhaust the read cursor before starting write transactions (SQLite readers
    # must not hold one another's commit locks while competing for IMMEDIATE).
    for row in list(rows):
        instance = row.unit.game_instance
        host_id = instance.hub_session.creator_id or row.unit.estimation_question_binding.quiz.creator_id
        try:
            result = reconcile_estimation_deadline(instance_id=instance.pk, unit_id=row.unit_id,
                actor=ActorContext(ReceiptActorType.HOST, host_id),
                client_action_id=f"deadline:{row.pk}:{row.deadline.isoformat()}")
            applied += result.status.value == "applied" and not result.replayed
        except CommandRejected:
            # Another serialized command may already have closed the instance.
            continue
    from .models import DuelQuizRoundState
    from .controllers.quiz import reconcile_quiz_deadline
    quiz_rows = DuelQuizRoundState.objects.filter(phase="answering", deadline__lte=timezone.now(),
        unit__game_instance__match_status="in_progress", unit__game_instance__game_key="quiz",
        unit__game_instance__hub_game_step__game_format__in=["duel","tournament"]).select_related(
            "unit__game_instance__hub_session", "unit__quiz_question_binding__quiz")
    for row in list(quiz_rows):
        instance = row.unit.game_instance
        host_id = instance.hub_session.creator_id or row.unit.quiz_question_binding.quiz.creator_id
        try:
            result = reconcile_quiz_deadline(instance_id=instance.pk, unit_id=row.unit_id,
                actor=ActorContext(ReceiptActorType.HOST, host_id),
                client_action_id=f"quiz-deadline:{row.pk}:{row.deadline.isoformat()}")
            applied += result.status.value == "applied" and not result.replayed
        except CommandRejected:
            continue
    from .models import DuelOralRound
    from .controllers.oral import reconcile_deadline
    from .oral_material import ORAL_GAMES
    oral_rows = DuelOralRound.objects.filter(phase="answering",deadline__lte=timezone.now(),
        unit__game_instance__game_key__in=ORAL_GAMES,unit__game_instance__match_status="in_progress",
        unit__game_instance__hub_game_step__game_format__in=["duel","tournament"]).select_related("unit__game_instance__hub_session", "unit__game_instance__hub_game_step")
    for row in list(oral_rows):
        instance = row.unit.game_instance
        host_id = instance.hub_session.creator_id
        if host_id is None:
            host_id = ORAL_GAMES[instance.game_key][0].objects.get(room_code=instance.hub_game_step.room_code).creator_id
        try:
            result = reconcile_deadline(instance_id=instance.pk,unit_id=row.unit_id,
                actor=ActorContext(ReceiptActorType.HOST,host_id),
                client_action_id=f"oral-deadline:{row.pk}:{row.deadline.isoformat()}")
            applied += result.status.value == "applied" and not result.replayed
        except CommandRejected:
            continue
    from .models import DuelInputRound
    from .controllers.inputs import reconcile_deadline as reconcile_input
    from .input_material import INPUT_GAMES
    rows = DuelInputRound.objects.filter(phase="answering", deadline__lte=timezone.now(),
        unit__game_instance__game_key__in=INPUT_GAMES, unit__game_instance__match_status="in_progress",
        unit__game_instance__hub_game_step__game_format__in=["duel","tournament"]).select_related("unit__game_instance__hub_session", "unit__game_instance__hub_game_step")
    for row in list(rows):
        instance = row.unit.game_instance
        host_id = instance.hub_session.creator_id or INPUT_GAMES[instance.game_key][0].objects.get(room_code=instance.hub_game_step.room_code).creator_id
        try:
            result = reconcile_input(instance_id=instance.pk, unit_id=row.unit_id,
                actor=ActorContext(ReceiptActorType.HOST, host_id), question_index=row.question_index,
                client_action_id=f"input-deadline:{row.pk}:{row.question_index}:{row.deadline.isoformat()}")
            applied += result.status.value == "applied" and not result.replayed
        except CommandRejected:
            continue
    from django.db.models import Q
    from .models import DuelControlRound
    from .controllers.control import host_action
    rows = DuelControlRound.objects.filter(Q(phase__in=["bidding", "needs_resolution"], bid_deadline__lte=timezone.now()) | Q(phase="answering", answer_deadline__lte=timezone.now()),
        unit__game_instance__game_key="bid", unit__game_instance__match_status="in_progress",
        unit__game_instance__hub_game_step__game_format__in=["duel","tournament"]).select_related("unit__game_instance__hub_session")
    for row in list(rows):
        instance = row.unit.game_instance
        host_id = instance.hub_session.creator_id
        if host_id is None:
            from .models import BidGame
            host_id = BidGame.objects.get(room_code=instance.hub_game_step.room_code).creator_id
        try:
            result = host_action(instance_id=instance.pk, unit_id=row.unit_id,
                actor=ActorContext(ReceiptActorType.HOST, host_id), action="reconcile",
                client_action_id=f"control-deadline:{row.pk}:{row.phase}")
            applied += result.status.value == "applied" and not result.replayed
        except CommandRejected:
            continue
    return applied
