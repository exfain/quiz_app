"""Behavioral regression coverage of the actual rendered lobby JavaScript."""
from urllib.parse import urlencode

from django.urls import reverse

from games_hub.models import HubGameParticipantSnapshot, HubGameStep
from games_hub.playwright_e2e import start_chromium_browser
from games_hub.test_session_repairs_browser import SessionBrowserBase

from . import tests as buzzer_tests
from .models import BuzzerGame, BuzzerParticipant


def assert_lobby_join_navigation(test_case, response, *, browser=None):
    """Execute the rendered handler, rather than assert a variable/branch spelling.

    Only unrelated DOM/credential side effects are stubbed. Navigation decisions
    and forwarded step data come verbatim from the server-rendered template.
    """
    html = response.content.decode(response.charset or "utf-8")
    start = html.index("if (data.type === 'lobby_join_success') {")
    end = html.index("if (data.type === 'nickname_status')", start)
    handler = html[start:end]
    old = {"game_key": "buzzer", "room_code": "OLD"}
    new = {"game_key": "buzzer", "room_code": "NEW", "game_instance_id": "new-instance"}
    other = {"game_key": "host_points", "room_code": "NEXT"}
    origin = "?return=1&from_game=buzzer&from_room=OLD"
    cases = [
        ("same game return", origin, old, None, []),
        ("same game reload", origin + "&nickname=Alice", old, None, []),
        ("legacy return link", "?return=1", old, None, []),
        ("normal rejoin", "?nickname=Alice", old, None, [old]),
        ("return flag is false", "?return=0&from_game=buzzer&from_room=OLD", old, None, [old]),
        ("new buzzer before reconnect", origin, new, None, [new]),
        ("new game type before reconnect", origin, other, None, [other]),
        ("queued host navigation survives old return", origin, old, new, [new]),
        ("no active game", origin, {}, None, []),
    ]
    playwright = None
    if browser is None:
        playwright, browser = start_chromium_browser(headless=True)
    page = browser.new_page()
    try:
        results = page.evaluate(r"""({handler, cases}) => {
          const run = new Function('data', 'search', 'pending', 'forwarded', `
            let joinInFlight = true;
            let currentNickname = '';
            let pendingNavigation = pending;
            const location = {search};
            const window = {location};
            const nicknameInput = {value: 'Alice'};
            const joinCard = {style: {display: ''}};
            const storeRejoinCredential = () => {};
            const renderNicknameStatus = () => {};
            const syncParticipantLobbyMetadata = () => {};
            const ws = {onmessage: event => forwarded.push(JSON.parse(event.data))};
            ${handler}
          `);
          return cases.map(([name, search, step, pending]) => {
            const forwarded = [];
            run({type:'lobby_join_success',nickname:'Alice',rejoin_token:'confirmed',
              ...step,step:Object.keys(step).length ? step : null}, search, pending, forwarded);
            return forwarded;
          });
        }""", {"handler": handler, "cases": cases})
        test_case.assertEqual(len(results), len(cases))
        for (name, _, _, _, expected), actual in zip(cases, results):
            with test_case.subTest(navigation_case=name):
                test_case.assertEqual(actual, [{"type": "navigate", "step": step} for step in expected])
    finally:
        page.close()
        if playwright is not None:
            browser.close()
            playwright.stop()


