#!/usr/bin/env python
"""
seed_test_data.py
=================
Befüllt die lokale Datenbank mit Testdaten für manuelle Entwicklungs- und Testzwecke.

Aufruf (mit aktiviertem venv):
    python seed_test_data.py           # Daten anlegen
    python seed_test_data.py --clear   # Vorherige Seed-Daten löschen, dann neu anlegen

Was wird angelegt:
  - 1 Admin-User (falls noch nicht vorhanden)
  - Je 2 Spielinstanzen pro Spieltyp (9 Typen × 2 = 18 Spiele)
  - 1 Hub-Session mit je einem Spiel pro Typ (9 Schritte)
"""

import os
import sys
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "games_website.settings")
sys.path.insert(0, os.path.dirname(__file__))
django.setup()

import json
import random
import string
from django.contrib.auth.models import User

# ---------------------------------------------------------------------------
# Modell-Imports
# ---------------------------------------------------------------------------
from QuizGame.models import Quiz, QuizSession
from Estimation.models import EstimationQuiz, EstimationSession
from Assign.models import AssignQuiz, AssignSession
from where_is_this.models import WhereQuiz, WhereSession
from who_is_lying.models import WhoQuiz, WhoSession
from who_is_that.models import WhoThatQuiz, WhoThatSession
from black_jack_quiz.models import BlackJackQuiz, BlackJackSession
from clue_rush.models import ClueRushGame, ClueRushSession
from sorting_ladder.models import SortingLadderGame, SortingLadderSession
from games_hub.models import HubSession, HubGameStep

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
ADMIN_EMAIL    = "admin@example.com"

SEED_TAG = "[seed]"  # Prefix für alle generierten Titel

GAME_CONFIGS = [
    {
        "key":        "quiz",
        "label":      "Quick Quiz",
        "model":      Quiz,
        "session_model": QuizSession,
        "session_fk": "quiz",
    },
    {
        "key":        "estimation",
        "label":      "Estimation",
        "model":      EstimationQuiz,
        "session_model": EstimationSession,
        "session_fk": "quiz",
    },
    {
        "key":        "assign",
        "label":      "Assign",
        "model":      AssignQuiz,
        "session_model": AssignSession,
        "session_fk": "quiz",
    },
    {
        "key":        "where",
        "label":      "Where Is This?",
        "model":      WhereQuiz,
        "session_model": WhereSession,
        "session_fk": "quiz",
    },
    {
        "key":        "who",
        "label":      "Who Is Lying?",
        "model":      WhoQuiz,
        "session_model": WhoSession,
        "session_fk": "quiz",
    },
    {
        "key":        "who_that",
        "label":      "Who Is That?",
        "model":      WhoThatQuiz,
        "session_model": WhoThatSession,
        "session_fk": "quiz",
    },
    {
        "key":        "blackjack",
        "label":      "Black Jack Quiz",
        "model":      BlackJackQuiz,
        "session_model": BlackJackSession,
        "session_fk": "quiz",
    },
    {
        "key":        "clue_rush",
        "label":      "Clue Rush",
        "model":      ClueRushGame,
        "session_model": ClueRushSession,
        "session_fk": "quiz",
    },
    {
        "key":        "sorting_ladder",
        "label":      "Sorting Ladder",
        "model":      SortingLadderGame,
        "session_model": SortingLadderSession,
        "session_fk": "quiz",
    },
]


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def rand_suffix(n=4):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def ensure_admin():
    user, created = User.objects.get_or_create(
        username=ADMIN_USERNAME,
        defaults={
            "email":        ADMIN_EMAIL,
            "is_staff":     True,
            "is_superuser": True,
        },
    )
    if created:
        user.set_password(ADMIN_PASSWORD)
        user.save()
        print(f"  ✓ Admin-User erstellt: {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    else:
        print(f"  · Admin-User bereits vorhanden: {ADMIN_USERNAME}")
    return user


def create_game_instance(cfg, user, index):
    """Legt eine Spielinstanz an und gibt (instance, room_code) zurück."""
    title = f"{SEED_TAG} {cfg['label']} #{index} {rand_suffix()}"
    game = cfg["model"].objects.create(
        title=title,
        creator=user,
        status="waiting",
    )
    # Session-Objekt anlegen (falls noch nicht via Signal erstellt)
    if not hasattr(game, "session"):
        cfg["session_model"].objects.get_or_create(**{cfg["session_fk"]: game})
    print(f"    + {title}  (room_code: {game.room_code})")
    return game


def clear_seed_data():
    """Löscht alle Objekte, deren Titel mit SEED_TAG beginnt."""
    print("\n── Lösche vorherige Seed-Daten ──")
    total = 0
    for cfg in GAME_CONFIGS:
        qs = cfg["model"].objects.filter(title__startswith=SEED_TAG)
        count = qs.count()
        qs.delete()
        if count:
            print(f"  - {count}× {cfg['label']} gelöscht")
        total += count
    # Hub-Sessions
    qs = HubSession.objects.filter(name__startswith=SEED_TAG)
    count = qs.count()
    qs.delete()
    if count:
        print(f"  - {count}× HubSession gelöscht")
    total += count
    print(f"  Gesamt gelöscht: {total} Objekte")


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

def seed():
    print("\n── Admin-User ──")
    user = ensure_admin()

    print("\n── Spielinstanzen (je 2 pro Typ) ──")
    first_games = {}  # game_key → erste Instanz (für Hub-Session)
    for cfg in GAME_CONFIGS:
        print(f"  {cfg['label']}:")
        for i in range(1, 3):
            game = create_game_instance(cfg, user, i)
            if i == 1:
                first_games[cfg["key"]] = game

    print("\n── Hub-Session (1 Spiel pro Typ) ──")
    session_name = f"{SEED_TAG} Test-Session {rand_suffix()}"
    session_code = "SEED01"
    # Bestehenden Code bereinigen
    HubSession.objects.filter(code=session_code).delete()
    session = HubSession.objects.create(name=session_name, code=session_code)
    print(f"  Session: {session_name}  (code: {session_code})")
    for order, cfg in enumerate(GAME_CONFIGS):
        game = first_games[cfg["key"]]
        HubGameStep.objects.create(
            session=session,
            order=order,
            game_key=cfg["key"],
            room_code=game.room_code,
            title=cfg["label"],
        )
        print(f"    Schritt {order}: {cfg['label']}  ({game.room_code})")

    print(f"""
── Fertig ──
Admin-Login:   http://localhost:8000/admin-dashboard/login/
               Benutzer: {ADMIN_USERNAME}  Passwort: {ADMIN_PASSWORD}
Lobby:         http://localhost:8000/hub/lobby/{session_code}/
Monitor:       http://localhost:8000/hub/monitor/{session_code}/
""")


if __name__ == "__main__":
    if "--clear" in sys.argv:
        clear_seed_data()
    seed()
