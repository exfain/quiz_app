"""
Automatisierte End-to-End-Tests für den Teilnehmer-Flow einer Hub-Session.

Abgedeckte Bereiche:
  - Teilnehmer tritt der Lobby bei (Nickname eingeben, Join klicken)
  - Admin startet die Session
  - Für jeden Spieltyp:
      Admin öffnet Spiel-Monitor → Spiel starten → Frage senden →
      Teilnehmer beantwortet Frage → Admin beendet Frage → Admin beendet Spiel →
      Admin zurück zur Übersicht
  - Admin beendet Session → Teilnehmer sieht Final Leaderboard

Voraussetzungen:
  - playwright + Chromium installiert
  - channels.testing.ChannelsLiveServerTestCase für WebSocket-Unterstützung
"""

import os
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import base64
import json
import random
import string

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as DjangoClient
from django.urls import reverse

# ── Minimales 1×1-PNG für who_that (Image-Pflichtfeld) ──────────────────────
MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# ── Hilfskonstanten ──────────────────────────────────────────────────────────

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
}

GAME_MONITOR_URL_NAMES = {
    "quiz":           "admin_dashboard:quiz_monitor",
    "estimation":     "admin_dashboard:estimation_monitor",
    "assign":         "admin_dashboard:assign_monitor",
    "where":          "admin_dashboard:where_monitor",
    "who":            "admin_dashboard:who_lying_monitor",
    "who_that":       "admin_dashboard:who_that_monitor",
    "blackjack":      "admin_dashboard:blackjack_monitor",
    "clue_rush":      "admin_dashboard:clue_rush_monitor",
    "sorting_ladder": "admin_dashboard:sorting_ladder_monitor",
}

# URL-Prefix der Teilnehmer-Spielseite pro Spieltyp
PARTICIPANT_PLAY_PREFIX = {
    "quiz":           "/quiz/play/",
    "estimation":     "/estimation/play/",
    "assign":         "/assign/play/",
    "where":          "/where/play/",
    "who":            "/who/play/",
    "who_that":       "/who-is-that/play/",
    "blackjack":      "/blackjack/play/",
    "clue_rush":      "/clue-rush/play/",
    "sorting_ladder": "/sorting-ladder/play/",
}


def rand_str(n=6):
    return "".join(random.choices(string.ascii_lowercase, k=n))


def make_admin(username=None, password="testpass123"):
    username = username or f"admin_{rand_str()}"
    return User.objects.create_superuser(username=username, password=password, email="")


# ── Haupt-Test-Klasse ────────────────────────────────────────────────────────

try:
    from channels.testing import ChannelsLiveServerTestCase as _Base
except ImportError:
    from django.test import LiveServerTestCase as _Base