class BuzzerLobbyReturnBrowserTests(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        buzzer_tests.BuzzerEndFlowTests.setUp(self)
        self.hub = self.session
        self.hub_players = list(self.hub.participants.order_by("id"))
        self.client.force_login(self.user)

    def game_page(self):
        page = self.page(reverse("buzzer:play", args=[self.game.room_code, "Alice"])
                         + "?" + urlencode({"hub_session": self.hub.code}), player=self.hub_players[0], theme="vhs")
        page.wait_for_function("currentState?.game.status==='active' && socket?.readyState===1")
        return page

    def wait_in_lobby(self, page):
        page.wait_for_url("**/hub/lobby/**")
        page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        self.assertIn("/hub/lobby/", page.url)

    def test_rendered_navigation_decisions_cover_return_rejoin_and_new_instances(self):
        response = self.client.get(reverse("games_hub:lobby", args=[self.hub.code]), {"nickname": "Alice", "return": "1"})
        assert_lobby_join_navigation(self, response, browser=self.browser)

    def test_round_end_stays_in_game_full_end_returns_and_reload_rejoins_same_party(self):
        page = self.game_page()
        def end_round():
            game = BuzzerGame.objects.get(pk=self.game.pk)
            game.start_round(self.hub.code)
            self.assertTrue(game.end_current_round())
        self.db(end_round)
        # The live server has its own in-memory channel layer. Request the
        # committed state through its real socket rather than broadcasting
        # into the test process's unrelated in-memory layer.
        page.evaluate("send({type:'get_state'})")
        page.wait_for_function("currentState?.round.status==='ended'")
        self.assertIn("/buzzer/play/", page.url)
        self.assertTrue(page.locator("#returnToLobbyActions").evaluate("element => element.hidden"))
        def end_game():
            game = BuzzerGame.objects.get(pk=self.game.pk)
            game.end_quiz()
        self.db(end_game)
        page.evaluate("send({type:'get_state'})")
        page.wait_for_url("**/buzzer/result/**")
        page.click("#returnToLobbyBtn")
        self.wait_in_lobby(page)
        self.assertIn("from_game=buzzer", page.url)
        self.assertIn("from_room=" + self.game.room_code, page.url)
        page.reload(wait_until="domcontentloaded")
        self.wait_in_lobby(page)
        self.assertEqual(self.db(lambda: self.hub.participants.filter(nickname="Alice").get().pk), self.hub_players[0].pk)
        self.assertFalse(self.db(lambda: BuzzerParticipant.objects.get(pk=self.alice.pk).is_active))
        self.assertFalse(self.errors)

    def test_return_to_same_active_game_is_not_reentered_on_reload(self):
        page = self.game_page()
        page.evaluate("lobbyReturnController.returnToLobby()")
        self.wait_in_lobby(page)
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.reload(wait_until="domcontentloaded")
        self.wait_in_lobby(page)
        self.assertFalse(any("/buzzer/join/" in url for url in requests))
        self.assertFalse(self.db(lambda: BuzzerParticipant.objects.get(pk=self.alice.pk).is_active))
        self.assertFalse(self.errors)

    def test_normal_rejoin_without_return_marker_enters_current_game(self):
        page = self.page(reverse("games_hub:lobby", args=[self.hub.code]), player=self.hub_players[0])
        page.wait_for_url("**/buzzer/play/**")
        page.wait_for_function("currentState?.game.status==='active'")
        self.assertIn(self.game.room_code, page.url)
        self.assertEqual(self.db(lambda: self.hub.participants.filter(nickname="Alice").count()), 1)
        self.assertFalse(self.errors)

    def test_new_buzzer_before_lobby_connect_is_not_blocked_by_old_return_context(self):
        def next_game():
            old = BuzzerGame.objects.get(pk=self.game.pk)
            old.start_round(self.hub.code)
            old.participants.filter(pk=self.alice.pk).update(total_score=9)
            old.end_quiz()
            old.participants.update(is_active=False)
            game = BuzzerGame.objects.create(creator=self.user, title="Next Buzzer")
            step = HubGameStep.objects.create(session=self.hub, game_key="buzzer", room_code=game.room_code, order=1)
            HubGameParticipantSnapshot.create_for_step(step)
            game.start_quiz(self.hub.code)
            return game.pk, game.room_code
        game_id, room = self.db(next_game)
        path = reverse("games_hub:lobby", args=[self.hub.code]) + "?" + urlencode({
            "return": "1", "from_game": "buzzer", "from_room": self.game.room_code,
        })
        page = self.page(path, player=self.hub_players[0])
        page.wait_for_url("**/buzzer/play/" + room + "/**")
        page.wait_for_function("id=>currentState?.game.id===id && currentState.round.number===0", arg=game_id)
        self.assertNotIn("from_room", page.url)
        self.assertFalse(self.db(lambda: BuzzerParticipant.objects.get(pk=self.alice.pk).is_active))
        self.assertEqual(self.db(lambda: BuzzerParticipant.objects.get(quiz_id=game_id, name="Alice").total_score), 0)
        self.assertEqual(self.db(lambda: BuzzerParticipant.objects.get(pk=self.alice.pk).total_score), 9)
        self.assertFalse(self.errors)
