"""
Local QA/dev seed command for manual smoke-test game evenings.

Usage:
    python manage.py seed_qa_evening
    python manage.py seed_qa_evening --participants 8 --mode ranking --weighting on

The command only creates new objects. It does not delete or overwrite existing
sessions, games, questions, participants, or scores.
"""

from __future__ import annotations

import random
import string
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from black_jack_quiz.models import BlackJackQuestion, BlackJackQuiz, BlackJackSession
from clue_rush.models import Clue, ClueQuestion, ClueRushGame, ClueRushSession
from games_hub.check_in import (
    complete_session_check_in,
    participant_check_in,
    start_session_check_in,
)
from games_hub.models import HubGameStep, HubParticipant, HubSession
from QuizGame.models import Quiz, QuizQuestion, QuizSession
from wer_weiss_mehr.models import (
    WerWeissMehrAnswerOption,
    WerWeissMehrGame,
    WerWeissMehrQuestion,
    WerWeissMehrSession,
)


DEFAULT_NAMES = [
    "Anna",
    "Ben",
    "Carla",
    "David",
    "Eva",
    "Felix",
    "Greta",
    "Hannah",
    "Ibrahim",
    "Jonas",
    "Kira",
    "Lukas",
]

QA_USERNAME = "qa_seed_host"
QA_PASSWORD = "qa-seed-pass"


