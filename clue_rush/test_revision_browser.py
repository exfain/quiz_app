import os

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from games_hub.authoritative_state import reset_question_flow
from games_hub.models import GameRuntimeState, HubGameStep, HubSession
from games_hub.playwright_e2e import (
    install_browser_test_stubs,
    start_chromium_browser,
)

from .models import (
    Clue,
    ClueQuestion,
    ClueRushGame,
    ClueRushParticipant,
    ClueRushSession,
)

try:
    from channels.testing import ChannelsLiveServerTestCase as _BaseLiveServerTestCase
except ImportError:
    from django.test import LiveServerTestCase as _BaseLiveServerTestCase


class ClueRushQuestionRevisionBrowserTests(_BaseLiveServerTestCase):
    TIMEOUT = 20_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls._playwright, cls._browser = start_chromium_browser(headless=True)
            cls._playwright_available = True
        except Exception as exc:
            cls._playwright_available = False
            cls._playwright_error = exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, '_playwright_available', False):
            cls._browser.close()
            cls._playwright.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._playwright_available:
            self.skipTest(
                f'Playwright/Chromium nicht verfuegbar: {self._playwright_error}'
            )
        self.password = 'testpass123'
        self.admin_user = User.objects.create_superuser(
            username='clue-revision-browser-host',
            password=self.password,
            email='',
        )

    def _create_run(self, index, *, stale_inactive=False):
        questions = []
        for question_index in range(2):
            question = ClueQuestion.objects.create(
                question_text=f'Browser question {index}-{question_index + 1}',
                answer='Brazil',
                points=3,
                time_limit=30,
                created_by=self.admin_user,
            )
            Clue.objects.create(
                clue_question=question,
                order=1,
                clue_text=f'Browser clue {index}-{question_index + 1}',
                duration=5,
            )
            questions.append(question)

        game = ClueRushGame.objects.create(
            title=f'Clue revision run {index}',
            room_code=f'C{index:03d}',
            creator=self.admin_user,
            status='active',
            started_at=timezone.now(),
            question_order=[question.id for question in questions],
        )
        game.selected_questions.set(questions)
        session_runtime = ClueRushSession.objects.create(quiz=game)
        if stale_inactive:
            stale_start = timezone.now() - timezone.timedelta(seconds=30)
            game.status = 'inactive'
            game.current_question = questions[0]
            game.question_start_time = stale_start
            game.current_clue = questions[0].clues.first()
            game.clue_start_time = stale_start
            game.save()
            session_runtime.is_question_active = True
            session_runtime.current_question_number = 1
            session_runtime.total_questions_sent = 1
            session_runtime.answer_deadline = timezone.now() + timezone.timedelta(seconds=5)
            session_runtime.question_end_time = session_runtime.answer_deadline
            session_runtime.clue_schedule = [{'question_id': questions[0].id}]
            session_runtime.save()
        session = HubSession.objects.create(
            code=f'CRB{index:03d}',
            name=f'Clue browser run {index}',
            creator=self.admin_user,
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            locked_participant_count=2,
        )
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='clue_rush',
            room_code=game.room_code,
            title=game.title,
        )
        participants = [
            ClueRushParticipant.objects.create(
                quiz=game,
                name=name,
                hub_session_code=session.code,
            )
            for name in ('Ada', 'Grace')
        ]
        reset_question_flow(
            game_key='clue_rush',
            room_code=game.room_code,
            session_code=session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        return game, session, questions, participants

    def _login_host(self, page):
        page.goto(f"{self.live_server_url}{reverse('admin_dashboard:login')}")
        page.fill("input[name='username']", self.admin_user.username)
        page.fill("input[name='password']", self.password)
        page.click("button[type='submit']")
        page.wait_for_url(
            f"**{reverse('admin_dashboard:home')}**",
            timeout=self.TIMEOUT,
        )

    def _wait_for_host_socket(self, page):
        page.wait_for_function(
            "() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN",
            timeout=self.TIMEOUT,
        )

    def _send_and_finish_question(self, page):
        page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        first_question_id = int(
            page.locator('.send-question-btn').first.get_attribute('data-question-id')
        )
        page.locator('.send-question-btn').first.click()
        page.locator('.send-question-btn').first.click()
        page.wait_for_selector('#activeQuestion', timeout=self.TIMEOUT)
        self._wait_for_host_socket(page)
        page.wait_for_selector('#startCluesBtn:not([disabled])', timeout=self.TIMEOUT)
        page.click('#startCluesBtn')
        page.wait_for_selector('#endQuestionBtn', timeout=self.TIMEOUT)
        self._wait_for_host_socket(page)
        page.click('#endQuestionBtn')
        page.wait_for_selector('#questionSelection', timeout=self.TIMEOUT)
        return first_question_id

    def _run_session(
        self,
        index,
        *,
        reload_host=False,
        reconnect_participant=False,
        stale_inactive=False,
    ):
        game, session, questions, participants = self._create_run(
            index,
            stale_inactive=stale_inactive,
        )
        host_context = self._browser.new_context()
        participant_contexts = [self._browser.new_context() for _ in participants]
        install_browser_test_stubs(host_context)
        for context in participant_contexts:
            install_browser_test_stubs(context)
        host_page = host_context.new_page()
        participant_pages = [context.new_page() for context in participant_contexts]
        dialogs = []
        host_page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        try:
            self._login_host(host_page)
            monitor_url = (
                f"{self.live_server_url}"
                f"{reverse('admin_dashboard:clue_rush_monitor', args=[game.room_code])}"
                f"?hub_session={session.code}"
            )
            host_page.goto(monitor_url)
            self._wait_for_host_socket(host_page)
            if stale_inactive:
                host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
                host_page.click('#startQuizBtn')
                host_page.wait_for_selector('#endQuizBtn', timeout=self.TIMEOUT)
                self._wait_for_host_socket(host_page)

            for page, participant in zip(participant_pages, participants):
                play_url = (
                    f"{self.live_server_url}"
                    f"{reverse('clue_rush:play', args=[game.room_code, participant.name])}"
                    f"?hub_session={session.code}"
                )
                page.goto(play_url)
                page.wait_for_timeout(300)

            host_page.wait_for_function(
                "() => Number(document.querySelector('#participantCount')?.textContent) === 2",
                timeout=self.TIMEOUT,
            )
            host_page.wait_for_function(
                "() => window.adminGameMonitor?.stateRevision >= 2",
                timeout=self.TIMEOUT,
            )
            if reload_host:
                host_page.reload()
                self._wait_for_host_socket(host_page)
            if reconnect_participant:
                participant_pages[1].reload()
                participant_pages[1].wait_for_timeout(300)
                host_page.wait_for_timeout(300)

            first_question_id = self._send_and_finish_question(host_page)
            host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
            second_question_id = int(
                host_page.locator('.send-question-btn').nth(1).get_attribute(
                    'data-question-id'
                )
            )
            host_page.locator('.send-question-btn').nth(1).click()
            host_page.locator('.send-question-btn').nth(1).click()
            host_page.wait_for_selector('#activeQuestion', timeout=self.TIMEOUT)

            game.refresh_from_db()
            self.assertNotEqual(first_question_id, second_question_id)
            self.assertEqual(game.current_question_id, second_question_id)
            self.assertNotIn('Der Spielzustand hat sich geaendert.', dialogs)
            self.assertNotIn('Der Spielzustand hat sich geändert.', dialogs)
            self.assertEqual(dialogs, [])
        finally:
            for page in [host_page, *participant_pages]:
                try:
                    page.close()
                except Exception:
                    pass
            for context in [host_context, *participant_contexts]:
                try:
                    context.close()
                except Exception:
                    pass

    def test_fresh_session_sends_two_questions_without_revision_dialog(self):
        self._run_session(1)

    def test_host_reload_sends_two_questions_without_revision_dialog(self):
        self._run_session(2, reload_host=True)

    def test_participant_reconnect_sends_two_questions_without_revision_dialog(self):
        self._run_session(3, reconnect_participant=True)

    def test_reactivated_stale_game_resets_runtime_and_sends_two_questions(self):
        self._run_session(4, stale_inactive=True)

    def test_mobile_hint_layout_stays_mounted_after_answer_submit(self):
        game, session, questions, participants = self._create_run(10)
        participants.append(ClueRushParticipant.objects.create(
            quiz=game,
            name='Linus',
            hub_session_code=session.code,
        ))
        for question in questions:
            first_clue = question.clues.get()
            first_clue.duration = 4
            first_clue.save(update_fields=['duration'])
            Clue.objects.create(
                clue_question=question,
                order=2,
                clue_text=f'{first_clue.clue_text} follow-up',
                duration=20,
            )

        host_context = self._browser.new_context()
        player_contexts = [
            self._browser.new_context(viewport={'width': width, 'height': 800})
            for width in (360, 390, 430)
        ]
        install_browser_test_stubs(host_context)
        for context in player_contexts:
            install_browser_test_stubs(context)
        host_page = host_context.new_page()
        player_pages = [context.new_page() for context in player_contexts]
        try:
            self._login_host(host_page)
            host_page.goto(
                f"{self.live_server_url}"
                f"{reverse('admin_dashboard:clue_rush_monitor', args=[game.room_code])}"
                f"?hub_session={session.code}"
            )
            self._wait_for_host_socket(host_page)
            for page, participant in zip(player_pages, participants):
                page.goto(
                    f"{self.live_server_url}"
                    f"{reverse('clue_rush:play', args=[game.room_code, participant.name])}"
                    f"?hub_session={session.code}"
                )

            host_page.locator('.send-question-btn').first.click()
            host_page.locator('.send-question-btn').first.click()
            host_page.wait_for_selector('#startCluesBtn:not([disabled])', timeout=self.TIMEOUT)
            host_page.click('#startCluesBtn')

            for width, player_page in zip((360, 390, 430), player_pages):
                with self.subTest(width=width):
                    player_page.wait_for_selector(
                        '#playerCluesList .clue-rush-clue',
                        timeout=self.TIMEOUT,
                    )
                    player_page.wait_for_selector(
                        '#shortAnswerInput:not([disabled])',
                        timeout=self.TIMEOUT,
                    )

                    before = player_page.evaluate(
                        """() => {
                            const list = document.getElementById('playerCluesList');
                            const clue = list.querySelector('.clue-rush-clue');
                            window.__clueRushHintList = list;
                            const rect = clue.getBoundingClientRect();
                            return {
                                text: clue.textContent.trim(),
                                x: rect.x,
                                y: rect.y + window.scrollY,
                                width: rect.width,
                                height: rect.height,
                            };
                        }"""
                    )
                    submit_dispatched = player_page.evaluate(
                        """() => {
                            const input = document.getElementById('shortAnswerInput');
                            const button = document.getElementById('submitAnswerBtn');
                            input.value = 'Brazil';
                            input.dispatchEvent(new Event('input', {bubbles: true}));
                            if (button.disabled) return false;
                            button.click();
                            return true;
                        }"""
                    )
                    self.assertTrue(submit_dispatched)
                    player_page.wait_for_selector(
                        '#clueRushSubmittedAnswer:not(.d-none)',
                        timeout=self.TIMEOUT,
                    )

                    after = player_page.evaluate(
                        """() => {
                            const list = document.getElementById('playerCluesList');
                            const clue = list.querySelector('.clue-rush-clue');
                            const rect = clue.getBoundingClientRect();
                            return {
                                sameListNode: window.__clueRushHintList === list,
                                questionVisible: !document.getElementById('questionState').classList.contains('d-none'),
                                text: clue.textContent.trim(),
                                x: rect.x,
                                y: rect.y + window.scrollY,
                                width: rect.width,
                                height: rect.height,
                            };
                        }"""
                    )

                    self.assertTrue(after['sameListNode'])
                    self.assertTrue(after['questionVisible'])
                    self.assertEqual(after['text'], before['text'])
                    for key in ('x', 'y', 'width', 'height'):
                        self.assertAlmostEqual(
                            after[key],
                            before[key],
                            delta=1.0,
                            msg=f'{width}px {key}: {before} -> {after}',
                        )

                    if width == 360:
                        player_page.wait_for_function(
                            "() => document.querySelectorAll('#playerCluesList .clue-rush-clue').length === 2",
                            timeout=self.TIMEOUT,
                        )

            self.assertEqual(
                player_pages[0].locator('#playerCluesList .clue-rush-clue').count(),
                2,
            )
        finally:
            host_page.close()
            for player_page in player_pages:
                player_page.close()
            host_context.close()
            for player_context in player_contexts:
                player_context.close()
