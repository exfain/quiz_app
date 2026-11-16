"""Pure match rules. No Django, persistence, score formulas or tie policy."""
from dataclasses import dataclass
from enum import Enum
from collections.abc import Sequence

from .contracts import RoundOutcome, RoundResult


class MatchMode(str, Enum):
    BEST_OF_N = "best_of_n"
    TARGET_SCORE = "target_score"


class MatchEvaluationState(str, Enum):
    RUNNING = "running"
    SUDDEN_DEATH = "sudden_death"
    DRAW = "draw"
    PARTY_A_WINS = "party_a_wins"
    PARTY_B_WINS = "party_b_wins"
    NEEDS_RESOLUTION = "needs_resolution"
    INVALID_CONFIGURATION = "invalid_configuration"


@dataclass(frozen=True, slots=True)
class MatchConfig:
    mode: MatchMode
    best_of: int | None = None
    target_score: int | None = None

    def __post_init__(self):
        if not isinstance(self.mode, MatchMode):
            raise TypeError("mode must be a MatchMode.")
        if self.mode == MatchMode.BEST_OF_N:
            if type(self.best_of) is not int or self.best_of <= 0 or self.best_of % 2 == 0:
                raise ValueError("best_of must be a positive odd integer.")
            if self.target_score is not None:
                raise ValueError("Best-of matches cannot also configure a target score.")
        else:
            if type(self.target_score) is not int or self.target_score <= 0:
                raise ValueError("target_score must be a positive integer.")
            if self.best_of is not None:
                raise ValueError("Target-score matches cannot also configure best_of.")

    @property
    def required_wins(self) -> int | None:
        return (self.best_of + 1) // 2 if self.best_of is not None else None

    def to_dict(self) -> dict:
        if self.mode == MatchMode.BEST_OF_N:
            return {"mode": self.mode.value, "best_of": self.best_of}
        return {"mode": self.mode.value, "target_score": self.target_score}


def parse_match_config(rule_config: dict) -> MatchConfig:
    """Dedicated rule_config['match']; absent/unknown values have no defaults."""
    if type(rule_config) is not dict or type(rule_config.get("match")) is not dict:
        raise ValueError("An explicit match configuration is required.")
    data = rule_config["match"]
    mode = MatchMode(data.get("mode"))
    key = "best_of" if mode == MatchMode.BEST_OF_N else "target_score"
    if set(data) != {"mode", key}:
        raise ValueError("Unexpected or missing match configuration fields.")
    return MatchConfig(mode, **{key: data[key]})


@dataclass(frozen=True, slots=True)
class MatchEvaluation:
    state: MatchEvaluationState
    wins_a: int = 0
    wins_b: int = 0
    completed_units: int = 0
    reason: str | None = None

    @property
    def winner_side(self) -> RoundOutcome | None:
        if self.state == MatchEvaluationState.PARTY_A_WINS:
            return RoundOutcome.PARTY_A_WINS
        if self.state == MatchEvaluationState.PARTY_B_WINS:
            return RoundOutcome.PARTY_B_WINS
        return None


def evaluate_match(
    config: MatchConfig, *, score_a: int, score_b: int,
    round_results: Sequence[RoundResult] = (),
    tiebreak_after: int | None = None,
) -> MatchEvaluation:
    if not isinstance(config, MatchConfig):
        raise TypeError("config must be a validated MatchConfig.")
    if type(score_a) is not int or type(score_b) is not int:
        raise TypeError("Scores must be integers; negative values are allowed.")
    if any(not isinstance(result, RoundResult) for result in round_results):
        raise TypeError("Round results must use the existing RoundResult contract.")
    outcomes = [result.outcome for result in round_results]
    if tiebreak_after is not None and (config.mode != MatchMode.TARGET_SCORE or type(tiebreak_after) is not int
            or not 0 < tiebreak_after <= len(outcomes)):
        raise ValueError("A tiebreak boundary must identify a completed target-score round.")
    a = outcomes.count(RoundOutcome.PARTY_A_WINS)
    b = outcomes.count(RoundOutcome.PARTY_B_WINS)
    completed = len(outcomes) - outcomes.count(RoundOutcome.UNDECIDED)

    def result(state, reason=None):
        return MatchEvaluation(state, a, b, completed, reason)

    if tiebreak_after is not None:
        for outcome in outcomes[tiebreak_after:]:
            if outcome in (RoundOutcome.PARTY_A_WINS, RoundOutcome.PARTY_B_WINS):
                return result(MatchEvaluationState(outcome.value), "sudden_death_winner")
        return result(MatchEvaluationState.SUDDEN_DEATH, "sudden_death")
    if config.mode == MatchMode.BEST_OF_N:
        if a + b > config.best_of:
            return result(MatchEvaluationState.NEEDS_RESOLUTION, "decisive_round_limit_exceeded")
        reached_a, reached_b = a >= config.required_wins, b >= config.required_wins
    else:
        reached_a, reached_b = score_a >= config.target_score, score_b >= config.target_score
    if reached_a and reached_b:
        return result(MatchEvaluationState.SUDDEN_DEATH, "sudden_death")
    if reached_a:
        return result(MatchEvaluationState.PARTY_A_WINS)
    if reached_b:
        return result(MatchEvaluationState.PARTY_B_WINS)
    if RoundOutcome.UNDECIDED in outcomes:
        return result(MatchEvaluationState.NEEDS_RESOLUTION, "undecided_unit")
    return result(MatchEvaluationState.RUNNING)
