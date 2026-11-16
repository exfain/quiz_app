"""Standard-library unit tests, runnable without Django settings or a DB."""

from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import UUID

from duel.contracts import (
    DuelInstanceContext,
    GameFormat,
    MatchState,
    MatchStatus,
    RoundOutcome,
    RoundResult,
    ScoreChange,
    SessionType,
    default_game_format,
    resolve_session_type,
    validate_game_format,
)


class DuelContractTests(unittest.TestCase):
    def setUp(self):
        self.context = DuelInstanceContext(
            hub_session_id=1,
            hub_game_step_id=2,
            duel_instance_id=UUID("8b760865-50c0-445d-a804-c782fd93bff6"),
            current_unit_id=3,
            game_key="estimation",
            participant_a_id=4,
            participant_b_id=5,
        )

    def test_valid_context_has_two_distinct_integer_participant_ids(self):
        self.assertEqual(self.context.hub_session_id, 1)
        self.assertEqual(self.context.hub_game_step_id, 2)
        self.assertIsInstance(self.context.duel_instance_id, UUID)
        self.assertEqual(self.context.current_unit_id, 3)
        self.assertEqual(self.context.game_key, "estimation")
        self.assertEqual(
            (self.context.participant_a_id, self.context.participant_b_id), (4, 5)
        )
        # BigAutoField IDs must not accidentally be restricted to 32 bits.
        self.assertEqual(
            replace(self.context, participant_b_id=2**40).participant_b_id, 2**40
        )

    def test_identical_participants_are_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.context, participant_b_id=self.context.participant_a_id)

    def test_context_cannot_have_fewer_or_more_than_two_parties(self):
        values = {field.name: getattr(self.context, field.name) for field in fields(self.context)}
        for missing in ("participant_a_id", "participant_b_id"):
            with self.subTest(missing=missing), self.assertRaises(TypeError):
                DuelInstanceContext(**{key: value for key, value in values.items() if key != missing})
        with self.assertRaises(TypeError):
            DuelInstanceContext(**values, participant_c_id=6)

    def test_context_rejects_invalid_id_types_and_nonpositive_ids(self):
        for name in (
            "hub_session_id", "hub_game_step_id", "current_unit_id",
            "participant_a_id", "participant_b_id",
        ):
            for value in (True, False, "4", 4.0, None, [4]):
                with self.subTest(field=name, value=value), self.assertRaises(TypeError):
                    replace(self.context, **{name: value})
            for value in (0, -1):
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    replace(self.context, **{name: value})

    def test_context_requires_uuid_and_nonempty_game_key(self):
        for value in (str(self.context.duel_instance_id), 1, None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                replace(self.context, duel_instance_id=value)
        for value in (None, 1, ["estimation"]):
            with self.subTest(value=value), self.assertRaises(TypeError):
                replace(self.context, game_key=value)
        for value in ("", " \t"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.context, game_key=value)

    def test_all_dataclass_fields_are_immutable(self):
        contracts = (
            self.context,
            ScoreChange(self.context.participant_a_id, 1),
            RoundResult(RoundOutcome.UNDECIDED),
            MatchState(MatchStatus.IN_PROGRESS),
        )
        for contract in contracts:
            for field in fields(contract):
                with self.subTest(contract=type(contract).__name__, field=field.name):
                    with self.assertRaises(FrozenInstanceError):
                        setattr(contract, field.name, getattr(contract, field.name))
                    with self.assertRaises(FrozenInstanceError):
                        delattr(contract, field.name)

    def test_new_unit_context_does_not_mutate_original(self):
        following = replace(self.context, current_unit_id=6)
        self.assertEqual(self.context.current_unit_id, 3)
        self.assertEqual(following.current_unit_id, 6)
        self.assertEqual(following.duel_instance_id, self.context.duel_instance_id)

    def test_positive_point_delta(self):
        change = ScoreChange(self.context.participant_a_id, 3)
        self.assertEqual(change.participant_id, self.context.participant_a_id)
        self.assertEqual(change.delta, 3)

    def test_negative_point_delta(self):
        self.assertEqual(ScoreChange(self.context.participant_a_id, -3).delta, -3)

    def test_zero_point_delta(self):
        self.assertEqual(ScoreChange(self.context.participant_a_id, 0).delta, 0)

    def test_point_delta_requires_integer_and_valid_participant_id(self):
        for value in (True, False, 1.0, "1", None):
            with self.subTest(delta=value), self.assertRaises(TypeError):
                ScoreChange(self.context.participant_a_id, value)
        for value in (True, "4", 4.0, None):
            with self.subTest(participant=value), self.assertRaises(TypeError):
                ScoreChange(value, 1)
        for value in (0, -1):
            with self.subTest(participant=value), self.assertRaises(ValueError):
                ScoreChange(value, 1)

    def test_either_party_can_be_explicit_round_winner(self):
        for outcome in (RoundOutcome.PARTY_A_WINS, RoundOutcome.PARTY_B_WINS):
            with self.subTest(outcome=outcome):
                self.assertIs(RoundResult(outcome).outcome, outcome)

    def test_draw_no_winner_and_undecided_are_distinct(self):
        results = tuple(RoundResult(outcome) for outcome in (
            RoundOutcome.DRAW, RoundOutcome.NO_WINNER, RoundOutcome.UNDECIDED
        ))
        self.assertEqual(len(set(results)), 3)
        self.assertEqual(len(RoundOutcome), 5)
        for result in results:
            self.assertNotIn(result.outcome, (
                RoundOutcome.PARTY_A_WINS, RoundOutcome.PARTY_B_WINS
            ))

    def test_match_statuses_include_running_completed_and_aborted(self):
        for status in (
            MatchStatus.NOT_STARTED, MatchStatus.IN_PROGRESS,
            MatchStatus.COMPLETED, MatchStatus.ABORTED,
        ):
            with self.subTest(status=status):
                self.assertIs(MatchState(status).status, status)
        self.assertEqual(len(MatchStatus), 4)

    def test_round_and_match_contracts_reject_untyped_or_crossed_states(self):
        for value in ("draw", None, MatchStatus.COMPLETED):
            with self.subTest(outcome=value), self.assertRaises(TypeError):
                RoundResult(value)
        for value in ("completed", None, RoundOutcome.PARTY_A_WINS):
            with self.subTest(status=value), self.assertRaises(TypeError):
                MatchState(value)

    def test_points_round_outcome_and_match_status_are_independent(self):
        # A has more points, yet B can explicitly win the round. No rule is inferred.
        changes = (ScoreChange(self.context.participant_a_id, 7),
                   ScoreChange(self.context.participant_b_id, 1))
        result = RoundResult(RoundOutcome.PARTY_B_WINS)
        match = MatchState(MatchStatus.IN_PROGRESS)
        self.assertGreater(changes[0].delta, changes[1].delta)
        self.assertIs(result.outcome, RoundOutcome.PARTY_B_WINS)
        self.assertIs(match.status, MatchStatus.IN_PROGRESS)
        for outcome in (RoundOutcome.DRAW, RoundOutcome.NO_WINNER, RoundOutcome.UNDECIDED):
            with self.subTest(outcome=outcome):
                self.assertIs(RoundResult(outcome).outcome, outcome)
                self.assertIs(match.status, MatchStatus.IN_PROGRESS)
        # Both parties scoring also imposes neither a round winner nor a match end.
        equal_changes = tuple(ScoreChange(participant, 1) for participant in (
            self.context.participant_a_id, self.context.participant_b_id
        ))
        self.assertEqual(equal_changes[0].delta, equal_changes[1].delta)
        self.assertIs(RoundResult(RoundOutcome.DRAW).outcome, RoundOutcome.DRAW)
        self.assertIs(MatchState(MatchStatus.COMPLETED).status, MatchStatus.COMPLETED)

    def test_import_works_without_site_packages_or_django_setup(self):
        root = str(Path(__file__).resolve().parent.parent)
        script = (
            f"import sys; sys.path.insert(0, {root!r}); "
            "import duel; import duel.contracts; "
            "assert not any(name == 'django' or name.startswith('django.') "
            "for name in sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class SessionFormatContractTests(unittest.TestCase):
    def test_session_types_have_distinct_stable_values(self):
        self.assertEqual(
            {member.name: member.value for member in SessionType},
            {"MULTIPLAYER": "multiplayer", "DUEL": "duel"},
        )
        self.assertNotEqual(SessionType.MULTIPLAYER, SessionType.DUEL)

    def test_all_four_formats_have_distinct_stable_values(self):
        self.assertEqual(
            {member.name: member.value for member in GameFormat},
            {"STANDARD": "standard", "DUEL": "duel",
             "TOURNAMENT": "tournament", "ONE_VS_MANY": "one_vs_many"},
        )
        self.assertEqual(len({member.value for member in GameFormat}), 4)

    def test_enum_values_and_names_cannot_be_reassigned(self):
        for member in (*SessionType, *GameFormat):
            with self.subTest(member=member):
                with self.assertRaises(AttributeError):
                    member.value = "changed"
                with self.assertRaises(AttributeError):
                    member.name = "CHANGED"

    def test_multiplayer_default_is_standard(self):
        self.assertIs(default_game_format(SessionType.MULTIPLAYER), GameFormat.STANDARD)

    def test_duel_default_is_duel(self):
        self.assertIs(default_game_format(SessionType.DUEL), GameFormat.DUEL)

    def test_missing_legacy_type_resolves_to_multiplayer(self):
        self.assertIs(resolve_session_type(), SessionType.MULTIPLAYER)
        self.assertIs(resolve_session_type(None), SessionType.MULTIPLAYER)
        self.assertIs(default_game_format(), GameFormat.STANDARD)
        self.assertIs(default_game_format(None), GameFormat.STANDARD)
        self.assertIsNone(validate_game_format(None, GameFormat.STANDARD))

    def test_explicit_session_type_is_preserved(self):
        for session_type in SessionType:
            with self.subTest(session_type=session_type):
                self.assertIs(resolve_session_type(session_type), session_type)

    def test_duel_session_accepts_only_duel_format(self):
        self.assertIsNone(validate_game_format(SessionType.DUEL, GameFormat.DUEL))
        for game_format in (GameFormat.STANDARD, GameFormat.TOURNAMENT, GameFormat.ONE_VS_MANY):
            with self.subTest(game_format=game_format):
                with self.assertRaisesRegex(ValueError, "DUEL sessions allow only"):
                    validate_game_format(SessionType.DUEL, game_format)

    def test_multiplayer_accepts_standard_and_duel(self):
        for game_format in (GameFormat.STANDARD, GameFormat.DUEL):
            with self.subTest(game_format=game_format):
                self.assertIsNone(validate_game_format(SessionType.MULTIPLAYER, game_format))

    def test_future_formats_are_structurally_valid_without_changing_defaults(self):
        for game_format in (GameFormat.TOURNAMENT, GameFormat.ONE_VS_MANY):
            with self.subTest(game_format=game_format):
                self.assertIsNone(validate_game_format(SessionType.MULTIPLAYER, game_format))
                self.assertIsNone(validate_game_format(None, game_format))
                self.assertIs(default_game_format(SessionType.MULTIPLAYER), GameFormat.STANDARD)
                self.assertIs(default_game_format(SessionType.DUEL), GameFormat.DUEL)

    def test_unknown_storage_values_are_rejected_by_enums(self):
        for enum in (SessionType, GameFormat):
            for value in ("", "unknown", "MULTIPLAYER", "two_party", None, 1):
                with self.subTest(enum=enum.__name__, value=value):
                    with self.assertRaises(ValueError):
                        enum(value)
            for member in enum:
                self.assertIs(enum(member.value), member)

    def test_functions_require_typed_session_values_except_missing_legacy_type(self):
        for value in ("", "unknown", "multiplayer", "duel", True, 1, [], GameFormat.DUEL):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    resolve_session_type(value)
                with self.assertRaises(TypeError):
                    default_game_format(value)
                with self.assertRaises(TypeError):
                    validate_game_format(value, GameFormat.DUEL)

    def test_validation_rejects_untyped_and_crossed_game_format_values(self):
        for value in ("", "unknown", "standard", "duel", None, True, 1, [], SessionType.DUEL):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    validate_game_format(SessionType.MULTIPLAYER, value)


if __name__ == "__main__":
    unittest.main()
