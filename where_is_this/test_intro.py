"""Hub intro/entry regressions; map rendering and scoring use their existing path."""
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.game_intro import get_game_step, start_game_intro
from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.test_session_repairs_browser import SessionBrowserBase
from . import runtime
from .models import WhereQuestion, WhereQuiz


def make_entry_material(case):
    case.user = User.objects.create_user('intro-entry-host', is_staff=True)
    case.hub = HubSession.objects.create(code='INTROENTRY', creator=case.user,
        is_active=True, started_at=timezone.now(), check_in_status='completed',
        locked_participant_count=2)
    case.parties = [HubParticipant.objects.create(session=case.hub, nickname=name,
        scoring_eligible=True) for name in ['Alpha', 'Beta']]
    case.question = WhereQuestion.objects.create(created_by=case.user,
        question_text='Wo liegt Berlin?', correct_latitude=52.52,
        correct_longitude=13.405, map_type='germany', time_limit=120)
    case.games, case.steps = [], []
    for number, title in enumerate(['Orte der Geschichte', 'Reise durch Europa']):
        game = WhereQuiz.objects.create(creator=case.user, title=f'Material {number}',
            zone_count=5, zone_width_km=10)
        game.selected_questions.add(case.question)
        case.games.append(game)
        case.steps.append(HubGameStep.objects.create(session=case.hub, order=number,
            game_key='where', room_code=game.room_code, title=title))


class WhereIntroStateTests(TransactionTestCase):
    def setUp(self):
        make_entry_material(self)

    def test_live_state_uses_persisted_intro_without_resetting_question_or_answer(self):
        game = self.games[0]
        start_game_intro(self.hub.code, 'where', game.room_code)
        runtime.start(game, self.hub.code)
        runtime.send_question(game, self.question)
        player = game.participants.get(name='Alpha')
        state = runtime.state(game, self.hub.code, player)
        runtime.save_input(game, player, {'question_id': state['current_question_id'],
            'round_id': state['current_round_id'], 'latitude': 52.5, 'longitude': 13.4}, confirm=True)
        before = runtime.state(game, self.hub.code, player)
        for observe in (True, False, True):
            after = runtime.state(game, self.hub.code, player, observe=observe)
            for key in ('question', 'own_answer', 'can_answer', 'ends_at'):
                self.assertEqual(after[key], before[key])
            self.assertEqual(str(after['current_round_id']), str(before['current_round_id']))
            self.assertEqual(after['intro']['game_instance_id'], f'where:{game.room_code}:{self.steps[0].pk}')
            self.assertEqual(after['intro']['game_title'], self.steps[0].title.upper())
        self.assertEqual(game.where_answers.count(), 1)

    def test_expired_intro_and_waiting_state_are_reconstructed_without_restart(self):
        game, step = self.games[0], self.steps[0]
        start_game_intro(self.hub.code, 'where', game.room_code)
        step.refresh_from_db()
        step.intro_started_at -= timedelta(seconds=30)
        step.intro_ends_at -= timedelta(seconds=30)
        step.save(update_fields=['intro_started_at', 'intro_ends_at'])
        for _ in range(2):
            state = runtime.state(game, self.hub.code)
            self.assertFalse(state['intro']['intro_active'])
            self.assertEqual(state['intro']['intro_started_at'], step.intro_started_at.isoformat())
            self.assertEqual(state['intro']['state_revision'], 1)
            self.assertIsNone(state['question'])
        self.assertNotIn('intro', runtime.state(game, ''))


