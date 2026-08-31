"""
Automatisierte Tests für das Admin-Dashboard.

Abgedeckte Bereiche:
  - Login
  - Manage Games: Spiel anlegen (alle Typen), 2 löschen, Suche & Filter (Playwright), Zurück-Link
  - Sessions: Zurück-Link, neue Session mit je einem Spiel pro Typ
"""

# Django 6 / Python 3.14: LiveServerTestCase läuft intern in einem Event-Loop.
# Ohne dieses Flag schlägt jede synchrone DB-Operation (inkl. Django-internes
# flush) mit SynchronousOnlyOperation fehl.
import os
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import json
import random
import string
from pathlib import Path

from Assign.models import AssignQuestion, AssignQuiz
from Estimation.models import EstimationQuestion, EstimationQuiz
from QuizGame.models import Quiz, QuizQuestion
from black_jack_quiz.models import BlackJackQuiz
from clue_rush.models import Clue, ClueQuestion, ClueRushGame, ClueRushSession
from django.contrib.auth.models import User
from django.test import Client, LiveServerTestCase, TestCase
from django.urls import reverse
from games_hub.models import HubGameStep, HubSession
from sorting_ladder.models import SortingLadderGame
from where_is_this.models import WhereQuiz
from who_is_lying.models import WhoQuiz
from who_is_that.models import WhoThatQuiz
from wer_weiss_mehr.models import WerWeissMehrGame
from host_points.models import HostPointsGame
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

GAME_CREATE_URLS = {
    "quiz":           "admin_dashboard:create_quiz",
    "estimation":     "admin_dashboard:create_estimation_quiz",
    "assign":         "admin_dashboard:create_assign_quiz",
    "where":          "admin_dashboard:create_where_quiz",
    "who":            "admin_dashboard:create_who_quiz",
    "who_that":       "admin_dashboard:create_who_that_quiz",
    "blackjack":      "admin_dashboard:create_blackjack_quiz",
    "clue_rush":      "admin_dashboard:create_clue_rush_game",
    "sorting_ladder": "admin_dashboard:create_sorting_ladder_game",
    "host_points":    "admin_dashboard:create_host_points_game",
}

# Custom-Create-Endpunkte akzeptieren einen "title"-Parameter im JSON-Body.
# Die Quick-Create-Endpunkte oben ignorieren den Titel (hardcoded).
GAME_CUSTOM_CREATE_URLS = {
    "quiz":           "admin_dashboard:create_custom_quiz",
    "estimation":     "admin_dashboard:create_estimation_custom_quiz",
    "assign":         "admin_dashboard:create_assign_custom_quiz",
    "where":          "admin_dashboard:create_where_custom_quiz",
    "who":            "admin_dashboard:create_who_custom_quiz",
    "who_that":       "admin_dashboard:create_who_that_custom_quiz",
    "blackjack":      "admin_dashboard:create_black_jack_custom_quiz",
    "clue_rush":      "admin_dashboard:create_clue_rush_custom_game",
    "sorting_ladder": "admin_dashboard:create_sorting_ladder_custom_game",
    "host_points":    "admin_dashboard:create_host_points_game",
}

GAME_CUSTOM_UPDATE_URLS = {
    "quiz":           "admin_dashboard:update_custom_quiz",
    "estimation":     "admin_dashboard:update_estimation_custom_quiz",
    "assign":         "admin_dashboard:update_assign_custom_quiz",
    "where":          "admin_dashboard:update_where_custom_quiz",
    "who":            "admin_dashboard:update_who_custom_quiz",
    "who_that":       "admin_dashboard:update_who_that_custom_quiz",
    "blackjack":      "admin_dashboard:update_black_jack_custom_quiz",
    "clue_rush":      "admin_dashboard:update_clue_rush_custom_game",
    "sorting_ladder": "admin_dashboard:update_sorting_ladder_custom_game",
    "host_points":    "admin_dashboard:update_host_points_game",
}

GAME_MODELS = {
    "quiz": Quiz,
    "estimation": EstimationQuiz,
    "assign": AssignQuiz,
    "where": WhereQuiz,
    "who": WhoQuiz,
    "who_that": WhoThatQuiz,
    "blackjack": BlackJackQuiz,
    "clue_rush": ClueRushGame,
    "sorting_ladder": SortingLadderGame,
    "host_points": HostPointsGame,
}

GAME_TYPE_DISPLAY = {
    "quiz":           "Quick Quiz",
    "estimation":     "Estimation",
    "assign":         "Assign",
    "where":          "Where Is This?",
    "who":            "Who Is Lying?",
    "who_that":       "Who Is That?",
    "blackjack":      "Black Jack Quiz",
    "clue_rush":      "Clue Rush",
    "sorting_ladder": "Sorting Ladder",
    "host_points":    "Host-Punktevergabe",
}


def rand_str(n=8):
    return "".join(random.choices(string.ascii_lowercase, k=n))


def make_admin(username=None, password="testpass123"):
    username = username or f"admin_{rand_str()}"
    return User.objects.create_superuser(username=username, password=password, email="")