class ParticipantFlowBrowserTest(_Base):
    """
    End-to-End-Browsertest für den vollständigen Teilnehmer-Flow.

    Startet zwei Browser-Kontexte:
      • Admin   – steuert die Session
      • Teilnehmer – nimmt an allen Spielen teil

    Der Test iteriert über alle 9 Spieltypen in einem ``subTest``-Block,
    sodass ein einzelner Fehler die übrigen Spielzyklen nicht abbricht.
    """

    NICKNAME = "TestSpieler"
    TIMEOUT  = 20_000   # ms – Standard-Wartezeit
    LONG     = 35_000   # ms – Wartezeit für WebSocket-Navigation

    # ── Klassen-Setup: Playwright starten ────────────────────────────────────

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
            cls._playwright_available = True
        except Exception:
            cls._playwright_available = False

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_playwright_available", False):
            cls._browser.close()
            cls._pw.stop()
        super().tearDownClass()

    # ── Instanz-Setup: Testdaten + Browser-Seiten ────────────────────────────

    def setUp(self):
        if not self._playwright_available:
            self.skipTest("Playwright/Chromium nicht verfügbar – Test übersprungen")

        self.password = "testpass123"
        self.admin = make_admin(password=self.password)
        self.dc = DjangoClient()
        self.dc.force_login(self.admin)

        # Spiele + Fragen erstellen und Session anlegen
        self.game_data = self._create_all_games_with_questions()
        self.session_code = self._create_session()

        # Browser-Kontexte öffnen
        self.admin_ctx = self._browser.new_context()
        self.part_ctx  = self._browser.new_context()
        self.admin_page = self.admin_ctx.new_page()
        self.part_page  = self.part_ctx.new_page()

        self._admin_login()

    def tearDown(self):
        for page in (self.admin_page, self.part_page):
            try:
                page.close()
            except Exception:
                pass
        for ctx in (self.admin_ctx, self.part_ctx):
            try:
                ctx.close()
            except Exception:
                pass

    # ── Hilfsmethoden: Testdaten ─────────────────────────────────────────────

    def _create_all_games_with_questions(self):
        """Legt für jeden Spieltyp eine Instanz mit einer Testfrage an."""
        data = {}
        for game_key, url_name in GAME_CREATE_URLS.items():
            resp = self.dc.post(
                reverse(url_name),
                data=json.dumps({"title": f"Flow-Test {GAME_TYPE_DISPLAY[game_key]}"}),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200, f"Game-Erstellung fehlgeschlagen: {game_key}")
            body = resp.json()
            self.assertTrue(body.get("success"), f"success!=True für {game_key}: {body}")
            data[game_key] = body["room_code"]
            self._add_question_for_game(game_key)
        return data

    def _add_question_for_game(self, game_key):
        """Fügt dem letzten Spiel des angegebenen Typs eine Testfrage hinzu."""

        if game_key == "quiz":
            self.dc.post(reverse("admin_dashboard:add_question"), {
                "question_text": "Was ist 2 + 2?",
                "question_type": "multiple_choice",
                "correct_answer": "A",
                "option_a": "4",
                "option_b": "3",
                "option_c": "5",
                "option_d": "6",
                "points": 10,
                "time_limit": 30,
            })

        elif game_key == "estimation":
            self.dc.post(
                reverse("admin_dashboard:add_estimation_question"),
                data=json.dumps({
                    "question_text": "Wie hoch ist der Eiffelturm (in Meter)?",
                    "correct_answer": 330,
                    "unit": "Meter",
                    "tolerance_percentage": 20.0,
                    "max_points": 100,
                }),
                content_type="application/json",
            )

        elif game_key == "assign":
            # 1 linkes Item → nur 1 Runde nötig, einfacherer Test-Flow
            self.dc.post(
                reverse("admin_dashboard:add_assign_question"),
                data=json.dumps({
                    "question_text": "Ordne das Tier seinem Laut zu",
                    "left_items": ["Katze"],
                    "right_items": ["Miaut", "Bellt"],
                    "correct_matches": {"0": "0"},
                    "points": 10,
                    "time_limit": 60,
                    "explanation": "",
                }),
                content_type="application/json",
            )

        elif game_key == "where":
            self.dc.post(reverse("admin_dashboard:add_where_question"), {
                "question_text": "Wo steht der Eiffelturm?",
                "latitude": 48.8584,
                "longitude": 2.2945,
                "time_limit": 60,
                "points": 100,
                "perfect_distance": 10,
                "good_distance": 100,
                "fair_distance": 500,
                "poor_distance": 2000,
            })

        elif game_key == "who":
            self.dc.post(
                reverse("admin_dashboard:add_who_question"),
                data=json.dumps({
                    "statement": "Ich war auf dem Mond.",
                    "people": [
                        {"name": "Alice", "is_lying": True},
                        {"name": "Bob", "is_lying": False},
                    ],
                    "points": 10,
                    "time_limit": 60,
                    "explanation": "",
                }),
                content_type="application/json",
            )

        elif game_key == "who_that":
            self.dc.post(
                reverse("admin_dashboard:add_who_that_question"),
                {
                    "question_text": "Wer ist das?",
                    "correct_answer": "Albert Einstein",
                    "points": 100,
                    "time_limit": 30,
                    "image": SimpleUploadedFile(
                        "test.png", MINIMAL_PNG, content_type="image/png"
                    ),
                },
            )

        elif game_key == "blackjack":
            self.dc.post(
                reverse("admin_dashboard:add_blackjack_question"),
                data=json.dumps({
                    "question_text": "Wie viele Tage hat ein Jahr?",
                    "correct_answer": 365,
                    "time_limit": 30,
                }),
                content_type="application/json",
            )

        elif game_key == "clue_rush":
            self.dc.post(
                reverse("admin_dashboard:add_clue_rush_question"),
                data=json.dumps({
                    "question_text": "Was ist die Hauptstadt von Frankreich?",
                    "answer": "Paris",
                    "points": 10,
                    "time_limit": 30,
                    "clues": [
                        {"clue_text": "Es liegt in Europa", "order": 1, "duration": 10},
                        {"clue_text": "Es hat den Eiffelturm", "order": 2, "duration": 10},
                    ],
                }),
                content_type="application/json",
            )

        elif game_key == "sorting_ladder":
            self.dc.post(
                reverse("admin_dashboard:add_sorting_topic"),
                {
                    "title": "Sortiere nach Größe",
                    "description": "Vom größten zum kleinsten",
                    "points": "10",
                    "round_time_limit": "30",
                    "upper_label": "Größte",
                    "lower_label": "Kleinste",
                    "items_json": json.dumps([
                        {"text": "Elefant", "order": 1},
                        {"text": "Hund",    "order": 2},
                        {"text": "Maus",    "order": 3},
                    ]),
                },
            )

    def _create_session(self):
        """Erstellt eine HubSession mit allen angelegten Spielen."""
        games_order = [
            {
                "game_key":  game_key,
                "room_code": room_code,
                "title":     GAME_TYPE_DISPLAY[game_key],
            }
            for game_key, room_code in self.game_data.items()
        ]
        session_name = f"Flow-Test-{rand_str()}"
        resp = self.dc.post(
            reverse("games_hub:create_session"),
            data={
                "name":        session_name,
                "games_order": json.dumps(games_order),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200, "Session-Erstellung fehlgeschlagen")

        from games_hub.models import HubSession
        session = HubSession.objects.get(name=session_name)
        return session.code

    # ── Hilfsmethoden: Browser ───────────────────────────────────────────────

    def _admin_login(self):
        self.admin_page.goto(
            f"{self.live_server_url}{reverse('admin_dashboard:login')}"
        )
        self.admin_page.fill("input[name='username']", self.admin.username)
        self.admin_page.fill("input[name='password']", self.password)
        self.admin_page.click("button[type='submit']")
        self.admin_page.wait_for_url(
            f"**{reverse('admin_dashboard:home')}**", timeout=self.TIMEOUT
        )

    def _wait_for_reload(self, page, timeout=None):
        """Wartet auf einen Seitenreload (Navigation + DOMContentLoaded)."""
        page.wait_for_load_state("domcontentloaded", timeout=timeout or self.TIMEOUT)

    def _start_game(self):
        """
        Klickt 'Spiel starten' und wartet auf den aktiven Monitor-Zustand.

        startQuizBtn ruft location.reload() sofort auf (nicht erst nach WS-
        Antwort). Wenn der WebSocket beim Klick noch nicht verbunden ist,
        passiert gar nichts. Daher:
          1. Kurz warten, bis der WS verbunden ist (2 s reichen lokal).
          2. Klicken + auf Reload warten.
          3. Falls noch 'waiting': erneut laden und prüfen.
        """
        # 1) Warte auf WS-Verbindung
        self.admin_page.wait_for_timeout(2000)
        self.admin_page.wait_for_selector("#startQuizBtn:not([disabled])", timeout=self.TIMEOUT)

        # 2) Klicken (löst location.reload() aus, falls WS offen)
        self.admin_page.click("#startQuizBtn")
        self._wait_for_reload(self.admin_page)

        # 3) Race Condition: Falls noch 'waiting', kurz warten und neu laden
        if not self.admin_page.is_visible("#endQuizBtn"):
            self.admin_page.wait_for_timeout(2000)
            self.admin_page.reload()
            self._wait_for_reload(self.admin_page)

        self.admin_page.wait_for_selector("#endQuizBtn", timeout=self.TIMEOUT)

    # ── Haupt-Test ───────────────────────────────────────────────────────────

    def test_full_participant_flow(self):
        """
        Vollständiger Teilnehmer-Flow:
        Lobby beitreten → alle Spieltypen durchspielen → Final Leaderboard.
        """
        # ── 1. Teilnehmer öffnet Lobby ──────────────────────────────────────
        self.part_page.goto(
            f"{self.live_server_url}/hub/lobby/{self.session_code}/"
        )
        self.part_page.wait_for_selector("#nickname", timeout=self.TIMEOUT)

        # ── 2. Nickname eingeben + Join ────────────────────────────────────
        self.part_page.fill("#nickname", self.NICKNAME)
        self.part_page.wait_for_selector(
            "#joinBtn:not([disabled])", timeout=self.TIMEOUT
        )
        self.part_page.click("#joinBtn")
        # Nach dem Beitritt wird die Join-Card ausgeblendet (maybeHideJoin)
        self.part_page.wait_for_selector(
            "#joinCard", state="hidden", timeout=self.TIMEOUT
        )

        # ── 3. Admin öffnet Hub-Monitor und startet die Session ─────────────
        self.admin_page.goto(
            f"{self.live_server_url}/hub/monitor/{self.session_code}/"
        )
        self.admin_page.wait_for_selector("#startSessionBtn", timeout=self.TIMEOUT)
        self.admin_page.click("#startSessionBtn")
        # Button verschwindet nach dem Start
        self.admin_page.wait_for_selector(
            "#startSessionBtn", state="detached", timeout=self.TIMEOUT
        )

        # ── 4. Alle Spiele durchspielen ─────────────────────────────────────
        for game_key, room_code in self.game_data.items():
            with self.subTest(spiel=game_key):
                self._play_game_cycle(game_key, room_code)

        # ── 5. Admin beendet die Session ────────────────────────────────────
        # Sicherstellen, dass Admin auf Hub-Monitor ist
        self.admin_page.wait_for_url(
            f"**/hub/monitor/{self.session_code}/**", timeout=self.TIMEOUT
        )
        self.admin_page.once("dialog", lambda d: d.accept())
        self.admin_page.click("#endSessionBtn")

        # ── 6. Teilnehmer sieht Final Leaderboard ───────────────────────────
        self.part_page.wait_for_url(
            f"**/hub/session/{self.session_code}/leaderboard/**",
            timeout=self.LONG,
        )
        content = self.part_page.content()
        self.assertIn(
            "Leaderboard",
            content,
            "Final Leaderboard nicht auf der Teilnehmer-Seite sichtbar",
        )

    # ── Spielzyklus ──────────────────────────────────────────────────────────

    def _play_game_cycle(self, game_key, room_code):
        """
        Führt einen vollständigen Zyklus für einen Spieltyp durch:
        Monitor öffnen → starten → Frage senden → Teilnehmer antwortet →
        Frage beenden → Spiel beenden → zurück zur Hub-Übersicht.
        """
        monitor_url = (
            f"{self.live_server_url}"
            f"{reverse(GAME_MONITOR_URL_NAMES[game_key], args=[room_code])}"
            f"?hub_session={self.session_code}"
        )

        # ── a. Admin: Spiel-Monitor öffnen ──────────────────────────────────
        self.admin_page.goto(monitor_url)
        self.admin_page.wait_for_selector("#startQuizBtn", timeout=self.TIMEOUT)

        # ── b. Admin: Spiel starten ──────────────────────────────────────────
        self._start_game()

        # ── c. Admin: Frage senden ──────────────────────────────────────────
        if game_key == "clue_rush":
            self.admin_page.wait_for_selector("#sendClueBtn", timeout=self.TIMEOUT)
            self.admin_page.click("#sendClueBtn")
        else:
            self.admin_page.wait_for_selector(
                ".send-question-btn:not([disabled])", timeout=self.TIMEOUT
            )
            self.admin_page.locator(".send-question-btn").first.click()
        # Spielmonitor lädt nach question_started neu
        self._wait_for_reload(self.admin_page)
        # Nach Reload: Frage aktiv – Assign: Runden-Button oder endQuestionBtn (je nach Rundenanzahl)
        if game_key == "assign":
            self.admin_page.wait_for_selector(
                "#endRoundEarlyBtn, #nextRoundBtn, #endQuestionBtn", timeout=self.TIMEOUT
            )
        else:
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)

        # ── d. Teilnehmer: Wird zur Spielseite navigiert ───────────────────
        play_pattern = f"**{PARTICIPANT_PLAY_PREFIX[game_key]}{room_code}/**"
        self.part_page.wait_for_url(play_pattern, timeout=self.LONG)

        # ── e. Teilnehmer: Frage beantworten ───────────────────────────────
        self._submit_participant_answer(game_key)

        # ── f. Admin: Frage/Runde beenden ───────────────────────────────────
        if game_key == "assign":
            # Runden-Button oder endQuestionBtn (bei 1 Runde)
            self.admin_page.locator(
                "#endRoundEarlyBtn, #nextRoundBtn, #endQuestionBtn"
            ).first.click()
            self._wait_for_reload(self.admin_page)
        else:
            self.admin_page.click("#endQuestionBtn")
            self._wait_for_reload(self.admin_page)

        # ── g. Admin: Spiel beenden ─────────────────────────────────────────
        self.admin_page.wait_for_selector("#endQuizBtn", timeout=self.TIMEOUT)
        self.admin_page.once("dialog", lambda d: d.accept())
        self.admin_page.click("#endQuizBtn")
        self._wait_for_reload(self.admin_page)

        # ── h. Admin: Zurück zur Übersicht ──────────────────────────────────
        self.admin_page.wait_for_selector("#backToHubBtn", timeout=self.TIMEOUT)
        self.admin_page.click("#backToHubBtn")
        self.admin_page.wait_for_url(
            f"**/hub/monitor/{self.session_code}/**", timeout=self.TIMEOUT
        )

        # ── i. Teilnehmer kehrt kontrolliert per Button zur Lobby zurück ──
        self.part_page.wait_for_selector("#returnToLobbyBtn", timeout=self.LONG)
        self.part_page.click("#returnToLobbyBtn")
        self.part_page.wait_for_url(
            f"**/hub/lobby/{self.session_code}/**", timeout=self.LONG
        )
        # Lobby-Titel ist immer sichtbar (Join-Card kann ausgeblendet sein)
        self.part_page.wait_for_selector("#lobbyTitle", timeout=self.TIMEOUT)

    # ── Antwort-Logik pro Spieltyp ───────────────────────────────────────────

    def _submit_participant_answer(self, game_key):
        """
        Lässt den Teilnehmer eine Antwort eingeben und abschicken.
        Bei Spielen mit komplexen Drag-Drop- oder Karten-Interfaces wird
        der Submit-Button per JavaScript freigeschaltet.
        """
        # Warte darauf, dass der Submit-Button überhaupt im DOM ist
        self.part_page.wait_for_selector("#submitAnswerBtn", timeout=self.LONG)

        if game_key == "quiz":
            # Erste Antwort-Option anklicken
            self.part_page.wait_for_selector(".answer-option", timeout=self.LONG)
            self.part_page.locator(".answer-option").first.click()
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "estimation":
            self.part_page.wait_for_selector("#estimateInput", timeout=self.LONG)
            self.part_page.fill("#estimateInput", "300")
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "assign":
            # Item in die Drop-Zone ziehen, dann Submit klicken
            self.part_page.wait_for_selector(".draggable-item", timeout=self.LONG)
            source = self.part_page.locator(".draggable-item").first
            target = self.part_page.locator(".drop-zone").first
            source.drag_to(target)
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "where":
            # Karte-Klick überspringen: Button per JS freischalten
            self.part_page.evaluate(
                "document.getElementById('submitAnswerBtn').disabled = false;"
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "who":
            # Erste Personen-Karte anklicken
            self.part_page.wait_for_selector(".person-card", timeout=self.LONG)
            self.part_page.locator(".person-card").first.click()
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "who_that":
            # Namen eintippen
            self.part_page.wait_for_selector("#nameInput", timeout=self.LONG)
            self.part_page.fill("#nameInput", "Albert Einstein")
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "blackjack":
            self.part_page.wait_for_selector("#answerInput", timeout=self.LONG)
            self.part_page.fill("#answerInput", "365")
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "clue_rush":
            # Texteingabe erscheint dynamisch, wenn Hinweise eintreffen
            self.part_page.wait_for_selector("#shortAnswerInput", timeout=self.LONG)
            self.part_page.fill("#shortAnswerInput", "Paris")
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "sorting_ladder":
            # Drag-to-sort überspringen: Button per JS freischalten
            self.part_page.evaluate(
                "document.getElementById('submitAnswerBtn').disabled = false;"
            )
            self.part_page.click("#submitAnswerBtn")

        # Warte auf Bestätigungsanzeige (where/sorting_ladder ohne Selektor)
        if game_key not in ("where", "sorting_ladder"):
            self.part_page.wait_for_selector(
                "#answerSubmittedState:not(.d-none)", timeout=self.TIMEOUT
            )
