"""Persistent duel identities only; no start, scoring or game mechanics."""

import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, router
from django.db.models.functions import Cast
from django.utils import timezone

from .contracts import MatchStatus, RoundOutcome
from .matches import MatchConfig, MatchMode


class DuelTournament(models.Model):
    """Organization only; points/answers remain in the existing encounters."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    step = models.OneToOneField('games_hub.HubGameStep', on_delete=models.DO_NOTHING, related_name='tournament')
    coordinator = models.OneToOneField('DuelGameInstance', on_delete=models.DO_NOTHING, null=True, blank=True, related_name='coordinated_tournament')
    status = models.CharField(max_length=16, choices=[('running', 'Running'), ('completed', 'Completed')], default='running')
    current_set = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(status__in=['running', 'completed']), name='duel_tournament_status'),
            models.CheckConstraint(condition=models.Q(status='running', completed_at__isnull=True) | models.Q(status='completed', completed_at__isnull=False), name='duel_tournament_completion')]


class DuelTournamentSet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tournament = models.ForeignKey(DuelTournament, on_delete=models.DO_NOTHING, related_name='sets')
    number = models.PositiveIntegerField()
    selection = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tournament', 'number'], name='duel_tournament_set_number'),
            models.CheckConstraint(condition=models.Q(number__gt=0), name='duel_tournament_set_positive')]


class DuelTournamentPair(models.Model):
    tournament_set = models.ForeignKey(DuelTournamentSet, on_delete=models.DO_NOTHING, related_name='pairs')
    game_instance = models.OneToOneField('DuelGameInstance', on_delete=models.DO_NOTHING, related_name='tournament_pair')
    position = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tournament_set', 'position'], name='duel_tournament_pair_position'),
            models.CheckConstraint(condition=models.Q(position__gt=0), name='duel_tournament_pair_positive')]

    def clean(self):
        super().clean()
        if self.game_instance_id and self.tournament_set_id:
            step = self.tournament_set.tournament.step
            if (self.game_instance.hub_game_step_id != step.pk or self.game_instance.hub_session_id != step.session_id
                    or self.game_instance.game_key != step.game_key):
                raise ValidationError('Encounter and tournament must use the same slot, session and game.')


class DuelTournamentSeat(models.Model):
    """One frozen place per eligible person and set, including the bye."""
    tournament_set = models.ForeignKey(DuelTournamentSet, on_delete=models.DO_NOTHING, related_name='seats')
    participant = models.ForeignKey('games_hub.HubParticipant', on_delete=models.DO_NOTHING, related_name='tournament_seats')
    pair = models.ForeignKey(DuelTournamentPair, on_delete=models.DO_NOTHING, null=True, blank=True, related_name='seats')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tournament_set', 'participant'], name='duel_tournament_one_seat')]

    def clean(self):
        super().clean()
        if self.tournament_set_id and self.participant_id:
            if self.participant.session_id != self.tournament_set.tournament.step.session_id:
                raise ValidationError('A tournament seat must belong to its session.')
            if self.pair_id and (self.pair.tournament_set_id != self.tournament_set_id or self.participant_id not in
                    (self.pair.game_instance.participant_a_id, self.pair.game_instance.participant_b_id)):
                raise ValidationError('The seat must belong to this set and its concrete encounter.')


def bid_room_code():
    while True:
        code = "G" + uuid.uuid4().hex[:5].upper()
        if not BidGame.objects.filter(room_code=code).exists():
            return code


class BidGame(models.Model):
    """Local, duel-only content configuration; covered by the duel sync exclusion."""
    title = models.CharField(max_length=200)
    creator = models.ForeignKey("auth.User", on_delete=models.DO_NOTHING, related_name="created_bid_games")
    room_code = models.CharField(max_length=6, unique=True, default=bid_room_code, editable=False)
    status = models.CharField(max_length=20, default="waiting", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)


class BidTask(models.Model):
    game = models.ForeignKey(BidGame, on_delete=models.CASCADE, related_name="tasks")
    text = models.TextField()
    bid_seconds = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    answer_seconds = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(bid_seconds__gt=0, answer_seconds__gt=0), name="duel_bid_task_durations")]


class ControlPhase(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    BIDDING = "bidding", "Hidden bids"
    ANSWERING = "answering", "Oral answers"
    READY = "ready", "Buzzer ready"
    OPEN = "open", "Buzzer open"
    LOCKED = "locked", "Answer right assigned"
    BLOCKED = "blocked", "All parties blocked"
    NEEDS_RESOLUTION = "needs_resolution", "Missing bid unresolved"
    SHOW_RESULT = "show_result", "Result"
    COMPLETED = "completed", "Completed"


class DuelControlRound(models.Model):
    """Durable phase/clock/counter; bids, buzzes, decisions and points stay separate.

    Frozen content belongs to this occurrence, never to a mutable source task.
    Normal writes must use execute_action; save() does not authorize/validate.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField("duel.DuelUnit", on_delete=models.DO_NOTHING, related_name="control_round")
    material = models.JSONField(default=dict)
    phase = models.CharField(max_length=24, choices=ControlPhase.choices, default=ControlPhase.PREPARED.value)
    bid_started_at = models.DateTimeField(null=True, blank=True)
    bid_deadline = models.DateTimeField(null=True, blank=True)
    answer_started_at = models.DateTimeField(null=True, blank=True)
    answer_deadline = models.DateTimeField(null=True, blank=True)
    right_participant = models.ForeignKey("games_hub.HubParticipant", null=True, blank=True, on_delete=models.DO_NOTHING, related_name="bid_answer_rights")
    counter = models.PositiveIntegerField(default=0)
    phase_changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(phase__in=ControlPhase.values), name="duel_control_phase"),
            models.CheckConstraint(condition=models.Q(bid_deadline__isnull=True) | models.Q(bid_deadline__gt=models.F("bid_started_at")), name="duel_control_bid_clock"),
            models.CheckConstraint(condition=models.Q(answer_deadline__isnull=True) | models.Q(answer_deadline__gt=models.F("answer_started_at")), name="duel_control_answer_clock"),
            models.CheckConstraint(condition=~models.Q(phase="bidding") | models.Q(bid_started_at__isnull=False, bid_deadline__isnull=False), name="duel_control_bidding_clock"),
            models.CheckConstraint(condition=~models.Q(phase="answering", right_participant__isnull=False) | models.Q(answer_started_at__isnull=False, answer_deadline__isnull=False), name="duel_control_right_clock"),
        ]

    def clean(self):
        super().clean()
        if type(self.material) is not dict: raise ValidationError("Frozen material must be an object.")
        unit = DuelUnit.objects.filter(pk=self.unit_id).select_related("game_instance").first()
        if unit:
            instance = unit.game_instance
            if instance.game_key not in {"bid", "buzzer", "host_points"}: raise ValidationError("Unsupported control game.")
            if self.right_participant_id and self.right_participant_id not in (instance.participant_a_id, instance.participant_b_id):
                raise ValidationError("The answer right must belong to a match party.")
            expected = "prepared" if self.phase == "prepared" else "completed" if self.phase in {"show_result", "completed"} else "active"
            if unit.status != expected: raise ValidationError("Unit and control lifecycle disagree.")
            if instance.game_key == "bid":
                if self.phase == "answering" and (not self.right_participant_id or not self.answer_started_at or not self.answer_deadline):
                    raise ValidationError("A bid answer phase requires its accepted right and server clock.")
                if self.phase in {"prepared", "bidding", "needs_resolution"} and (self.right_participant_id or self.answer_started_at or self.answer_deadline or self.counter):
                    raise ValidationError("No answer right, clock or count exists before an accepted bid.")
        for field in ("bid_started_at", "bid_deadline", "answer_started_at", "answer_deadline", "phase_changed_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value): raise ValidationError("Aware server timestamps are required.")
        old = type(self).objects.filter(pk=self.pk).values().first()
        if old:
            if old["unit_id"] != self.unit_id or old["material"] != self.material: raise ValidationError("Frozen control material is immutable.")
            for field in ("bid_started_at", "bid_deadline", "answer_started_at", "answer_deadline", "right_participant_id"):
                if old[field] is not None and old[field] != getattr(self, field): raise ValidationError("Accepted clocks and bid right cannot be replaced.")
            if self.counter < old["counter"]: raise ValidationError("The answer counter cannot move backwards.")
            transitions = {"prepared": {"bidding", "ready", "answering"}, "bidding": {"answering", "needs_resolution", "show_result"},
                "needs_resolution": {"answering", "show_result"},
                "ready": {"open", "show_result"}, "open": {"locked", "show_result"}, "locked": {"open", "blocked", "show_result"},
                "blocked": {"show_result"}, "answering": {"show_result"}, "show_result": {"completed"}}
            if self.phase != old["phase"] and self.phase not in transitions.get(old["phase"], set()):
                raise ValidationError("Invalid control phase transition.")


class DuelGameInstance(models.Model):
    """One independent run; a HubGameStep may have multiple such runs.

    Call full_clean() for model validation. Like ordinary Django models, save()
    and bulk writes do not automatically call it. The future transactional
    start/write path must lock and recheck the referenced session, step and
    participants, their session membership and eligibility, and the allowed
    status/configuration/revision changes. clean() alone cannot prevent races.

    DO_NOTHING avoids adding queries to the new, possibly not yet migrated table
    when existing Hub objects are deleted. Database foreign keys still prevent
    orphaned instances once this table exists. Deleting parents with duel data
    will require an explicit lifecycle policy; there is no cascading deletion
    of duel history in this step.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hub_session = models.ForeignKey(
        "games_hub.HubSession", on_delete=models.DO_NOTHING,
        related_name="duel_instances",
    )
    hub_game_step = models.ForeignKey(
        "games_hub.HubGameStep", on_delete=models.DO_NOTHING,
        related_name="duel_instances",
    )
    participant_a = models.ForeignKey(
        "games_hub.HubParticipant", on_delete=models.DO_NOTHING,
        related_name="duel_instances_as_a",
    )
    participant_b = models.ForeignKey(
        "games_hub.HubParticipant", on_delete=models.DO_NOTHING,
        related_name="duel_instances_as_b",
    )
    game_key = models.CharField(max_length=32)
    match_status = models.CharField(
        max_length=20,
        choices=[(status.value, status.name.replace("_", " ").title()) for status in MatchStatus],
        default=MatchStatus.NOT_STARTED.value,
    )
    rule_config = models.JSONField(default=dict, blank=True)
    state_revision = models.PositiveBigIntegerField(default=0)
    tiebreak_unit = models.ForeignKey('duel.DuelUnit', on_delete=models.DO_NOTHING,
        null=True, blank=True, editable=False, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(participant_a=models.F("participant_b")),
                name="duel_distinct_participants",
            ),
            models.CheckConstraint(
                condition=models.Q(match_status__in=[status.value for status in MatchStatus]),
                name="duel_valid_match_status",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.participant_a_id is not None and self.participant_a_id == self.participant_b_id:
            errors["participant_b"] = "A duel requires two different participants."
        if not isinstance(self.rule_config, dict):
            errors["rule_config"] = "Rule configuration must be a JSON object."
        if self.tiebreak_unit_id is not None and not DuelUnit.objects.filter(
                pk=self.tiebreak_unit_id, game_instance_id=self.pk, status='completed').exists():
            errors['tiebreak_unit'] = 'The tiebreak must refer to a completed unit of this match.'

        # Cross-table checks cannot be expressed as ordinary CHECK constraints.
        # Query stored membership, not potentially stale cached related objects.
        if self.hub_session_id is not None:
            alias = self._state.db or router.db_for_read(type(self), instance=self)
            for name in ("hub_game_step", "participant_a", "participant_b"):
                field = self._meta.get_field(name)
                related_id = getattr(self, field.attname)
                if related_id is None:
                    continue
                session_id = (
                    field.remote_field.model._base_manager.using(alias)
                    .filter(pk=related_id).values_list("session_id", flat=True).first()
                )
                # full_clean() reports nonexistent FK targets separately.
                if session_id is not None and session_id != self.hub_session_id:
                    errors[name] = "The referenced object must belong to the instance's HubSession."
        if errors:
            raise ValidationError(errors)


class QuizPhase(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    ANSWERING = "answering", "Answering"
    AWAITING_DECISION = "awaiting_decision", "Awaiting host decision"
    AWAITING_REVEAL = "awaiting_reveal", "Awaiting reveal"
    SHOW_ANSWERS = "show_answers", "Show answers"
    SHOW_SOLUTION = "show_solution", "Show solution"
    SHOW_RESULT = "show_result", "Show result"
    COMPLETED = "completed", "Completed"


class InputPhase(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    ANSWERING = "answering", "Answering"
    AWAITING_REVEAL = "awaiting_reveal", "Awaiting host reveal"
    SHOW_ANSWERS = "show_answers", "Answers"
    SHOW_SOLUTION = "show_solution", "Solution"
    QUESTION_RESULT = "question_result", "Question evaluated, set continues"
    NEEDS_RESOLUTION = "needs_resolution", "Scoring unresolved"
    SHOW_RESULT = "show_result", "Round result"
    COMPLETED = "completed", "Completed"


class DuelInputRound(models.Model):
    """Frozen Where question or Black Jack set, including durable question clocks.

    One Unit is one match round (a whole Black Jack set). Previous question
    windows are append-only; inputs remain solely in DuelSubmission. No star
    totals, points or independent winner are cached here. Authorized commands
    validate all relationships; save() does not call full_clean().
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField("duel.DuelUnit", on_delete=models.DO_NOTHING, related_name="input_round")
    material = models.JSONField()
    image = models.BinaryField(null=True, blank=True)
    question_index = models.PositiveIntegerField(default=0)
    history = models.JSONField(default=list, blank=True)
    phase = models.CharField(max_length=24, choices=InputPhase.choices, default=InputPhase.PREPARED.value)
    started_at = models.DateTimeField(null=True, blank=True)
    question_started_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    reveal_started_at = models.DateTimeField(null=True, blank=True)
    phase_changed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(phase__in=InputPhase.values), name="duel_input_phase"),
            models.CheckConstraint(condition=(
                models.Q(phase="prepared", question_started_at__isnull=True, deadline__isnull=True, closed_at__isnull=True, reveal_started_at__isnull=True)
                | models.Q(phase="answering", started_at__isnull=False, question_started_at__isnull=False, deadline__isnull=False, closed_at__isnull=True, reveal_started_at__isnull=True)
                | models.Q(phase="awaiting_reveal", started_at__isnull=False, question_started_at__isnull=False, deadline__isnull=False, closed_at__isnull=False, reveal_started_at__isnull=True)
                | models.Q(phase__in=InputPhase.values[3:], started_at__isnull=False, question_started_at__isnull=False, deadline__isnull=False, closed_at__isnull=False, reveal_started_at__isnull=False)
            ), name="duel_input_phase_clock"),
            models.CheckConstraint(condition=models.Q(deadline__isnull=True) | models.Q(deadline__gt=models.F("question_started_at")), name="duel_input_deadline_order"),
            models.CheckConstraint(condition=models.Q(closed_at__isnull=True) | (models.Q(closed_at__gte=models.F("question_started_at")) & models.Q(closed_at__lte=models.F("deadline"))), name="duel_input_close_order"),
        ]

    def clean(self):
        super().clean()
        if type(self.material) is not dict or type(self.history) is not list:
            raise ValidationError("A frozen question set and clock history are required.")
        questions = self.material.get("questions", [])
        if not questions or type(self.question_index) is not int or not 0 <= self.question_index < len(questions) or len(self.history) != self.question_index:
            raise ValidationError("The concrete question index/history is invalid.")
        for field in ("started_at", "question_started_at", "deadline", "closed_at", "reveal_started_at", "phase_changed_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                raise ValidationError({field: "An aware server time is required."})
        old = type(self).objects.filter(pk=self.pk).values().first()
        if old:
            if any(old[f] != getattr(self, f) for f in ("unit_id", "material")) or (bytes(old["image"]) if old["image"] is not None else None) != (bytes(self.image) if self.image is not None else None):
                raise ValidationError("Frozen material cannot be replaced.")
            advancing = self.question_index == old["question_index"]+1 and old["phase"] == "question_result" and self.phase == "prepared"
            if advancing:
                if len(self.history) != len(old["history"])+1 or self.history[:-1] != old["history"]:
                    raise ValidationError("Question history must be appended once.")
            elif self.question_index != old["question_index"] or self.history != old["history"]:
                raise ValidationError("Question identity/history cannot be replaced.")
            if old["started_at"] and old["started_at"] != self.started_at:
                raise ValidationError("A set cannot be restarted.")
            if not advancing:
                for field in ("question_started_at", "deadline", "closed_at", "reveal_started_at"):
                    if old[field] is not None and old[field] != getattr(self, field):
                        raise ValidationError("An accepted question clock cannot be changed.")
                transitions = {"prepared": {"answering"}, "answering": {"awaiting_reveal"},
                    "awaiting_reveal": {"show_answers", "show_solution"}, "show_answers": {"show_solution"},
                    "show_solution": {"question_result", "show_result", "needs_resolution"}, "needs_resolution": {"show_result"}, "show_result": {"completed"}}
                if self.phase != old["phase"] and self.phase not in transitions.get(old["phase"], set()):
                    raise ValidationError("Invalid input phase transition.")
        elif self.phase != "prepared" or self.question_index or self.history or self.started_at:
            raise ValidationError("A new round must be prepared.")
        unit = DuelUnit.objects.filter(pk=self.unit_id).select_related("game_instance").first()
        if unit:
            if unit.game_instance.game_key not in {"where", "blackjack"}:
                raise ValidationError("This is not a Where/Black Jack round.")
            expected = "prepared" if self.started_at is None else "completed" if self.phase in {"show_result", "completed"} else "active"
            if unit.status != expected or (self.started_at and unit.started_at != self.started_at):
                raise ValidationError("Unit and question/set lifecycle disagree.")


class TurnPhase(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    PLAYING = "playing", "Alternating turns"
    AWAITING_RESULT = "awaiting_result", "Round ended, awaiting reveal"
    NEEDS_RESOLUTION = "needs_resolution", "Solved, scoring unresolved"
    RESOLUTION_REVEALED = "resolution_revealed", "Solution shown, scoring unresolved"
    SHOW_RESULT = "show_result", "Result"
    COMPLETED = "completed", "Completed"


class DuelTurnRound(models.Model):
    """Frozen oral task and append-only accepted turns; authorized commands only.

    Decisions use TURN + this UUID/turn number. The parent command transaction
    serializes appends. A direct save does not call full_clean or authorize a
    host. Source identities are audited within the instance's concrete game.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField("duel.DuelUnit", on_delete=models.DO_NOTHING, related_name="turn_round")
    source_question_id = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    material = models.JSONField()
    actions = models.JSONField(default=list, blank=True)
    starting_participant = models.ForeignKey("games_hub.HubParticipant", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="started_duel_turn_rounds")
    phase = models.CharField(max_length=24, choices=TurnPhase.choices, default=TurnPhase.PREPARED.value)
    started_at = models.DateTimeField(null=True, blank=True)
    phase_changed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(phase__in=TurnPhase.values), name="duel_turn_phase"),
            models.CheckConstraint(condition=models.Q(source_question_id__gt=0), name="duel_turn_source_positive"),
            models.CheckConstraint(condition=(
                models.Q(phase="prepared", started_at__isnull=True, starting_participant__isnull=True)
                | (~models.Q(phase="prepared") & models.Q(started_at__isnull=False, starting_participant__isnull=False))
            ), name="duel_turn_start_context"),
        ]

    def clean(self):
        super().clean()
        if type(self.material) is not dict or type(self.actions) is not list:
            raise ValidationError("Frozen task and accepted turns must be JSON objects/list.")
        for field in ("started_at", "phase_changed_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                raise ValidationError({field: "An aware server time is required."})
        old = type(self).objects.filter(pk=self.pk).values().first()
        if old:
            if any(old[f] != getattr(self, f) for f in ("unit_id", "source_question_id", "material")):
                raise ValidationError("Frozen task cannot be replaced.")
            if self.actions[:len(old["actions"])] != old["actions"] or len(self.actions) < len(old["actions"]):
                raise ValidationError("Accepted turns cannot be replaced or removed.")
            if old["started_at"] and (old["started_at"], old["starting_participant_id"]) != (self.started_at, self.starting_participant_id):
                raise ValidationError("Starting identity cannot be changed.")
            transitions = {"prepared": {"playing"}, "playing": {"awaiting_result", "needs_resolution"},
                "awaiting_result": {"show_result"}, "needs_resolution": {"resolution_revealed", "show_result"}, "resolution_revealed": {"show_result"},
                "show_result": {"completed"}}
            if self.phase != old["phase"] and self.phase not in transitions.get(old["phase"], set()):
                raise ValidationError("Invalid turn phase transition.")
        elif self.phase != TurnPhase.PREPARED or self.actions:
            raise ValidationError("A new task starts prepared with no turns.")
        unit = DuelUnit.objects.filter(pk=self.unit_id).select_related("game_instance").first()
        if unit:
            instance = unit.game_instance
            if instance.game_key not in {"assign", "sorting_ladder", "wer_weiss_mehr"}:
                raise ValidationError("Not a supported alternating duel.")
            if self.starting_participant_id is not None and self.starting_participant_id not in (instance.participant_a_id, instance.participant_b_id):
                raise ValidationError("Starting participant must be a duel party.")
            expected = "prepared" if self.phase == "prepared" else "completed" if self.phase in {"show_result", "completed"} else "active"
            if unit.status != expected or (self.started_at and self.started_at != unit.started_at):
                raise ValidationError("Unit and alternating task lifecycle disagree.")


class OralPhase(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    ANSWERING = "answering", "Answering"
    AWAITING_DECISION = "awaiting_decision", "Awaiting host decision"
    AWAITING_RESULT = "awaiting_result", "Awaiting result release"
    SHOW_RESULT = "show_result", "Result"
    COMPLETED = "completed", "Completed"


class DuelOralRound(models.Model):
    """Frozen material and clock for the four oral duel games.

    source_question_id is an audit identity scoped by the instance's game_key;
    binding verifies the actual selected source through its concrete model.
    No GenericForeignKey or live-source lookup is used for running rounds.
    Frozen PNG bytes avoid mutable files/descriptive solution-bearing filenames.
    Normal writes exclusively use execute_action; save does not full_clean.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField("duel.DuelUnit", on_delete=models.DO_NOTHING, related_name="oral_round")
    source_question_id = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    material = models.JSONField()
    image = models.BinaryField(null=True, blank=True)
    phase = models.CharField(max_length=24, choices=OralPhase.choices, default=OralPhase.PREPARED.value)
    started_at = models.DateTimeField(null=True, blank=True)
    resumed_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    elapsed_us = models.PositiveBigIntegerField(default=0)
    closed_at = models.DateTimeField(null=True, blank=True)
    phase_changed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(phase__in=OralPhase.values), name="duel_oral_phase"),
            models.CheckConstraint(condition=models.Q(source_question_id__gt=0), name="duel_oral_source_positive"),
            models.CheckConstraint(condition=(
                models.Q(phase="prepared", started_at__isnull=True, resumed_at__isnull=True, deadline__isnull=True, closed_at__isnull=True, elapsed_us=0)
                | models.Q(phase="answering", started_at__isnull=False, resumed_at__isnull=False, deadline__isnull=False, closed_at__isnull=True)
                | models.Q(phase="awaiting_decision", started_at__isnull=False, resumed_at__isnull=True, deadline__isnull=True, closed_at__isnull=True)
                | models.Q(phase__in=["awaiting_result", "show_result", "completed"], started_at__isnull=False, resumed_at__isnull=True, deadline__isnull=True, closed_at__isnull=False)
            ), name="duel_oral_phase_clock"),
            models.CheckConstraint(condition=models.Q(deadline__isnull=True) | models.Q(deadline__gt=models.F("resumed_at")), name="duel_oral_deadline_order"),
        ]

    def clean(self):
        super().clean()
        for field in ("started_at", "resumed_at", "deadline", "closed_at", "phase_changed_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                raise ValidationError({field: "An aware server time is required."})
        old = type(self).objects.filter(pk=self.pk).values().first()
        if old:
            for field in ("unit_id", "source_question_id", "material", "image"):
                a, b = old[field], getattr(self, field)
                if field == "image":
                    a, b = bytes(a) if a is not None else None, bytes(b) if b is not None else None
                if a != b:
                    raise ValidationError("Frozen oral material cannot be replaced.")
            if old["started_at"] and old["started_at"] != self.started_at:
                raise ValidationError("A round cannot be restarted.")
            if old["closed_at"] and old["closed_at"] != self.closed_at:
                raise ValidationError("An existing closure cannot be changed.")
            if self.elapsed_us < old["elapsed_us"]:
                raise ValidationError("The round clock cannot go backwards.")
            transitions = {"prepared": {"answering"}, "answering": {"awaiting_decision", "show_result", "awaiting_result"},
                "awaiting_decision": {"answering", "show_result"}, "awaiting_result": {"show_result"}, "show_result": {"completed"}}
            if self.phase != old["phase"] and self.phase not in transitions.get(old["phase"], set()):
                raise ValidationError("Invalid oral phase transition.")
        elif self.phase != OralPhase.PREPARED:
            raise ValidationError("A new round must be prepared.")
        unit = DuelUnit.objects.filter(pk=self.unit_id).select_related("game_instance").first()
        if unit:
            if unit.game_instance.game_key not in {"clue_rush", "who", "who_that", "wann_war_das"}:
                raise ValidationError("This is not a supported oral duel unit.")
            expected = "prepared" if self.phase == "prepared" else "completed" if self.phase in {"show_result", "completed"} else "active"
            if unit.status != expected or (self.started_at is not None and unit.started_at != self.started_at):
                raise ValidationError("Unit lifecycle and oral phase disagree.")
        if type(self.material) is not dict or type(self.material.get("duration_us")) is not int or self.material["duration_us"] <= 0:
            raise ValidationError("A frozen, finite positive duration is required.")


class DuelBuzz(models.Model):
    """One server-accepted attempt. Clue Rush allows one per party and unit.

    captured is private authoritative context, never a caller-selected name or
    tolerance. Decisions reference this immutable UUID through subject_key.
    Direct ORM writes are outside the authorized command boundary.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.ForeignKey("duel.DuelUnit", on_delete=models.DO_NOTHING, related_name="buzzes")
    participant = models.ForeignKey("games_hub.HubParticipant", on_delete=models.DO_NOTHING, related_name="duel_buzzes")
    sequence = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    accepted_at = models.DateTimeField(default=timezone.now, editable=False)
    elapsed_us = models.PositiveBigIntegerField()
    captured = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["unit", "participant"], name="duel_buzz_once_per_party"),
            models.UniqueConstraint(fields=["unit", "sequence"], name="duel_buzz_sequence"),
            models.CheckConstraint(condition=models.Q(sequence__gt=0), name="duel_buzz_positive_sequence"),
        ]

    def clean(self):
        super().clean()
        old = type(self).objects.filter(pk=self.pk).values().first()
        if old and any(old[f.attname] != getattr(self, f.attname) for f in self._meta.fields):
            raise ValidationError("An accepted buzz is immutable.")
        unit = DuelUnit.objects.filter(pk=self.unit_id).select_related("game_instance").first()
        if unit and self.participant_id not in (unit.game_instance.participant_a_id, unit.game_instance.participant_b_id):
            raise ValidationError("Only a party of this unit may buzz.")


class DuelQuizQuestionBinding(models.Model):
    """Frozen material for one occurrence; normal writes use the quiz controller.

    DO_NOTHING preserves existing parent deletion paths before migration. SQL
    protects identity/required fields; full_clean and the locked command check
    quiz/session membership and material immutability. save does not validate.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField("duel.DuelUnit", on_delete=models.DO_NOTHING, related_name="quiz_question_binding")
    question = models.ForeignKey("QuizGame.QuizQuestion", on_delete=models.DO_NOTHING, related_name="duel_bindings")
    quiz = models.ForeignKey("QuizGame.Quiz", on_delete=models.DO_NOTHING, related_name="duel_question_bindings")
    question_text = models.TextField()
    question_type = models.CharField(max_length=20, choices=[(v, v) for v in ("short_answer", "true_false", "multiple_choice")])
    options = models.JSONField(default=list, blank=True)
    solution = models.TextField()
    correct_option = models.CharField(max_length=5, blank=True)
    time_limit = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    reveal_ms = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(question_type__in=["short_answer", "true_false", "multiple_choice"]), name="duel_quiz_material_type"),
            models.CheckConstraint(condition=~models.Q(question_text="") & ~models.Q(solution=""), name="duel_quiz_material_nonempty"),
            models.CheckConstraint(condition=models.Q(time_limit__gt=0, reveal_ms__gt=0), name="duel_quiz_material_timing"),
        ]

    def clean(self):
        super().clean()
        old = type(self).objects.filter(pk=self.pk).values().first()
        if old:
            if any(old[f.attname] != getattr(self, f.attname) for f in self._meta.fields):
                raise ValidationError("Stored quiz material is immutable.")
            return
        unit = DuelUnit.objects.filter(pk=self.unit_id).select_related("game_instance__hub_game_step").first()
        quiz_model = self._meta.get_field("quiz").remote_field.model
        quiz = quiz_model.objects.filter(pk=self.quiz_id).first()
        if unit:
            instance, step = unit.game_instance, unit.game_instance.hub_game_step
            if (instance.game_key != "quiz" or step.game_key != "quiz" or step.session_id != instance.hub_session_id
                    or unit.status != UnitStatus.PREPARED or unit.started_at or unit.completed_at
                    or (quiz and step.room_code != quiz.room_code)):
                raise ValidationError("Prepared Quick Quiz material must match the instance and slot.")
        if quiz and not quiz.selected_questions.filter(pk=self.question_id).exists():
            raise ValidationError("The question is not selected in the planned quiz.")
        if type(self.options) is not list or any(type(o) is not dict or set(o) != {"key", "text"}
                or any(type(v) is not str for v in o.values()) for o in self.options):
            raise ValidationError("Invalid frozen answer options.")
        if self.question_type != "short_answer" and self.correct_option not in [o["key"] for o in self.options]:
            raise ValidationError("The correct option must exist in the frozen options.")


