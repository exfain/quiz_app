from pathlib import Path

from django.template import Template
from django.template.backends.django import get_installed_libraries
from django.template.loader import render_to_string
from django.test import TestCase

from games_hub.models import HubGameStep, HubSession
from games_hub.templatetags.session_game_tags import session_game_number


REPO_ROOT = Path(__file__).resolve().parent.parent

PLAYER_TEMPLATE_EXPECTATIONS = [
    ("templates/quiz/play.html", "game_key='quiz'", "room_code=quiz.room_code"),
    ("templates/estimation/play.html", "game_key='estimation'", "room_code=quiz.room_code"),
    ("templates/where_is_this/play.html", "game_key='where'", "room_code=quiz.room_code"),
    ("templates/who_is_that/play.html", "game_key='who_that'", "room_code=quiz.room_code"),
    ("templates/clue_rush/play.html", "game_key='clue_rush'", "room_code=quiz.room_code"),
    ("templates/wann_war_das/play.html", "game_key='wann_war_das'", "room_code=game.room_code"),
    ("templates/buzzer/play.html", "game_key='buzzer'", "room_code=game.room_code"),
    ("templates/host_points/play.html", "game_key='host_points'", "room_code=game.room_code"),
    ("templates/assign/play.html", "game_key='assign'", "room_code=quiz.room_code"),
    ("templates/sorting_ladder/play.html", "game_key='sorting_ladder'", "room_code=quiz.room_code"),
    ("templates/black_jack_quiz/play.html", "game_key='blackjack'", "room_code=quiz.room_code"),
    ("templates/wer_weiss_mehr/play.html", "game_key='wer_weiss_mehr'", "room_code=quiz.room_code"),
    ("templates/who_is_lying/play.html", "game_key='who'", "room_code=quiz.room_code"),
]


def read_template(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class SessionGameNumberTests(TestCase):
    def test_session_game_tags_library_is_registered(self):
        libraries = get_installed_libraries()

        self.assertIn("session_game_tags", libraries)
        self.assertEqual(libraries["session_game_tags"], "games_hub.templatetags.session_game_tags")
        Template("{% load session_game_tags %}")

    def test_session_game_number_uses_one_based_game_plan_order(self):
        session = HubSession.objects.create(code="NUM123", name="Numbered Session")
        HubGameStep.objects.create(
            session=session,
            order=2,
            game_key="quiz",
            room_code="ROOM1",
            title="Third Game",
        )

        self.assertEqual(session_game_number("NUM123", "quiz", "ROOM1"), 3)
        self.assertEqual(session_game_number("NUM123", "quiz", "missing"), "")
        self.assertEqual(session_game_number("", "quiz", "ROOM1"), "")

    def test_shared_include_renders_decent_game_number_label(self):
        content = read_template("templates/includes/_session_game_number.html")

        self.assertIn("session_game_number hub_session game_key room_code", content)
        self.assertIn("Spiel {{ session_game_number_value }}", content)
        self.assertIn("class=\"session-game-number\"", content)

    def test_shared_include_renders_number_from_session_step(self):
        session = HubSession.objects.create(code="INC123", name="Include Session")
        HubGameStep.objects.create(
            session=session,
            order=1,
            game_key="who_that",
            room_code="WHO1",
            title="Who Game",
        )

        html = render_to_string(
            "includes/_session_game_number.html",
            {"hub_session": "INC123", "game_key": "who_that", "room_code": "WHO1"},
        )

        self.assertIn("Spiel 2", html)
        self.assertIn("session-game-number", html)

    def test_estimation_player_template_loads_session_game_tags(self):
        html = render_to_string("estimation/play.html", {"hub_session": "INC123"})

        self.assertIn("<!DOCTYPE html>", html)

    def test_player_templates_show_session_game_number_below_game_title(self):
        for relative_path, game_key_snippet, room_code_snippet in PLAYER_TEMPLATE_EXPECTATIONS:
            with self.subTest(template=relative_path):
                content = read_template(relative_path)
                self.assertIn("_session_game_number.html", content)
                self.assertIn(game_key_snippet, content)
                self.assertIn(room_code_snippet, content)
