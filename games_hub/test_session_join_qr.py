import os
from pathlib import Path
from unittest import mock

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from django.conf import settings
from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from games_hub.models import HubSession
from games_hub.playwright_e2e import start_chromium_browser
from games_hub.templatetags.session_game_tags import session_join_qr_svg


class SessionJoinQrTests(TestCase):
    def setUp(self):
        self.session_a = HubSession.objects.create(code="QRA001", name="QR A")
        self.session_b = HubSession.objects.create(code="QRB002", name="QR B")

    def _absolute_lobby_url(self, session):
        return f"http://testserver{reverse('games_hub:lobby', args=[session.code])}"

    def test_spectator_uses_one_absolute_url_for_qr_and_visible_link(self):
        response = self.client.get(
            reverse("games_hub:spectate_session", args=[self.session_a.code])
        )
        join_url = self._absolute_lobby_url(self.session_a)
        content = response.content.decode("utf-8")

        self.assertEqual(response.context["join_url"], join_url)
        self.assertContains(response, f'data-qr-payload="{join_url}"', html=False)
        self.assertContains(response, f'href="{join_url}"', html=False)
        self.assertLess(
            content.index("data-session-join-qr"),
            content.index("data-session-join-url"),
        )
        self.assertIn("Teilnahme-QR-Code", content)
        self.assertContains(response, "data-session-join-disclosure")
        self.assertNotIn("data-session-join-disclosure open", content)
        self.assertIn("waitingMarkup(message, showJoin = true)", content)
        self.assertIn(
            "waitingMarkup('Fuer diesen Spieltyp fehlen oeffentliche Spectator-Daten.', false)",
            content,
        )

    def test_participant_replaces_share_link_with_qr_then_same_link(self):
        response = self.client.get(
            reverse("games_hub:lobby", args=[self.session_a.code])
        )
        join_url = self._absolute_lobby_url(self.session_a)
        content = response.content.decode("utf-8")

        self.assertEqual(response.context["join_url"], join_url)
        self.assertContains(response, "data-participant-lobby-share")
        self.assertContains(response, "data-session-join-toggle")
        self.assertContains(response, "Teilnahme-QR-Code")
        self.assertContains(response, f'data-qr-payload="{join_url}"', html=False)
        self.assertContains(response, f'href="{join_url}"', html=False)
        self.assertLess(
            content.index("data-session-join-qr"),
            content.index("data-session-join-url"),
        )
        self.assertNotIn("<code data-participant-lobby-share-value>", content)

    def test_sessions_generate_distinct_qr_codes_for_their_own_urls(self):
        responses = [
            self.client.get(reverse("games_hub:spectate_session", args=[session.code]))
            for session in (self.session_a, self.session_b)
        ]
        join_urls = [
            self._absolute_lobby_url(session)
            for session in (self.session_a, self.session_b)
        ]

        for response, join_url in zip(responses, join_urls):
            self.assertEqual(response.context["join_url"], join_url)
            self.assertContains(response, f'data-qr-payload="{join_url}"', html=False)
        self.assertNotEqual(
            responses[0].context["join_url"],
            responses[1].context["join_url"],
        )

    def test_qr_generator_encodes_the_supplied_join_url_exactly(self):
        join_url = self._absolute_lobby_url(self.session_a)
        with mock.patch("qrcode.QRCode.add_data", autospec=True) as add_data:
            session_join_qr_svg(join_url)

        self.assertEqual(add_data.call_args.args[1], join_url)

    def test_qr_render_failure_keeps_both_views_available_with_link(self):
        with mock.patch(
            "qrcode.QRCode.make", side_effect=RuntimeError("render failed")
        ):
            spectator = self.client.get(
                reverse("games_hub:spectate_session", args=[self.session_a.code])
            )
            participant = self.client.get(
                reverse("games_hub:lobby", args=[self.session_a.code])
            )

        join_url = self._absolute_lobby_url(self.session_a)
        for response in (spectator, participant):
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "data-session-join-qr")
            self.assertContains(response, f'href="{join_url}"', html=False)

    def test_qr_css_guards_quiet_zone_and_mobile_overflow(self):
        css = (
            Path(settings.BASE_DIR) / "static" / "css" / "session_join_qr.css"
        ).read_text(encoding="utf-8")

        self.assertIn("background: #fff", css)
        self.assertIn("align-items: baseline", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn(".session-join-qr[open]", css)
        self.assertIn("width: min(100%, var(--session-join-qr-size))", css)
        self.assertIn("--session-join-qr-size: clamp(260px, 18vw, 360px)", css)


class SessionJoinQrBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.playwright, cls.browser = start_chromium_browser(headless=True)
            cls.playwright_error = ""
        except Exception as exc:  # pragma: no cover - environment-specific skip
            cls.playwright = None
            cls.browser = None
            cls.playwright_error = str(exc)

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.close()
        if cls.playwright:
            cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        self.session = HubSession.objects.create(code="QRBROW", name="QR Browser")

    def _page(self, viewport):
        if not self.browser:
            self.skipTest(
                f"Playwright/Chromium nicht verfuegbar: {self.playwright_error}"
            )
        context = self.browser.new_context(viewport=viewport)
        return context, context.new_page()

    def test_spectator_qr_geometry_at_large_screen_sizes(self):
        url = f"{self.live_server_url}{reverse('games_hub:spectate_session', args=[self.session.code])}"
        for width, height in ((1920, 1080), (2560, 1440), (3840, 2160)):
            with self.subTest(viewport=(width, height)):
                context, page = self._page({"width": width, "height": height})
                try:
                    page.goto(url)
                    disclosure = page.locator(".spectator-join-area [data-session-join-disclosure]")
                    toggle = disclosure.locator("[data-session-join-toggle]")
                    page.wait_for_selector(".spectator-join-area [data-session-join-toggle]")
                    self.assertFalse(disclosure.get_attribute("open") is not None)
                    self.assertFalse(page.locator(".session-join-qr__code").is_visible())
                    self.assertTrue(toggle.inner_text().endswith("▼"))
                    toggle.click()
                    self.assertTrue(toggle.inner_text().endswith("▲"))
                    qr_box = page.locator(".session-join-qr__code").bounding_box()
                    link_box = page.locator(".session-join-qr__link").bounding_box()
                    label_box = page.locator(".session-join-qr__label").bounding_box()

                    self.assertIsNotNone(qr_box)
                    self.assertIsNotNone(link_box)
                    self.assertAlmostEqual(qr_box["width"], qr_box["height"], delta=1)
                    self.assertGreaterEqual(qr_box["width"], 260)
                    self.assertLessEqual(qr_box["width"], 360)
                    self.assertGreaterEqual(
                        link_box["y"], qr_box["y"] + qr_box["height"]
                    )
                    self.assertLess(abs(label_box["y"] - link_box["y"]), 12)
                    toggle.click()
                    self.assertFalse(page.locator(".session-join-qr__code").is_visible())
                    page.reload()
                    page.wait_for_selector(".spectator-join-area [data-session-join-toggle]")
                    self.assertFalse(disclosure.get_attribute("open") is not None)
                    self.assertLessEqual(
                        page.evaluate("document.body.scrollWidth"),
                        page.evaluate("window.innerWidth"),
                    )
                finally:
                    page.close()
                    context.close()

    def test_participant_qr_geometry_at_mobile_sizes(self):
        url = f"{self.live_server_url}{reverse('games_hub:lobby', args=[self.session.code])}"
        for width in (360, 390, 430):
            with self.subTest(viewport=width):
                context, page = self._page({"width": width, "height": 900})
                try:
                    page.goto(url)
                    disclosure = page.locator(
                        "[data-participant-lobby-share] [data-session-join-disclosure]"
                    )
                    toggle = disclosure.locator("[data-session-join-toggle]")
                    page.wait_for_selector(
                        "[data-participant-lobby-share] [data-session-join-toggle]"
                    )
                    self.assertFalse(disclosure.get_attribute("open") is not None)
                    self.assertFalse(page.locator(".session-join-qr__code").is_visible())
                    self.assertTrue(toggle.inner_text().endswith("▼"))
                    toggle.click()
                    self.assertTrue(toggle.inner_text().endswith("▲"))
                    qr_box = page.locator(".session-join-qr__code").bounding_box()
                    link_box = page.locator(".session-join-qr__link").bounding_box()
                    label_box = page.locator(".session-join-qr__label").bounding_box()

                    self.assertIsNotNone(qr_box)
                    self.assertIsNotNone(link_box)
                    self.assertAlmostEqual(qr_box["width"], qr_box["height"], delta=1)
                    self.assertLessEqual(qr_box["width"], width)
                    self.assertGreaterEqual(
                        link_box["y"], qr_box["y"] + qr_box["height"]
                    )
                    self.assertLess(abs(label_box["y"] - link_box["y"]), 12)
                    self.assertEqual(
                        page.locator(".session-join-qr__link").get_attribute("href"),
                        url,
                    )
                    toggle.click()
                    self.assertFalse(page.locator(".session-join-qr__code").is_visible())
                    page.reload()
                    page.wait_for_selector(
                        "[data-participant-lobby-share] [data-session-join-toggle]"
                    )
                    self.assertFalse(disclosure.get_attribute("open") is not None)
                    self.assertLessEqual(
                        page.evaluate("document.body.scrollWidth"),
                        page.evaluate("window.innerWidth"),
                    )
                finally:
                    page.close()
                    context.close()

    def test_participant_qr_toggle_at_desktop_size(self):
        url = f"{self.live_server_url}{reverse('games_hub:lobby', args=[self.session.code])}"
        context, page = self._page({"width": 1280, "height": 900})
        try:
            page.goto(url)
            disclosure = page.locator(
                "[data-participant-lobby-share] [data-session-join-disclosure]"
            )
            toggle = disclosure.locator("[data-session-join-toggle]")
            page.wait_for_selector(
                "[data-participant-lobby-share] [data-session-join-toggle]"
            )

            self.assertFalse(disclosure.get_attribute("open") is not None)
            self.assertEqual(toggle.inner_text().split(), ["TEILNAHME-QR-CODE", "▼"])
            toggle.click()
            self.assertTrue(page.locator(".session-join-qr__code").is_visible())
            self.assertEqual(toggle.inner_text().split(), ["TEILNAHME-QR-CODE", "▲"])
            self.assertLessEqual(
                page.evaluate("document.body.scrollWidth"),
                page.evaluate("window.innerWidth"),
            )
            toggle.click()
            self.assertFalse(page.locator(".session-join-qr__code").is_visible())
        finally:
            page.close()
            context.close()
