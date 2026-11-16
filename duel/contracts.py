"""Immutable duel data contracts; no persistence, game rules or app wiring.

Existing HubSession, HubGameStep and HubParticipant primary keys are integers
(Django BigAutoField). Unit IDs use the same integer convention; the new duel
instance UUID distinguishes separate runs without changing those existing IDs.
Callers supply all identities. These contracts do not look up database records
or verify their existence, session membership or global uniqueness.
"""

from dataclasses import dataclass
from enum import Enum, unique
from uuid import UUID


def _require_positive_id(value: int, field_name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer ID.")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive.")


@dataclass(frozen=True, slots=True)
class DuelInstanceContext:
    """One run and its current unit, with exactly two distinct participants.

    A new context is constructed for a new unit; existing contexts cannot be
    mutated. Party A and party B refer to these two participant IDs throughout
    contracts associated with this context, not to a separate team model.
    """

    hub_session_id: int
    hub_game_step_id: int
    duel_instance_id: UUID
    current_unit_id: int
    game_key: str
    participant_a_id: int
    participant_b_id: int

    def __post_init__(self) -> None:
        for name in (
            "hub_session_id",
            "hub_game_step_id",
            "current_unit_id",
            "participant_a_id",
            "participant_b_id",
        ):
            _require_positive_id(getattr(self, name), name)
        if not isinstance(self.duel_instance_id, UUID):
            raise TypeError("duel_instance_id must be a UUID.")
        if not isinstance(self.game_key, str):
            raise TypeError("game_key must be a string.")
        if not self.game_key.strip():
            raise ValueError("game_key must not be empty.")
        if self.participant_a_id == self.participant_b_id:
            raise ValueError("A duel requires two different participant IDs.")


@dataclass(frozen=True, slots=True)
class ScoreChange:
    """An integer point delta for a participant, without any implied winner.

    Membership in a particular duel is checked by the future command layer.
    Positive, negative and zero deltas are equally representable.
    """

    participant_id: int
    delta: int

    def __post_init__(self) -> None:
        _require_positive_id(self.participant_id, "participant_id")
        if type(self.delta) is not int:
            raise TypeError("delta must be an integer.")


@unique
class RoundOutcome(str, Enum):
    PARTY_A_WINS = "party_a_wins"
    PARTY_B_WINS = "party_b_wins"
    DRAW = "draw"
    NO_WINNER = "no_winner"
    UNDECIDED = "undecided"


@dataclass(frozen=True, slots=True)
class RoundResult:
    """An explicit unit outcome, independent of points and match completion."""

    outcome: RoundOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RoundOutcome):
            raise TypeError("outcome must be a RoundOutcome.")


@unique
class MatchStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class MatchState:
    """Explicit match status; no transitions or match winner are inferred."""

    status: MatchStatus

    def __post_init__(self) -> None:
        if not isinstance(self.status, MatchStatus):
            raise TypeError("status must be a MatchStatus.")


@unique
class SessionType(str, Enum):
    """Session-wide planning type, to be locked by the future lifecycle layer.

    DUEL requires exactly two parties and duel rules for every game. MULTIPLAYER
    preserves the existing participant rules and allows per-game formats.
    This enum does not enforce participant counts or persist a running session.
    """

    MULTIPLAYER = "multiplayer"
    DUEL = "duel"


@unique
class GameFormat(str, Enum):
    """A game step's rules/organization, independent of session population.

    DUEL selects two parties, including within a MULTIPLAYER session. TOURNAMENT
    describes separate, potentially changing pairings; its future support must
    reject buzzer variants. ONE_VS_MANY is reserved for a future group format.
    Defined values express contracts, not implemented or enabled game features.
    """

    STANDARD = "standard"
    DUEL = "duel"
    TOURNAMENT = "tournament"
    ONE_VS_MANY = "one_vs_many"


def resolve_session_type(session_type: SessionType | None = None) -> SessionType:
    """Treat an omitted/None legacy type as MULTIPLAYER; reject other types.

    Explicit values must be SessionType members, as with the other typed
    contracts. Storage adapters can explicitly parse strings with SessionType.
    Unknown values, empty strings and unrelated enums never become defaults.
    """
    if session_type is None:
        return SessionType.MULTIPLAYER
    if not isinstance(session_type, SessionType):
        raise TypeError("session_type must be a SessionType or None.")
    return session_type


def default_game_format(session_type: SessionType | None = None) -> GameFormat:
    """Return a planning default without creating or modifying a session."""
    resolved = resolve_session_type(session_type)
    return GameFormat.DUEL if resolved is SessionType.DUEL else GameFormat.STANDARD


def validate_game_format(
    session_type: SessionType | None, game_format: GameFormat,
) -> None:
    """Validate structural compatibility only, not availability or permission.

    MULTIPLAYER allows all declared formats, including future TOURNAMENT and
    ONE_VS_MANY. DUEL allows only DUEL. None denotes a legacy MULTIPLAYER type.
    Success does not authorize actions, start games or enable features. Game
    support, buzzer restrictions, party selection and lifecycle locks must be
    checked separately by future integration; none is implemented here.
    """
    resolved = resolve_session_type(session_type)
    if not isinstance(game_format, GameFormat):
        raise TypeError("game_format must be a GameFormat.")
    if resolved is SessionType.DUEL and game_format is not GameFormat.DUEL:
        raise ValueError("DUEL sessions allow only the DUEL game format.")