class Command(BaseCommand):
    help = (
        "Create a local QA smoke-test Hub session with participants, check-in "
        "state, planned games, and reusable test questions."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--participants",
            type=int,
            default=6,
            help="Number of prepared Hub participants to create. Default: 6.",
        )
        parser.add_argument(
            "--mode",
            choices=["ranking", "simple"],
            default="ranking",
            help="Overall scoring mode for the Hub session. Default: ranking.",
        )
        parser.add_argument(
            "--weighting",
            choices=["on", "off"],
            default="on",
            help="Enable linear capped overall weighting. Default: on.",
        )
        parser.add_argument(
            "--check-in",
            dest="check_in",
            choices=["completed", "open", "not-started"],
            default="completed",
            help=(
                "Initial check-in state. completed checks in all prepared "
                "participants, open starts check-in, not-started leaves it idle. "
                "Default: completed."
            ),
        )
        parser.add_argument(
            "--with-late-join",
            action="store_true",
            help="Create an extra participant after completed check-in for late-join QA.",
        )
        parser.add_argument(
            "--with-leaver",
            action="store_true",
            help="Mark the last prepared participant as permanently left.",
        )
        parser.add_argument(
            "--base-url",
            default="http://127.0.0.1:8000",
            help="Base URL used in printed links. Default: http://127.0.0.1:8000.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        participant_count = max(1, int(options["participants"]))
        if participant_count > 50:
            raise CommandError("--participants must be 50 or less for this QA seed command.")

        creator, user_created = self._get_or_create_qa_user()
        session = self._create_hub_session(
            mode=options["mode"],
            weighting=options["weighting"],
        )
        participants = self._create_hub_participants(session, participant_count)

        games = [
            self._create_quick_quiz(creator, session.code),
            self._create_clue_rush(creator, session.code),
            self._create_wer_weiss_mehr(creator, session.code),
            self._create_blackjack(creator, session.code),
        ]
        self._create_hub_steps(session, games)

        check_in_result = self._prepare_check_in(session, participants, options["check_in"])
        late_participant = None
        if options["with_late_join"]:
            late_participant = self._create_late_join_participant(session)

        leaver = None
        if options["with_leaver"]:
            leaver = participants[-1]
            leaver.is_active = False
            leaver.left_permanently_at = timezone.now()
            leaver.save(update_fields=["is_active", "left_permanently_at", "updated_at"])

        session.refresh_from_db()
        self._print_summary(
            session=session,
            participants=participants,
            games=games,
            base_url=options["base_url"],
            creator=creator,
            user_created=user_created,
            check_in_result=check_in_result,
            late_participant=late_participant,
            leaver=leaver,
        )

    def _get_or_create_qa_user(self):
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=QA_USERNAME,
            defaults={
                "email": "qa-seed-host@example.invalid",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            user.set_password(QA_PASSWORD)
            user.save(update_fields=["password"])
        return user, created

    def _create_hub_session(self, mode, weighting):
        code = self._generate_session_code()
        return HubSession.objects.create(
            code=code,
            name=f"QA Smoke Evening {code}",
            is_active=True,
            overall_scoring_mode=(
                HubSession.OVERALL_SCORING_RANKING
                if mode == "ranking"
                else HubSession.OVERALL_SCORING_SIMPLE
            ),
            overall_weighting_mode=(
                HubSession.OVERALL_WEIGHTING_LINEAR_CAP
                if weighting == "on"
                else HubSession.OVERALL_WEIGHTING_NONE
            ),
            weighting_step=0.15,
            weighting_cap=2.0,
        )

    def _create_hub_participants(self, session, participant_count):
        names = self._participant_names(participant_count)
        now = timezone.now()
        return [
            HubParticipant.objects.create(
                session=session,
                nickname=name,
                is_active=True,
                last_seen=now,
            )
            for name in names
        ]

    def _participant_names(self, count):
        if count <= len(DEFAULT_NAMES):
            return DEFAULT_NAMES[:count]
        extra = [f"QAPlayer{index}" for index in range(len(DEFAULT_NAMES) + 1, count + 1)]
        return DEFAULT_NAMES + extra

    def _create_quick_quiz(self, creator, session_code):
        quiz = Quiz.objects.create(
            title=f"QA Quick Quiz {session_code}",
            creator=creator,
            max_participants=50,
        )
        questions = [
            QuizQuestion.objects.create(
                question_text="Welche HTTP-Methode wird typischerweise zum Erstellen genutzt?",
                question_type="multiple_choice",
                option_a="GET",
                option_b="POST",
                option_c="HEAD",
                option_d="TRACE",
                correct_answer="B",
                points=1,
                time_limit=25,
                explanation="POST ist die uebliche Methode fuer Create-Aktionen.",
                created_by=creator,
            ),
            QuizQuestion.objects.create(
                question_text="Django ist ein Python-Webframework.",
                question_type="true_false",
                correct_answer="True",
                points=1,
                time_limit=20,
                explanation="Django basiert auf Python.",
                created_by=creator,
            ),
            QuizQuestion.objects.create(
                question_text="Welchen Song hoerst du und von wem ist er gesungen?",
                question_type="double_answer",
                correct_answer="Wonderwall",
                correct_answer_2="Oasis",
                double_answer_label_1="Titel",
                double_answer_label_2="Kuenstler",
                points=1,
                time_limit=35,
                explanation="Mehrfeld-Freitext fuer manuelle Feldkorrektur.",
                created_by=creator,
            ),
        ]
        quiz.selected_questions.set(questions)
        quiz.question_order = [question.id for question in questions]
        quiz.save(update_fields=["question_order", "updated_at"])
        QuizSession.objects.get_or_create(quiz=quiz)
        return {"key": "quiz", "game": quiz}

    def _create_clue_rush(self, creator, session_code):
        game = ClueRushGame.objects.create(
            title=f"QA Clue Rush {session_code}",
            creator=creator,
            max_participants=50,
        )
        question_one = ClueQuestion.objects.create(
            question_text="Gesuchter Begriff: Programmiersprache",
            answer="Python",
            points=5,
            time_limit=45,
            created_by=creator,
        )
        for order, text in enumerate(
            [
                "Wird oft fuer Django genutzt",
                "Hat ein Schlangenlogo",
                "Dateien enden haeufig auf .py",
                "Guido van Rossum hat sie erfunden",
                "Name ist auch eine Schlange",
            ],
            start=1,
        ):
            Clue.objects.create(clue_question=question_one, order=order, clue_text=text, duration=8)

        question_two = ClueQuestion.objects.create(
            question_text="Gesuchte Stadt: Hauptstadt Deutschlands",
            answer="Berlin",
            points=5,
            time_limit=45,
            created_by=creator,
        )
        for order, text in enumerate(
            [
                "Liegt an der Spree",
                "Hat ein Brandenburger Tor",
                "War lange geteilt",
                "Hat den Fernsehturm am Alexanderplatz",
            ],
            start=1,
        ):
            Clue.objects.create(clue_question=question_two, order=order, clue_text=text, duration=8)

        questions = [question_one, question_two]
        game.selected_questions.set(questions)
        game.question_order = [question.id for question in questions]
        game.save(update_fields=["question_order", "updated_at"])
        ClueRushSession.objects.get_or_create(quiz=game)
        return {"key": "clue_rush", "game": game}

    def _create_wer_weiss_mehr(self, creator, session_code):
        game = WerWeissMehrGame.objects.create(
            title=f"QA Wer weiss mehr {session_code}",
            creator=creator,
            max_participants=50,
        )
        question = WerWeissMehrQuestion.objects.create(
            question_text="Nenne deutsche Bundeslaender.",
            round_time_limit=35,
            created_by=creator,
        )
        answer_data = [
            ("Baden-Wuerttemberg", ["Baden Wuerttemberg", "BW"]),
            ("Bayern", []),
            ("Berlin", []),
            ("Hamburg", []),
            ("Hessen", []),
            ("Nordrhein-Westfalen", ["Nordrhein Westfalen", "NRW"]),
            ("Sachsen", []),
            ("Thueringen", ["Thuringen", "Thueringen"]),
        ]
        for canonical_text, aliases in answer_data:
            WerWeissMehrAnswerOption.objects.create(
                question=question,
                canonical_text=canonical_text,
                aliases=aliases,
            )
        question.recalculate_answer_sort_order()
        game.selected_questions.set([question])
        game.question_order = [question.id]
        game.save(update_fields=["question_order", "updated_at"])
        WerWeissMehrSession.objects.get_or_create(quiz=game)
        return {"key": "wer_weiss_mehr", "game": game}

    def _create_blackjack(self, creator, session_code):
        quiz = BlackJackQuiz.objects.create(
            title=f"QA Black Jack {session_code}",
            creator=creator,
            max_participants=50,
            total_questions=4,
            scoring_mode="simple",
        )
        questions = [
            BlackJackQuestion.objects.create(
                question_text="Wie viele Bundeslaender hat Deutschland?",
                correct_answer=16,
                time_limit=30,
                explanation="Deutschland hat 16 Bundeslaender.",
                created_by=creator,
            ),
            BlackJackQuestion.objects.create(
                question_text="Wie viele Spieler stehen beim Fussball pro Team auf dem Feld?",
                correct_answer=11,
                time_limit=30,
                explanation="Ein Team spielt mit 11 Spielern.",
                created_by=creator,
            ),
            BlackJackQuestion.objects.create(
                question_text="Wie viele Minuten dauert eine regulaere Halbzeit im Fussball?",
                correct_answer=45,
                time_limit=30,
                explanation="Eine Halbzeit dauert 45 Minuten.",
                created_by=creator,
            ),
            BlackJackQuestion.objects.create(
                question_text="Wie viele Karten hat ein Standard-Kartenspiel ohne Joker?",
                correct_answer=52,
                time_limit=30,
                explanation="Ein Standarddeck hat 52 Karten.",
                created_by=creator,
            ),
        ]
        quiz.selected_questions.set(questions)
        quiz.question_order = [[question.id for question in questions]]
        quiz.save(update_fields=["question_order", "updated_at"])
        BlackJackSession.objects.get_or_create(quiz=quiz)
        return {"key": "blackjack", "game": quiz}

    def _create_hub_steps(self, session, games):
        for order, item in enumerate(games):
            game = item["game"]
            HubGameStep.objects.create(
                session=session,
                order=order,
                game_key=item["key"],
                room_code=game.room_code,
                title=game.title,
            )

    def _prepare_check_in(self, session, participants, check_in_mode):
        if check_in_mode == "not-started":
            return {"success": True, "mode": "not-started"}

        result = start_session_check_in(session)
        if not result.get("success"):
            return result

        if check_in_mode == "open":
            return {"success": True, "mode": "open"}

        for participant in participants:
            result = participant_check_in(session, participant.nickname)
            if not result.get("success"):
                return result
        result = complete_session_check_in(session)
        result["mode"] = "completed"
        return result

    def _create_late_join_participant(self, session):
        return HubParticipant.objects.create(
            session=session,
            nickname="LateLisa",
            is_active=True,
            last_seen=timezone.now(),
        )

    def _generate_session_code(self):
        alphabet = string.ascii_uppercase + string.digits
        while True:
            code = "".join(random.choices(alphabet, k=6))
            if not HubSession.objects.filter(code=code).exists():
                return code

    def _absolute_url(self, base_url, path, query=None):
        base_url = (base_url or "").rstrip("/")
        url = f"{base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url

    def _print_summary(
        self,
        *,
        session,
        participants,
        games,
        base_url,
        creator,
        user_created,
        check_in_result,
        late_participant,
        leaver,
    ):
        monitor_path = reverse("games_hub:monitor", args=[session.code])
        lobby_path = reverse("games_hub:lobby", args=[session.code])
        join_path = reverse("games_hub:join_session")
        leaderboard_path = reverse("games_hub:session_leaderboard", args=[session.code])

        self.stdout.write(self.style.SUCCESS("QA smoke evening created."))
        self.stdout.write("")
        self.stdout.write(f"Session code: {session.code}")
        self.stdout.write(f"Session name: {session.name}")
        self.stdout.write(f"Scoring mode: {session.overall_scoring_mode}")
        self.stdout.write(f"Weighting mode: {session.overall_weighting_mode}")
        self.stdout.write(f"Check-in status: {session.check_in_status}")
        self.stdout.write(f"Locked participant count: {session.locked_participant_count}")
        self.stdout.write("")
        self.stdout.write(f"Host URL: {self._absolute_url(base_url, monitor_path)}")
        self.stdout.write(f"Participant join URL: {self._absolute_url(base_url, join_path)}")
        self.stdout.write(f"Session lobby URL: {self._absolute_url(base_url, lobby_path)}")
        self.stdout.write(f"Scoreboard URL: {self._absolute_url(base_url, leaderboard_path)}")
        self.stdout.write("")
        self.stdout.write("Prepared participant names:")
        for participant in participants:
            direct_lobby = self._absolute_url(
                base_url,
                lobby_path,
                query={"nickname": participant.nickname},
            )
            suffix = ""
            if leaver and participant.id == leaver.id:
                suffix = " (marked permanently left)"
            self.stdout.write(f"  - {participant.nickname}{suffix}: {direct_lobby}")
        if late_participant:
            direct_lobby = self._absolute_url(
                base_url,
                lobby_path,
                query={"nickname": late_participant.nickname},
            )
            self.stdout.write(f"  - {late_participant.nickname} (late join QA): {direct_lobby}")
        self.stdout.write("")
        self.stdout.write("Planned games:")
        for index, item in enumerate(games, start=1):
            game = item["game"]
            self.stdout.write(f"  {index}. {game.title} [{item['key']}] room={game.room_code}")
        self.stdout.write("")
        if user_created:
            self.stdout.write(f"QA host user created: {creator.username} / {QA_PASSWORD}")
        else:
            self.stdout.write(
                f"QA host user reused: {creator.username} "
                "(password was not changed by this command)"
            )
        if not check_in_result.get("success"):
            self.stdout.write(self.style.WARNING(f"Check-in preparation warning: {check_in_result.get('error')}"))
