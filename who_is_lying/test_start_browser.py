import os
import json
import random
import string
import time
from unittest.mock import patch

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from django.contrib.auth.models import User
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubSession, HubParticipant
from games_hub.lobby_join import issue_rejoin_token
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser

from .models import WhoAnswer, WhoParticipant, WhoQuestion, WhoQuiz, WhoSession

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserLiveServerTestCase
except ImportError:
    from django.test import LiveServerTestCase as _BrowserLiveServerTestCase


def _random_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


class WhoLyingStartBrowserFlowTests(_BrowserLiveServerTestCase):
    TIMEOUT = 15_000
    LONG_TIMEOUT = 35_000

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
            self.skipTest(f'Playwright/Chromium nicht verfuegbar: {self._playwright_error}')

        self.password = 'testpass123'
        self.admin = User.objects.create_superuser(
            username=f'who_admin_{_random_code()}',
            password=self.password,
            email='',
        )
        self.setup_client = DjangoClient()
        self.setup_client.force_login(self.admin)
        self.quiz = WhoQuiz.objects.create(
            title='Who Browser Start',
            room_code=''.join(random.choices(string.digits, k=4)),
            creator=self.admin,
            status='waiting',
        )
        self.questions = [
            WhoQuestion.objects.create(
                statement=f'Who browser set {index}',
                points=10,
                time_limit=5,
                people=[
                    {'name': 'Ada', 'is_lying': False},
                    {'name': 'Bob', 'is_lying': True},
                ],
                created_by=self.admin,
            )
            for index in (1, 2)
        ]
        self.quiz.selected_questions.set(self.questions)
        self.quiz.question_order = [question.id for question in self.questions]
        self.quiz.save(update_fields=['question_order', 'updated_at'])
        WhoSession.objects.create(quiz=self.quiz)
        self.session = HubSession.objects.create(
            code=_random_code(),
            name='Who Browser Session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='who',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )

        self.host_context = self._browser.new_context()
        self.participant_contexts = [self._browser.new_context(), self._browser.new_context()]
        for context in [self.host_context, *self.participant_contexts]:
            install_browser_test_stubs(context)
        self.host_page = self.host_context.new_page()
        self.participant_pages = [context.new_page() for context in self.participant_contexts]
        self.participant_pages[1].route(
            '**/who/join/',
            lambda route: (time.sleep(1.0), route.continue_()),
        )
        self.browser_errors = []
        self.pending_requests = set()
        for label, page in [('host', self.host_page), ('participant-a', self.participant_pages[0]), ('participant-b', self.participant_pages[1])]:
            page.on('pageerror', lambda exc, label=label: self.browser_errors.append(f'{label}: {exc}'))
            page.on('request', lambda request: self.pending_requests.add(request))
            page.on('requestfinished', lambda request: self.pending_requests.discard(request))
            page.on('requestfailed', lambda request: self.pending_requests.discard(request))
        self._login_host()
        self.first_nickname = "Team <'&\"\\" if self._testMethodName == 'test_special_name_pause_resume_and_repeat_keep_round_state' else 'Alice'
        if self._testMethodName == 'test_special_name_pause_resume_and_repeat_keep_round_state':
            # New names reject these characters; this case exercises an
            # existing legacy identity through its authenticated rejoin path.
            participant = HubParticipant.objects.create(session=self.session, nickname=self.first_nickname)
            credential = {'nickname': self.first_nickname, 'token': issue_rejoin_token(participant)}
            self.participant_contexts[0].add_init_script(
                f'localStorage.setItem({json.dumps("hub_lobby_rejoin:" + self.session.code)}, '
                f'{json.dumps(json.dumps(credential))});'
            )
        self._join_lobby(self.participant_pages[0], self.first_nickname)
        self._join_lobby(self.participant_pages[1], 'Bob')

    def tearDown(self):
        pages = [getattr(self, 'host_page', None), *getattr(self, 'participant_pages', [])]
        for page in pages:
            if page:
                page.route('**/*', lambda route: route.abort())
        deadline = time.monotonic() + 5
        while getattr(self, 'pending_requests', None) and time.monotonic() < deadline:
            self.host_page.wait_for_timeout(50)
        for page in [getattr(self, 'host_page', None), *getattr(self, 'participant_pages', [])]:
            if page:
                page.close()
        for context in [getattr(self, 'host_context', None), *getattr(self, 'participant_contexts', [])]:
            if context:
                context.close()

    def _login_host(self):
        self.host_page.goto(f'{self.live_server_url}{reverse("admin_dashboard:login")}')
        self.host_page.fill("input[name='username']", self.admin.username)
        self.host_page.fill("input[name='password']", self.password)
        self.host_page.click("button[type='submit']")
        self.host_page.wait_for_url(f'**{reverse("admin_dashboard:home")}**', timeout=self.TIMEOUT)

    def _join_lobby(self, page, nickname):
        page.goto(f'{self.live_server_url}{reverse("games_hub:lobby", args=[self.session.code])}')
        page.wait_for_selector('#nickname', timeout=self.TIMEOUT)
        page.fill('#nickname', nickname)
        page.wait_for_selector('#joinBtn:not([disabled])', timeout=self.TIMEOUT)
        page.click('#joinBtn')
        page.wait_for_selector('#joinCard', state='hidden', timeout=self.TIMEOUT)

    def _monitor_url(self):
        return (
            f'{self.live_server_url}'
            f'{reverse("admin_dashboard:who_monitor", args=[self.quiz.room_code])}'
            f'?hub_session={self.session.code}'
        )

    def _send_and_start_set(self, question):
        send_button = self.host_page.locator(
            f'.send-question-btn[data-question-id="{question.id}"]'
        )
        send_button.wait_for(state='visible', timeout=self.TIMEOUT)
        self.host_page.wait_for_function(
            "() => window.adminGameMonitor?.websocket?.readyState === WebSocket.OPEN && window.adminGameMonitor.stateRevision >= 0",
            timeout=self.TIMEOUT,
        )
        send_button.click()
        send_button.click()
        self.host_page.wait_for_selector('#startSetBtn', timeout=self.TIMEOUT)
        self.host_page.wait_for_selector('#startSetBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#startSetBtn')
        self.host_page.wait_for_selector('#endQuestionBtn', timeout=self.TIMEOUT)

    def test_person_choices_survive_reload_toggle_and_normal_set_end_for_two_players(self):
        for question in self.questions:
            question.time_limit = 8
            question.save(update_fields=['time_limit'])
        sent, received = [], []
        for context, page in zip(self.participant_contexts, self.participant_pages):
            context.add_init_script("localStorage.setItem('participant_interface_theme', 'vhs');")

            def instrument(socket):
                if '/ws/who/' in socket.url:
                    socket.on('framesent', lambda frame: sent.append(json.loads(frame)))
                    socket.on('framereceived', lambda frame: received.append(json.loads(frame)))

            page.on('websocket', instrument)
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#startQuizBtn')
        for page in self.participant_pages:
            page.wait_for_url(f'**/who/play/{self.quiz.room_code}/**', timeout=self.LONG_TIMEOUT)
            page.wait_for_function(
                "() => window.sessionGameStartIntro && document.getElementById('sessionGameStartIntro').hidden",
                timeout=self.LONG_TIMEOUT,
            )
        self._send_and_start_set(self.questions[0])
        for page in self.participant_pages:
            page.wait_for_selector('#accuseLiarBtn:not([disabled])', timeout=self.TIMEOUT)
        alice, bob = self.participant_pages

        def press(page, selected):
            page.click('#accuseLiarBtn')
            page.wait_for_function(
                "selected => document.getElementById('accuseLiarBtn').getAttribute('aria-pressed') === String(selected) && !document.getElementById('accuseLiarBtn').disabled",
                arg=selected, timeout=self.TIMEOUT,
            )

        first_name = alice.locator('#currentPersonName').inner_text()
        press(alice, True)
        press(alice, False)
        press(alice, True)
        alice.reload()
        alice.wait_for_function(
            "() => document.getElementById('accuseLiarBtn').getAttribute('aria-pressed') === 'true' && !document.getElementById('accuseLiarBtn').disabled",
            timeout=self.TIMEOUT,
        )
        self.assertEqual(alice.locator('#currentPersonName').inner_text(), first_name)
        truth_by_name = {person['name']: person['is_lying'] for person in self.questions[0].people}
        if not truth_by_name[first_name]:
            press(alice, False)
            press(bob, True)
        self.assertEqual(WhoAnswer.objects.filter(quiz=self.quiz).count(), 0)
        for page in self.participant_pages:
            page.wait_for_function(
                "name => document.getElementById('currentPersonName').textContent !== name",
                arg=first_name, timeout=self.TIMEOUT,
            )
            self.assertEqual(page.locator('#accuseLiarBtn').get_attribute('aria-pressed'), 'false')
            self.assertFalse(page.locator('#accuseLiarBtn').is_disabled())
        second_name = alice.locator('#currentPersonName').inner_text()
        press(alice if truth_by_name[second_name] else bob, True)
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.LONG_TIMEOUT)
        for page in self.participant_pages:
            page.wait_for_selector('#setRevealState:not(.d-none)', timeout=self.TIMEOUT)
        self.assertEqual(
            dict(WhoAnswer.objects.filter(quiz=self.quiz).values_list('participant__name', 'points_earned')),
            {'Alice': 1, 'Bob': -1},
        )
        self.assertFalse(any(frame.get('type') == 'participant_submit_answer' for frame in sent))
        self.assertFalse(any(frame.get('type') in {'action_rejected', 'answer_rejected', 'person_choice_rejected'}
                             for frame in received))
        self.assertEqual(WhoSession.objects.get(quiz=self.quiz).pending_answers, {})

        # The next set starts unmarked; early host end finalizes both empty selections.
        self._send_and_start_set(self.questions[1])
        for page in self.participant_pages:
            page.wait_for_selector('#accuseLiarBtn:not([disabled])', timeout=self.TIMEOUT)
            self.assertEqual(page.locator('#accuseLiarBtn').get_attribute('aria-pressed'), 'false')
        self.host_page.click('#endQuestionBtn')
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.assertEqual(WhoAnswer.objects.filter(quiz=self.quiz).count(), 4)
        self.assertEqual(list(WhoAnswer.objects.filter(question=self.questions[1]).values_list('points_earned', flat=True)), [0, 0])
        # The existing host markup defect is tracked separately in W2.
        self.assertEqual([error for error in self.browser_errors if error.startswith('participant-')], [])

    def test_special_name_pause_resume_and_repeat_keep_round_state(self):
        self.host_frames = []
        def instrument_host(socket):
            socket.on('framesent', lambda payload: self.host_frames.append(('sent', str(payload))))
            socket.on('framereceived', lambda payload: self.host_frames.append(('received', str(payload))))
        self.host_page.on('websocket', instrument_host)
        self.host_page.on('dialog', lambda dialog: dialog.accept())
        for question in self.questions:
            question.time_limit = 12
            question.save(update_fields=['time_limit'])
        for context in self.participant_contexts:
            context.add_init_script("localStorage.setItem('participant_interface_theme', 'vhs');")
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.click('#startQuizBtn')
        for page in self.participant_pages:
            page.wait_for_url(f'**/who/play/{self.quiz.room_code}/**', timeout=self.LONG_TIMEOUT)
            page.wait_for_function("() => window.sessionGameStartIntro && document.getElementById('sessionGameStartIntro').hidden", timeout=self.LONG_TIMEOUT)
        self._send_and_start_set(self.questions[0])
        alice, bob = self.participant_pages
        for page in self.participant_pages:
            page.wait_for_selector('#accuseLiarBtn:not([disabled])', timeout=self.TIMEOUT)
            self.assertEqual(page.locator('#currentQuestionNumber').inner_text(), '1')
        alice.click('#accuseLiarBtn')
        alice.wait_for_function("() => document.getElementById('accuseLiarBtn').getAttribute('aria-pressed') === 'true' && !document.getElementById('accuseLiarBtn').disabled")
        first_name = alice.locator('#currentPersonName').inner_text()
        self.host_page.click('#setInactiveBtn')
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        for page in self.participant_pages:
            page.wait_for_function("() => document.getElementById('waitingQuestionState').textContent.includes('pausiert') && !document.getElementById('waitingQuestionState').classList.contains('d-none')")
        session = WhoSession.objects.get(quiz=self.quiz)
        before_deadline = session.question_end_time
        self.host_page.wait_for_timeout(1200)
        self.assertEqual(WhoSession.objects.get(quiz=self.quiz).question_end_time, before_deadline)
        self.host_page.click('#startQuizBtn')
        for page in self.participant_pages:
            page.wait_for_selector('#accuseLiarBtn:not([disabled])', timeout=self.TIMEOUT)
        self.assertEqual(alice.locator('#currentPersonName').inner_text(), first_name)
        self.assertEqual(alice.locator('#accuseLiarBtn').get_attribute('aria-pressed'), 'true')
        self.host_page.wait_for_selector('#endQuestionBtn', timeout=self.TIMEOUT)
        self.host_page.click('#endQuestionBtn')
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        try:
            self._send_and_start_set(self.questions[0])
        except Exception as exc:
            self.fail(f'Repeat set failed: {exc}; frames={self.host_frames[-12:]}')
        for page in self.participant_pages:
            page.wait_for_selector('#accuseLiarBtn:not([disabled])', timeout=self.TIMEOUT)
            self.assertEqual(page.locator('#accuseLiarBtn').get_attribute('aria-pressed'), 'false')
            self.assertEqual(page.locator('#currentQuestionNumber').inner_text(), '2')
            self.assertEqual(page.locator('#totalSetCount').inner_text(), '3')
        self.host_page.click('#endQuestionBtn')
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.assertEqual(WhoAnswer.objects.filter(quiz=self.quiz).count(), 4)
        for page in self.participant_pages:
            page.reload()
            page.wait_for_selector('#scoreHistoryList .score-history-row', timeout=self.TIMEOUT)
            self.assertEqual(page.locator('#currentQuestionNumber').inner_text(), '2')
            self.assertEqual(page.locator('#totalSetCount').inner_text(), '3')
        self.assertEqual(self.browser_errors, [])

    def test_host_score_dialog_saves_negative_score_and_hub_listener_navigates(self):
        participant = WhoParticipant.objects.create(
            quiz=self.quiz, name='Alice', hub_session_code=self.session.code,
        )
        next_quiz = WhoQuiz.objects.create(title='Next Who', creator=self.admin)
        HubGameStep.objects.create(
            session=self.session, order=1, game_key='who', room_code=next_quiz.room_code,
        )
        self.host_context.add_init_script("""(() => {
            const NativeWebSocket = window.WebSocket;
            window.__whoHostSockets = [];
            window.WebSocket = new Proxy(NativeWebSocket, {
                construct(Target, args) {
                    const socket = new Target(...args);
                    window.__whoHostSockets.push(socket);
                    return socket;
                },
            });
        })();""")
        self.host_page.goto(self._monitor_url())
        self.assertEqual(self.host_page.locator('#editScoreModal').count(), 1)
        self.assertTrue(self.host_page.locator('#editScoreModal').evaluate('element => element.parentElement === document.body'))
        self.host_page.click(f'.edit-score-btn[data-participant-id="{participant.id}"]')
        self.host_page.wait_for_selector('#editScoreModal.show', timeout=self.TIMEOUT)
        self.assertEqual(self.host_page.locator('#editScoreName').inner_text(), 'Alice')
        self.host_page.fill('#editScoreInput', '-3')
        with self.host_page.expect_response(
            lambda response: response.url.endswith(reverse('admin_dashboard:set_who_participant_score')),
            timeout=self.TIMEOUT,
        ) as response:
            self.host_page.click('#editScoreSave')
        self.assertTrue(response.value.json()['success'])
        self.host_page.wait_for_function(
            "id => document.getElementById('score-' + id)?.textContent.includes('-3')",
            arg=participant.id, timeout=self.TIMEOUT,
        )
        self.host_page.wait_for_selector('#editScoreModal', state='hidden', timeout=self.TIMEOUT)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, -3)
        self.host_page.reload()
        self.assertEqual(
            self.host_page.locator(f'.edit-score-btn[data-participant-id="{participant.id}"]').get_attribute('data-current-score'),
            '-3',
        )
        self.host_page.wait_for_function(
            "() => window.__whoHostSockets.some(socket => socket.url.includes('/ws/hub/') && socket.readyState === WebSocket.OPEN)",
            timeout=self.TIMEOUT,
        )
        # Exercise the existing public Hub event contract in the monitor page.
        self.host_page.evaluate("""step => {
            const socket = window.__whoHostSockets.find(candidate => candidate.url.includes('/ws/hub/'));
            socket.onmessage(new MessageEvent('message', {
                data: JSON.stringify({type: 'navigate', step}),
            }));
        }""", {'game_key': 'who', 'room_code': next_quiz.room_code})
        self.host_page.wait_for_url(f'**/who/{next_quiz.room_code}/monitor/**', timeout=self.TIMEOUT)
        self.assertEqual(self.browser_errors, [])

    def test_start_reload_and_two_sets_use_running_monitor(self):
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.wait_for_timeout(500)
        self.host_page.click('#startQuizBtn')

        self.host_page.wait_for_selector('#startQuizBtn', state='detached', timeout=self.TIMEOUT)
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.assertIsNotNone(self.quiz.started_at)

        play_pattern = f'**/who/play/{self.quiz.room_code}/**'
        for page in self.participant_pages:
            page.wait_for_url(play_pattern, timeout=self.LONG_TIMEOUT)

        self.session.refresh_from_db()
        self.assertEqual(self.session.current_step_index, 0)
        self.assertEqual(
            set(
                WhoParticipant.objects.filter(
                    quiz=self.quiz,
                    hub_session_code=self.session.code,
                    is_active=True,
                ).values_list('name', flat=True)
            ),
            {'Alice', 'Bob'},
        )

        self.host_page.reload()
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)
        self.assertEqual(self.host_page.locator('#startQuizBtn').count(), 0)

        self._send_and_start_set(self.questions[0])
        self.assertEqual(self.host_page.locator('#startQuizBtn').count(), 0)
        self.host_page.click('#endQuestionBtn')
        self.host_page.wait_for_selector('.send-question-btn:not([disabled])', timeout=self.TIMEOUT)

        self._send_and_start_set(self.questions[1])
        self.assertEqual(self.host_page.locator('#startQuizBtn').count(), 0)
        self.assertFalse(any('updateQuizStatus is not a function' in error for error in self.browser_errors), self.browser_errors)

    def test_reused_game_start_routes_participants_and_spectator(self):
        previous_started_at = self.session.started_at - timezone.timedelta(hours=1)
        self.quiz.started_at = previous_started_at
        self.quiz.save(update_fields=['started_at', 'updated_at'])
        for nickname in ('Alice', 'Bob'):
            WhoParticipant.objects.get_or_create(
                quiz=self.quiz,
                name=nickname,
                hub_session_code=self.session.code,
                defaults={'is_active': False},
            )

        spectator_context = self._browser.new_context()
        install_browser_test_stubs(spectator_context)
        spectator_page = spectator_context.new_page()
        try:
            spectator_page.goto(
                f'{self.live_server_url}'
                f'{reverse("games_hub:spectate_session", args=[self.session.code])}'
            )
            self.host_page.goto(self._monitor_url())
            self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
            self.host_page.click('#startQuizBtn')

            self.host_page.wait_for_selector('#startQuizBtn', state='detached', timeout=self.TIMEOUT)
            play_pattern = f'**/who/play/{self.quiz.room_code}/**'
            for page in self.participant_pages:
                page.wait_for_url(play_pattern, timeout=self.LONG_TIMEOUT)
            spectator_page.wait_for_selector(
                '[data-spectator-game="who"]',
                timeout=self.LONG_TIMEOUT,
            )

            self.host_page.reload()
            self.host_page.wait_for_selector(
                '.send-question-btn:not([disabled])',
                timeout=self.TIMEOUT,
            )
            self.participant_pages[0].reload()
            self.participant_pages[0].wait_for_selector(
                '#waitingQuestionState',
                state='attached',
                timeout=self.TIMEOUT,
            )
            spectator_page.reload()
            spectator_page.wait_for_selector(
                '[data-spectator-game="who"]',
                timeout=self.LONG_TIMEOUT,
            )

            self._send_and_start_set(self.questions[0])
            self.participant_pages[0].wait_for_selector(
                '#questionState:not(.d-none)',
                timeout=self.LONG_TIMEOUT,
            )

            self.quiz.refresh_from_db()
            self.assertEqual(self.quiz.status, 'active')
            self.assertGreater(self.quiz.started_at, previous_started_at)
            self.assertGreaterEqual(self.quiz.started_at, self.session.started_at)
        finally:
            spectator_page.close()
            spectator_context.close()

    def test_recall_happens_once_before_retrying_start(self):
        stale_participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        self.host_page.goto(self._monitor_url())
        self.host_page.wait_for_selector('#startQuizBtn:not([disabled])', timeout=self.TIMEOUT)
        self.host_page.wait_for_timeout(500)
        self.host_page.click('#startQuizBtn')
        self.host_page.wait_for_function(
            "() => document.querySelector('#startQuizBtn')?.disabled === true",
            timeout=self.TIMEOUT,
        )

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'waiting')
        self.assertIsNone(self.quiz.started_at)

        self.host_page.evaluate(
            "const button = document.querySelector('#lobbyReturnGuardPrimaryBtn'); button.disabled = false; button.click()"
        )
        with patch('games_hub.views.broadcast_players_recalled_to_lobby'):
            recall_response = self.setup_client.post(
                reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
            )
        self.assertEqual(recall_response.status_code, 200)
        self.assertTrue(recall_response.json()['all_in_lobby'])
        stale_participant.refresh_from_db()
        self.assertFalse(stale_participant.is_active)
        self.host_page.evaluate(
            """async () => {
                const modal = document.querySelector('#lobbyReturnGuardModal');
                const instance = window.bootstrap?.Modal.getInstance(modal);
                if (instance?._isShown) {
                    await new Promise(resolve => {
                        modal.addEventListener('hidden.bs.modal', resolve, {once: true});
                        if (instance._isTransitioning) {
                            modal.addEventListener('shown.bs.modal', () => instance.hide(), {once: true});
                        } else instance.hide();
                    });
                }
                modal.dispatchEvent(new Event('hidden.bs.modal'));
            }"""
        )

        self.host_page.wait_for_function(
            "() => document.querySelector('#startQuizBtn')?.disabled === false",
            timeout=self.TIMEOUT,
        )
        self.host_page.click('#startQuizBtn')
        self.host_page.wait_for_selector('#startQuizBtn', state='detached', timeout=self.TIMEOUT)

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'active')
        self.assertIsNotNone(self.quiz.started_at)
