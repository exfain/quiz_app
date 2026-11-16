from django.urls import reverse

from admin_dashboard.models import DashboardSettings
from .models import HubSession
from .test_session_repairs_browser import SessionBrowserBase
from . import test_manual_repairs as fixtures


class TextMatchingBrowserTests(SessionBrowserBase):
    def setUp(self):
        super().setUp()
        fixtures.ManualGameRepairs.setUp(self)

    def set_slider(self, page, selector, value):
        page.locator(selector).evaluate('(input,value)=>{input.value=value;input.dispatchEvent(new Event("input",{bubbles:true}));}', str(value))

    def test_global_warning_and_default_change_never_overwrite_running_session(self):
        page = self.page(reverse('admin_dashboard:settings'), host=True)
        self.assertEqual(page.input_value('#defaultTextMatchThreshold'), '90')
        self.assertIn('keine Auswirkungen', page.locator('.alert-warning[role="status"]').inner_text())
        self.set_slider(page, '#defaultTextMatchThreshold', 76)
        self.assertEqual(page.locator('output[for="defaultTextMatchThreshold"]').inner_text(), '76 %')
        page.locator('button[type="submit"]').click()
        page.wait_for_load_state('domcontentloaded')
        self.assertEqual(self.db(lambda: DashboardSettings.load().text_match_threshold), 76)
        self.assertEqual(self.db(lambda: HubSession.objects.get(pk=self.hub.pk).text_match_threshold), 90)
        self.assertIn('nicht betroffen', page.locator('body').inner_text())
        self.assertFalse(self.errors)

    def test_session_creation_slider_override_is_separate_from_default(self):
        page = self.page(reverse('games_hub:create_session'), host=True)
        self.set_slider(page, '#sessionTextMatchThreshold', 83)
        page.fill('#sessionName', 'Text Matching Session')
        page.fill('input[name="code"]', 'MATCHUI')
        page.locator(f'.game-instance-row[data-game-key="buzzer"][data-room-code="{self.buzzer.room_code}"]').click()
        page.click('#submitBtn')
        page.wait_for_url('**/sessions/**')
        self.assertEqual(self.db(lambda: HubSession.objects.get(code='MATCHUI').text_match_threshold), 83)
        self.assertEqual(self.db(lambda: DashboardSettings.load().text_match_threshold), 90)
        self.assertFalse(self.errors)

    def test_session_setting_saves_and_running_game_disables_slider_after_reload(self):
        path = reverse('games_hub:monitor', args=[self.hub.code])
        page = self.page(path, host=True)
        page.locator('[data-session-panel-target="scoringPanel"]').evaluate('(button)=>button.click()')
        self.set_slider(page, '#sessionTextMatchThreshold', 78)
        page.click('#saveTextMatchSettingsBtn')
        page.wait_for_function("document.getElementById('textMatchSettingsStatus').textContent.includes('gespeichert')")
        self.assertEqual(self.db(lambda: HubSession.objects.get(pk=self.hub.pk).text_match_threshold), 78)
        self.db(lambda: self.points.start_quiz(self.hub.code))
        page.reload()
        page.locator('[data-session-panel-target="scoringPanel"]').evaluate('(button)=>button.click()')
        self.assertTrue(page.locator('#sessionTextMatchThreshold').is_disabled())
        self.assertTrue(page.locator('#saveTextMatchSettingsBtn').is_disabled())
        self.assertFalse(self.errors)
