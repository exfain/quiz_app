"""Internal participant input service, unconnected to transports/game rules.

Every mutation uses execute_action's parent lock, SQLite IMMEDIATE transaction,
whole-transaction lock retry, receipt and revision. ActorContext is a trusted
server assertion, NOT authentication. Future adapters must confirm identity.

Only running matches and active units accept drafts/commits. Controllers must
add their current-unit, answer-phase, payload-schema and deadline checks through
validate. This trusted, read-only callback runs UNDER the existing lock, receives
the resolved own payload and server time, and returns None or a rejection code. It
must have no external side effects: the entire transaction can be retried.

An optional trusted on_committed callback composes controller state changes
after the row is saved, inside the SAME transaction, receipt and revision. It
must return None; any error rolls back the submission and all composed writes.

There is one committed input per party/unit/kind/server-defined input_key.
The empty key preserves all existing input identities and receipt fingerprints.
Black Jack uses question-scoped keys for independent questions within a set.
No scoring, outcome, reveal, timer, bid comparison or transport is implemented.
Direct ORM/SQL changes bypass this service; no model-level immutability is claimed.
"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import DEFAULT_DB_ALIAS
from django.db.models import Max, QuerySet
from django.utils import timezone

from games_hub.models import HubParticipant

from .commands import (
    ActionContext, ActionResult, ActorContext, CommandRejected, HandlerResult,
    _canonical_json, execute_action,
)
from .contracts import MatchStatus
from .models import (
    DuelGameInstance, DuelSubmission, DuelUnit, ReceiptActorType, ReceiptStatus,
    SubmissionKind, SubmissionStatus, UnitStatus,
)


SubmissionValidator = Callable[[ActionContext, dict[str, Any], datetime], str | None]
SubmissionCommitted = Callable[[ActionContext, DuelSubmission], None]


def _participant_actor(actor: ActorContext) -> None:
    if not isinstance(actor, ActorContext):
        raise TypeError("actor must be a server-confirmed ActorContext.")
    if actor.actor_type != ReceiptActorType.PARTICIPANT:
        raise CommandRejected("participant_required", "Only participants submit input.")


def _arguments(
    instance_id: UUID, unit_id: int, actor: ActorContext,
    submission_kind: SubmissionKind, draft_id: UUID | None = None,
) -> None:
    _participant_actor(actor)
    if not isinstance(instance_id, UUID):
        raise TypeError("instance_id must be a concrete duel instance UUID.")
    if type(unit_id) is not int or unit_id <= 0:
        raise ValueError("unit_id must be a positive concrete unit ID.")
    if not isinstance(submission_kind, SubmissionKind):
        raise TypeError("submission_kind must be ANSWER or BID from SubmissionKind.")
    if draft_id is not None and not isinstance(draft_id, UUID):
        raise TypeError("draft_id must be a submission UUID or None.")


def _json_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if type(payload) is not dict:
        raise TypeError("Submission payload must be a JSON object.")
    # Reuse the command boundary's strict JSON policy, also for stored drafts.
    return json.loads(_canonical_json(payload))


def _metadata(submission: DuelSubmission, instance_id: UUID) -> dict[str, Any]:
    return {
        "submission_id": str(submission.pk), "instance_id": str(instance_id),
        "unit_id": submission.unit_id, "submission_kind": submission.submission_kind,
        "status": submission.status,
        "committed_at": submission.committed_at.isoformat() if submission.committed_at else None,
        "acceptance_sequence": submission.acceptance_sequence,
    }


def _reject(code: str) -> HandlerResult:
    return HandlerResult(ReceiptStatus.REJECTED, {"code": code})


def _write(
    *, instance_id: UUID, unit_id: int, actor: ActorContext,
    submission_kind: SubmissionKind, client_action_id: str,
    payload: dict[str, Any] | None, draft_id: UUID | None,
    commit: bool, expected_revision: int | None, validate: SubmissionValidator | None,
    on_committed: SubmissionCommitted | None = None,
    input_key: str = "",
) -> ActionResult:
    _arguments(instance_id, unit_id, actor, submission_kind, draft_id)
    if type(input_key) is not str or len(input_key) > 64:
        raise ValueError("A server-defined input key of at most 64 characters is required.")
    if validate is not None and not callable(validate):
        raise TypeError("validate must be a trusted server-side callable or None.")
    if on_committed is not None and not callable(on_committed):
        raise TypeError("on_committed must be a trusted server-side callable or None.")
    if payload is None and (not commit or draft_id is None):
        raise TypeError("A JSON payload is required unless committing an explicit draft.")
    request = {
        "submission_kind": submission_kind.value,
        "payload": _json_payload(payload) if payload is not None else None,
        "draft_id": str(draft_id) if draft_id is not None else None,
    }
    if input_key:
        request["input_key"] = input_key  # Preserve existing unscoped receipt fingerprints.

    def handler(context: ActionContext) -> HandlerResult:
        if input_key and context.instance.game_key != "blackjack":
            return _reject("input_scope_not_allowed")
        if context.instance.match_status != MatchStatus.IN_PROGRESS.value:
            return _reject("match_not_running")
        if context.unit.status != UnitStatus.ACTIVE or context.unit.completed_at is not None:
            return _reject("unit_not_active")
        inputs = DuelSubmission.objects.using(context.using).filter(
            unit=context.unit, participant=context.participant, submission_kind=submission_kind.value, input_key=input_key,
        )
        if inputs.filter(status=SubmissionStatus.COMMITTED).exists():
            return _reject("already_committed")
        draft = None
        if draft_id is not None:
            # Do not disclose whether an inaccessible UUID exists for another party.
            draft = inputs.filter(pk=draft_id, status=SubmissionStatus.DRAFT).first()
            if draft is None:
                return _reject("invalid_draft")
        elif not commit:
            # Historical duplicates stay drafts. Stable ordering never interprets
            # an arbitrary draft as a committed response or changes another party.
            draft = inputs.filter(status=SubmissionStatus.DRAFT).order_by("-created_at", "-id").first()
        submission = draft or DuelSubmission(
            unit=context.unit, participant=context.participant,
            submission_kind=submission_kind.value,
            input_key=input_key,
        )
        final_payload = context.payload["payload"]
        if final_payload is None:
            final_payload = submission.payload
        submission.payload = _json_payload(final_payload)
        now = timezone.now()
        if validate is not None:
            code = validate(context, _json_payload(submission.payload), now)
            if code is not None:
                if type(code) is not str or not code.strip():
                    raise TypeError("Validator must return None or a nonempty rejection code.")
                return _reject(code)
        if commit:
            # Safe ONLY under execute_action's locked parent/IMMEDIATE transaction.
            # The counter covers ALL parties and kinds, not just this input type.
            highest = DuelSubmission.objects.using(context.using).filter(unit=context.unit).aggregate(
                highest=Max("acceptance_sequence"),
            )["highest"] or 0
            submission.status = SubmissionStatus.COMMITTED.value
            submission.committed_at = now
            submission.acceptance_sequence = highest + 1
        submission.full_clean()
        submission.save(using=context.using)
        if commit and on_committed is not None:
            # Trusted controller composition, within this SAME command/savepoint.
            # Exceptions roll back input, phase changes, receipt and revision.
            if on_committed(context, submission) is not None:
                raise TypeError("on_committed must return None.")
        # Acknowledgements expose metadata only, never the other party's input.
        return HandlerResult(ReceiptStatus.APPLIED, _metadata(submission, context.instance.pk))

    return execute_action(
        instance_id=instance_id, unit_id=unit_id, actor=actor, client_action_id=client_action_id,
        action_type="duel.submission.commit" if commit else "duel.submission.save_draft",
        payload=request, handler=handler, expected_revision=expected_revision,
    )


def save_draft(
    *, instance_id: UUID, unit_id: int, actor: ActorContext, submission_kind: SubmissionKind,
    payload: dict[str, Any], client_action_id: str, draft_id: UUID | None = None,
    expected_revision: int | None = None, validate: SubmissionValidator | None = None,
    input_key: str = "",
) -> ActionResult:
    """Create/update the newest own draft, or an explicitly identified own draft.

    Drafts carry neither commitment time nor sequence. After a commit of this
    kind all drafts are frozen through this service. Old duplicate drafts are
    retained, never counted as answers. Identical retries return their original
    acknowledgement even if a subsequent action has updated/committed the draft.
    """
    return _write(instance_id=instance_id, unit_id=unit_id, actor=actor,
        submission_kind=submission_kind, payload=payload, client_action_id=client_action_id,
        draft_id=draft_id, commit=False, expected_revision=expected_revision, validate=validate, input_key=input_key)


def commit_submission(
    *, instance_id: UUID, unit_id: int, actor: ActorContext, submission_kind: SubmissionKind,
    client_action_id: str, payload: dict[str, Any] | None = None, draft_id: UUID | None = None,
    expected_revision: int | None = None, validate: SubmissionValidator | None = None,
    on_committed: SubmissionCommitted | None = None,
    input_key: str = "",
) -> ActionResult:
    """Commit a new input or an explicit own draft exactly once.

    With draft_id and no payload, commit its currently persisted data. Supplying
    payload replaces that draft's data as part of this commitment. No draft ID
    means a new committed row; historical drafts do not block it. ActionResult
    contains the original receipt revision on replay. expected_revision is only
    a controller hint, never a blanket gate on independent party submissions.
    """
    return _write(instance_id=instance_id, unit_id=unit_id, actor=actor,
        submission_kind=submission_kind, payload=payload, client_action_id=client_action_id,
        draft_id=draft_id, commit=True, expected_revision=expected_revision, validate=validate,
        on_committed=on_committed, input_key=input_key)


def _own_inputs(
    *, instance_id: UUID, unit_id: int, actor: ActorContext, submission_kind: SubmissionKind,
    input_key: str = "",
) -> QuerySet[DuelSubmission]:
    """Read authorization; no receipts, transaction, revision or state mutation."""
    _arguments(instance_id, unit_id, actor, submission_kind)
    try:
        instance = DuelGameInstance.objects.using(DEFAULT_DB_ALIAS).get(pk=instance_id)
    except DuelGameInstance.DoesNotExist as exc:
        raise CommandRejected("instance_not_found", "Unknown duel instance.") from exc
    if (actor.actor_id not in (instance.participant_a_id, instance.participant_b_id)
            or not HubParticipant.objects.using(DEFAULT_DB_ALIAS).filter(
                pk=actor.actor_id, session_id=instance.hub_session_id).exists()):
        raise CommandRejected("actor_not_allowed", "Participant is not a party of this instance.")
    if not DuelUnit.objects.using(DEFAULT_DB_ALIAS).filter(pk=unit_id, game_instance=instance).exists():
        raise CommandRejected("invalid_unit", "Unit does not belong to this duel instance.")
    return DuelSubmission.objects.using(DEFAULT_DB_ALIAS).filter(
        unit_id=unit_id, participant_id=actor.actor_id, submission_kind=submission_kind.value, input_key=input_key,
    )


def get_committed_submission(
    *, instance_id: UUID, unit_id: int, actor: ActorContext, submission_kind: SubmissionKind,
    input_key: str = "",
) -> dict[str, Any] | None:
    """Read ONLY the confirmed actor's committed input, also after match end.

    Return detached JSON/metadata, not a mutable ORM object. No opponent lookup
    API or receipt contents are exposed. This read does not advance revision.
    """
    submission = _own_inputs(instance_id=instance_id, unit_id=unit_id, actor=actor,
                             submission_kind=submission_kind, input_key=input_key).filter(status=SubmissionStatus.COMMITTED).first()
    if submission is None:
        return None
    return {**_metadata(submission, instance_id), "payload": _json_payload(submission.payload)}


def get_submission_status(
    *, instance_id: UUID, unit_id: int, actor: ActorContext, submission_kind: SubmissionKind,
    input_key: str = "",
) -> SubmissionStatus | None:
    """A committed input takes precedence over any leftover historical drafts."""
    inputs = _own_inputs(instance_id=instance_id, unit_id=unit_id, actor=actor, submission_kind=submission_kind, input_key=input_key)
    # One statement: a draft-to-commit transition between two exists() calls
    # must not briefly report 'no input'. At most the two defined statuses.
    statuses = set(inputs.values_list("status", flat=True).distinct())
    if SubmissionStatus.COMMITTED in statuses:
        return SubmissionStatus.COMMITTED
    return SubmissionStatus.DRAFT if SubmissionStatus.DRAFT in statuses else None
