from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from games_hub.models import HubSession
from games_hub.views import monitor


class HubMonitorAccessTests(TestCase):
    def setUp(self):
        self.session = HubSession.objects.create(code='MONITOR1', name='Monitor')
        self.url = reverse('games_hub:monitor', args=[self.session.code])
        self.staff = User.objects.create_user(
            username='monitor_staff',
            password='testpass123',
            is_staff=True,
        )
        self.superuser = User.objects.create_superuser(
            username='monitor_admin',
            password='testpass123',
            email='',
        )
        self.regular_user = User.objects.create_user(
            username='monitor_other',
            password='testpass123',
        )

    def assert_login_redirect(self, response, requested_path):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f'{settings.LOGIN_URL}?next={requested_path}',
        )

    def test_anonymous_existing_monitor_redirects_before_querying_session(self):
        request = RequestFactory().get(self.url)
        request.user = SimpleLazyObject(lambda: AnonymousUser())

        with patch('games_hub.views.get_object_or_404') as get_object:
            response = monitor(request, self.session.code)

        self.assert_login_redirect(response, self.url)
        get_object.assert_not_called()

    def test_anonymous_unknown_monitor_has_same_controlled_response(self):
        unknown_url = reverse('games_hub:monitor', args=['UNKNOWN'])

        response = self.client.get(unknown_url)

        self.assert_login_redirect(response, unknown_url)

    def test_staff_user_can_open_monitor_from_dashboard_url(self):
        self.client.force_login(self.staff)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['session'], self.session)
        self.assertContains(response, self.session.code)

    def test_staff_login_returns_to_requested_monitor(self):
        anonymous_response = self.client.get(self.url)
        self.assert_login_redirect(anonymous_response, self.url)

        login_response = self.client.post(
            anonymous_response.url,
            {
                'username': self.staff.username,
                'password': 'testpass123',
            },
        )

        self.assertRedirects(
            login_response,
            self.url,
            fetch_redirect_response=False,
        )
        monitor_response = self.client.get(self.url)
        self.assertEqual(monitor_response.status_code, 200)
        self.assertEqual(monitor_response.context['session'], self.session)

    def test_superuser_can_open_monitor(self):
        self.client.force_login(self.superuser)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['session'], self.session)

    def test_authenticated_non_staff_user_cannot_open_monitor(self):
        self.client.force_login(self.regular_user)

        existing_response = self.client.get(self.url)
        unknown_response = self.client.get(
            reverse('games_hub:monitor', args=['UNKNOWN'])
        )

        self.assertEqual(existing_response.status_code, 404)
        self.assertEqual(unknown_response.status_code, 404)