class DuelQuizRoundState(models.Model):
    """Persisted phase, deadline and the single accepted oral answer right.

    No duplicated scores or answers. All transitions/decisions are serialized by
    execute_action. full_clean checks cross-table consistency; direct ORM writes
    do not provide authorization, transitions or an immutable journal.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField("duel.DuelUnit", on_delete=models.DO_NOTHING, related_name="quiz_round_state")
    phase = models.CharField(max_length=24, choices=QuizPhase.choices, default=QuizPhase.PREPARED.value)
    started_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    answering_closed_at = models.DateTimeField(null=True, blank=True)
    phase_changed_at = models.DateTimeField(default=timezone.now, editable=False)
    buzz_id = models.UUIDField(null=True, blank=True, unique=True)
    buzz_participant = models.ForeignKey("games_hub.HubParticipant", null=True, blank=True, on_delete=models.DO_NOTHING, related_name="duel_quiz_buzzes")
    buzzed_at = models.DateTimeField(null=True, blank=True)
    frozen_characters = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(phase__in=QuizPhase.values), name="duel_quiz_phase_valid"),
            models.CheckConstraint(condition=(
                models.Q(phase="prepared", started_at__isnull=True, deadline__isnull=True, answering_closed_at__isnull=True)
                | models.Q(phase="answering", started_at__isnull=False, deadline__isnull=False, answering_closed_at__isnull=True)
                | models.Q(phase__in=QuizPhase.values[2:], started_at__isnull=False, deadline__isnull=False, answering_closed_at__isnull=False)
            ), name="duel_quiz_phase_clock"),
            models.CheckConstraint(condition=models.Q(deadline__isnull=True) | models.Q(deadline__gt=models.F("started_at")), name="duel_quiz_deadline_order"),
            models.CheckConstraint(condition=models.Q(answering_closed_at__isnull=True) | (
                models.Q(answering_closed_at__gte=models.F("started_at")) & models.Q(answering_closed_at__lte=models.F("deadline"))
                & models.Q(phase_changed_at__gte=models.F("answering_closed_at"))), name="duel_quiz_close_order"),
            models.CheckConstraint(condition=(
                models.Q(buzz_id__isnull=True, buzz_participant__isnull=True, buzzed_at__isnull=True, frozen_characters__isnull=True)
                | models.Q(buzz_id__isnull=False, buzz_participant__isnull=False, buzzed_at__isnull=False, frozen_characters__isnull=False)
            ), name="duel_quiz_buzz_complete"),
            models.CheckConstraint(condition=~models.Q(phase="awaiting_decision") | models.Q(buzz_id__isnull=False), name="duel_quiz_decision_has_buzz"),
        ]

    def clean(self):
        super().clean()
        for field in ("started_at", "deadline", "answering_closed_at", "phase_changed_at", "buzzed_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                raise ValidationError({field: "Server timestamps must be timezone-aware."})
        old = type(self).objects.filter(pk=self.pk).values().first()
        if old:
            for fields in (("unit_id",), ("started_at", "deadline"), ("answering_closed_at",),
                           ("buzz_id", "buzz_participant_id", "buzzed_at", "frozen_characters")):
                if old[fields[0]] is not None and any(old[f] != getattr(self, f) for f in fields):
                    raise ValidationError("An existing round identity, clock, closure or buzz cannot be replaced.")
            transitions = {"prepared": {"answering"}, "answering": {"awaiting_decision", "awaiting_reveal"},
                "awaiting_decision": {"show_result"}, "awaiting_reveal": {"show_answers"},
                "show_answers": {"show_solution"}, "show_solution": {"show_result"}, "show_result": {"completed"}}
            if old["phase"] != self.phase and self.phase not in transitions.get(old["phase"], set()):
                raise ValidationError("Invalid quiz phase transition.")
        elif self.phase != QuizPhase.PREPARED:
            raise ValidationError("A new round must be prepared.")
        unit = DuelUnit.objects.filter(pk=self.unit_id).select_related("game_instance").first()
        if unit:
            if unit.game_instance.game_key != "quiz" or not DuelQuizQuestionBinding.objects.filter(unit=unit).exists():
                raise ValidationError("Frozen Quick Quiz material is required.")
            expected = UnitStatus.PREPARED if self.phase == QuizPhase.PREPARED else UnitStatus.COMPLETED if self.phase in (QuizPhase.SHOW_RESULT, QuizPhase.COMPLETED) else UnitStatus.ACTIVE
            if unit.status != expected or (self.started_at is not None and unit.started_at != self.started_at):
                raise ValidationError("Unit lifecycle and quiz phase disagree.")
            if self.buzz_participant_id and self.buzz_participant_id not in (unit.game_instance.participant_a_id, unit.game_instance.participant_b_id):
                raise ValidationError("Only a party of this instance may hold the answer right.")


class UnitStatus(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"


class DuelUnit(models.Model):
    """A single occurrence, independent of material and parent match status.

    The integer primary key preserves DuelInstanceContext.current_unit_id;
    uuid supplies an additional unique occurrence identity. number is supplied
    explicitly, is one-based and is unique only within game_instance. There is
    no automatic numbering, contiguity check, timer or lifecycle transition.

    No common question/set reference format has been established yet. A later
    material reference or content snapshot belongs to each occurrence and must
    allow the same source to appear in several units of the same instance.

    full_clean() validates fields, uniqueness and timestamp order. save() and
    bulk writes do not call it; the database also enforces the constraints in
    Meta. Optional timestamps are not inferred from status. When both are set,
    completion must not precede start; equal timestamps are valid. The future
    transactional write path must recheck ownership and allocate unit numbers
    under a parent lock. These declarations do not implement that write path.

    DO_NOTHING keeps parent deletion paths from querying this table before its
    migration is applied. The database FK prevents orphaned units afterwards;
    deleting a parent with units will require an explicit lifecycle policy.
    """

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    game_instance = models.ForeignKey(
        DuelGameInstance, on_delete=models.DO_NOTHING, related_name="units",
    )
    number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(
        max_length=20, choices=UnitStatus.choices, default=UnitStatus.PREPARED.value,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["game_instance", "number"], name="duel_unit_instance_number",
            ),
            models.CheckConstraint(
                condition=models.Q(number__gt=0), name="duel_unit_positive_number",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=UnitStatus.values), name="duel_unit_valid_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(started_at__isnull=True)
                    | models.Q(completed_at__isnull=True)
                    | models.Q(completed_at__gte=models.F("started_at"))
                ),
                name="duel_unit_chronological_times",
            ),
        ]

    def clean(self):
        super().clean()
        # full_clean() still invokes clean() after a field has failed parsing.
        if isinstance(self.started_at, datetime) and isinstance(self.completed_at, datetime):
            if self.completed_at < self.started_at:
                raise ValidationError({"completed_at": "Completion must not precede start."})


class SubmissionKind(models.TextChoices):
    ANSWER = "answer", "Answer"
    BID = "bid", "Bid"


class SubmissionStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    COMMITTED = "committed", "Committed"


class DuelSubmission(models.Model):
    """Generic participant input; no interpretation, allocation or commit API.

    full_clean() checks stored party membership. save() and bulk writes do not
    invoke it. Cross-table eligibility cannot be guaranteed by CHECK constraints.
    The future central transactional write path must lock the unit/instance,
    recheck authorization and lifecycle, generate committed_at on the server,
    and allocate acceptance_sequence atomically. Neither value may come from
    the client. After commitment that path must preserve the original payload,
    identity, kind, timestamp and sequence, including on retried requests.
    These declarations alone do not enforce committed-row immutability.

    A multi-part answer can be one payload. Black Jack uses a server-defined
    input_key to distinguish question occurrences within the same set/Unit.
    All earlier games retain the empty key and their original uniqueness.
    Buzzer actions and host decisions remain separate, not submissions.

    Nullable acceptance fields support drafts only; committed rows require
    both. Payload shape and permitted content belong to future controllers;
    no correct solution is fetched or added here. DO_NOTHING avoids queries
    to an unmigrated submission table on existing parent deletion paths.
    Database foreign keys prevent orphaned rows after migration; parent
    deletion with submission history requires an explicit retention policy.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.ForeignKey(
        DuelUnit, on_delete=models.DO_NOTHING, related_name="submissions",
    )
    participant = models.ForeignKey(
        "games_hub.HubParticipant", on_delete=models.DO_NOTHING,
        related_name="duel_submissions",
    )
    submission_kind = models.CharField(max_length=10, choices=SubmissionKind.choices)
    input_key = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=10, choices=SubmissionStatus.choices, default=SubmissionStatus.DRAFT.value,
    )
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    acceptance_sequence = models.PositiveBigIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)],
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(submission_kind__in=SubmissionKind.values),
                name="duel_submission_valid_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=SubmissionStatus.values),
                name="duel_submission_valid_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="draft", committed_at__isnull=True, acceptance_sequence__isnull=True)
                    | models.Q(
                        status="committed", committed_at__isnull=False,
                        acceptance_sequence__isnull=False, acceptance_sequence__gt=0,
                    )
                ),
                name="duel_submission_state",
            ),
            models.UniqueConstraint(
                fields=["unit", "participant", "submission_kind"],
                condition=models.Q(status="committed", input_key=""), name="duel_unique_committed_input",
            ),
            models.UniqueConstraint(
                fields=["unit", "participant", "submission_kind", "input_key"],
                condition=models.Q(status="committed") & ~models.Q(input_key=""), name="duel_scoped_committed_input",
            ),
            models.UniqueConstraint(
                fields=["unit", "acceptance_sequence"],
                condition=models.Q(acceptance_sequence__isnull=False),
                name="duel_unit_acceptance_sequence",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.status == SubmissionStatus.DRAFT:
            if self.committed_at is not None:
                errors["committed_at"] = "Drafts cannot have a commitment timestamp."
            if self.acceptance_sequence is not None:
                errors["acceptance_sequence"] = "Drafts cannot have an acceptance sequence."
        elif self.status == SubmissionStatus.COMMITTED:
            if self.committed_at is None:
                errors["committed_at"] = "Committed input requires a server timestamp."
            if self.acceptance_sequence is None:
                errors["acceptance_sequence"] = "Committed input requires a server sequence."

        if self.unit_id is not None and self.participant_id is not None:
            alias = self._state.db or router.db_for_read(type(self), instance=self)
            parties = (
                DuelUnit._base_manager.using(alias).filter(pk=self.unit_id)
                .values_list("game_instance__participant_a_id", "game_instance__participant_b_id", "game_instance__game_key")
                .first()
            )
            # full_clean() validates missing/nonexistent FK targets separately.
            if parties is not None and self.participant_id not in parties[:2]:
                errors["participant"] = "Only a party of this duel instance may submit input."
            if self.input_key and parties is not None and parties[2] != "blackjack":
                errors["input_key"] = "Scoped input is reserved for Black Jack question occurrences."
        if errors:
            raise ValidationError(errors)


class DuelScoreEntry(models.Model):
    """One signed point booking, independent of round wins and match status.

    source_key is an opaque, stable business cause, unique per instance and
    party. It is not generated here. A correction is a NEW signed booking with
    its own cause, not a replacement of an earlier row. There is no stored
    total, default points formula or automatic winner/target calculation.

    full_clean() validates integer input and stored party/unit membership;
    save() and bulk writes do not call it. The database enforces Meta's local
    constraints and FKs, not the cross-table membership checks. The integer
    check also rejects fractional stored values on SQLite's permissive column
    types. Direct save() still uses Django's normal integer coercion.

    The future central transactional write path must lock the instance/unit,
    recheck authorization, party/unit ownership, phase and once-only evaluation,
    and commit all bookings of a result atomically. It must create stable cause
    keys, preserve completed rows against updates/deletes and authorize new
    correction bookings. Uniqueness alone does not prove an evaluation was
    completed. These declarations do not implement those guarantees or an API.

    No metadata schema is currently required. DO_NOTHING avoids querying this
    table on old parent-deletion paths before migration; database FKs prevent
    orphaned bookings afterwards. Deleting referenced history will require an
    explicit retention policy, not implicit cascading journal deletion.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    game_instance = models.ForeignKey(
        DuelGameInstance, on_delete=models.DO_NOTHING, related_name="score_entries",
    )
    unit = models.ForeignKey(
        DuelUnit, on_delete=models.DO_NOTHING, related_name="score_entries",
        null=True, blank=True,
    )
    participant = models.ForeignKey(
        "games_hub.HubParticipant", on_delete=models.DO_NOTHING,
        related_name="duel_score_entries",
    )
    delta = models.BigIntegerField()
    source_key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game_instance", "participant", "source_key"],
                name="duel_score_unique_cause",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_key=""), name="duel_score_nonempty_source",
            ),
            models.CheckConstraint(
                condition=models.Q(delta=Cast(models.F("delta"), models.BigIntegerField())),
                name="duel_score_integer_delta",
            ),
        ]

    def clean_fields(self, exclude=None):
        # IntegerField normally truncates floats; preserve ScoreChange's strict
        # integer contract instead of accepting a silently altered point delta.
        if "delta" not in (exclude or ()) and self.delta is not None and type(self.delta) is not int:
            raise ValidationError({"delta": "Point changes must be integers."})
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        errors = {}
        if isinstance(self.source_key, str) and not self.source_key.strip():
            errors["source_key"] = "A booking requires a nonempty business cause."
        alias = self._state.db or router.db_for_read(type(self), instance=self)
        if self.game_instance_id is not None:
            parties = (
                DuelGameInstance._base_manager.using(alias).filter(pk=self.game_instance_id)
                .values_list("participant_a_id", "participant_b_id").first()
            )
            if parties is not None and self.participant_id is not None and self.participant_id not in parties:
                errors["participant"] = "Only a party of this duel instance may receive a booking."
            if self.unit_id is not None:
                instance_id = (
                    DuelUnit._base_manager.using(alias)
                    .filter(pk=self.unit_id).values_list("game_instance_id", flat=True).first()
                )
                # full_clean() reports missing/nonexistent FK targets separately.
                if instance_id is not None and instance_id != self.game_instance_id:
                    errors["unit"] = "The unit must belong to the booking's duel instance."
        if errors:
            raise ValidationError(errors)


class ReceiptActorType(models.TextChoices):
    PARTICIPANT = "participant", "Participant"
    HOST = "host", "Host"


class ReceiptStatus(models.TextChoices):
    APPLIED = "applied", "Applied"
    REJECTED = "rejected", "Rejected"


class DuelActionReceipt(models.Model):
    """Completed action result only; no reservation, execution or retry API.

    actor_type namespaces actor_key: the future server adapter uses the stable
    string HubParticipant.pk for a participant, and the authenticated User.pk
    for an authorized host. Neither nickname nor a shared 'host' placeholder is
    an identity. No authentication or actor lookup is implemented here; actor
    identity/eligibility must never be accepted from an untrusted payload.

    client_action_id is an explicit opaque string, including existing UUID
    strings. It has no generated default and is scoped to instance and actor,
    NOT action_type or unit: changing either must not bypass retry detection.
    request_fingerprint stores a 64-character SHA-256 hex digest. Its future
    server-side canonical input must cover action type, unit/context and action
    content; browser clocks and client-supplied digests are not authoritative.
    This model neither computes a digest nor compares retries.

    There is no PENDING state and no success default. The future central write
    path must authenticate first, check instance/unit access, find a receipt,
    return its result only to that same actor on an identical retry, and reject
    conflicting ID reuse. New actions must lock/recheck context and phase,
    persist business changes, a terminal receipt and the instance revision in
    one transaction, and notify sockets only after commit. Completed receipts
    must retain their original identity, fingerprint and result.

    result must later contain only the minimal actor-private retry response,
    never a full secret snapshot, unrevealed solutions or other parties' data.
    No game state is fetched or added here. Response filtering and replay
    authorization belong to that future path, not to arbitrary JSON key tests.

    full_clean() checks fields and stored unit ownership. save()/bulk writes
    do not call it; the cross-table relation must be rechecked under the future
    transaction lock. DO_NOTHING preserves existing deletion paths before this
    table is migrated; database FKs prevent orphaned receipts afterwards.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    game_instance = models.ForeignKey(
        DuelGameInstance, on_delete=models.DO_NOTHING, related_name="action_receipts",
    )
    unit = models.ForeignKey(
        DuelUnit, on_delete=models.DO_NOTHING, related_name="action_receipts",
        null=True, blank=True,
    )
    actor_type = models.CharField(max_length=16, choices=ReceiptActorType.choices)
    actor_key = models.CharField(max_length=128)
    client_action_id = models.CharField(max_length=128)
    action_type = models.CharField(max_length=64)
    request_fingerprint = models.CharField(
        max_length=64,
        validators=[RegexValidator(
            regex=r"\A[0-9a-fA-F]{64}\Z",
            message="The request fingerprint must contain exactly 64 hexadecimal characters.",
        )],
    )
    status = models.CharField(max_length=8, choices=ReceiptStatus.choices)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game_instance", "actor_type", "actor_key", "client_action_id"],
                name="duel_receipt_unique_action",
            ),
            models.CheckConstraint(
                condition=models.Q(actor_type__in=ReceiptActorType.values),
                name="duel_receipt_valid_actor",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=ReceiptStatus.values),
                name="duel_receipt_valid_status",
            ),
            models.CheckConstraint(
                condition=~models.Q(actor_key=""), name="duel_receipt_nonempty_actor",
            ),
            models.CheckConstraint(
                condition=~models.Q(client_action_id=""), name="duel_receipt_nonempty_id",
            ),
            models.CheckConstraint(
                condition=~models.Q(action_type=""), name="duel_receipt_nonempty_type",
            ),
            models.CheckConstraint(
                condition=models.Q(request_fingerprint__regex=r"\A[0-9a-fA-F]{64}\Z"),
                name="duel_receipt_valid_fingerprint",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for name in ("actor_key", "client_action_id", "action_type"):
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                errors[name] = "An action receipt requires a nonempty identifier."
        if self.game_instance_id is not None and self.unit_id is not None:
            alias = self._state.db or router.db_for_read(type(self), instance=self)
            instance_id = (
                DuelUnit._base_manager.using(alias).filter(pk=self.unit_id)
                .values_list("game_instance_id", flat=True).first()
            )
            # full_clean() reports missing/nonexistent FK targets separately.
            if instance_id is not None and instance_id != self.game_instance_id:
                errors["unit"] = "The unit must belong to the receipt's duel instance."
        if errors:
            raise ValidationError(errors)


class DecisionSubjectType(models.TextChoices):
    BUZZ = "buzz", "Buzz"
    TURN = "turn", "Turn"
    SUBMISSION = "submission", "Submission"


class DecisionVerdict(models.TextChoices):
    CORRECT = "correct", "Correct"
    INCORRECT = "incorrect", "Incorrect"


class DuelDecision(models.Model):
    """Original binding host verdict for ONE concrete attempt, without effects.

    subject_type/subject_key identify an attempt, not a participant or question
    number. Keys are opaque stable server-resolved IDs (including existing
    submission UUIDs). Buzz/turn models do not yet exist; there is no generic FK,
    lookup, attempt allocation or subject-specific validation here.

    host_key follows DuelActionReceipt/ActorContext.key: the string ID of the
    server-confirmed, authorized host User. It is not a nickname or shared 'host'
    constant. The timestamp defaults to the server clock; neither host identity
    nor authoritative decision time may later be accepted from client payloads.
    These fields and model validation are NOT authentication or authorization.

    full_clean() checks stored unit ownership; save()/bulk writes do not call
    it. The future host controller must use the unchanged duel.commands write
    boundary, recheck authorization, instance/unit/attempt ownership, current
    answer right and phase under its lock, resolve the concrete subject from
    trusted state and prevent changes/deletion of an existing decision. It will
    apply game-specific consequences in that SAME transaction. This model never
    books points, changes revision/lifecycle, locks players or triggers reveals.

    The UUID permits a later append-only correction/revision structure to refer
    to the original decision and record the correcting host, time, reason and
    replacement verdict. That structure is not implemented here. Corrections
    must preserve this original row; uniqueness must not be removed to insert
    a second original verdict for the same attempt. Score compensation is a
    separate future controller concern.

    DO_NOTHING avoids queries to an unmigrated decision table on existing parent
    deletion paths. Database FKs prevent orphaned decisions after migration;
    deleting referenced history requires an explicit retention policy.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    game_instance = models.ForeignKey(
        DuelGameInstance, on_delete=models.DO_NOTHING, related_name="decisions",
    )
    unit = models.ForeignKey(
        DuelUnit, on_delete=models.DO_NOTHING, related_name="decisions",
    )
    subject_type = models.CharField(max_length=16, choices=DecisionSubjectType.choices)
    subject_key = models.CharField(max_length=128)
    verdict = models.CharField(max_length=16, choices=DecisionVerdict.choices)
    host_key = models.CharField(max_length=128)
    decided_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["unit", "subject_type", "subject_key"], name="duel_decision_unique_attempt",
            ),
            models.CheckConstraint(
                condition=models.Q(verdict__in=DecisionVerdict.values), name="duel_decision_valid_verdict",
            ),
            models.CheckConstraint(
                condition=models.Q(subject_type__in=DecisionSubjectType.values),
                name="duel_decision_valid_subject",
            ),
            models.CheckConstraint(
                condition=~models.Q(subject_key=""), name="duel_decision_nonempty_subject",
            ),
            models.CheckConstraint(
                condition=~models.Q(host_key=""), name="duel_decision_nonempty_host",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for name in ("subject_key", "host_key"):
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                errors[name] = "A decision requires a nonempty attempt and host identity."
        if self.decided_at is None:
            errors["decided_at"] = "A decision timestamp is required."
        if self.game_instance_id is not None and isinstance(self.unit_id, int):
            alias = self._state.db or router.db_for_read(type(self), instance=self)
            instance_id = (
                DuelUnit._base_manager.using(alias).filter(pk=self.unit_id)
                .values_list("game_instance_id", flat=True).first()
            )
            # full_clean() reports missing/nonexistent FK targets separately.
            if instance_id is not None and instance_id != self.game_instance_id:
                errors["unit"] = "The unit must belong to the decision's duel instance."
        if errors:
            raise ValidationError(errors)


FINAL_UNIT_OUTCOMES = tuple(value for value in RoundOutcome if value != RoundOutcome.UNDECIDED)


class DuelUnitOutcome(models.Model):
    """One original final result per occurrence; never an implicit point award.

    UNDECIDED is represented by the absence of a final result, not a completed
    result row. game_instance is unambiguously derived through unit, so no
    redundant instance FK is stored. full_clean() checks the winner's side;
    save()/bulk writes skip that cross-table validation. The match service must
    hold the existing command lock and complete unit+result atomically.
    Original outcomes must not be updated/deleted by ordinary commands;
    future corrections need a separate authorized audit path.
    DO_NOTHING preserves unmigrated parent deletion paths and existing history.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField(DuelUnit, on_delete=models.DO_NOTHING, related_name="outcome")
    outcome = models.CharField(
        max_length=32, choices=[(value.value, value.name) for value in FINAL_UNIT_OUTCOMES],
    )
    winner = models.ForeignKey(
        "games_hub.HubParticipant", on_delete=models.DO_NOTHING,
        related_name="duel_unit_wins", null=True, blank=True,
    )
    completed_at = models.DateTimeField(default=timezone.now, editable=False)
    reason = models.CharField(max_length=32, blank=True, default='')

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(outcome__in=[value.value for value in FINAL_UNIT_OUTCOMES]),
                name="duel_unit_outcome_final",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(outcome__in=[RoundOutcome.PARTY_A_WINS.value, RoundOutcome.PARTY_B_WINS.value], winner__isnull=False)
                    | models.Q(outcome__in=[RoundOutcome.DRAW.value, RoundOutcome.NO_WINNER.value], winner__isnull=True)
                ), name="duel_unit_outcome_winner",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.completed_at is None:
            errors["completed_at"] = "A final result requires a completion timestamp."
        if isinstance(self.unit_id, int):
            alias = self._state.db or router.db_for_read(type(self), instance=self)
            parties = DuelUnit.objects.using(alias).filter(pk=self.unit_id).values_list(
                "game_instance__participant_a_id", "game_instance__participant_b_id",
            ).first()
            if parties is not None:
                expected = {RoundOutcome.PARTY_A_WINS.value: parties[0], RoundOutcome.PARTY_B_WINS.value: parties[1]}
                if self.outcome in expected and self.winner_id != expected[self.outcome]:
                    errors["winner"] = "The winner must match the declared side of this unit's instance."
        if self.outcome in (RoundOutcome.DRAW.value, RoundOutcome.NO_WINNER.value) and self.winner_id is not None:
            errors["winner"] = "A draw or no-winner result cannot name a winner."
        if errors:
            raise ValidationError(errors)


