"""Internal, unconnected duel write boundary; no transport or game handlers.

Only trusted server code supplies ActorContext and handler. A future adapter
must authenticate participants and authorize hosts before calling this module;
constructing a context is NOT authentication. All handler writes must use the
default database in this transaction. Handlers must not commit, perform external
I/O or send notifications. Future transports may notify after execute_action()
returns, or use on_commit; a lost response is recovered with the original ID.

The existing project retry policy re-runs whole rolled-back SQLite transactions.
Handlers therefore must have no nontransactional side effects. This entry point
requires an outer transaction and SQLite IMMEDIATE, rather than pretending that
select_for_update supplies SQLite row locks. It never changes DB configuration.
"""

import hashlib
import json
import math
from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, OperationalError, connection, transaction
from django.db.models import F
from django.utils import timezone

from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubParticipant

from .models import DuelActionReceipt, DuelGameInstance, DuelUnit, ReceiptActorType, ReceiptStatus, UnitStatus
from .contracts import MatchStatus


class CommandRejected(ValueError):
    """Invalid identity/context: no business processing and no receipt."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ActionConflict(CommandRejected):
    """An actor reused an action ID for different request content."""


@dataclass(frozen=True, slots=True)
class ActorContext:
    actor_type: ReceiptActorType
    actor_id: int

    def __post_init__(self):
        if not isinstance(self.actor_type, ReceiptActorType):
            raise TypeError("actor_type must be a ReceiptActorType.")
        if type(self.actor_id) is not int or self.actor_id <= 0:
            raise ValueError("actor_id must be a positive server-confirmed identity ID.")

    @property
    def key(self) -> str:
        return str(self.actor_id)


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Locked identity plus request; phase/current-unit rules belong to handler.

    Models currently contain no authoritative 'current unit' pointer. The
    supplied concrete unit is checked for existence/ownership, never inferred
    from a question number. Controllers must check its lifecycle and their
    active-unit/turn context. expected_revision is a hint for those rules, not
    an automatic global equality requirement. Only this boundary owns revision
    increments. The handler must validate any new related model writes too.
    """

    instance: DuelGameInstance
    unit: DuelUnit | None
    actor: ActorContext
    participant: HubParticipant | None
    action_type: str
    client_action_id: str
    payload: dict[str, Any]
    expected_revision: int | None
    using: str = DEFAULT_DB_ALIAS


@dataclass(frozen=True, slots=True)
class HandlerResult:
    status: ReceiptStatus
    data: dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.status, ReceiptStatus):
            raise TypeError("Handler status must be APPLIED or REJECTED.")


@dataclass(frozen=True, slots=True)
class ActionResult:
    status: ReceiptStatus
    data: dict[str, Any]
    state_revision: int
    receipt_id: UUID
    replayed: bool