class HostEndStateTemplateTest(TestCase):
    """Regression coverage for live host end-state rendering."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(__file__).resolve().parents[1]

    def read_template(self, relative_path):
        return (self.root / relative_path).read_text(encoding="utf-8")

    def test_base_template_provides_common_host_end_state_for_all_monitor_routes(self):
        content = self.read_template("templates/admin_dashboard/base.html")

        self.assertIn("window.hostEndState", content)
        self.assertNotIn("hostEndStateBanner", content)
        self.assertNotIn("Zur Lobby", content)
        self.assertNotIn("Zur Session-&Uuml;bersicht", content)
        self.assertNotIn("Zum Dashboard", content)
        self.assertIn("#endGameBtn", content)
        for route_token in [
            "admin-dashboard\\/quiz",
            "admin-dashboard\\/estimation",
            "admin-dashboard\\/where",
            "admin-dashboard\\/assign",
            "admin-dashboard\\/sorting-ladder",
            "admin-dashboard\\/blackjack",
            "admin-dashboard\\/who",
            "admin-dashboard\\/who-that",
            "admin-dashboard\\/clue-rush",
            "admin-dashboard\\/wer-weiss-mehr",
            "admin-dashboard\\/buzzer",
            "admin-dashboard\\/host-points",
            "admin-dashboard\\/wann-war-das",
        ]:
            self.assertIn(route_token, content)

    def test_legacy_quiz_ended_handlers_apply_end_state_without_reload(self):
        monitor_paths = [
            "templates/admin_dashboard/quiz_monitor.html",
            "templates/admin_dashboard/estimation_monitor.html",
            "templates/admin_dashboard/where_monitor.html",
            "templates/admin_dashboard/assign_monitor.html",
            "templates/admin_dashboard/sorting_ladder_monitor.html",
            "templates/admin_dashboard/blackjack_monitor.html",
            "templates/admin_dashboard/who_lying_monitor.html",
            "templates/admin_dashboard/who_that_monitor.html",
            "templates/admin_dashboard/clue_rush_monitor.html",
        ]

        for path in monitor_paths:
            with self.subTest(path=path):
                content = self.read_template(path)
                start = content.find("case 'quiz_ended':")
                self.assertNotEqual(start, -1)
                end = content.find("case 'quiz_inactive':", start)
                block = content[start:end if end != -1 else start + 600]
                self.assertIn("hostEndState?.apply", block)
                self.assertNotIn("location.reload", block)

    def test_state_based_monitors_apply_common_end_state_when_completed(self):
        monitor_paths = [
            "templates/admin_dashboard/buzzer_monitor.html",
            "templates/admin_dashboard/host_points_monitor.html",
            "templates/admin_dashboard/wann_war_das_monitor.html",
            "templates/admin_dashboard/wer_weiss_mehr_monitor.html",
        ]

        for path in monitor_paths:
            with self.subTest(path=path):
                content = self.read_template(path)
                self.assertIn("hostEndState?.apply", content)
                self.assertRegex(content, r"completed|cancelled")


class HostQuestionStartTemplateTest(TestCase):
    """Regression coverage for question start acknowledgement flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(__file__).resolve().parents[1]

    def read_template(self, relative_path):
        return (self.root / relative_path).read_text(encoding="utf-8")

    def question_send_block(self, content):
        signatures = [
            "        sendQuestion(questionId) {",
            "        async sendQuestion(questionId) {",
            "        sendQuestion(questionId, totalElements) {",
        ]
        start = -1
        for signature in signatures:
            start = content.find(signature)
            if start != -1:
                break
        self.assertNotEqual(start, -1)
        end = content.find("endCurrentQuestion", start)
        self.assertNotEqual(end, -1)
        return content[start:end]

    def question_started_block(self, content):
        start = content.find("case 'question_started':")
        self.assertNotEqual(start, -1)
        end = content.find("break;", start)
        self.assertNotEqual(end, -1)
        return content[start:end]

    def test_host_does_not_mark_question_sent_before_server_ack(self):
        monitor_paths = [
            "templates/admin_dashboard/quiz_monitor.html",
            "templates/admin_dashboard/estimation_monitor.html",
            "templates/admin_dashboard/where_monitor.html",
            "templates/admin_dashboard/who_that_monitor.html",
            "templates/admin_dashboard/who_lying_monitor.html",
            "templates/admin_dashboard/clue_rush_monitor.html",
            "templates/admin_dashboard/assign_monitor.html",
            "templates/admin_dashboard/sorting_ladder_monitor.html",
            "templates/admin_dashboard/blackjack_monitor.html",
        ]

        for path in monitor_paths:
            with self.subTest(path=path):
                block = self.question_send_block(self.read_template(path))
                if "this.websocket.send(JSON.stringify(payload));" in block:
                    after_send = block.split("this.websocket.send(JSON.stringify(payload));", 1)[1]
                else:
                    self.assertIn("this.sendJsonWhenReady(payload", block)
                    after_send = block.split("this.sendJsonWhenReady(payload", 1)[1]
                self.assertNotIn("markQuestionSent(questionId)", after_send)
                self.assertNotIn("updateSendButtonAsSent(btn)", after_send)

    def test_host_marks_question_sent_only_after_question_started_event(self):
        monitor_paths = [
            "templates/admin_dashboard/quiz_monitor.html",
            "templates/admin_dashboard/estimation_monitor.html",
            "templates/admin_dashboard/where_monitor.html",
            "templates/admin_dashboard/who_that_monitor.html",
            "templates/admin_dashboard/who_lying_monitor.html",
            "templates/admin_dashboard/clue_rush_monitor.html",
            "templates/admin_dashboard/assign_monitor.html",
            "templates/admin_dashboard/sorting_ladder_monitor.html",
            "templates/admin_dashboard/blackjack_monitor.html",
        ]

        for path in monitor_paths:
            with self.subTest(path=path):
                block = self.question_started_block(self.read_template(path))
                self.assertIn("data.question?.id", block)
                self.assertIn("this.markQuestionSent(data.question.id)", block)

    def test_send_question_buttons_are_non_submit_buttons(self):
        monitor_paths = [
            "templates/admin_dashboard/quiz_monitor.html",
            "templates/admin_dashboard/estimation_monitor.html",
            "templates/admin_dashboard/where_monitor.html",
            "templates/admin_dashboard/who_that_monitor.html",
            "templates/admin_dashboard/who_lying_monitor.html",
            "templates/admin_dashboard/clue_rush_monitor.html",
            "templates/admin_dashboard/assign_monitor.html",
            "templates/admin_dashboard/sorting_ladder_monitor.html",
            "templates/admin_dashboard/blackjack_monitor.html",
        ]

        for path in monitor_paths:
            with self.subTest(path=path):
                content = self.read_template(path)
                self.assertRegex(content, r'<button\s+type="button"[^>]*class="[^"]*\bsend-question-btn\b')

    def test_quick_quiz_send_question_has_visible_error_and_ready_state_guard(self):
        content = self.read_template("templates/admin_dashboard/quiz_monitor.html")
        block = self.question_started_block(content)

        self.assertIn("type: 'admin_send_question'", content)
        self.assertIn("question_id: questionId", content)
        self.assertIn("hub_session: this.getHubSession()", content)
        self.assertIn("sendJsonWhenReady", content)
        self.assertIn("case 'error':", content)
        self.assertIn("showHostError", content)
        self.assertIn("Verbindung zum Server ist nicht bereit", content)
        self.assertIn(
            "this.renderActiveQuestion(data.question || {}, data.question_phase, data)",
            block,
        )
        self.assertNotIn("location.reload()", block)


# ---------------------------------------------------------------------------
# 1. Login-Tests
# ---------------------------------------------------------------------------