class DuelMatchOutcome(models.Model):
    """Immutable original completion record, not a second match runtime.

    The parent remains authoritative for lifecycle. mode/threshold freeze only
    the completion rule; scores and wins are NEVER duplicated here. full_clean
    validates party membership and configuration, while ordinary save/bulk
    writes do not. The match service verifies the authoritative tally and
    writes this row plus COMPLETED parent status in one execute_action call.
    Direct ORM calls cannot establish that cross-table invariant. No ordinary
    update/delete or reopening is authorized; corrections require a future
    audited feature. DO_NOTHING preserves history and unmigrated delete paths.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    game_instance = models.OneToOneField(
        DuelGameInstance, on_delete=models.DO_NOTHING, related_name="match_outcome",
    )
    winner = models.ForeignKey(
        "games_hub.HubParticipant", on_delete=models.DO_NOTHING, related_name="duel_match_wins",
        null=True, blank=True,
    )
    reason = models.CharField(max_length=32, default='win', choices=[
        ('win', 'Winner'), ('material_exhausted', 'Material exhausted'), ('tournament_no_winner', 'Tournament draw')])
    mode = models.CharField(max_length=16, choices=[(mode.value, mode.name) for mode in MatchMode])
    threshold = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    completed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(mode__in=[mode.value for mode in MatchMode]), name="duel_match_outcome_mode"),
            models.CheckConstraint(condition=models.Q(threshold__gt=0), name="duel_match_positive_threshold"),
            models.CheckConstraint(condition=(models.Q(reason='win', winner__isnull=False)
                | models.Q(reason__in=['material_exhausted', 'tournament_no_winner'], winner__isnull=True)), name='duel_match_completion_kind'),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.completed_at is None:
            errors["completed_at"] = "A match completion timestamp is required."
        try:
            mode = MatchMode(self.mode)
            MatchConfig(mode, **{"best_of" if mode == MatchMode.BEST_OF_N else "target_score": self.threshold})
        except (TypeError, ValueError):
            errors["threshold"] = "A valid explicit completion rule is required."
        if self.game_instance_id is not None:
            alias = self._state.db or router.db_for_read(type(self), instance=self)
            parties = DuelGameInstance.objects.using(alias).filter(pk=self.game_instance_id).values_list(
                "participant_a_id", "participant_b_id",
            ).first()
            if parties is not None and self.winner_id is not None and self.winner_id not in parties:
                errors["winner"] = "Only a party of this instance may win the match."
        if errors:
            raise ValidationError(errors)


class DuelEstimationQuestionBinding(models.Model):
    """Immutable material snapshot for one occurrence, not a generic unit field.

    Quiz identity records the selected configuration behind the step's room
    code. Selection/session/game consistency is checked at creation, under the
    binding service's transaction. Later source edits/removal from the selection
    do not invalidate an already frozen snapshot. full_clean() also rejects
    replacement of a stored binding. save()/bulk SQL do NOT call full_clean();
    lifecycle, authorization and immutability must use the internal service.

    DO_NOTHING follows the existing duel migration convention: old parent
    deletion paths never query a not-yet-created table. DB FKs prevent orphaned
    history after migration; deleting bound material needs a future retention
    policy. Source questions remain editable. No signals or save overrides.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField("duel.DuelUnit",
        on_delete=models.DO_NOTHING, related_name="estimation_question_binding")
    question = models.ForeignKey("Estimation.EstimationQuestion", on_delete=models.DO_NOTHING,
                                 related_name="duel_bindings")
    quiz = models.ForeignKey("Estimation.EstimationQuiz", on_delete=models.DO_NOTHING,
                             related_name="duel_question_bindings")
    question_text = models.TextField()
    target_value = models.CharField(max_length=512)
    unit_key = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(question_text=""), name="duel_estimation_text_nonempty"),
            models.CheckConstraint(condition=~models.Q(unit_key=""), name="duel_estimation_unit_nonempty"),
            models.CheckConstraint(condition=(
                models.Q(target_value__regex=r"\A-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
                & ~models.Q(target_value="-0")
            ), name="duel_estimation_target_decimal"),
        ]

    def clean(self):
        super().clean()
        from .estimation_questions import canonical_target
        alias = self._state.db or router.db_for_read(type(self), instance=self)
        fields = ("unit_id", "question_id", "quiz_id", "question_text", "target_value", "unit_key", "created_at")
        original = type(self).objects.using(alias).filter(pk=self.pk).values(*fields).first()
        errors = {}
        if original is not None:
            for field in fields:
                if original[field] != getattr(self, field):
                    errors[field.removesuffix("_id")] = "A stored material binding cannot be replaced."
        else:
            unit = DuelUnit.objects.using(alias).filter(pk=self.unit_id).select_related(
                "game_instance__hub_game_step").first()
            quiz_field = self._meta.get_field("quiz").remote_field.model
            quiz = quiz_field.objects.using(alias).filter(pk=self.quiz_id).first()
            if unit is not None:
                instance, step = unit.game_instance, unit.game_instance.hub_game_step
                if (instance.game_key != "estimation" or step.game_key != "estimation"
                        or step.session_id != instance.hub_session_id):
                    errors["unit"] = "An Estimation unit of the correct session is required."
                if unit.status != UnitStatus.PREPARED or unit.started_at is not None or unit.completed_at is not None:
                    errors["unit"] = "Only a not-yet-started prepared unit may be bound."
                if quiz is not None and step.room_code != quiz.room_code:
                    errors["quiz"] = "The quiz must match the instance's HubGameStep."
            if quiz is not None and self.question_id is not None and not quiz.selected_questions.using(alias).filter(pk=self.question_id).exists():
                errors["question"] = "The question must be selected in this quiz."
            source_model = self._meta.get_field("question").remote_field.model
            source = source_model.objects.using(alias).filter(pk=self.question_id).first()
            if source is not None:
                try:
                    expected = (source.question_text, canonical_target(source.correct_answer), source.unit)
                    if (self.question_text, self.target_value, self.unit_key) != expected:
                        errors["question"] = "A new snapshot must match the server's question material."
                except (ValueError, TypeError):
                    errors["target_value"] = "The source question needs a finite target."
        try:
            if canonical_target(self.target_value) != self.target_value:
                errors["target_value"] = "A canonical finite decimal string is required."
        except (ValueError, TypeError):
            errors["target_value"] = "A canonical finite decimal string is required."
        for field in ("question_text", "unit_key"):
            if isinstance(getattr(self, field), str) and not getattr(self, field).strip():
                errors[field] = "A nonempty snapshot value is required."
        if errors:
            raise ValidationError(errors)


