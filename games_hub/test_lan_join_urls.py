from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz
from games_hub.models import HubGameStep, HubSession
from games_hub.utils import build_share_url, get_server_ip, session_join_url


class LanAddressTests(SimpleTestCase):
    def test_loopback_urls_use_lan_ipv4_and_keep_actual_port(self):
        for host in ('127.0.0.1:8001', 'localhost:8001', '[::1]:8001', '0.0.0.0:8001'):
            with self.subTest(host=host), patch('games_hub.utils.get_server_ip', return_value='192.168.178.20'):
                request = RequestFactory().get('/', HTTP_HOST=host)
                self.assertEqual(session_join_url(request, 'LAN123'), 'http://192.168.178.20:8001/hub/lobby/LAN123/')

    def test_existing_lan_addresses_and_public_domains_are_preserved(self):
        for host in ('192.168.178.20:8002', 'quiz.example.org', '[2001:db8::1]:8443'):
            with self.subTest(host=host), patch('games_hub.utils.get_server_ip') as detect:
                request = RequestFactory().get('/', HTTP_HOST=host, secure=True)
                self.assertEqual(session_join_url(request, 'LAN123'), f'https://{host}/hub/lobby/LAN123/')
                detect.assert_not_called()

    def test_scheme_path_query_and_fragment_are_preserved(self):
        request = RequestFactory().get('/', HTTP_HOST='localhost:8443', secure=True)
        with patch('games_hub.utils.get_server_ip', return_value='192.168.178.20'):
            self.assertEqual(build_share_url(request, '/hub/lobby/LAN123/?nickname=Tim#join'),
                'https://192.168.178.20:8443/hub/lobby/LAN123/?nickname=Tim#join')

    def test_no_network_address_does_not_create_an_invalid_url(self):
        request = RequestFactory().get('/', HTTP_HOST='localhost:8001')
        for address in (None, '127.0.0.1', '0.0.0.0', '169.254.1.2'):
            with self.subTest(address=address), patch('games_hub.utils.get_server_ip', return_value=address):
                self.assertEqual(session_join_url(request, 'LAN123'), 'http://localhost:8001/hub/lobby/LAN123/')

    def test_primary_interface_address_is_detected(self):
        with patch('games_hub.utils.socket.socket') as factory:
            probe = factory.return_value.__enter__.return_value
            probe.getsockname.return_value = ('192.168.178.20', 12345)
            self.assertEqual(get_server_ip(default=None), '192.168.178.20')

    def test_lan_address_is_detected_without_an_internet_route(self):
        addresses = [(None, None, None, None, (address, 0)) for address in (
            '127.0.0.1', '169.254.10.20', '192.168.178.20')]
        with patch('games_hub.utils.socket.socket', side_effect=OSError('No route')), patch(
            'games_hub.utils.socket.getaddrinfo', return_value=addresses,
        ):
            self.assertEqual(get_server_ip(default=None), '192.168.178.20')

    def test_address_detection_failure_returns_the_requested_fallback(self):
        with patch('games_hub.utils.socket.socket', side_effect=OSError('No network')), patch(
            'games_hub.utils.socket.getaddrinfo', side_effect=OSError('No addresses'),
        ):
            self.assertIsNone(get_server_ip(default=None))


class LanJoinLinkTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='lan-host', is_staff=True, is_superuser=True)
        self.session = HubSession.objects.create(code='LAN123', creator=self.user, started_at=timezone.now())
        self.quiz = Quiz.objects.create(title='LAN quiz', creator=self.user)
        HubGameStep.objects.create(session=self.session, order=0, game_key='quiz', room_code=self.quiz.room_code)
        self.detect = patch('games_hub.utils.get_server_ip', return_value='192.168.178.20')
        self.detect.start()
        self.addCleanup(self.detect.stop)
        self.expected = 'http://192.168.178.20:8001/hub/lobby/LAN123/'

    def test_lobby_and_spectator_use_identical_lan_url_in_link_and_encoded_qr(self):
        import qrcode
        original_add_data = qrcode.QRCode.add_data
        for route in ('games_hub:lobby', 'games_hub:spectate_session'):
            with self.subTest(route=route), patch('qrcode.QRCode.add_data', autospec=True, side_effect=original_add_data) as add_data:
                response = self.client.get(reverse(route, args=[self.session.code]), HTTP_HOST='127.0.0.1:8001')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['join_url'], self.expected)
                self.assertContains(response, f'data-qr-payload="{self.expected}"')
                self.assertContains(response, f'href="{self.expected}"')
                self.assertEqual(add_data.call_args.args[1], self.expected)

    def test_hub_monitor_copies_lan_url_with_the_actual_port(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('games_hub:monitor', args=[self.session.code]), HTTP_HOST='127.0.0.1:8001')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{self.expected}"')
        self.assertNotContains(response, '192.168.178.20:8000')

    def test_session_overview_link_and_copy_button_use_same_lan_url(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('admin_dashboard:sessions_overview'), HTTP_HOST='127.0.0.1:8001')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{self.expected}"')
        self.assertContains(response, f'data-url="{self.expected}"')

    def test_game_monitor_uses_same_session_link(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('admin_dashboard:quiz_monitor', args=[self.quiz.room_code]),
            {'hub_session': self.session.code}, HTTP_HOST='127.0.0.1:8001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['lobby_url'], self.expected)
        self.assertContains(response, f'data-url="{self.expected}"')
