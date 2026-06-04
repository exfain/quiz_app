from urllib.parse import parse_qs, urlparse

from django.test import TestCase
from django.urls import reverse

from games_hub.models import HubSession


class RootEntryPointTests(TestCase):
    def test_root_renders_session_join_instead_of_legacy_game_overview(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Session beitreten")
        self.assertContains(response, 'name="code"')
        self.assertContains(response, 'name="nickname"')
        self.assertContains(response, reverse("admin_dashboard:home"))
        self.assertNotContains(response, "Choose Your Challenge")
        self.assertNotContains(response, reverse("quiz:join"))
        self.assertNotContains(response, reverse("assign:join"))
        self.assertNotContains(response, reverse("estimation:join"))
        self.assertNotContains(response, reverse("where_is_this:join"))
        self.assertNotContains(response, reverse("who_is_lying:join"))
        self.assertNotContains(response, reverse("who_is_that:join"))
        self.assertNotContains(response, reverse("black_jack_quiz:join"))

    def test_root_post_with_valid_session_code_uses_hub_lobby_flow(self):
        session = HubSession.objects.create(code="ABC123", name="Root Entry")

        response = self.client.post("/", {"code": "abc123", "nickname": "Anna"})

        self.assertEqual(response.status_code, 302)
        redirect = urlparse(response["Location"])
        self.assertEqual(redirect.path, f"/hub/lobby/{session.code}/")
        self.assertEqual(parse_qs(redirect.query), {"nickname": ["Anna"]})

    def test_root_post_with_invalid_session_code_shows_error(self):
        response = self.client.post("/", {"code": "NOPE", "nickname": "Anna"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Session nicht gefunden")
        self.assertContains(response, 'name="code"')
