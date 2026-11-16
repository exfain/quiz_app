from django.urls import reverse
from pathlib import Path
import tempfile

from games_hub.test_session_repairs_browser import SessionBrowserBase
from . import test_live as fixtures
from . import runtime as r


class WhereLiveBrowserTests(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        self.db(lambda: fixtures.WhereLiveTests.setUp(self))
        self.client.force_login(self.user)

    def host_page(self):
        page = self.page(reverse('admin_dashboard:where_monitor', args=[self.game.room_code]) + '?hub_session=' + self.hub.code, host=True)
        page.wait_for_selector('#endQuestionBtn:not([disabled])')
        page.wait_for_selector('#whereMap[data-loaded="true"]')
        return page

    def player_page(self, index=0, theme='standard'):
        player = self.players[index]
        page = self.page(reverse('where_is_this:play', args=[self.game.room_code, player.name]) + '?hub_session=' + self.hub.code,
            player=self.hub.participants.get(nickname=player.name), theme=theme)
        page.wait_for_selector('#whereMap[data-loaded="true"]')
        return page

    def pin(self, page, x=.55, y=.5):
        box = page.locator('#whereMap').bounding_box()
        page.mouse.click(box['x'] + box['width'] * x, box['y'] + box['height'] * y)
        page.wait_for_selector('#submitAnswerBtn:not([disabled])')
        for _ in range(60):
            if self.db(lambda: bool(self.game.session.pending_answers)):
                break
            page.wait_for_timeout(50)

    def spectator_page(self):
        page = self.page(reverse('games_hub:spectate_session', args=[self.hub.code]))
        page.wait_for_selector('#whereSpectatorMap[data-loaded="true"]')
        return page

    def test_both_themes_confirm_or_keep_draft_and_reveal_before_final_result(self):
        host = self.host_page()
        players = [self.player_page(), self.player_page(1, 'vhs')]
        spectator = self.spectator_page()
        self.assertEqual(spectator.locator('[data-marker="target"]').count(), 0)
        defaults = [page.locator('#whereMap svg').get_attribute('viewBox') for page in players]
        for page in players:
            page.click('#zoomIn')
            self.pin(page)
        players[0].click('#submitAnswerBtn')
        players[0].wait_for_function("document.getElementById('whereMessage').textContent.includes('eingeloggt')")
        players[1].wait_for_timeout(300)
        players[1].reload()
        players[1].wait_for_selector('#whereMap[data-loaded="true"]')
        players[1].wait_for_selector('#submitAnswerBtn:not([disabled])')
        self.assertEqual(self.db(lambda: self.game.where_answers.count()), 1)
        host.click('#endQuizBtn')
        for i, page in enumerate(players):
            page.wait_for_selector('#whereMap [data-marker="target"]')
            page.wait_for_selector('#continueRevealBtn:not([hidden])')
            self.assertEqual(page.locator('#whereMap svg').get_attribute('viewBox'), defaults[i])
            self.assertGreater(page.locator('[data-distance-label]').count(), 0)
            page.click('#continueRevealBtn')
            self.assertEqual(page.inner_text('#whereMessage'), 'Gesamtergebnis')
        spectator.wait_for_selector('#whereSpectatorMap [data-marker="target"]')
        self.assertEqual(spectator.locator('[data-marker="guess"], [data-marker="winner"]').count(), 2)
        self.assertEqual(spectator.locator('[data-distance-line]').count(), 1)
        self.assertEqual(spectator.locator('[data-team-label="winner"]').count(), 1)
        self.assertEqual(self.db(lambda: self.game.where_answers.count()), 2)
        self.assertFalse(self.errors)

    def test_zones_are_only_visible_at_reveal_on_player_and_spectator(self):
        def configure():
            r.reveal(self.game)
            self.game.scoring_mode = 'zones'
            self.game.save(update_fields=['scoring_mode'])
            r.send_question(self.game, self.q)
        self.db(configure)
        host, player, spectator = self.host_page(), self.player_page(theme='vhs'), self.spectator_page()
        self.pin(player)
        self.assertEqual(spectator.locator('[data-zone-radius]').count(), 0)
        host.click('#endQuestionBtn')
        spectator.wait_for_selector('[data-zone-radius]')
        player.wait_for_selector('[data-zone-radius]')
        self.assertEqual(spectator.locator('[data-zone-radius]').count(), 5)
        self.assertEqual(spectator.locator('[data-distance-line]').count(), 0)
        self.assertEqual(player.locator('[data-distance-line]').count(), 1)
        spectator.locator('#whereSpectatorMap').screenshot(path=str(Path(tempfile.gettempdir()) / 'where-zones-spectator.png'))
        self.assertFalse(self.errors)

    def test_world_wrap_and_all_three_map_projections_round_trip(self):
        host = self.host_page()
        player = self.player_page()
        for region in ['germany', 'europe', 'world_mercator']:
            if region != 'germany':
                host.click('#endQuestionBtn')
                host.wait_for_selector('#sendQuestionBtn:not([disabled])')
                def configure():
                    self.q.map_type = region
                    self.q.save(update_fields=['map_type'])
                self.db(configure)
                host.select_option('#questionSelect', str(self.q.pk))
                host.click('#sendQuestionBtn')
            player.wait_for_selector(f'#whereMap[data-loaded="true"] [data-map-region="{region}"]', state='attached')
            if region == 'world_mercator':
                box = player.locator('#whereMap').bounding_box()
                for _ in range(4):
                    player.mouse.move(box['x']+box['width']*.8,box['y']+box['height']*.5)
                    player.mouse.down()
                    player.mouse.move(box['x']+box['width']*.2,box['y']+box['height']*.5,steps=8)
                    player.mouse.up()
            self.pin(player)
            player.locator('#whereMap').screenshot(path=str(Path(tempfile.gettempdir()) / f'where-map-{region}.png'))
            if region == 'germany':
                from PIL import Image
                with Image.open(Path(tempfile.gettempdir()) / 'where-map-germany.png') as screenshot:
                    pixels = list(screenshot.convert('RGB').getdata())
                    self.assertGreater(pixels.count((54, 202, 56)), len(pixels) * .15)
                    self.assertLess(pixels.count((18, 21, 23)), len(pixels) * .02)
                self.assertEqual(player.locator('[data-germany-border]').count(), 3)
            self.assertGreaterEqual(float(player.locator('#whereMap [data-marker="own"]').first.get_attribute('cx')), -10)
            state = self.db(lambda: r.state(self.game, self.hub.code, self.players[0]))
            point = state['pending_input']
            self.assertTrue(0 <= point['x_norm'] <= 1 and 0 <= point['y_norm'] <= 1)
        self.assertFalse(self.errors)

    def test_native_hub_start_routes_both_lobby_teams_and_opens_same_question(self):
        from games_hub.models import HubGameStep, HubSession, HubParticipant
        from .models import WhereQuiz
        from django.utils import timezone
        def prepare():
            self.hub = HubSession.objects.create(code='NEWWHERE', creator=self.user, is_active=True,
                started_at=timezone.now(), check_in_status='completed', locked_participant_count=2)
            self.game = WhereQuiz.objects.create(creator=self.user, zone_count=5, zone_width_km=10)
            self.game.selected_questions.add(self.q)
            HubGameStep.objects.create(session=self.hub, game_key='where', room_code=self.game.room_code, order=0)
            return [HubParticipant.objects.create(session=self.hub, nickname=name, scoring_eligible=True) for name in ['Alpha', 'Beta']]
        participants = self.db(prepare)
        pages = [self.page(reverse('games_hub:lobby', args=[self.hub.code]), player=p,
                           theme='vhs' if i else 'standard') for i, p in enumerate(participants)]
        for page in pages:
            page.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host = self.page(reverse('admin_dashboard:where_monitor', args=[self.game.room_code]) + '?hub_session=' + self.hub.code, host=True)
        host.wait_for_selector('#startQuizBtn:not([disabled])')
        host.click('#startQuizBtn')
        host.wait_for_selector('#sendQuestionBtn:not([disabled])')
        host.click('#sendQuestionBtn')
        for page in pages:
            page.wait_for_url('**/where/play/**')
            page.wait_for_selector('#whereMap[data-loaded="true"]')
            page.wait_for_function("document.getElementById('questionText').textContent.includes('Berlin')")
        self.assertEqual(self.db(lambda: self.game.participants.count()), 2)
        self.assertFalse(self.errors)

    def test_saved_marker_waits_after_deadline_until_host_reconnects(self):
        def shorten():
            r.reveal(self.game)
            r.send_question(self.game, self.q, time_limit=7)
        self.db(shorten)
        player = self.player_page(theme='vhs')
        self.pin(player)
        player.wait_for_timeout(8000)
        self.assertEqual(self.db(lambda: self.game.where_answers.count()), 0)
        self.assertEqual(player.locator('[data-marker="target"]').count(), 0)
        host = self.page(reverse('admin_dashboard:where_monitor', args=[self.game.room_code]) + '?hub_session=' + self.hub.code, host=True)
        player.wait_for_selector('[data-marker="target"]')
        self.assertEqual(self.db(lambda: self.game.where_answers.count()), 1)
        self.assertFalse(self.errors)
