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
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser

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
    "who":            "admin_dashboard:who_monitor",
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
      • Teilnehmer – nimmt an einem isolierten Spiel teil

    Jeder Spieltyp besitzt einen eigenen Test mit eigener Session und eigenen
    Browser-Kontexten, damit ein Fehler keinen anderen Spielzyklus beschädigt.
    """

    NICKNAME = "TestSpieler"
    TIMEOUT  = 20_000   # ms – Standard-Wartezeit
    LONG     = 35_000   # ms – Wartezeit für WebSocket-Navigation
    FLOW_GAME_BY_TEST = {
        "test_quiz_participant_flow": "quiz",
        "test_estimation_participant_flow": "estimation",
        "test_estimation_question_shell_stays_mounted_across_three_questions": "estimation",
        "test_assign_participant_flow": "assign",
        "test_where_participant_flow": "where",
        "test_who_participant_flow": "who",
        "test_who_that_participant_flow": "who_that",
        "test_blackjack_participant_flow": "blackjack",
        "test_clue_rush_participant_flow": "clue_rush",
        "test_sorting_ladder_participant_flow": "sorting_ladder",
    }

    # ── Klassen-Setup: Playwright starten ────────────────────────────────────

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

    # ── Instanz-Setup: Testdaten + Browser-Seiten ────────────────────────────

    def setUp(self):
        if not self._playwright_available:
            self.skipTest("Playwright/Chromium nicht verfügbar – Test übersprungen")

        self.password = "testpass123"
        self.admin = make_admin(password=self.password)
        self.dc = DjangoClient()
        self.dc.force_login(self.admin)

        # Each browser flow gets its own Hub/session state.
        game_key = self.FLOW_GAME_BY_TEST[self._testMethodName]
        self.game_data = self._create_all_games_with_questions((game_key,))
        self.session_code = self._create_session()

        # Browser-Kontexte öffnen
        self.admin_ctx = self._browser.new_context()
        self.part_ctx  = self._browser.new_context()
        install_browser_test_stubs(self.admin_ctx)
        install_browser_test_stubs(self.part_ctx)
        self.part_ctx.add_init_script(
            "localStorage.setItem('participant_interface_theme', 'vhs');"
        )
        self.admin_page = self.admin_ctx.new_page()
        self.part_page  = self.part_ctx.new_page()

        self._admin_login()

    def tearDown(self):
        for page in (self.admin_page, self.part_page):
            self._close_page_websockets(page)
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

    def _close_page_websockets(self, page):
        """Complete current Hub/game socket handshakes before the next DB test."""
        try:
            page.evaluate(
                """() => {
                    const sockets = [
                        window.hubMonitorSocket,
                        window.hubLobbySocket,
                        window.adminGameMonitor?.websocket,
                    ].filter((socket, index, all) => (
                        socket instanceof WebSocket && all.indexOf(socket) === index
                    ));
                    return Promise.all(sockets.map((socket) => new Promise((resolve) => {
                        if (socket.readyState === WebSocket.CLOSED) {
                            resolve();
                            return;
                        }
                        const fallback = setTimeout(resolve, 2000);
                        socket.addEventListener('close', () => {
                            clearTimeout(fallback);
                            resolve();
                        }, {once: true});
                        if (socket.readyState < WebSocket.CLOSING) socket.close(1000);
                    })));
                }"""
            )
        except Exception:
            pass

    # ── Hilfsmethoden: Testdaten ─────────────────────────────────────────────

    def _create_all_games_with_questions(self, game_keys=None):
        """Legt für jeden Spieltyp eine Instanz mit einer Testfrage an."""
        data = {}
        selected_game_keys = tuple(game_keys or GAME_CREATE_URLS)
        for game_key in selected_game_keys:
            url_name = GAME_CREATE_URLS[game_key]
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
                    "round_time_limit": "180",
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

    def _safe_reload(self, page):
        """Reloads a page while tolerating Playwright's ERR_ABORTED reload race."""
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception as exc:
            if "ERR_ABORTED" not in str(exc):
                raise
            page.wait_for_load_state("domcontentloaded", timeout=self.TIMEOUT)

    def _goto_admin(self, url):
        """Navigate the admin page, retrying only monitor reload races."""
        try:
            self.admin_page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            if "ERR_ABORTED" not in str(exc):
                raise
            self.admin_page.wait_for_load_state("domcontentloaded", timeout=self.TIMEOUT)
            self.admin_page.goto(url, wait_until="domcontentloaded")

    def _wait_admin_game_ws_open(self):
        """Wait until the current game monitor can send WebSocket actions."""
        self.admin_page.wait_for_function(
            "() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN",
            timeout=self.TIMEOUT,
        )

    def _wait_admin_hub_ws_open(self):
        """Wait until the Hub monitor can send WebSocket actions."""
        self.admin_page.wait_for_function(
            "() => window.hubMonitorSocket?.readyState === WebSocket.OPEN",
            timeout=self.TIMEOUT,
        )

    def _wait_participant_hub_ws_open(self):
        """Wait until the participant lobby can receive Hub WebSocket events."""
        self.part_page.wait_for_function(
            "() => window.hubLobbySocket?.readyState === WebSocket.OPEN",
            timeout=self.TIMEOUT,
        )

    def _wait_participant_game_interactive(self):
        readiness_check = """() => {
            const intro = document.getElementById('sessionGameStartIntro');
            const introHidden = !intro
                || intro.hidden
                || getComputedStyle(intro).display === 'none';
            const shell = document.querySelector('.vhs-theme-shell');
            return introHidden
                && !document.documentElement.classList.contains(
                    'is-session-game-intro-active'
                )
                && !document.documentElement.classList.contains(
                    'qa-vhs-transition--active'
                )
                && (!shell || !shell.hasAttribute('inert'))
                && getComputedStyle(document.body).pointerEvents !== 'none';
        }"""
        try:
            self.part_page.wait_for_function(readiness_check, timeout=self.LONG)
        except Exception as exc:
            diagnostics = self.part_page.evaluate(
                """() => {
                    const intro = document.getElementById('sessionGameStartIntro');
                    const shell = document.querySelector('.vhs-theme-shell');
                    return {
                        url: location.href,
                        introHidden: intro?.hidden ?? null,
                        introDisplay: intro ? getComputedStyle(intro).display : null,
                        introActive: document.documentElement.classList.contains(
                            'is-session-game-intro-active'
                        ),
                        transitionActive: document.documentElement.classList.contains(
                            'qa-vhs-transition--active'
                        ),
                        shellInert: shell?.hasAttribute('inert') ?? null,
                        bodyPointerEvents: getComputedStyle(document.body).pointerEvents,
                    };
                }"""
            )
            self.fail(f"Participant screen did not become interactive: {diagnostics}; {exc}")

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
        self._wait_admin_game_ws_open()
        self.admin_page.wait_for_selector("#startQuizBtn:not([disabled])", timeout=self.TIMEOUT)

        # 2) Klicken (löst location.reload() aus, falls WS offen)
        self.admin_page.click("#startQuizBtn")
        self._wait_for_reload(self.admin_page)

        # 3) Race Condition: Falls noch 'waiting', kurz warten und neu laden
        if not self.admin_page.is_visible("#endQuizBtn"):
            self.admin_page.wait_for_timeout(2000)
            self._safe_reload(self.admin_page)

        self.admin_page.wait_for_selector("#endQuizBtn", timeout=self.TIMEOUT)

    # ── Haupt-Test ───────────────────────────────────────────────────────────

    def _complete_check_in_for_joined_participant(self):
        """Complete the required Hub check-in for the already joined participant."""
        self.admin_page.click('[data-session-panel-target="checkInPanel"]')
        self.admin_page.wait_for_selector("#checkInPanel.is-open", timeout=self.TIMEOUT)
        self.admin_page.wait_for_selector("#startCheckInBtn:not([disabled])", timeout=self.TIMEOUT)
        self.admin_page.click("#startCheckInBtn")

        self.part_page.wait_for_selector("#readyCheckInBtn:not([disabled])", timeout=self.TIMEOUT)
        self.part_page.click("#readyCheckInBtn")

        self.admin_page.wait_for_function(
            "() => document.querySelector('#checkInReadyBadge')?.textContent.includes('Bereit: 1')",
            timeout=self.TIMEOUT,
        )
        self.admin_page.wait_for_selector("#completeCheckInBtn:not([disabled])", timeout=self.TIMEOUT)
        self.admin_page.click("#completeCheckInBtn")
        self.admin_page.wait_for_function(
            "() => document.querySelector('#checkInLockedBadge')?.textContent.includes('Locked: 1')",
            timeout=self.TIMEOUT,
        )

    def _run_participant_flow(self, game_key):
        """
        Vollständiger Teilnehmer-Flow:
        Lobby beitreten → einen Spieltyp durchspielen → Final Leaderboard.
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
        self._goto_admin(
            f"{self.live_server_url}/hub/monitor/{self.session_code}/"
        )
        self.admin_page.wait_for_selector("#startSessionBtn", timeout=self.TIMEOUT)
        self._wait_admin_hub_ws_open()
        self.admin_page.click("#startSessionBtn")
        # Button verschwindet nach dem Start
        self.admin_page.wait_for_selector(
            "#startSessionBtn", state="detached", timeout=self.TIMEOUT
        )

        # ── 4. Spiel durchspielen ───────────────────────────────────────────
        self._complete_check_in_for_joined_participant()

        self._play_game_cycle(game_key, self.game_data[game_key])

        # ── 5. Admin beendet die Session ────────────────────────────────────
        # Sicherstellen, dass Admin auf Hub-Monitor ist
        self.admin_page.wait_for_url(
            f"**/hub/monitor/{self.session_code}/**", timeout=self.TIMEOUT
        )
        self._wait_admin_hub_ws_open()
        self._wait_participant_hub_ws_open()
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

    def test_quiz_participant_flow(self):
        self._run_participant_flow("quiz")

    def test_estimation_participant_flow(self):
        self._run_participant_flow("estimation")

    def test_estimation_question_shell_stays_mounted_across_three_questions(self):
        self._add_question_for_game("estimation")
        self._add_question_for_game("estimation")

        self.part_page.goto(f"{self.live_server_url}/hub/lobby/{self.session_code}/")
        self.part_page.fill("#nickname", self.NICKNAME)
        self.part_page.wait_for_selector("#joinBtn:not([disabled])", timeout=self.TIMEOUT)
        self.part_page.click("#joinBtn")
        self.part_page.wait_for_selector("#joinCard", state="hidden", timeout=self.TIMEOUT)

        self._goto_admin(f"{self.live_server_url}/hub/monitor/{self.session_code}/")
        self._wait_admin_hub_ws_open()
        self.admin_page.click("#startSessionBtn")
        self.admin_page.wait_for_selector("#startSessionBtn", state="detached", timeout=self.TIMEOUT)
        self._complete_check_in_for_joined_participant()

        room_code = self.game_data["estimation"]
        monitor_url = (
            f"{self.live_server_url}"
            f"{reverse(GAME_MONITOR_URL_NAMES['estimation'], args=[room_code])}"
            f"?hub_session={self.session_code}"
        )
        self._goto_admin(monitor_url)
        self._start_game()
        self.part_page.wait_for_url(
            f"**{PARTICIPANT_PLAY_PREFIX['estimation']}{room_code}/**",
            timeout=self.LONG,
        )
        self._wait_participant_game_interactive()

        for question_number in range(1, 4):
            self._wait_admin_game_ws_open()
            self.admin_page.locator(".send-question-btn:not([disabled])").first.click()
            self.admin_page.wait_for_selector(
                "#sendPreparedQuestionBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.wait_for_selector("#questionState:not(.d-none)", timeout=self.TIMEOUT)
            self.part_page.wait_for_function(
                "() => document.querySelector('#questionText')?.textContent.trim() === ''",
                timeout=self.TIMEOUT,
            )
            self.part_page.evaluate(
                """() => {
                    window.__estimationTransitionNodes = {
                        shell: document.querySelector('.vhs-theme-shell'),
                        state: document.querySelector('#questionState'),
                        content: document.querySelector('#estimationQuestionContent'),
                    };
                }"""
            )

            self.admin_page.click("#sendPreparedQuestionBtn")
            self._wait_for_reload(self.admin_page)
            self.admin_page.wait_for_selector(
                "#openAnsweringBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.wait_for_function(
                """() => (
                    document.querySelector('#questionState')?.classList.contains('is-question-presented')
                    && document.querySelector('#questionText')?.textContent.trim().length > 0
                )""",
                timeout=self.TIMEOUT,
            )
            continuity = self.part_page.evaluate(
                """() => ({
                    sameShell: window.__estimationTransitionNodes.shell
                        === document.querySelector('.vhs-theme-shell'),
                    sameState: window.__estimationTransitionNodes.state
                        === document.querySelector('#questionState'),
                    sameContent: window.__estimationTransitionNodes.content
                        === document.querySelector('#estimationQuestionContent'),
                    waitingVisible: !document.querySelector('#waitingQuestionState').classList.contains('d-none'),
                })"""
            )
            self.assertTrue(continuity["sameShell"], question_number)
            self.assertTrue(continuity["sameState"], question_number)
            self.assertTrue(continuity["sameContent"], question_number)
            self.assertFalse(continuity["waitingVisible"], question_number)

            self.admin_page.click("#openAnsweringBtn")
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
            self._wait_admin_game_ws_open()
            self.admin_page.click("#endQuestionBtn")
            self._wait_for_reload(self.admin_page)
            self.part_page.wait_for_selector(
                "#correctAnswerState:not(.d-none)", timeout=self.TIMEOUT
            )

    def test_assign_participant_flow(self):
        self._run_participant_flow("assign")

    def test_where_participant_flow(self):
        self._run_participant_flow("where")

    def test_who_participant_flow(self):
        self._run_participant_flow("who")

    def test_who_that_participant_flow(self):
        self._run_participant_flow("who_that")

    def test_blackjack_participant_flow(self):
        self._run_participant_flow("blackjack")

    def test_clue_rush_participant_flow(self):
        self._run_participant_flow("clue_rush")

    def test_sorting_ladder_participant_flow(self):
        self._run_participant_flow("sorting_ladder")

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
        self._goto_admin(monitor_url)
        self.admin_page.wait_for_selector("#startQuizBtn", timeout=self.TIMEOUT)

        # ── b. Admin: Spiel starten ──────────────────────────────────────────
        self._start_game()
        if game_key == "assign":
            self._safe_reload(self.admin_page)
            self.admin_page.wait_for_selector("#endQuizBtn", timeout=self.TIMEOUT)

        # ── c. Admin: Frage senden ──────────────────────────────────────────
        self._wait_admin_game_ws_open()
        self.admin_page.wait_for_selector(
            ".send-question-btn:not([disabled])", timeout=self.TIMEOUT
        )
        send_button = self.admin_page.locator(".send-question-btn").first
        if game_key in {"quiz", "estimation"}:
            send_button.click()
            self.admin_page.wait_for_selector(
                "#sendPreparedQuestionBtn:not([disabled])", timeout=self.TIMEOUT
            )
            if game_key == "estimation":
                self.part_page.wait_for_selector(
                    "#questionState:not(.d-none)", timeout=self.TIMEOUT
                )
                self.assertEqual(
                    self.part_page.locator("#questionText").inner_text().strip(),
                    "",
                )
                self.part_page.wait_for_selector(
                    "#questionState .vhs-question-kicker", timeout=self.TIMEOUT
                )
                self.assertTrue(
                    self.part_page.locator(
                        "#questionState .vhs-question-kicker"
                    ).inner_text().strip()
                )
                self.assertTrue(
                    self.part_page.locator("#estimationQuestionContent").evaluate(
                        "element => element.classList.contains('is-question-content-pending')"
                    )
                )
                self.part_page.evaluate(
                    """() => {
                        const shell = document.querySelector('.vhs-theme-shell');
                        const questionState = document.querySelector('#questionState');
                        const questionContent = document.querySelector('#questionState .question-content');
                        const estimationContent = document.querySelector('#estimationQuestionContent');
                        window.__estimationPreparedNodes = {
                            shell,
                            questionState,
                            questionContent,
                            estimationContent,
                        };
                    }"""
                )
                self._estimation_transition_viewports = (
                    {"width": 1920, "height": 1080},
                    {"width": 1366, "height": 768},
                    {"width": 1024, "height": 650},
                )
                self._estimation_prepared_layouts = {}
                for viewport in self._estimation_transition_viewports:
                    self.part_page.set_viewport_size(viewport)
                    self.part_page.wait_for_timeout(50)
                    key = f'{viewport["width"]}x{viewport["height"]}'
                    self._estimation_prepared_layouts[key] = self.part_page.evaluate(
                        """() => {
                            const rect = element => {
                                const box = element.getBoundingClientRect();
                                return {x: box.x, y: box.y, width: box.width, height: box.height};
                            };
                            const shell = document.querySelector('.vhs-theme-shell');
                            return {
                                shell: rect(shell),
                                kicker: rect(document.querySelector('#questionState .vhs-question-kicker')),
                                background: getComputedStyle(shell).backgroundImage,
                            };
                        }"""
                    )
            self.admin_page.click("#sendPreparedQuestionBtn")
        else:
            send_button.click()
            send_button.click()
        # Spielmonitor lädt nach question_started neu
        self._wait_for_reload(self.admin_page)
        # Nach Reload: Frage aktiv – Assign: Runden-Button oder endQuestionBtn (je nach Rundenanzahl)
        if game_key == "assign":
            self._wait_admin_game_ws_open()
            try:
                self.admin_page.wait_for_selector(
                    "#revealAssignContentBtn:not([disabled])", timeout=self.TIMEOUT
                )
            except Exception:
                diagnostics = self.admin_page.evaluate(
                    """() => {
                        const monitor = window.adminGameMonitor;
                        const button = document.querySelector('#revealAssignContentBtn');
                        return {
                            phase: monitor?.questionPhase,
                            visibleAt: monitor?.questionVisibleAt,
                            serverNow: monitor?.presentationServerNow,
                            estimatedServerNow: monitor?.estimatedServerNow?.(),
                            buttonDisabled: button?.disabled,
                            buttonAriaDisabled: button?.getAttribute('aria-disabled'),
                            buttonVisible: !!button && getComputedStyle(button).display !== 'none',
                        };
                    }"""
                )
                self.fail(f"Assign reveal action did not become ready: {diagnostics}")
            self.admin_page.click("#revealAssignContentBtn")
            self._wait_for_reload(self.admin_page)
            self.admin_page.wait_for_selector(
                "#endRoundEarlyBtn, #nextRoundBtn, #endQuestionBtn", timeout=self.TIMEOUT
            )
        elif game_key == "sorting_ladder":
            self._open_sorting_ladder_round()
        elif game_key == "quiz":
            self._wait_admin_game_ws_open()
            self.admin_page.wait_for_selector(
                "#revealQuestionContentBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.admin_page.click("#revealQuestionContentBtn")
            self.assertEqual(self.admin_page.locator("#openAnsweringBtn").count(), 0)
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
            self.admin_page.wait_for_selector(
                "#questionTimerWrapper:not(.d-none)", timeout=self.TIMEOUT
            )
        elif game_key == "estimation":
            self._wait_admin_game_ws_open()
            self.admin_page.wait_for_selector(
                "#openAnsweringBtn:not([disabled])", timeout=self.TIMEOUT
            )
            for viewport in self._estimation_transition_viewports:
                self.part_page.set_viewport_size(viewport)
                self.part_page.wait_for_timeout(50)
                continuity = self.part_page.evaluate(
                    """() => {
                        const nodes = window.__estimationPreparedNodes;
                        const shell = document.querySelector('.vhs-theme-shell');
                        const questionState = document.querySelector('#questionState');
                        const questionContent = document.querySelector('#questionState .question-content');
                        const estimationContent = document.querySelector('#estimationQuestionContent');
                        const rect = element => {
                            const box = element.getBoundingClientRect();
                            return {x: box.x, y: box.y, width: box.width, height: box.height};
                        };
                        return {
                            sameShell: nodes.shell === shell,
                            sameQuestionState: nodes.questionState === questionState,
                            sameQuestionContent: nodes.questionContent === questionContent,
                            sameEstimationContent: nodes.estimationContent === estimationContent,
                            shell: rect(shell),
                            kicker: rect(document.querySelector('#questionState .vhs-question-kicker')),
                            background: getComputedStyle(shell).backgroundImage,
                            contentPending: estimationContent.classList.contains('is-question-content-pending'),
                            waitingVisible: !document.querySelector('#waitingQuestionState').classList.contains('d-none'),
                        };
                    }"""
                )
                key = f'{viewport["width"]}x{viewport["height"]}'
                prepared = self._estimation_prepared_layouts[key]
                self.assertTrue(continuity["sameShell"], key)
                self.assertTrue(continuity["sameQuestionState"], key)
                self.assertTrue(continuity["sameQuestionContent"], key)
                self.assertTrue(continuity["sameEstimationContent"], key)
                self.assertAlmostEqual(
                    prepared["shell"]["width"], continuity["shell"]["width"], delta=1, msg=key
                )
                self.assertAlmostEqual(
                    prepared["shell"]["height"], continuity["shell"]["height"], delta=1, msg=key
                )
                self.assertEqual(prepared["background"], continuity["background"], key)
                self.assertFalse(continuity["contentPending"], key)
                self.assertFalse(continuity["waitingVisible"], key)
                self.assertLess(continuity["kicker"]["y"], prepared["kicker"]["y"], key)
            self.admin_page.click("#openAnsweringBtn")
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
        elif game_key == "who_that":
            self._wait_admin_game_ws_open()
            self.assertEqual(
                self.admin_page.locator("#questionEndedStatus").count(),
                0,
            )
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
            self.assertEqual(
                self.admin_page.locator("#openAnsweringBtn").count(),
                0,
            )
            self.assertEqual(
                self.admin_page.locator("#questionEndedStatus").count(),
                0,
            )
        elif game_key == "who":
            self._wait_admin_game_ws_open()
            self.admin_page.wait_for_selector(
                "#startSetBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.admin_page.click("#startSetBtn")
            self._wait_for_reload(self.admin_page)
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
        elif game_key == "blackjack":
            self._wait_admin_game_ws_open()
            self.admin_page.wait_for_selector(
                "#openAnsweringBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.admin_page.click("#openAnsweringBtn")
            self._wait_for_reload(self.admin_page)
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
        elif game_key == "clue_rush":
            self._wait_admin_game_ws_open()
            self.admin_page.wait_for_selector(
                "#startCluesBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.admin_page.click("#startCluesBtn")
            self._wait_for_reload(self.admin_page)
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
        else:
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)

        # ── d. Teilnehmer: Wird zur Spielseite navigiert ───────────────────
        play_pattern = f"**{PARTICIPANT_PLAY_PREFIX[game_key]}{room_code}/**"
        self.part_page.wait_for_url(play_pattern, timeout=self.LONG)
        self._wait_participant_game_interactive()

        # ── e. Teilnehmer: Frage beantworten ───────────────────────────────
        self._submit_participant_answer(game_key)

        # ── f. Admin: Frage/Runde beenden ───────────────────────────────────
        if game_key == "assign":
            # Runden-Button oder endQuestionBtn (bei 1 Runde)
            self._wait_admin_game_ws_open()
            self.admin_page.locator(
                "#endRoundEarlyBtn, #nextRoundBtn, #endQuestionBtn"
            ).first.click()
            self._wait_for_reload(self.admin_page)
        elif game_key == "sorting_ladder":
            total_rounds = int(
                self.admin_page.locator("#activeQuestion").get_attribute("data-total-rounds")
                or "1"
            )
            for expected_round in range(2, total_rounds + 1):
                self._wait_admin_game_ws_open()
                self.admin_page.wait_for_selector(
                    "#startNextRoundBtn:not([disabled]):not(.d-none)",
                    timeout=self.TIMEOUT,
                )
                self.admin_page.click("#startNextRoundBtn")
                self._wait_for_reload(self.admin_page)
                self.part_page.wait_for_function(
                    "(roundNumber) => window.sortingPlayer?.currentRound === roundNumber",
                    arg=expected_round,
                    timeout=self.TIMEOUT,
                )
                self._open_sorting_ladder_round()
                self._submit_participant_answer(game_key)

            self._wait_admin_game_ws_open()
            self.admin_page.wait_for_selector(
                "#endQuestionBtn:not(.d-none)", timeout=self.TIMEOUT
            )
            self.admin_page.click("#endQuestionBtn")
            self.admin_page.wait_for_selector("#showSolutionBtn", timeout=self.TIMEOUT)
            self.admin_page.click("#showSolutionBtn")
            self.admin_page.wait_for_selector("#endQuestionBtn", timeout=self.TIMEOUT)
            self.admin_page.click("#endQuestionBtn")
            self._wait_for_reload(self.admin_page)
        else:
            self._wait_admin_game_ws_open()
            self.admin_page.click("#endQuestionBtn")
            self._wait_for_reload(self.admin_page)
            if game_key == "who_that":
                self.admin_page.wait_for_selector(
                    "#questionEndedStatus", timeout=self.TIMEOUT
                )

        # ── g. Admin: Spiel beenden ─────────────────────────────────────────
        self.admin_page.wait_for_selector("#endQuizBtn", timeout=self.TIMEOUT)
        self._wait_admin_game_ws_open()
        self.admin_page.once("dialog", lambda d: d.accept())
        self.admin_page.click("#endQuizBtn")
        self.part_page.wait_for_selector("#returnToLobbyBtn", timeout=self.LONG)

        # ── h. Admin: Zurück zur Übersicht ──────────────────────────────────
        self._goto_admin(f"{self.live_server_url}/hub/monitor/{self.session_code}/")
        self.admin_page.wait_for_url(
            f"**/hub/monitor/{self.session_code}/**", timeout=self.TIMEOUT
        )

        # ── i. Teilnehmer kehrt kontrolliert per Button zur Lobby zurück ──
        self.part_page.wait_for_selector("#returnToLobbyBtn", timeout=self.LONG)
        self.part_page.click("#returnToLobbyBtn")
        self.part_page.wait_for_url(
            f"**/hub/lobby/{self.session_code}/**", timeout=self.LONG
        )
        # The VHS shell replaces the legacy heading while keeping the lobby root.
        self.part_page.wait_for_selector(
            "[data-participant-lobby].vhs-lobby-root", timeout=self.TIMEOUT
        )
        self._wait_participant_hub_ws_open()

    # ── Antwort-Logik pro Spieltyp ───────────────────────────────────────────

    def _open_sorting_ladder_round(self):
        self._wait_admin_game_ws_open()
        self.admin_page.wait_for_selector(
            "#revealSortingContentBtn:not([disabled])", timeout=self.TIMEOUT
        )
        self.admin_page.click("#revealSortingContentBtn")
        self._wait_for_reload(self.admin_page)
        self._wait_admin_game_ws_open()
        self.admin_page.wait_for_selector(
            "#openSortingRoundBtn:not([disabled])", timeout=self.TIMEOUT
        )
        self.admin_page.click("#openSortingRoundBtn")
        self._wait_for_reload(self.admin_page)
        self.admin_page.wait_for_selector(
            "#startNextRoundBtn:not(.d-none), #endQuestionBtn:not(.d-none)",
            timeout=self.TIMEOUT,
        )

    def _submit_participant_answer(self, game_key):
        """
        Lässt den Teilnehmer eine Antwort eingeben und abschicken.
        Bei Spielen mit komplexen Drag-Drop- oder Karten-Interfaces wird
        der Submit-Button per JavaScript freigeschaltet.
        """
        # Warte darauf, dass der Submit-Button überhaupt im DOM ist
        if game_key not in ("assign", "who", "sorting_ladder"):
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
            target.scroll_into_view_if_needed(timeout=self.TIMEOUT)
            target.evaluate(
                """(element) => new Promise((resolve) => {
                    const animations = element.closest('.game-state')?.getAnimations() || [];
                    Promise.allSettled(animations.map((animation) => animation.finished)).then(resolve);
                })"""
            )
            self.part_page.wait_for_function(
                """(element) => {
                    const rect = element.getBoundingClientRect();
                    const hit = document.elementFromPoint(
                        rect.left + rect.width / 2,
                        rect.top + rect.height / 2
                    );
                    return hit === element || element.contains(hit);
                }""",
                arg=target.element_handle(),
                timeout=self.TIMEOUT,
            )
            source.drag_to(target)
            self.part_page.wait_for_selector(
                "#logRoundBtn:not(.d-none)", timeout=self.TIMEOUT
            )
            self.part_page.click("#logRoundBtn")

        elif game_key == "where":
            # Use a real map click so the participant answer state is populated.
            self.part_page.wait_for_selector("#gameMap", timeout=self.LONG)
            self.part_page.click("#gameMap")
            self.part_page.wait_for_selector(
                "#submitAnswerBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitAnswerBtn")

        elif game_key == "who":
            self.part_page.wait_for_selector(
                "#accuseLiarBtn:not([disabled])", timeout=self.LONG
            )
            self.part_page.click("#accuseLiarBtn")

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
            # Use the current ladder UI: drag one item into a slot, then lock it in.
            active_card = "#topicLayout:not(.round-locked) .answer-card[draggable='true']"
            try:
                self.part_page.wait_for_selector(active_card, timeout=self.LONG)
            except Exception as exc:
                diagnostics = self.part_page.evaluate(
                    """() => ({
                        phase: window.sortingPlayer?.setPhase,
                        round: window.sortingPlayer?.currentRound,
                        locked: window.sortingPlayer?.isInputLocked,
                        eliminated: window.sortingPlayer?.isEliminated,
                        hasSubmitted: window.sortingPlayer?.hasSubmittedMove,
                        moreRounds: window.sortingPlayer?.moreRoundsInQuestion,
                        timeLeft: window.sortingPlayer?.roundTimeLeft,
                        timerEnded: window.sortingPlayer?.roundTimerEnded,
                        layoutClass: document.querySelector('#topicLayout')?.className,
                        visibleState: Array.from(document.querySelectorAll('.game-state'))
                            .filter((element) => !element.classList.contains('d-none'))
                            .map((element) => element.id),
                    })"""
                )
                self.fail(f'Sorting Ladder did not unlock its active round: {diagnostics}')
            source = self.part_page.locator(active_card).first
            test_ranks = {"Elefant": 1, "Hund": 2, "Maus": 3}
            source_text = source.locator(".option-text").inner_text().strip()
            ladder_texts = [
                text.strip()
                for text in self.part_page.locator(".ladder-row-text").all_inner_texts()
            ]
            source_rank = test_ranks[source_text]
            target_position = sum(
                test_ranks[text] < source_rank for text in ladder_texts
            )
            target = self.part_page.locator(
                f".triangle-container[data-position='{target_position}']"
            )
            source.drag_to(target)
            self.part_page.wait_for_selector(
                "#submitRoundBtn:not([disabled])", timeout=self.TIMEOUT
            )
            self.part_page.click("#submitRoundBtn")
            self.admin_page.wait_for_function(
                """participantName => Array.from(
                    document.querySelectorAll('#roundAnswerStatusList > div')
                ).some(row => (
                    row.querySelector('strong')?.textContent.trim() === participantName
                    && row.querySelector('.badge-success')?.textContent.trim() === 'eingeloggt'
                ))""",
                arg=self.NICKNAME,
                timeout=self.TIMEOUT,
            )

        # Warte auf Bestätigungsanzeige (where/sorting_ladder ohne Selektor)
        if game_key == "estimation":
            self.part_page.wait_for_selector(
                "#answerSubmittedState:not(.d-none), #zoneSubmitFeedback:not(.d-none)",
                timeout=self.TIMEOUT,
            )
        elif game_key == "assign":
            self.part_page.wait_for_selector(
                "#roundSubmittedMessage:not(.d-none), #answerSubmittedState:not(.d-none)",
                timeout=self.TIMEOUT,
            )
        elif game_key == "blackjack":
            self.part_page.wait_for_selector(
                "#questionSubmitFeedback:not(.d-none)", timeout=self.TIMEOUT
            )
        elif game_key == "clue_rush":
            self.part_page.wait_for_selector(
                "#questionState:not(.d-none) #clueRushSubmittedAnswer:not(.d-none)",
                timeout=self.TIMEOUT,
            )
        elif game_key not in ("where", "who", "sorting_ladder"):
            self.part_page.wait_for_selector(
                "#answerSubmittedState:not(.d-none)", timeout=self.TIMEOUT
            )