class WhereHubIntroBrowserTests(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        self.db(lambda: make_entry_material(self))
        self.client.force_login(self.user)

    def context(self, **options):
        context = super().context(**options)
        # Observe/close the real transport to test reconnect, without mocks.
        context.add_init_script("""(() => {
          const Native = window.WebSocket;
          window.__entrySockets = [];
          window.__entryMessages = [];
          window.WebSocket = class extends Native {
            constructor(...args) {
              super(...args); window.__entrySockets.push(this);
              this.addEventListener('message', event => {
                const data = JSON.parse(event.data);
                window.__entryMessages.push({type:data.type, hasPending:!!data.pending_input});
              });
            }
          };
        })();""")
        return context

    def wait_intro_finished(self, page):
        page.wait_for_function("!document.documentElement.classList.contains('is-session-game-intro-active')"
            " && !document.documentElement.classList.contains('qa-vhs-transition--active')", timeout=20000)

    def start_from_hub(self, host, game):
        host.goto(self.live_server_url + reverse('games_hub:monitor', args=[self.hub.code]))
        host.click(f'.launch-btn[data-room-code="{game.room_code}"]')
        host.wait_for_url('**/where/monitor/**')
        host.wait_for_selector('#startQuizBtn:not([disabled])')
        host.click('#startQuizBtn')
        host.wait_for_selector('#sendQuestionBtn:not([disabled])')

    def assert_intro(self, page, game, step):
        page.wait_for_url(f'**/where/play/{game.room_code}/**')
        page.wait_for_selector('#sessionGameStartIntro:not([hidden])')
        intro = page.evaluate('window.sessionGameStartIntro.getState()')
        expected = self.db(lambda: get_game_step(self.hub.code, 'where', game.room_code)['intro'])
        self.assertEqual(intro['gameNumber'], str(expected['game_number']))
        self.assertEqual(intro['gameTitle'], step.title.upper())
        self.assertNotEqual(intro['gameTitle'], game.title.upper())
        self.assertEqual(intro['gameInstanceId'], expected['game_instance_id'])
        self.assertEqual(intro['introStartedAt'], expected['intro_started_at'])
        self.assertEqual(intro['introEndsAt'], expected['intro_ends_at'])
        query = parse_qs(urlparse(page.url).query)
        self.assertEqual(query['game_start_instance'], [expected['game_instance_id']])
        self.assertEqual(query['game_start_order'], [str(step.order + 1)])
        self.assertEqual(query['game_start_title'], [step.title.upper()])

    def test_two_regular_starts_wait_question_commit_reload_and_lobby_rejoin(self):
        lobby = reverse('games_hub:lobby', args=[self.hub.code])
        pages = [self.page(lobby, player=party, theme='vhs') for party in self.parties]
        for page in pages:
            page.wait_for_function("document.getElementById('joinCard').style.display === 'none'")
        host = self.page(reverse('games_hub:monitor', args=[self.hub.code]), host=True)
        identities = []
        for game, step in zip(self.games, self.steps):
            self.start_from_hub(host, game)
            for page in pages:
                self.assert_intro(page, game, step)
            identities.append(pages[0].evaluate('window.sessionGameStartIntro.getState().gameInstanceId'))
            for page in pages:
                self.wait_intro_finished(page)
                self.assertTrue(page.locator('#whereWaiting').is_visible())
                self.assertFalse(page.locator('#whereQuestion').is_visible())
            pages[1].reload()
            pages[1].wait_for_selector('#whereWaiting:not([hidden])')
            self.assertTrue(pages[1].locator('#sessionGameStartIntro').is_hidden())
            host.click('#sendQuestionBtn')
            for page in pages:
                page.wait_for_selector('#whereQuestion:not([hidden])')
                page.wait_for_selector('#whereMap[data-loaded="true"]')
                self.assertEqual(page.inner_text('#questionText'), self.question.question_text)
            box = pages[0].locator('#whereMap').bounding_box()
            pages[0].mouse.click(box['x'] + box['width']*.5, box['y'] + box['height']*.5)
            # Wait for the authoritative draft snapshot, not just the input ACK.
            pages[0].wait_for_function("window.__entryMessages.some(m=>m.type==='where_state' && m.hasPending)")
            pages[0].wait_for_selector('#submitAnswerBtn:not([disabled])')
            pages[0].click('#submitAnswerBtn')
            pages[0].wait_for_function("document.getElementById('whereMessage').textContent.includes('eingeloggt')")
            saved = self.db(lambda: list(game.where_answers.values('id', 'x_norm', 'y_norm', 'submitted_at')))
            for rejoin in (False, True):
                if rejoin:
                    pages[0].goto(self.live_server_url + lobby)
                    pages[0].wait_for_url(f'**/where/play/{game.room_code}/**')
                else:
                    pages[0].reload()
                pages[0].wait_for_function("document.getElementById('whereMessage').textContent.includes('eingeloggt')")
                self.assertTrue(pages[0].locator('#submitAnswerBtn').is_disabled())
                self.assertTrue(pages[0].locator('#sessionGameStartIntro').is_hidden())
                self.assertEqual(self.db(lambda: list(game.where_answers.values('id', 'x_norm', 'y_norm', 'submitted_at'))), saved)
            pages[1].evaluate("window.__entrySockets.find(s=>s.url.includes('/ws/where/')).close()")
            pages[1].wait_for_function("window.__entrySockets.filter(s=>s.url.includes('/ws/where/') && s.readyState===1).length===1")
            self.assertEqual(pages[1].inner_text('#questionText'), self.question.question_text)
            self.assertTrue(pages[1].locator('#sessionGameStartIntro').is_hidden())
            host.click('#endQuizBtn')
            for page, party in zip(pages, self.parties):
                page.wait_for_selector('#continueRevealBtn:not([hidden])')
                # Use the existing return transport to leave before the next start.
                response = self.client.post(reverse('games_hub:participant_return_to_lobby', args=[self.hub.code]),
                    data=json.dumps({'game_key':'where', 'room_code':game.room_code, 'participant_name':party.nickname}),
                    content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                page.goto(self.live_server_url + lobby + '?return=1&from_game=where&from_room=' + game.room_code)
                page.wait_for_function("document.getElementById('joinCard').style.display === 'none'")
        self.assertNotEqual(*identities)
        self.assertFalse(self.errors)

    def test_question_started_during_intro_survives_transition_and_fresh_entry(self):
        game, step = self.games[0], self.steps[0]
        lobby = reverse('games_hub:lobby', args=[self.hub.code])
        page = self.page(lobby, player=self.parties[0], theme='vhs')
        page.wait_for_function("document.getElementById('joinCard').style.display === 'none'")
        host = self.page(reverse('games_hub:monitor', args=[self.hub.code]), host=True)
        self.start_from_hub(host, game)
        self.assert_intro(page, game, step)
        host.click('#sendQuestionBtn')
        page.wait_for_function("document.getElementById('questionText').textContent === 'Wo liegt Berlin?'")
        before = self.db(lambda: runtime.state(game, self.hub.code, game.participants.get(name='Alpha')))
        page.reload()
        self.wait_intro_finished(page)
        self.assertEqual(page.evaluate('window.sessionGameStartIntro.getState().introEndsAt'), before['intro']['intro_ends_at'])
        page.wait_for_selector('#whereQuestion:not([hidden])')
        after = self.db(lambda: runtime.state(game, self.hub.code, game.participants.get(name='Alpha')))
        for key in ('current_question_id', 'current_round_id', 'starts_at', 'ends_at'):
            self.assertEqual(after[key], before[key])
        self.assertFalse(page.locator('#whereWaiting').is_visible())
        self.assertEqual(page.inner_text('#questionText'), self.question.question_text)
        # A fresh browser arriving after the timeline expires gets the live DTO,
        # without relying on a completed-intro record in sessionStorage.
        fresh = self.page(lobby, player=self.parties[0], theme='vhs')
        fresh.wait_for_url(f'**/where/play/{game.room_code}/**')
        fresh.wait_for_selector('#whereQuestion:not([hidden])')
        self.assertTrue(fresh.locator('#sessionGameStartIntro').is_hidden())
        self.assertEqual(fresh.inner_text('#questionText'), self.question.question_text)
        self.assertFalse(self.errors)
