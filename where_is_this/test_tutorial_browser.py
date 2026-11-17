from django.urls import reverse
from games_hub.test_session_repairs_browser import SessionBrowserBase
from . import test_tutorial_live as tutorial_fixture


class WhereTutorialBrowserTests(SessionBrowserBase):
    def setUp(self):
        super().setUp();self.db(lambda:tutorial_fixture.WhereTutorialTransportTests.setUp(self))
        self.client.force_login(self.user)

    def test_hub_start_explanation_rejoin_tutorial_without_points_and_normal_question(self):
        party=self.db(lambda:self.hub.participants.get(nickname='Alpha'))
        player=self.page(reverse('games_hub:lobby',args=[self.hub.code]),player=party)
        player.wait_for_function("document.getElementById('joinCard').style.display==='none'")
        host=self.page(reverse('games_hub:monitor',args=[self.hub.code]),host=True)
        host.click(f'.launch-btn[data-room-code="{self.game.room_code}"]')
        host.wait_for_url('**/where/**/monitor/**')
        host.check('#showTutorialToggle');host.check('#playTutorialToggle')
        host.wait_for_selector('#startQuizBtn:not([disabled])');host.click('#startQuizBtn')
        player.wait_for_url('**/where/play/**')
        player.wait_for_selector('#sessionGameStartIntro[hidden]',state='attached',timeout=25000)
        player.wait_for_selector('#gameTutorialOverlay.is-visible')
        self.assertIn('Karte ausprobieren',player.inner_text('#gameTutorialText'))
        player.click('#gameTutorialCloseBtn')
        player.wait_for_selector('#gameTutorialOverlay:not(.is-visible)',state='attached')
        # Wait for server acknowledgement, not merely the immediate local hide.
        from games_hub.tutorial_runtime import get_tutorial_payload
        import time
        end=time.monotonic()+10
        while self.db(lambda:get_tutorial_payload('where',self.game.room_code,self.hub.code,'Alpha')) is not None:
            self.assertLess(time.monotonic(),end);player.wait_for_timeout(50)
        player.reload();player.wait_for_selector('#whereWaiting:not([hidden])')
        self.assertFalse(player.locator('#gameTutorialOverlay').evaluate("el=>el.classList.contains('is-visible')"))
        host.click('#sendQuestionBtn')
        player.wait_for_function("document.getElementById('questionText').textContent==='Training Berlin'")
        player.wait_for_selector('#unitTutorialNotice:not(.d-none)')
        box=player.locator('#whereMap').bounding_box();player.mouse.click(box['x']+box['width']/2,box['y']+box['height']/2)
        player.wait_for_selector('#submitAnswerBtn:not([disabled])');player.click('#submitAnswerBtn')
        player.wait_for_function("document.getElementById('whereMessage').textContent.includes('eingeloggt')")
        host.click('#endQuestionBtn')
        host.wait_for_selector('#sendQuestionBtn:not([disabled])')
        self.assertEqual(self.db(lambda:self.game.participants.get(name='Alpha').total_score),0)
        self.assertEqual(player.locator('#whereScoreboard tbody tr').count(),0)
        host.click('#sendQuestionBtn')
        player.wait_for_function("document.getElementById('questionText').textContent==='Wertungsfrage'")
        self.assertFalse(player.locator('#unitTutorialNotice').is_visible())
        navigations=[];host.on('framenavigated',lambda frame:navigations.append(frame.url) if frame==host.main_frame else None)
        host.click('#endQuizBtn')
        host.wait_for_function("document.body.dataset.hostGameStatus==='completed'")
        self.assertFalse(navigations,navigations)
        player.wait_for_selector('#returnToLobbyBtn:not([disabled])');player.click('#returnToLobbyBtn')
        player.wait_for_url('**/hub/lobby/WTUTORIAL/**')
        self.assertFalse(self.errors,self.errors)