class LoginTest(TestCase):

    def setUp(self):
        self.password = "securePass99"
        self.user = make_admin(username="testadmin", password=self.password)
        self.login_url = reverse("admin_dashboard:login")
        self.home_url = reverse("admin_dashboard:home")

    def test_login_valid_credentials(self):
        """Korrektes Login leitet zum Dashboard weiter."""
        resp = self.client.post(self.login_url, {
            "username": self.user.username,
            "password": self.password,
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.redirect_chain[-1][0], [self.home_url, "/"])

    def test_login_invalid_credentials(self):
        """Falsches Passwort führt NICHT zum Dashboard (bleibt auf Login-Seite)."""
        resp = self.client.post(self.login_url, {
            "username": self.user.username,
            "password": "wrongpassword",
        }, follow=True)
        # Bleibt auf Login-Seite (kein Redirect zum Dashboard)
        final_url = resp.redirect_chain[-1][0] if resp.redirect_chain else resp.wsgi_request.path
        self.assertNotIn("dashboard", final_url.replace("admin-dashboard/login", ""))

    def test_login_page_accessible(self):
        """Login-Seite ist ohne Authentifizierung erreichbar."""
        resp = self.client.get(self.login_url)
        self.assertEqual(resp.status_code, 200)

    def test_logout(self):
        """Logout beendet die Session."""
        self.client.force_login(self.user)
        resp = self.client.post(reverse("admin_dashboard:logout"), follow=True)
        self.assertEqual(resp.status_code, 200)
        # Nach Logout: Dashboard nicht mehr erreichbar ohne Redirect zu Login
        resp2 = self.client.get(self.home_url)
        self.assertNotEqual(resp2.status_code, 200)  # Redirect oder 403

    def test_already_logged_in_redirects(self):
        """Eingeloggter Admin wird vom Login auf Dashboard weitergeleitet."""
        self.client.force_login(self.user)
        resp = self.client.get(self.login_url)
        self.assertIn(resp.status_code, [301, 302])


# ---------------------------------------------------------------------------
# 2. Manage-Games-Tests (Backend / API)
# ---------------------------------------------------------------------------

class ManageGamesApiTest(TestCase):
    """Testet das Erstellen und Löschen von Spielen via AJAX-Endpunkte."""

    def setUp(self):
        self.user = make_admin()
        self.client.force_login(self.user)

    # -- Spiel anlegen (je Typ) -----------------------------------------------

    def _create_game(self, game_key):
        url = reverse(GAME_CREATE_URLS[game_key])
        resp = self.client.post(
            url,
            data=json.dumps({"title": f"Test {GAME_TYPE_DISPLAY[game_key]} {rand_str(4)}"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, f"Erstellen fehlgeschlagen für '{game_key}': {resp.content}")
        data = resp.json()
        self.assertTrue(data.get("success"), f"Kein success=True für '{game_key}': {data}")
        self.assertIn("room_code", data, f"Kein room_code in Antwort für '{game_key}'")
        return data

    def test_create_quiz_game(self):
        self._create_game("quiz")

    def test_create_estimation_game(self):
        self._create_game("estimation")

    def test_create_assign_game(self):
        self._create_game("assign")

    def test_create_where_game(self):
        self._create_game("where")

    def test_create_who_game(self):
        self._create_game("who")

    def test_create_who_that_game(self):
        self._create_game("who_that")

    def test_create_blackjack_game(self):
        self._create_game("blackjack")

    def test_create_clue_rush_game(self):
        self._create_game("clue_rush")

    def test_create_sorting_ladder_game(self):
        self._create_game("sorting_ladder")

    def test_create_host_points_game(self):
        self._create_game("host_points")

    # -- Zwei Spiele löschen --------------------------------------------------

    def test_delete_two_games(self):
        """Legt je einen Quiz und einen Estimation-Eintrag an und löscht beide."""
        quiz_data = self._create_game("quiz")
        estimation_data = self._create_game("estimation")

        delete_url = reverse("admin_dashboard:delete_game_instance")

        for game_type, data in [("quiz", quiz_data), ("estimation", estimation_data)]:
            game_id = data.get("quiz_id") or data.get("game_id")
            resp = self.client.post(
                delete_url,
                data=json.dumps({"game_type": game_type, "game_id": game_id}),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200, f"Löschen fehlgeschlagen für '{game_type}'")
            result = resp.json()
            self.assertTrue(result.get("success"), f"success != True beim Löschen von '{game_type}': {result}")

    # -- Manage-Games-Seite ladbar --------------------------------------------

    def test_manage_games_page_loads(self):
        """Die Übersichtsseite aller Spiele lädt ohne Fehler."""
        resp = self.client.get(reverse("admin_dashboard:manage_games"))
        self.assertEqual(resp.status_code, 200)
        # Such-Input und Typ-Filter müssen im HTML vorhanden sein
        content = resp.content.decode()
        self.assertIn("gameSearchInput", content)
        self.assertIn("gameTypeFilter", content)

    def test_manage_games_shows_created_game(self):
        """Ein neu erstelltes Spiel erscheint auf der Übersichtsseite."""
        title = f"Suchtest_{rand_str(6)}"
        # Muss Custom-Create verwenden, da Quick-Create den Titel ignoriert
        resp = self.client.post(
            reverse(GAME_CUSTOM_CREATE_URLS["quiz"]),
            data=json.dumps({"title": title}),
            content_type="application/json",
        )
        self.assertTrue(resp.json().get("success"))

        page = self.client.get(reverse("admin_dashboard:manage_games"))
        self.assertContains(page, title)

    # -- Zurück zum Hauptmenü -------------------------------------------------

    def test_back_to_main_menu_link_present(self):
        """Das Manage-Games-Template enthält einen Link zurück zum Dashboard."""
        resp = self.client.get(reverse("admin_dashboard:manage_games"))
        self.assertEqual(resp.status_code, 200)
        home_url = reverse("admin_dashboard:home")
        self.assertContains(resp, home_url)

    def test_manage_games_create_flow_renders_explanation_fields_without_toggle(self):
        resp = self.client.get(reverse("admin_dashboard:create_game"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="gameTutorialEnabled"', html=False)
        self.assertContains(resp, "game-compact-card")
        self.assertContains(resp, "game-compact-grid")
        self.assertContains(resp, "Interner Name")
        self.assertContains(resp, "Spielerläuterung")
        self.assertContains(resp, "Text der Spielerläuterung")
        self.assertNotContains(resp, "Titel der Spielerläuterung")
        self.assertNotContains(resp, 'id="gameTutorialTitle"', html=False)
        self.assertContains(resp, 'id="gameTutorialText"', html=False)
        self.assertContains(resp, "tutorial_title: ''")
        self.assertContains(resp, "als Tutorial verwenden")
        self.assertContains(resp, "als Tutorialset verwenden")
        self.assertContains(resp, "tutorial_question_id")
        self.assertContains(resp, "tutorial_set_number")
        self.assertContains(resp, "tutorial-question-check")
        self.assertContains(resp, "game-question-workspace")
        self.assertContains(resp, "question-form-column")
        self.assertContains(resp, "question-bank-column")
        self.assertContains(resp, "question-bank-scroll")
        self.assertContains(resp, "assigned-questions-card")
        self.assertContains(resp, "assigned-questions-scroll")
        self.assertContains(resp, "max. Punktzahl ändern")
        self.assertContains(resp, "estimation-manual-points-switch")
        self.assertNotContains(resp, "Punkte manuell definieren")
        self.assertContains(resp, "const lastQuestionTimeValues = {};")
        self.assertContains(resp, "function rememberQuestionTimeValues(type)")
        self.assertContains(resp, "clearRememberedQuestionTimeValues();")
        self.assertContains(resp, "rememberQuestionTimeValues('quiz');")
        self.assertContains(resp, "rememberQuestionTimeValues(currentType);")
        self.assertContains(resp, "getRememberedQuestionTimeValue('quiz', 'quizTimeLimit', '30')")
        self.assertContains(resp, "getRememberedQuestionTimeValue('estimation', 'est-time', '30')")
        self.assertContains(resp, "getRememberedQuestionTimeValue('where', 'where-time', '30')")
        self.assertContains(resp, "getRememberedQuestionTimeValue('who_that', 'whot-time', '30')")
        self.assertContains(resp, "getRememberedQuestionTimeValue('clue_rush', 'cr-clue-dur', '10')")
        self.assertContains(resp, "getRememberedQuestionTimeValue('wann_war_das', 'wwd-time-limit', '')")
        content = resp.content.decode("utf-8")
        self.assertLess(content.index('id="quizQuestionFormCard"'), content.index('id="assignedQuestionsBody"'))
        self.assertLess(content.index('id="quizBankTableBody"'), content.index('id="assignedQuestionsBody"'))
        self.assertLess(content.index('id="genericQuestionFormCard"'), content.index('id="genericAssignedBody"'))
        self.assertLess(content.index('id="genericBankTableBody"'), content.index('id="genericAssignedBody"'))
        self.assertGreater(content.index("Spielerläuterung"), content.index('id="section-host_points"'))
        self.assertGreater(content.index('id="gameTutorialText"'), content.index('id="assignedQuestionsBody"'))
        self.assertGreater(content.index('id="gameTutorialText"'), content.index('id="genericAssignedBody"'))

    def test_game_models_expose_single_tutorial_question_slot(self):
        for model in [
            Quiz,
            EstimationQuiz,
            AssignQuiz,
            WhereQuiz,
            WhoQuiz,
            WhoThatQuiz,
            ClueRushGame,
            SortingLadderGame,
            WerWeissMehrGame,
        ]:
            self.assertTrue(hasattr(model, "tutorial_question"), model.__name__)
        self.assertTrue(hasattr(BlackJackQuiz, "tutorial_set_number"))
        self.assertFalse(hasattr(BlackJackQuiz, "tutorial_question"))

    def test_custom_quiz_stores_one_selected_tutorial_question(self):
        question_one = QuizQuestion.objects.create(
            question_text="Question one",
            question_type="short_answer",
            correct_answer="One",
            created_by=self.user,
        )
        question_two = QuizQuestion.objects.create(
            question_text="Question two",
            question_type="short_answer",
            correct_answer="Two",
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("admin_dashboard:create_custom_quiz"),
            data=json.dumps({
                "title": "Tutorial Quiz",
                "question_ids": [question_one.id, question_two.id],
                "tutorial_question_id": question_two.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data.get("success"), data)
        quiz = Quiz.objects.get(id=data["quiz_id"])
        self.assertEqual(quiz.tutorial_question_id, question_two.id)

        selected_resp = self.client.get(reverse("admin_dashboard:get_quiz_selected_questions", args=[quiz.id]))
        selected_data = selected_resp.json()
        self.assertEqual(selected_data["tutorial_question_id"], question_two.id)
        row_by_id = {row["id"]: row for row in selected_data["questions"]}
        self.assertFalse(row_by_id[question_one.id]["is_tutorial"])
        self.assertTrue(row_by_id[question_two.id]["is_tutorial"])

    def test_custom_quiz_rejects_multiple_tutorial_questions(self):
        question_one = QuizQuestion.objects.create(
            question_text="Question one",
            question_type="short_answer",
            correct_answer="One",
            created_by=self.user,
        )
        question_two = QuizQuestion.objects.create(
            question_text="Question two",
            question_type="short_answer",
            correct_answer="Two",
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("admin_dashboard:create_custom_quiz"),
            data=json.dumps({
                "title": "Invalid Tutorial Quiz",
                "question_ids": [question_one.id, question_two.id],
                "tutorial_question_ids": [question_one.id, question_two.id],
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json().get("success"))

    def test_custom_quiz_rejects_tutorial_question_outside_selection(self):
        selected_question = QuizQuestion.objects.create(
            question_text="Selected",
            question_type="short_answer",
            correct_answer="Selected",
            created_by=self.user,
        )
        other_question = QuizQuestion.objects.create(
            question_text="Other",
            question_type="short_answer",
            correct_answer="Other",
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("admin_dashboard:create_custom_quiz"),
            data=json.dumps({
                "title": "Invalid Tutorial Quiz",
                "question_ids": [selected_question.id],
                "tutorial_question_id": other_question.id,
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json().get("success"))

    def test_custom_quiz_clears_tutorial_question_when_removed_from_selection(self):
        question_one = QuizQuestion.objects.create(
            question_text="Question one",
            question_type="short_answer",
            correct_answer="One",
            created_by=self.user,
        )
        question_two = QuizQuestion.objects.create(
            question_text="Question two",
            question_type="short_answer",
            correct_answer="Two",
            created_by=self.user,
        )
        quiz = Quiz.objects.create(title="Tutorial Quiz", creator=self.user, status="waiting")
        quiz.selected_questions.set([question_one, question_two])
        quiz.tutorial_question = question_two
        quiz.question_order = [question_one.id, question_two.id]
        quiz.save(update_fields=["tutorial_question", "question_order"])

        resp = self.client.post(
            reverse("admin_dashboard:update_custom_quiz"),
            data=json.dumps({
                "quiz_id": quiz.id,
                "title": quiz.title,
                "question_ids": [question_one.id],
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get("success"))
        quiz.refresh_from_db()
        self.assertIsNone(quiz.tutorial_question_id)

    def test_custom_create_endpoints_store_explanation_fields_without_toggle(self):
        for game_key, url_name in GAME_CUSTOM_CREATE_URLS.items():
            title = f"Explanation {game_key} {rand_str(4)}"
            payload = {
                "title": title,
                "internal_description": "Admin note",
                "tutorial_title": f"{game_key} intro",
                "tutorial_text": f"{game_key} explanation text",
            }
            resp = self.client.post(
                reverse(url_name),
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200, f"{game_key}: {resp.content}")
            data = resp.json()
            self.assertTrue(data.get("success"), f"{game_key}: {data}")
            object_id = data.get("quiz_id") or data.get("game_id")
            obj = GAME_MODELS[game_key].objects.get(id=object_id)
            self.assertTrue(obj.tutorial_enabled, game_key)
            self.assertEqual(obj.tutorial_title, f"{game_key} intro", game_key)
            self.assertEqual(obj.tutorial_text, f"{game_key} explanation text", game_key)

    def test_custom_create_without_explanation_text_keeps_player_explanation_disabled(self):
        for game_key, url_name in GAME_CUSTOM_CREATE_URLS.items():
            title = f"No explanation {game_key} {rand_str(4)}"
            resp = self.client.post(
                reverse(url_name),
                data=json.dumps({
                    "title": title,
                    "tutorial_title": "",
                    "tutorial_text": "",
                }),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200, f"{game_key}: {resp.content}")
            data = resp.json()
            self.assertTrue(data.get("success"), f"{game_key}: {data}")
            object_id = data.get("quiz_id") or data.get("game_id")
            obj = GAME_MODELS[game_key].objects.get(id=object_id)
            self.assertFalse(obj.tutorial_enabled, game_key)
            self.assertEqual(obj.tutorial_title, "", game_key)
            self.assertEqual(obj.tutorial_text, "", game_key)

    def test_custom_update_endpoints_persist_tutorial_fields(self):
        id_key_by_game = {
            "quiz": "quiz_id",
            "estimation": "quiz_id",
            "assign": "quiz_id",
            "where": "quiz_id",
            "who": "quiz_id",
            "who_that": "quiz_id",
            "blackjack": "quiz_id",
            "clue_rush": "game_id",
            "sorting_ladder": "quiz_id",
            "host_points": "game_id",
        }
        selected_key_by_game = {
            "sorting_ladder": "topic_ids",
        }

        for game_key, create_url_name in GAME_CUSTOM_CREATE_URLS.items():
            create_resp = self.client.post(
                reverse(create_url_name),
                data=json.dumps({"title": f"Before {game_key}"}),
                content_type="application/json",
            )
            self.assertEqual(create_resp.status_code, 200, f"{game_key}: {create_resp.content}")
            create_data = create_resp.json()
            object_id = create_data.get("quiz_id") or create_data.get("game_id")
            update_payload = {
                id_key_by_game[game_key]: object_id,
                "title": f"After {game_key}",
                "tutorial_title": f"{game_key} updated intro",
                "tutorial_text": f"{game_key} updated explanation",
                selected_key_by_game.get(game_key, "question_ids"): [],
            }
            update_resp = self.client.post(
                reverse(GAME_CUSTOM_UPDATE_URLS[game_key]),
                data=json.dumps(update_payload),
                content_type="application/json",
            )
            self.assertEqual(update_resp.status_code, 200, f"{game_key}: {update_resp.content}")
            update_data = update_resp.json()
            self.assertTrue(update_data.get("success"), f"{game_key}: {update_data}")
            obj = GAME_MODELS[game_key].objects.get(id=object_id)
            self.assertEqual(obj.title, f"After {game_key}", game_key)
            self.assertTrue(obj.tutorial_enabled, game_key)
            self.assertEqual(obj.tutorial_title, f"{game_key} updated intro", game_key)
            self.assertEqual(obj.tutorial_text, f"{game_key} updated explanation", game_key)

    def test_edit_game_prefills_tutorial_values(self):
        quiz = Quiz.objects.create(
            title="Explanation Quiz",
            internal_description="Admin info",
            tutorial_enabled=True,
            tutorial_title="Welcome",
            tutorial_text="Read this first",
            creator=self.user,
            status="waiting",
        )
        question = QuizQuestion.objects.create(
            question_text="Tutorial question",
            question_type="short_answer",
            correct_answer="Answer",
            created_by=self.user,
        )
        quiz.selected_questions.set([question])
        quiz.tutorial_question = question
        quiz.save(update_fields=["tutorial_question"])

        resp = self.client.get(reverse("admin_dashboard:edit_game", args=["quiz", quiz.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'EDIT_TUTORIAL_TITLE', html=False)
        self.assertNotContains(resp, 'id="gameTutorialTitle"', html=False)
        self.assertContains(resp, 'const EDIT_TUTORIAL_TEXT = "Read this first";', html=False)
        self.assertContains(resp, f'const EDIT_TUTORIAL_QUESTION_ID = {question.id};', html=False)
        self.assertNotContains(resp, 'id="gameTutorialEnabled"', html=False)

    def test_tutorial_toggle_is_gated_by_tutorial_enabled_in_all_monitors(self):
        monitor_templates = [
            "templates/admin_dashboard/quiz_monitor.html",
            "templates/admin_dashboard/estimation_monitor.html",
            "templates/admin_dashboard/assign_monitor.html",
            "templates/admin_dashboard/where_monitor.html",
            "templates/admin_dashboard/who_lying_monitor.html",
            "templates/admin_dashboard/who_that_monitor.html",
            "templates/admin_dashboard/blackjack_monitor.html",
            "templates/admin_dashboard/clue_rush_monitor.html",
            "templates/admin_dashboard/sorting_ladder_monitor.html",
            "templates/admin_dashboard/wer_weiss_mehr_monitor.html",
        ]
        repo_root = Path(__file__).resolve().parent.parent
        expected_gate = "{% if quiz.status == 'waiting' and quiz.tutorial_enabled %}"

        for relative_path in monitor_templates:
            content = (repo_root / relative_path).read_text(encoding="utf-8")
            self.assertIn(expected_gate, content, relative_path)
            self.assertIn("Spielerläuterung anzeigen", content, relative_path)
            self.assertNotIn('id="showTutorialToggle" checked', content, relative_path)
            self.assertIn("Tutorial spielen", content, relative_path)
            self.assertIn('id="playTutorialToggle"', content, relative_path)
            self.assertNotIn('id="playTutorialToggle" checked', content, relative_path)
            self.assertIn("play_tutorial", content, relative_path)
            self.assertIn("tutorial_question_missing", content, relative_path)
            self.assertIn("showTutorialQuestionMissing", content, relative_path)
            self.assertIn("tutorial_ack_warning", content, relative_path)
            self.assertIn("handleTutorialAckWarning", content, relative_path)
            self.assertIn("_unit_tutorial_notice.html", content, relative_path)
            self.assertIn("unitTutorialNotice", content, relative_path)
            self.assertIn("host-monitor-content", content, relative_path)
            if relative_path.endswith("blackjack_monitor.html"):
                self.assertIn("Für dieses Spiel wurde kein Tutorialset festgelegt.", content)

        blackjack_content = (repo_root / "templates/admin_dashboard/blackjack_monitor.html").read_text(encoding="utf-8")
        self.assertIn("tutorial_notice_label='Tutorialset'", blackjack_content)

        base_content = (repo_root / "templates/admin_dashboard/base.html").read_text(encoding="utf-8")
        self.assertIn("Compact Host Monitor UI", base_content)
        self.assertIn(".host-monitor-content", base_content)
        self.assertIn(".host-session-monitor", base_content)
        self.assertIn("#endQuizBtn::before", base_content)
        self.assertIn("scrollbar-gutter: stable", base_content)
        self.assertIn("Nicht alle Teilnehmer haben die Erläuterung bestätigt", base_content)
        self.assertIn("tutorialQuestionMissingModal", base_content)
        self.assertIn("showTutorialQuestionMissing", base_content)
        self.assertIn("F&uuml;r dieses Spiel wurde keine Tutorialfrage festgelegt.", base_content)
        self.assertIn("Trotzdem fortfahren", base_content)
        self.assertIn("Zurück", base_content)

    def test_tutorial_overlay_is_wired_into_all_active_player_templates(self):
        play_templates = [
            "templates/quiz/play.html",
            "templates/estimation/play.html",
            "templates/assign/play.html",
            "templates/where_is_this/play.html",
            "templates/who_is_lying/play.html",
            "templates/who_is_that/play.html",
            "templates/black_jack_quiz/play.html",
            "templates/clue_rush/play.html",
            "templates/sorting_ladder/play.html",
            "templates/wer_weiss_mehr/play.html",
        ]
        repo_root = Path(__file__).resolve().parent.parent

        for relative_path in play_templates:
            content = (repo_root / relative_path).read_text(encoding="utf-8")
            self.assertIn("{% include 'includes/_game_tutorial_overlay.html' %}", content, relative_path)
            self.assertIn("_unit_tutorial_notice.html", content, relative_path)
            self.assertIn("unitTutorialNotice", content, relative_path)
            self.assertTrue(
                "case 'tutorial_start':" in content or "data.type === 'tutorial_start'" in content,
                relative_path,
            )
            self.assertIn("tutorial_force_close", content, relative_path)

        blackjack_content = (repo_root / "templates/black_jack_quiz/play.html").read_text(encoding="utf-8")
        self.assertIn("tutorial_notice_label='Tutorialset'", blackjack_content)
        spectator_content = (repo_root / "templates/hub/spectate.html").read_text(encoding="utf-8")
        self.assertIn("_unit_tutorial_notice.html", spectator_content)
        self.assertIn("unitTutorialNotice", spectator_content)
        session_monitor_content = (repo_root / "templates/hub/monitor.html").read_text(encoding="utf-8")
        self.assertIn("host-session-monitor", session_monitor_content)


# Assign-Fragen dÃ¼rfen fÃ¼r Tests jetzt auch rechtsseitige Distractors oder Gleichstand haben.
    def test_add_assign_question_allows_right_side_to_be_larger(self):
        resp = self.client.post(
            reverse("admin_dashboard:add_assign_question"),
            data=json.dumps({
                "question_text": "Right distractor test",
                "points": 10,
                "time_limit": 45,
                "left_items": ["A", "B", "C", "D"],
                "right_items": ["W", "X", "Y", "Z", "Distractor"],
                "correct_matches": {"0": 0, "1": 1, "2": 2, "3": 3},
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data.get("success"), data)
        self.assertTrue(AssignQuestion.objects.filter(id=data["question_id"]).exists())

    def test_update_assign_question_allows_equal_side_lengths(self):
        question = AssignQuestion.objects.create(
            question_text="Before update",
            points=10,
            time_limit=45,
            left_items=["A", "B", "C", "D", "E"],
            right_items=["W", "X", "Y", "Z"],
            correct_matches={"0": 0, "1": 1, "2": 2, "3": 3},
            created_by=self.user,
        )

        resp = self.client.post(
            reverse("admin_dashboard:update_assign_question"),
            data=json.dumps({
                "question_id": question.id,
                "question_text": "Equal size update",
                "points": 10,
                "time_limit": 45,
                "left_items": ["A", "B", "C", "D"],
                "right_items": ["W", "X", "Y", "Z"],
                "correct_matches": {"0": 0, "1": 1, "2": 2, "3": 3},
            }),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get("success"), resp.json())
        question.refresh_from_db()
        self.assertEqual(question.question_text, "Equal size update")
        self.assertEqual(len(question.left_items), 4)
        self.assertEqual(len(question.right_items), 4)


# ---------------------------------------------------------------------------
# 3. Sessions-Tests (Backend)
# ---------------------------------------------------------------------------

class SessionsTest(TestCase):

    def setUp(self):
        self.user = make_admin()
        self.client.force_login(self.user)
        self.create_session_url = reverse("games_hub:create_session")

    def _create_game_room(self, game_key):
        """Legt ein Spiel an und gibt dessen room_code zurück."""
        url = reverse(GAME_CREATE_URLS[game_key])
        resp = self.client.post(
            url,
            data=json.dumps({"title": f"Session-Test {GAME_TYPE_DISPLAY[game_key]}"}),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data.get("success"), f"Spiel-Erstellung fehlgeschlagen für {game_key}: {data}")
        return data["room_code"]

    # -- Zurück-Link auf Sessions-Seite ---------------------------------------

    def test_sessions_overview_back_link(self):
        """Die Sessions-Übersicht enthält einen Link zum Dashboard (Hauptmenü)."""
        resp = self.client.get(reverse("admin_dashboard:sessions_overview"))
        self.assertEqual(resp.status_code, 200)
        home_url = reverse("admin_dashboard:home")
        self.assertContains(resp, home_url)

    def test_create_session_page_loads(self):
        """Die Seite zum Erstellen einer Session lädt ohne Fehler."""
        resp = self.client.get(self.create_session_url)
        self.assertEqual(resp.status_code, 200)

    def test_active_games_end_button_prefers_session_view_redirect(self):
        quiz = Quiz.objects.create(
            creator=self.user,
            title="Active Session Quiz",
            status="active",
        )
        session = HubSession.objects.create(code=f"SESS{rand_str(4)}")
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key="quiz",
            room_code=quiz.room_code,
            title=quiz.title,
        )

        resp = self.client.get(reverse("admin_dashboard:sessions_overview"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="btn btn-sm btn-outline-danger end-game-btn"')
        self.assertContains(resp, f'data-session-url="{reverse("games_hub:monitor", args=[session.code])}"')
        self.assertContains(
            resp,
            f'window.location.href = this.dataset.sessionUrl || \'{reverse("admin_dashboard:sessions_overview")}\';'
        )

    def test_active_games_end_button_falls_back_to_sessions_overview_without_session_context(self):
        Quiz.objects.create(
            creator=self.user,
            title="Standalone Active Quiz",
            status="active",
        )

        resp = self.client.get(reverse("admin_dashboard:sessions_overview"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            f'data-session-url="{reverse("admin_dashboard:sessions_overview")}"'
        )

    def test_active_games_overview_renders_end_button_for_clue_rush(self):
        game = ClueRushGame.objects.create(
            creator=self.user,
            title="Active Clue Rush",
            status="active",
        )

        resp = self.client.get(reverse("admin_dashboard:sessions_overview"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            reverse("admin_dashboard:end_clue_rush_game_by_room_code", args=[game.room_code]),
        )
        self.assertContains(resp, 'class="btn btn-sm btn-outline-danger end-game-btn"')

    def test_end_clue_rush_game_by_room_code_completes_game_and_clears_runtime_state(self):
        question = ClueQuestion.objects.create(
            question_text="Guess the city",
            answer="Berlin",
            created_by=self.user,
        )
        clue = Clue.objects.create(
            clue_question=question,
            clue_text="Capital of Germany",
            order=1,
            duration=10,
        )
        game = ClueRushGame.objects.create(
            creator=self.user,
            title="Endable Clue Rush",
            status="active",
            current_question=question,
            current_clue=clue,
        )
        ClueRushSession.objects.create(
            quiz=game,
            is_question_active=True,
            is_clue_active=True,
            current_clue_number=1,
        )

        resp = self.client.post(
            reverse("admin_dashboard:end_clue_rush_game_by_room_code", args=[game.room_code]),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["success"])

        game.refresh_from_db()
        game.session.refresh_from_db()

        self.assertEqual(game.status, "completed")
        self.assertIsNotNone(game.ended_at)
        self.assertIsNone(game.current_question_id)
        self.assertIsNone(game.current_clue_id)
        self.assertFalse(game.session.is_question_active)
        self.assertFalse(game.session.is_clue_active)
        self.assertEqual(game.session.current_clue_number, 0)
        self.assertIsNone(game.session.question_end_time)
        self.assertIsNone(game.session.clue_end_time)

    def test_sessions_overview_renders_end_all_active_games_button(self):
        Quiz.objects.create(
            creator=self.user,
            title="Bulk End Visible Quiz",
            status="active",
        )

        resp = self.client.get(reverse("admin_dashboard:sessions_overview"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="endAllActiveGamesBtn"', html=False)
        self.assertContains(resp, 'Alle aktiven Spiele beenden')
        self.assertContains(resp, reverse("admin_dashboard:end_all_active_games"))
        self.assertNotContains(resp, 'id="endAllActiveGamesBtn" disabled', html=False)

    def test_end_all_active_games_completes_every_active_game_type(self):
        active_quiz = Quiz.objects.create(
            creator=self.user,
            title="Bulk End Quick Quiz",
            status="active",
        )
        active_blackjack = BlackJackQuiz.objects.create(
            creator=self.user,
            title="Bulk End Black Jack",
            status="active",
        )
        active_clue_rush = ClueRushGame.objects.create(
            creator=self.user,
            title="Bulk End Clue Rush",
            status="active",
        )
        waiting_quiz = Quiz.objects.create(
            creator=self.user,
            title="Waiting Quiz",
            status="waiting",
        )

        resp = self.client.post(
            reverse("admin_dashboard:end_all_active_games"),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["ended_games_count"], 3)

        active_quiz.refresh_from_db()
        active_blackjack.refresh_from_db()
        active_clue_rush.refresh_from_db()
        waiting_quiz.refresh_from_db()

        self.assertEqual(active_quiz.status, "completed")
        self.assertEqual(active_blackjack.status, "completed")
        self.assertEqual(active_clue_rush.status, "completed")
        self.assertIsNotNone(active_quiz.ended_at)
        self.assertIsNotNone(active_blackjack.ended_at)
        self.assertIsNotNone(active_clue_rush.ended_at)
        self.assertEqual(waiting_quiz.status, "waiting")

    # -- Neue Session mit je einem Spiel pro Typ ------------------------------

    def test_create_session_with_all_game_types(self):
        """Erstellt eine Session mit einem Spiel pro Spieltyp und zufälligem Namen."""
        session_name = f"Test-Session-{rand_str(6)}"

        # Für jeden Spieltyp ein Spiel anlegen
        games_order = []
        for game_key in GAME_CREATE_URLS:
            room_code = self._create_game_room(game_key)
            games_order.append({
                "game_key": game_key,
                "room_code": room_code,
                "title": GAME_TYPE_DISPLAY[game_key],
            })

        resp = self.client.post(
            self.create_session_url,
            data={
                "name": session_name,
                "games_order": json.dumps(games_order),
            },
            follow=True,
        )

        # Erfolg: Redirect zur Session-Übersicht der neu erstellten Session
        self.assertEqual(resp.status_code, 200)
        final_url = resp.redirect_chain[-1][0] if resp.redirect_chain else ""
        self.assertIn("/admin-dashboard/sessions/", final_url, f"Kein Redirect zur Session-Übersicht: {resp.redirect_chain}")

        # Session wurde in DB angelegt
        from games_hub.models import HubSession, HubGameStep
        session = HubSession.objects.get(name=session_name)
        self.assertEqual(session.steps.count(), len(GAME_CREATE_URLS))

    def test_create_session_random_name(self):
        """Session-Name ist frei wählbar (zufälliger String)."""
        random_name = rand_str(12)
        room_code = self._create_game_room("quiz")

        resp = self.client.post(
            self.create_session_url,
            data={
                "name": random_name,
                "games_order": json.dumps([
                    {"game_key": "quiz", "room_code": room_code, "title": "Quick Quiz"}
                ]),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

        from games_hub.models import HubSession
        self.assertTrue(HubSession.objects.filter(name=random_name).exists())

    def test_create_session_back_link(self):
        """Die Session-Erstellungsseite enthält einen Link zum Hauptmenü."""
        resp = self.client.get(self.create_session_url)
        self.assertEqual(resp.status_code, 200)
        home_url = reverse("admin_dashboard:home")
        self.assertContains(resp, home_url)


# ---------------------------------------------------------------------------
# Estimation Monitor Rendering
# ---------------------------------------------------------------------------

class EstimationMonitorRenderTest(TestCase):
    def setUp(self):
        self.user = make_admin()
        self.client.force_login(self.user)
        self.quiz = EstimationQuiz.objects.create(
            title="Monitor Estimation",
            creator=self.user,
            status="waiting",
        )
        EstimationQuestion.objects.create(
            question_text="How high is the Eiffel Tower?",
            correct_answer=330,
            unit="meters",
            created_by=self.user,
        )

    def test_estimation_monitor_prefills_host_time_input_with_runtime_default(self):
        resp = self.client.get(reverse("admin_dashboard:estimation_monitor", args=[self.quiz.room_code]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            'class="form-control form-control-sm question-time-input" placeholder="Time (s)" min="5" step="1" style="max-width: 120px;" value="90"',
            html=False,
        )


# ---------------------------------------------------------------------------
# 4. Browser-Tests mit Playwright (Suche & Filter in Manage Games)
# ---------------------------------------------------------------------------

class ManageGamesBrowserTest(LiveServerTestCase):
    """
    Testet clientseitige Funktionen (Suche, Typ-Filter) mit Playwright.
    Voraussetzung: playwright + Chromium installiert.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls._pw, cls._browser = start_chromium_browser(headless=True)
            cls._playwright_available = True
        except Exception:
            cls._playwright_available = False

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_playwright_available", False):
            cls._browser.close()
            cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._playwright_available:
            self.skipTest("Playwright/Chromium nicht verfügbar")
        self.password = "testpass123"
        self.admin = make_admin(username=f"btest_{rand_str()}", password=self.password)
        self.context = self._browser.new_context()
        install_browser_test_stubs(self.context)
        self.page = self.context.new_page()
        self._login()
        self._create_test_games()

    def tearDown(self):
        self.page.close()
        self.context.close()

    def _login(self):
        """Loggt den Admin-User via Browser ein."""
        self.page.goto(f"{self.live_server_url}{reverse('admin_dashboard:login')}")
        self.page.fill("input[name='username']", self.admin.username)
        self.page.fill("input[name='password']", self.password)
        self.page.click("button[type='submit']")
        self.page.wait_for_url(f"**{reverse('admin_dashboard:home')}**", timeout=5000)

    def _create_test_games(self):
        """Legt Testspiele mit Custom-Titeln an (verwendet Custom-Create-Endpunkte)."""
        from django.test import Client as DjangoClient
        c = DjangoClient()
        c.force_login(self.admin)
        self._test_titles = {}
        for game_key, url_name in GAME_CUSTOM_CREATE_URLS.items():
            title = f"Suchspiel {GAME_TYPE_DISPLAY[game_key]} {rand_str(4)}"
            resp = c.post(
                reverse(url_name),
                data=json.dumps({"title": title}),
                content_type="application/json",
            )
            if resp.status_code == 200 and resp.json().get("success"):
                self._test_titles[game_key] = title

    def test_search_by_name_filters_rows(self):
        """Tippen in das Suchfeld blendet nicht-passende Spiele aus."""
        self.page.goto(f"{self.live_server_url}{reverse('admin_dashboard:manage_games')}")
        self.page.wait_for_selector("#gameSearchInput")

        # Alle Testspiele haben "Suchspiel" im Titel — gemeinsamer Prefix der nur
        # auf unsere Testdaten passt, nicht auf Default-Titel wie "Quick Quiz".
        search_term = "Suchspiel"
        self.page.fill("#gameSearchInput", search_term)

        # Kurz warten, damit JS-Filter läuft
        self.page.wait_for_timeout(300)

        # Alle sichtbaren Zeilen lesen
        rows = self.page.locator("tbody tr[onclick]")
        visible_count = 0
        for i in range(rows.count()):
            row = rows.nth(i)
            if row.is_visible():
                visible_count += 1
                name_text = row.locator("td:nth-child(2)").text_content().lower()
                self.assertIn("suchspiel", name_text,
                              f"Sichtbare Zeile passt nicht zum Suchbegriff: {name_text!r}")
        self.assertGreaterEqual(visible_count, 1, "Keine Treffer nach Suche")

    def test_filter_by_game_type(self):
        """Der Typ-Filter blendet nur Spiele des gewählten Typs ein."""
        self.page.goto(f"{self.live_server_url}{reverse('admin_dashboard:manage_games')}")
        self.page.wait_for_selector("#gameTypeFilter")

        # Auf "Quick Quiz" filtern
        self.page.select_option("#gameTypeFilter", "quick quiz")
        self.page.wait_for_timeout(300)

        rows = self.page.locator("tbody tr[onclick]")
        for i in range(rows.count()):
            row = rows.nth(i)
            if row.is_visible():
                type_text = row.locator("td:nth-child(3)").text_content().lower()
                self.assertIn("quick quiz", type_text,
                              f"Sichtbare Zeile hat falschen Typ: {type_text!r}")

    def test_filter_reset_shows_all(self):
        """Zurücksetzen des Filters zeigt wieder alle Zeilen."""
        self.page.goto(f"{self.live_server_url}{reverse('admin_dashboard:manage_games')}")
        self.page.wait_for_selector("#gameTypeFilter")

        total_before = self.page.locator("tbody tr[onclick]").count()

        self.page.select_option("#gameTypeFilter", "quick quiz")
        self.page.wait_for_timeout(200)

        self.page.select_option("#gameTypeFilter", "")
        self.page.wait_for_timeout(200)

        visible_after = sum(
            1 for i in range(self.page.locator("tbody tr[onclick]").count())
            if self.page.locator("tbody tr[onclick]").nth(i).is_visible()
        )
        self.assertEqual(visible_after, total_before)

    def test_back_to_main_menu_click(self):
        """Klick auf Logo/Hauptmenü-Link navigiert zurück zum Dashboard."""
        self.page.goto(f"{self.live_server_url}{reverse('admin_dashboard:manage_games')}")
        home_url = reverse("admin_dashboard:home")
        # Link mit href zum Dashboard anklicken
        self.page.click(f"a[href='{home_url}']")
        self.page.wait_for_url(f"**{home_url}**", timeout=5000)
        self.assertIn(home_url, self.page.url)

    def test_sessions_back_to_main_menu_click(self):
        """Auf der Sessions-Seite navigiert der Hauptmenü-Link korrekt."""
        self.page.goto(f"{self.live_server_url}{reverse('admin_dashboard:sessions_overview')}")
        home_url = reverse("admin_dashboard:home")
        self.page.click(f"a[href='{home_url}']")
        self.page.wait_for_url(f"**{home_url}**", timeout=5000)
        self.assertIn(home_url, self.page.url)
