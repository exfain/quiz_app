import unittest
from dataclasses import FrozenInstanceError
import subprocess
import sys

from .contracts import RoundOutcome, RoundResult
from .matches import MatchConfig, MatchEvaluationState as State, MatchMode, evaluate_match, parse_match_config


class MatchRuleTests(unittest.TestCase):
    def evaluate(self, n=7, a=0, b=0, **overrides):
        args = dict(score_a=0, score_b=0, round_results=[RoundResult(RoundOutcome.PARTY_A_WINS)] * a
                    + [RoundResult(RoundOutcome.PARTY_B_WINS)] * b)
        args.update(overrides)
        return evaluate_match(MatchConfig(MatchMode.BEST_OF_N, best_of=n), **args)

    def target(self, a, b, target=10, **overrides):
        return evaluate_match(MatchConfig(MatchMode.TARGET_SCORE, target_score=target), score_a=a, score_b=b, **overrides)

    def test_best_of_three_five_seven_and_one(self):
        for n, threshold in ((1, 1), (3, 2), (5, 3), (7, 4)):
            with self.subTest(n=n):
                self.assertEqual(MatchConfig(MatchMode.BEST_OF_N, best_of=n).required_wins, threshold)
                self.assertEqual(self.evaluate(n=n, a=threshold).state, State.PARTY_A_WINS)
                self.assertEqual(self.evaluate(n=n, b=threshold).state, State.PARTY_B_WINS)
                self.assertEqual(self.evaluate(n=n, a=threshold - 1).state, State.RUNNING)

    def test_seven_four_zero_and_four_three(self):
        for b in (0, 3):
            self.assertEqual(self.evaluate(a=4, b=b).winner_side, RoundOutcome.PARTY_A_WINS)

    def test_points_never_count_as_round_wins(self):
        result = self.evaluate(a=2, b=1, score_a=800, score_b=-50)
        self.assertEqual(result.state, State.RUNNING)
        self.assertEqual((result.wins_a, result.wins_b), (2, 1))

    def test_invalid_best_of(self):
        for n in (0, 2, 4, -1, 1.0, True, "3", None):
            with self.subTest(n=n), self.assertRaises(ValueError):
                MatchConfig(MatchMode.BEST_OF_N, best_of=n)

    def test_target_examples(self):
        for a, b, state in ((9, 7, State.RUNNING), (10, 7, State.PARTY_A_WINS),
                            (7, 10, State.PARTY_B_WINS), (12, 7, State.PARTY_A_WINS),
                            (-2, 4, State.RUNNING)):
            with self.subTest(a=a, b=b):
                self.assertEqual(self.target(a, b).state, state)

    def test_both_at_or_above_target_never_choose_leader(self):
        for a, b in ((10, 10), (11, 12), (12, 11)):
            result = self.target(a, b)
            self.assertEqual(result.state, State.SUDDEN_DEATH)
            self.assertIsNone(result.winner_side)

    def test_invalid_target(self):
        for n in (0, -1, 1.1, True, "10", None):
            with self.subTest(n=n), self.assertRaises(ValueError):
                MatchConfig(MatchMode.TARGET_SCORE, target_score=n)

    def test_draw_no_winner_and_undecided_remain_explicit(self):
        for outcome in (RoundOutcome.DRAW, RoundOutcome.NO_WINNER, RoundOutcome.UNDECIDED):
            result = self.evaluate(round_results=[RoundResult(outcome)])
            self.assertEqual(result.state, State.NEEDS_RESOLUTION if outcome == RoundOutcome.UNDECIDED else State.RUNNING)
            self.assertEqual((result.wins_a, result.wins_b), (0, 0))
            self.assertEqual(result.completed_units, 0 if outcome == RoundOutcome.UNDECIDED else 1)

    def test_clear_win_threshold_still_decides_despite_unresolved_draw_policy(self):
        rounds = [RoundResult(RoundOutcome.DRAW)] * 8 + [RoundResult(RoundOutcome.PARTY_A_WINS)] * 4
        self.assertEqual(self.evaluate(round_results=rounds).state, State.PARTY_A_WINS)

    def test_sudden_death_counts_only_solo_wins_after_the_saved_boundary(self):
        rounds = [RoundResult(RoundOutcome.PARTY_A_WINS), RoundResult(RoundOutcome.DRAW),
                  RoundResult(RoundOutcome.NO_WINNER), RoundResult(RoundOutcome.PARTY_B_WINS)]
        self.assertEqual(self.target(12, 12, round_results=rounds[:3], tiebreak_after=2).state, State.SUDDEN_DEATH)
        self.assertEqual(self.target(12, 13, round_results=rounds, tiebreak_after=2).state, State.PARTY_B_WINS)
        with self.assertRaises(ValueError): self.target(12, 12, round_results=rounds, tiebreak_after=5)

    def test_impossible_decisive_count_not_a_match_win(self):
        self.assertEqual(self.evaluate(a=4, b=4).state, State.NEEDS_RESOLUTION)

    def test_target_does_not_convert_round_wins_to_points(self):
        result = self.target(0, 0, round_results=[RoundResult(RoundOutcome.PARTY_A_WINS)] * 7)
        self.assertEqual(result.state, State.RUNNING)

    def test_explicit_config_mapping_and_no_defaults(self):
        for config in (MatchConfig(MatchMode.BEST_OF_N, best_of=5), MatchConfig(MatchMode.TARGET_SCORE, target_score=10)):
            self.assertEqual(parse_match_config({"match": config.to_dict(), "game_option": 42}), config)
        for data in ({}, {"match": {}}, {"match": {"mode": "unknown"}},
                     {"match": {"mode": "best_of_n", "best_of": 3, "target_score": 10}}):
            with self.subTest(data=data), self.assertRaises((TypeError, ValueError)):
                parse_match_config(data)

    def test_invalid_types_and_conflicting_configuration(self):
        with self.assertRaises(TypeError):
            MatchConfig("best_of_n", best_of=3)
        with self.assertRaises(ValueError):
            MatchConfig(MatchMode.BEST_OF_N, best_of=3, target_score=10)
        with self.assertRaises(ValueError):
            MatchConfig(MatchMode.TARGET_SCORE, target_score=10, best_of=3)
        with self.assertRaises(TypeError):
            self.target(True, 1)
        with self.assertRaises(TypeError):
            self.evaluate(round_results=["party_a_wins"])

    def test_contracts_are_frozen(self):
        config = MatchConfig(MatchMode.BEST_OF_N, best_of=3)
        with self.assertRaises(FrozenInstanceError):
            config.best_of = 5

    def test_imports_without_django_or_site_packages(self):
        result = subprocess.run([sys.executable, "-S", "-B", "-c", "import duel.matches"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
