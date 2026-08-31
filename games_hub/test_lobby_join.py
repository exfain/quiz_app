from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .lobby_join import (
    check_nickname_availability,
    issue_rejoin_token,
    join_lobby_participant,
)
from .models import HubParticipant, HubSession


class LobbyJoinServiceTests(TestCase):
    def setUp(self):
        self.session = HubSession.objects.create(code='JOIN01', name='Join test')

    def test_minimum_length_is_three_after_trimming(self):
        empty = join_lobby_participant(self.session.code, '   ')
        too_short = join_lobby_participant(self.session.code, ' ab ')
        valid = join_lobby_participant(self.session.code, ' abc ')

        self.assertFalse(empty['success'])
        self.assertEqual(empty['code'], 'name_required')
        self.assertFalse(too_short['success'])
        self.assertEqual(too_short['code'], 'name_too_short')
        self.assertTrue(valid['success'])
        self.assertEqual(valid['nickname'], 'abc')

    def test_existing_name_requires_matching_signed_rejoin_token(self):
        first_join = join_lobby_participant(self.session.code, 'Alice')

        without_token = check_nickname_availability(self.session.code, 'Alice')
        wrong_token = check_nickname_availability(
            self.session.code,
            'Alice',
            'not-a-valid-token',
        )
        rejoin = check_nickname_availability(
            self.session.code,
            'alice',
            first_join['rejoin_token'],
        )

        self.assertEqual(without_token['status'], 'taken')
        self.assertEqual(wrong_token['status'], 'taken')
        self.assertEqual(rejoin['status'], 'rejoin')
        self.assertEqual(rejoin['nickname'], 'Alice')

    def test_token_is_bound_to_session_and_participant(self):
        alice = HubParticipant.objects.create(session=self.session, nickname='Alice')
        bob = HubParticipant.objects.create(session=self.session, nickname='Bob')
        other_session = HubSession.objects.create(code='JOIN02', name='Other session')
        other_alice = HubParticipant.objects.create(session=other_session, nickname='Alice')
        alice_token = issue_rejoin_token(alice)

        self.assertEqual(
            check_nickname_availability(self.session.code, 'Bob', alice_token)['status'],
            'taken',
        )
        self.assertEqual(
            check_nickname_availability(other_session.code, 'Alice', alice_token)['status'],
            'taken',
        )
        self.assertEqual(
            check_nickname_availability(
                other_session.code,
                'Alice',
                issue_rejoin_token(other_alice),
            )['status'],
            'rejoin',
        )
        self.assertNotEqual(alice.pk, bob.pk)

    def test_final_join_rechecks_availability(self):
        availability = check_nickname_availability(self.session.code, 'Alice')
        first_join = join_lobby_participant(self.session.code, 'Alice')
        stale_second_join = join_lobby_participant(self.session.code, 'Alice')

        self.assertEqual(availability['status'], 'available')
        self.assertTrue(first_join['success'])
        self.assertFalse(stale_second_join['success'])
        self.assertEqual(stale_second_join['code'], 'nickname_taken')
        self.assertEqual(
            HubParticipant.objects.filter(session=self.session, nickname='Alice').count(),
            1,
        )

    def test_join_session_view_enforces_same_minimum(self):
        too_short = self.client.post(reverse('games_hub:join_session'), {
            'code': self.session.code,
            'nickname': 'ab',
        })
        valid = self.client.post(reverse('games_hub:join_session'), {
            'code': self.session.code,
            'nickname': 'abc',
        })

        self.assertContains(too_short, 'Der Name ist zu kurz.')
        self.assertRedirects(
            valid,
            f"{reverse('games_hub:lobby', args=[self.session.code])}?nickname=abc",
            fetch_redirect_response=False,
        )

    def test_lobby_template_has_neutral_initial_state_and_stale_response_guard(self):
        response = self.client.get(reverse('games_hub:lobby', args=[self.session.code]))

        self.assertContains(response, 'placeholder="Name eingeben"')
        self.assertContains(response, 'Dieser Name ist bereits vergeben.')
        self.assertContains(response, 'Willkommen zurück! Als „${data.nickname}“ fortfahren?')
        self.assertContains(response, 'requestId !== latestNicknameRequestId')
        self.assertContains(response, "candidate_name: nickname")
        self.assertNotContains(response, 'Enter a nickname')
        self.assertNotContains(response, 'Your nickname')
        self.assertNotContains(response, 'Nickname too short')
        self.assertNotContains(response, '✓ Available')

    def test_vhs_join_area_reuses_existing_theme_tokens_without_outer_card(self):
        project_root = Path(__file__).resolve().parents[1]
        css = (project_root / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(
            encoding='utf-8'
        )

        self.assertIn('.vhs-lobby-root #joinCard {', css)
        self.assertIn('background: transparent !important;', css)
        self.assertIn('color: var(--vhs-orange) !important;', css)
        self.assertIn('color: var(--accent) !important;', css)
        self.assertIn('color: var(--vhs-muted) !important;', css)
        self.assertIn('font: 700 11px/1.35 "Courier New"', css)
        self.assertIn('#joinBtn.vhs-lobby-ready-button:not(:disabled):active', css)
        self.assertIn('#joinBtn.vhs-lobby-ready-button:disabled', css)


class LobbyJoinConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.session = HubSession.objects.create(code='RACE01', name='Race test')

    def test_simultaneous_join_with_same_name_creates_one_participant(self):
        barrier = Barrier(2)

        def join():
            close_old_connections()
            barrier.wait(timeout=5)
            try:
                return join_lobby_participant(self.session.code, 'Alice')
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: join(), range(2)))

        self.assertEqual(sum(bool(result['success']) for result in results), 1)
        self.assertEqual(
            sum(result.get('code') == 'nickname_taken' for result in results),
            1,
        )
        self.assertEqual(
            HubParticipant.objects.filter(session=self.session, nickname='Alice').count(),
            1,
        )