class EstimationPhase(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    ANSWERING = "answering", "Answering"
    AWAITING_REVEAL = "awaiting_reveal", "Awaiting host reveal"
    SHOW_ANSWERS = "show_answers", "Answers"
    SHOW_SOLUTION = "show_solution", "Solution"
    SHOW_RESULT = "show_result", "Result"
    COMPLETED = "completed", "Completed"


ESTIMATION_PHASE_ORDER = tuple(EstimationPhase)


class DuelEstimationRoundState(models.Model):
    """One authoritative phase/clock per occurrence, no duplicated game data.

    Unit PREPARED means material ready; ACTIVE covers answering through solution.
    At SHOW_RESULT the decision and match scoring are already completed, so the
    generic unit is COMPLETED. The final presentation acknowledgement then moves
    only this phase to COMPLETED (also after a recorded match win).

    Local timestamp/phase validity is enforced in SQL; material, unit lifecycle,
    legal transitions and frozen clocks also need full_clean and the locked
    service. Ordinary save()/bulk writes do not run full_clean. DO_NOTHING keeps
    old parent deletion paths compatible with unapplied migrations.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.OneToOneField(DuelUnit, on_delete=models.DO_NOTHING, related_name="estimation_round_state")
    phase = models.CharField(max_length=24, choices=EstimationPhase.choices, default=EstimationPhase.PREPARED.value)
    started_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    answering_closed_at = models.DateTimeField(null=True, blank=True)
    phase_changed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(phase__in=EstimationPhase.values), name="duel_est_phase_valid"),
            models.CheckConstraint(condition=(
                models.Q(phase="prepared", started_at__isnull=True, deadline__isnull=True, answering_closed_at__isnull=True)
                | models.Q(phase="answering", started_at__isnull=False, deadline__isnull=False, answering_closed_at__isnull=True)
                | models.Q(phase__in=EstimationPhase.values[2:], started_at__isnull=False,
                           deadline__isnull=False, answering_closed_at__isnull=False)
            ), name="duel_est_phase_clock"),
            models.CheckConstraint(condition=models.Q(deadline__isnull=True) | models.Q(deadline__gt=models.F("started_at")),
                                   name="duel_est_deadline_order"),
            models.CheckConstraint(condition=models.Q(answering_closed_at__isnull=True) | (
                models.Q(answering_closed_at__gte=models.F("started_at"))
                & models.Q(answering_closed_at__lte=models.F("deadline"))
                & models.Q(phase_changed_at__gte=models.F("answering_closed_at"))
            ), name="duel_est_close_order"),
            models.CheckConstraint(condition=models.Q(started_at__isnull=True) | models.Q(phase_changed_at__gte=models.F("started_at")),
                                   name="duel_est_phase_time_order"),
        ]

    def clean(self):
        super().clean()
        alias = self._state.db or router.db_for_read(type(self), instance=self)
        errors = {}
        for field in ("started_at", "deadline", "answering_closed_at", "phase_changed_at"):
            value = getattr(self, field)
            if isinstance(value, datetime) and timezone.is_naive(value):
                errors[field] = "Server timestamps must be timezone-aware."
        old = type(self).objects.using(alias).filter(pk=self.pk).values().first()
        if old is not None:
            if self.unit_id != old["unit_id"]:
                errors["unit"] = "A round occurrence cannot be reassigned."
            if old["started_at"] is not None and (self.started_at, self.deadline) != (old["started_at"], old["deadline"]):
                errors["deadline"] = "A started round clock cannot be restarted or changed."
            if old["answering_closed_at"] is not None and self.answering_closed_at != old["answering_closed_at"]:
                errors["answering_closed_at"] = "The original answer closure cannot be changed."
            if self.phase != old["phase"] and self.phase in EstimationPhase.values and old["phase"] in EstimationPhase.values:
                if ESTIMATION_PHASE_ORDER.index(EstimationPhase(self.phase)) != ESTIMATION_PHASE_ORDER.index(EstimationPhase(old["phase"])) + 1:
                    errors["phase"] = "Only the next phase is allowed."
        elif self.phase != EstimationPhase.PREPARED:
            errors["phase"] = "A new round must be prepared first."
        unit = DuelUnit.objects.using(alias).filter(pk=self.unit_id).select_related("game_instance").first()
        if unit is not None:
            if unit.game_instance.game_key != "estimation" or not DuelEstimationQuestionBinding.objects.using(alias).filter(unit=unit).exists():
                errors["unit"] = "Bound Estimation material is required."
            expected = (UnitStatus.PREPARED if self.phase == EstimationPhase.PREPARED else
                        UnitStatus.COMPLETED if self.phase in (EstimationPhase.SHOW_RESULT, EstimationPhase.COMPLETED)
                        else UnitStatus.ACTIVE)
            if unit.status != expected:
                errors["unit"] = "Unit lifecycle and round phase disagree."
            if self.phase != EstimationPhase.PREPARED and unit.started_at != self.started_at:
                errors["started_at"] = "Round and unit must share their server start."
            if expected == UnitStatus.COMPLETED and (unit.completed_at is None or not DuelUnitOutcome.objects.using(alias).filter(unit=unit).exists()):
                errors["unit"] = "The result phase requires a persisted completed decision."
        if errors:
            raise ValidationError(errors)