def _canonical_json(value: Any) -> str:
    """Strict JSON values, sorted string keys, no coercion/default=str/NaN."""
    def validate(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("Requests and results must contain only finite, strict JSON values.")

    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def request_fingerprint(
    *, instance_id: UUID, unit_id: int | None, action_type: str,
    payload: dict[str, Any], expected_revision: int | None = None,
) -> str:
    """Hash server-canonical action content, never a client-claimed digest.

    An expected revision is request context and included if supplied. A retry
    resends its original hint; the live DB revision is NOT part of the digest.
    No browser clock or generated receipt identity participates.
    """
    if not isinstance(instance_id, UUID):
        raise TypeError("instance_id must be a duel instance UUID.")
    if unit_id is not None and (type(unit_id) is not int or unit_id <= 0):
        raise ValueError("unit_id must be a positive concrete unit ID or None.")
    if type(action_type) is not str or not action_type.strip() or len(action_type) > 64:
        raise ValueError("action_type must be a nonempty string of at most 64 characters.")
    if type(payload) is not dict:
        raise TypeError("payload must be a JSON object.")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ValueError("expected_revision must be a nonnegative integer or None.")
    canonical = _canonical_json({
        "action_type": action_type, "instance_id": str(instance_id), "unit_id": unit_id,
        "payload": payload, "expected_revision": expected_revision,
    })
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stored_result(receipt: DuelActionReceipt, *, replayed: bool) -> ActionResult:
    stored = receipt.result
    if (not isinstance(stored, dict) or not isinstance(stored.get("data"), dict)
            or type(stored.get("state_revision")) is not int or stored["state_revision"] < 0):
        raise RuntimeError("Receipt does not contain a valid command result envelope.")
    return ActionResult(
        ReceiptStatus(receipt.status), json.loads(_canonical_json(stored["data"])),
        stored["state_revision"], receipt.pk, replayed,
    )


def execute_action(
    *, instance_id: UUID, actor: ActorContext, client_action_id: str, action_type: str,
    payload: dict[str, Any], handler: Callable[[ActionContext], HandlerResult],
    unit_id: int | None = None, expected_revision: int | None = None,
    _completed_presentation: bool = False,
    _initialize_instance: Callable[[], None] | None = None,
    _tournament_organization: bool = False,
) -> ActionResult:
    """Execute one internal command and commit its terminal receipt atomically.

    Authorization/context failures and conflicts raise CommandRejected without
    a receipt. Controlled handler rejection rolls back ALL handler writes to a
    savepoint, then stores a REJECTED result without advancing revision. Any
    unexpected handler/DB/result error rolls back the outer transaction.

    Result envelopes retain the original acknowledgement revision for replay;
    no fresh revision or snapshot is substituted. No global phase, winner,
    scoring, answer or timer policy is introduced here.

    _completed_presentation is restricted to the internal duel final
    presentation acknowledgement: scoring may have already completed its match.
    Only a HOST with an already-completed unit may use that exception. It cannot
    reopen/change the unit, match status or configuration. All other closed-match
    commands, and every aborted-match command, retain the normal rejection.

    _tournament_organization permits only draw/end on the registered coordinator
    encounter. Completed encounter identities, units and configuration stay frozen.
    Tournament commands lock the shared set before the encounter, so a reveal
    barrier and concurrent submissions observe one serialized set state.
    """
    if not isinstance(actor, ActorContext):
        raise TypeError("actor must be an internal ActorContext.")
    if type(client_action_id) is not str or not client_action_id.strip() or len(client_action_id) > 128:
        raise ValueError("client_action_id must be a nonempty string of at most 128 characters.")
    if not callable(handler):
        raise TypeError("handler must be a trusted server-side callable.")
    if _initialize_instance is not None and not callable(_initialize_instance):
        raise TypeError("Instance initialization must be a trusted callable.")
    if type(_completed_presentation) is not bool or (
            _completed_presentation and action_type not in {"duel.estimation.complete_round", "duel.quiz.complete_round", "duel.oral.complete_round", "duel.turn.complete_round", "duel.input.complete_round", "duel.control.complete_round"}):
        raise ValueError("Terminal presentation maintenance is a restricted internal command.")
    if type(_tournament_organization) is not bool or (_tournament_organization and (
            actor.actor_type != ReceiptActorType.HOST or action_type not in {'duel.tournament.draw', 'duel.tournament.end'})):
        raise ValueError('Tournament organization is restricted to trusted host commands.')
    payload_json = _canonical_json(payload)
    fingerprint = request_fingerprint(
        instance_id=instance_id, unit_id=unit_id, action_type=action_type,
        payload=json.loads(payload_json), expected_revision=expected_revision,
    )
    # Snapshot request data once; caller/handler mutations cannot change retry
    # content during a whole-transaction SQLite lock retry.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Duel commands require their own outer transaction.")
    if connection.vendor == "sqlite" and connection.transaction_mode != "IMMEDIATE":
        raise RuntimeError("Duel commands require the existing SQLite IMMEDIATE configuration.")

    def write_once():
        committed = False

        def mark_committed():
            nonlocal committed
            committed = True

        try:
            with transaction.atomic(using=DEFAULT_DB_ALIAS, durable=True):
                # Registered before handler callbacks: an OperationalError in a
                # post-commit callback must not retry/mask an already committed action.
                transaction.on_commit(mark_committed, using=DEFAULT_DB_ALIAS)
                # Trusted Hub provisioning only: acquire its session/slot locks,
                # validate and create the identity in THIS durable transaction.
                # It must be a read-only no-op if the identity already exists.
                if _initialize_instance is not None and _initialize_instance() is not None:
                    raise TypeError("Instance initializer must return None.")
                from .tournaments import pair_for, validate_action
                from .models import DuelTournamentSet, DuelTournament
                tournament_pair = pair_for(instance_id)
                if tournament_pair:
                    DuelTournamentSet.objects.select_for_update().get(pk=tournament_pair.tournament_set_id)
                if _tournament_organization and not DuelTournament.objects.filter(coordinator_id=instance_id).exists():
                    raise CommandRejected('invalid_tournament', 'No matching tournament coordinator.')
                try:
                    instance = DuelGameInstance.objects.using(DEFAULT_DB_ALIAS).select_for_update().get(pk=instance_id)
                except DuelGameInstance.DoesNotExist as exc:
                    raise CommandRejected("instance_not_found", "Unknown duel instance.") from exc
                try:
                    instance.full_clean()
                except ValidationError as exc:
                    raise CommandRejected("invalid_instance", "Invalid persisted duel relationships.") from exc
                participant = None
                if actor.actor_type == ReceiptActorType.PARTICIPANT:
                    participant = (
                        HubParticipant.objects.using(DEFAULT_DB_ALIAS).select_for_update().filter(pk=actor.actor_id).first()
                    )
                    if participant is None:
                        raise CommandRejected("actor_not_found", "Unknown participant.")
                    if (participant.pk not in (instance.participant_a_id, instance.participant_b_id)
                            or participant.session_id != instance.hub_session_id):
                        raise CommandRejected("actor_not_allowed", "Participant is not a party of this instance.")
                # HOST is an internal, preauthorized assertion, not a public auth path.
                unit = None
                if unit_id is not None:
                    unit = DuelUnit.objects.using(DEFAULT_DB_ALIAS).select_for_update().filter(pk=unit_id).first()
                    if unit is None or unit.game_instance_id != instance.pk:
                        raise CommandRejected("invalid_unit", "Unit does not belong to this duel instance.")
                    unit.full_clean()
                receipt = DuelActionReceipt.objects.using(DEFAULT_DB_ALIAS).filter(
                    game_instance=instance, actor_type=actor.actor_type.value,
                    actor_key=actor.key, client_action_id=client_action_id,
                ).first()
                if receipt is not None:
                    if (receipt.request_fingerprint.lower() != fingerprint
                            or receipt.action_type != action_type or receipt.unit_id != unit_id):
                        raise ActionConflict("action_id_conflict", "Action ID was already used for different content.")
                    return _stored_result(receipt, replayed=True)

                initial_revision = instance.state_revision
                validate_action(instance, action_type, json.loads(payload_json), organization=_tournament_organization)
                initial_status = instance.match_status
                initial_tiebreak = instance.tiebreak_unit_id
                terminal_presentation = (
                    (_completed_presentation or _tournament_organization) and initial_status == MatchStatus.COMPLETED.value
                    and actor.actor_type == ReceiptActorType.HOST
                    and (_tournament_organization or (unit is not None and unit.status == UnitStatus.COMPLETED))
                )
                terminal_unit = (list(DuelUnit.objects.filter(game_instance=instance).values()) if terminal_presentation else None)
                initial_config = deepcopy(instance.rule_config)
                initial_match_config = deepcopy(instance.rule_config.get("match"))
                identity = (instance.hub_session_id, instance.hub_game_step_id,
                            instance.participant_a_id, instance.participant_b_id, instance.game_key)
                with transaction.atomic(using=DEFAULT_DB_ALIAS):
                    if initial_status in (MatchStatus.COMPLETED.value, MatchStatus.ABORTED.value) and not terminal_presentation:
                        outcome = HandlerResult(ReceiptStatus.REJECTED, {"code": "match_closed"})
                    else:
                        outcome = handler(ActionContext(
                            instance, unit, actor, participant, action_type, client_action_id,
                            json.loads(payload_json), expected_revision,
                        ))
                    if not isinstance(outcome, HandlerResult) or type(outcome.data) is not dict:
                        raise TypeError("Handler must return HandlerResult with JSON object data.")
                    result_data = json.loads(_canonical_json(outcome.data))
                    if outcome.status == ReceiptStatus.REJECTED:
                        transaction.set_rollback(True, using=DEFAULT_DB_ALIAS)

                # The boundary alone owns revision. Controlled rejection may
                # have changed in-memory objects; reload after its savepoint rollback.
                if instance.pk != instance_id or (unit is not None and unit.pk != unit_id):
                    raise RuntimeError("Handlers must not replace command identities.")
                instance.refresh_from_db(using=DEFAULT_DB_ALIAS)
                if identity != (instance.hub_session_id, instance.hub_game_step_id,
                                instance.participant_a_id, instance.participant_b_id, instance.game_key):
                    raise RuntimeError("Handlers must not change the instance identity or parties.")
                if instance.state_revision != initial_revision:
                    raise RuntimeError("Handlers must not update the instance state revision.")
                if initial_tiebreak is not None and instance.tiebreak_unit_id != initial_tiebreak:
                    raise RuntimeError('A latched tiebreak boundary cannot be replaced.')
                if instance.tiebreak_unit_id != initial_tiebreak:
                    from .match_service import _read
                    _read(instance)  # Validate the new boundary before commit, not in a later GET.
                if terminal_presentation and (
                        instance.match_status != initial_status or instance.rule_config != initial_config or instance.tiebreak_unit_id != initial_tiebreak
                        or list(DuelUnit.objects.filter(game_instance=instance).values()) != terminal_unit):
                    raise RuntimeError("Presentation completion cannot reopen or change the match/unit.")
                if initial_status != MatchStatus.NOT_STARTED.value and instance.rule_config.get("match") != initial_match_config:
                    raise RuntimeError("An already started match configuration is immutable.")
                if outcome.status == ReceiptStatus.APPLIED:
                    DuelGameInstance.objects.using(DEFAULT_DB_ALIAS).filter(pk=instance_id).update(
                        state_revision=F("state_revision") + 1, updated_at=timezone.now(),
                    )
                    instance.refresh_from_db(using=DEFAULT_DB_ALIAS, fields=["state_revision"])
                receipt = DuelActionReceipt(
                    game_instance=instance, unit=unit, actor_type=actor.actor_type.value,
                    actor_key=actor.key, client_action_id=client_action_id, action_type=action_type,
                    request_fingerprint=fingerprint, status=outcome.status.value,
                    result={"data": result_data, "state_revision": instance.state_revision},
                )
                receipt.full_clean()
                receipt.save(using=DEFAULT_DB_ALIAS, force_insert=True)
                return _stored_result(receipt, replayed=False)
        except OperationalError as exc:
            if committed:
                raise RuntimeError("Post-commit callback failed; recover using the same action ID.") from exc
            raise

    return _retry_sqlite_locked(write_once)
