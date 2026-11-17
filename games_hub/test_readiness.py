import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.urls import reverse
from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.utils import timezone

from games_hub.consumers import HubConsumer
from games_hub.models import HubGameStep, HubParticipant, HubReadinessCheck, HubSession
from games_hub.readiness import (
    end_readiness_check,
    get_readiness_state,
    mark_participant_ready,
    start_readiness_check,
)


class ReadinessCheckStateTests(TestCase):
    def setUp(self):
        self.session = HubSession.objects.create(code='READY1', name='Readiness')
        self.alice = HubParticipant.objects.create(session=self.session, nickname='Alice')
        self.bob = HubParticipant.objects.create(session=self.session, nickname='Bob')

    def test_get_state_without_active_check_returns_neutral_state(self):
        state = get_readiness_state(self.session)

        self.assertEqual(state['readiness_check'], {'active': False, 'id': None})
        self.assertEqual(state['readiness_participants'], [])
        self.assertEqual(state['readiness_counts'], {'total': 0, 'ready': 0, 'pending': 0})

    def test_start_readiness_check_initializes_active_participants_as_pending(self):
        result = start_readiness_check(self.session)

        self.assertTrue(result['success'])
        self.assertTrue(result['readiness_check']['active'])
        self.assertEqual(result['readiness_counts'], {'total': 2, 'ready': 0, 'pending': 2})
        self.assertEqual(
            [participant['nickname'] for participant in result['readiness_participants']],
            ['Alice', 'Bob'],
        )
        self.assertFalse(any(participant['ready'] for participant in result['readiness_participants']))

    def test_participant_ready_keeps_pending_participants_sorted_first(self):
        start_readiness_check(self.session)

        result = mark_participant_ready(self.session, 'Bob')

        self.assertTrue(result['success'])
        self.assertEqual(result['readiness_counts'], {'total': 2, 'ready': 1, 'pending': 1})
        self.assertEqual(
            [(participant['nickname'], participant['ready']) for participant in result['readiness_participants']],
            [('Alice', False), ('Bob', True)],
        )

    def test_all_ready_keeps_check_active_until_host_ends_it(self):
        start_readiness_check(self.session)
        mark_participant_ready(self.session, 'Alice')
        result = mark_participant_ready(self.session, 'Bob')

        self.assertEqual(result['readiness_counts']['pending'], 0)
        self.assertTrue(result['readiness_check']['active'])

        end_result = end_readiness_check(self.session)

        self.assertTrue(end_result['success'])
        self.assertFalse(end_result['readiness_check']['active'])

    def test_end_requires_confirmation_when_participants_are_pending(self):
        start_readiness_check(self.session)
        mark_participant_ready(self.session, 'Alice')

        blocked = end_readiness_check(self.session)

        self.assertFalse(blocked['success'])
        self.assertTrue(blocked['requires_confirmation'])
        self.assertEqual(blocked['readiness_counts']['pending'], 1)
        self.assertTrue(HubReadinessCheck.objects.get(session=self.session).active)

        forced = end_readiness_check(self.session, force=True)

        self.assertTrue(forced['success'])
        self.assertFalse(forced['readiness_check']['active'])

    def test_reload_state_reconstructs_active_check_and_ready_status(self):
        start_readiness_check(self.session)
        mark_participant_ready(self.session, 'Alice')

        state = get_readiness_state(HubSession.objects.get(pk=self.session.pk))

        self.assertTrue(state['readiness_check']['active'])
        by_name = {participant['nickname']: participant for participant in state['readiness_participants']}
        self.assertTrue(by_name['Alice']['ready'])
        self.assertFalse(by_name['Bob']['ready'])


class ReadinessCheckConsumerTests(TransactionTestCase):
    def setUp(self):
        self.session = HubSession.objects.create(code='READY2', name='Readiness Consumer')
        HubParticipant.objects.create(session=self.session, nickname='Alice')
        HubParticipant.objects.create(session=self.session, nickname='Bob')
        self.consumer = HubConsumer()
        self.consumer.session_code = self.session.code
        self.consumer.group_name = f'hub_{self.session.code}'
        self.consumer.hub_participant_id = HubParticipant.objects.get(
            session=self.session,
            nickname='Alice',
        ).id
        self.consumer.hub_participant_name = 'Alice'
        self.consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
        self.consumer.send = AsyncMock()

    def test_running_game_returns_error_to_host_without_broadcasting_overlay(self):
        from QuizGame.models import Quiz
        host = User.objects.create_user('readiness-host')
        game = Quiz.objects.create(creator=host, status='active')
        HubGameStep.objects.create(session=self.session, game_key='quiz', room_code=game.room_code, order=0)
        async_to_sync(self.consumer.handle_start_readiness_check)()
        self.consumer.channel_layer.group_send.assert_not_awaited()
        payload = json.loads(self.consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(payload['type'], 'readiness_error')
        self.assertIn('zwischen Spielen', payload['error'])
        self.assertFalse(self.session.readiness_checks.exists())

    def test_start_and_confirm_broadcast_readiness_updates(self):
        async_to_sync(self.consumer.handle_start_readiness_check)()

        self.consumer.channel_layer.group_send.assert_awaited_once()
        group_name, event = self.consumer.channel_layer.group_send.await_args.args
        self.assertEqual(group_name, self.consumer.group_name)
        self.assertEqual(event['type'], 'readiness_update')
        self.assertEqual(event['event_type'], 'readiness_started')
        self.assertEqual(event['state']['readiness_counts']['pending'], 2)

        self.consumer.channel_layer.group_send.reset_mock()
        async_to_sync(self.consumer.handle_participant_ready)({'nickname': 'Alice'})

        group_name, event = self.consumer.channel_layer.group_send.await_args.args
        self.assertEqual(group_name, self.consumer.group_name)
        self.assertEqual(event['event_type'], 'readiness_participant_ready')
        self.assertEqual(event['state']['readiness_counts']['ready'], 1)

    def test_end_with_pending_participants_returns_confirmation_to_host_only(self):
        start_readiness_check(self.session)

        async_to_sync(self.consumer.handle_end_readiness_check)({})

        self.consumer.channel_layer.group_send.assert_not_awaited()
        payload = json.loads(self.consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(payload['type'], 'readiness_end_requires_confirmation')
        self.assertTrue(payload['readiness_check']['active'])

        async_to_sync(self.consumer.handle_end_readiness_check)({'force': True})

        self.consumer.channel_layer.group_send.assert_awaited_once()
        _, event = self.consumer.channel_layer.group_send.await_args.args
        self.assertEqual(event['event_type'], 'readiness_ended')
        self.assertFalse(event['state']['readiness_check']['active'])


class ReadinessCheckBoundaryTests(TestCase):
    def setUp(self):
        from host_points.models import HostPointsGame
        self.host = User.objects.create_user('readiness-boundary-host')
        self.session = HubSession.objects.create(code='READY4', creator=self.host, is_active=True,
            check_in_status='completed', locked_participant_count=2)
        for name in ['Alice', 'Bob']:
            HubParticipant.objects.create(session=self.session, nickname=name, scoring_eligible=True)
        self.game = HostPointsGame.objects.create(creator=self.host)
        self.step = HubGameStep.objects.create(session=self.session, game_key='host_points', room_code=self.game.room_code, order=0)

    def test_active_game_blocks_readiness_for_each_audited_game_type_even_without_players(self):
        from games_hub.active_game_guard import get_game_model_map
        for key, model in get_game_model_map().items():
            if key == 'where':
                continue
            with self.subTest(game_key=key):
                config = {'title': 'Zielansage', 'scoring_mode': 'simple', 'answer_seconds': 30} if key == 'target' else {}
                game = model.objects.create(creator=self.host, status='active', **config)
                step = HubGameStep.objects.create(session=self.session, game_key=key, room_code=game.room_code, order=1)
                result = start_readiness_check(self.session)
                self.assertFalse(result['success'])
                self.assertFalse(self.session.readiness_checks.exists())
                game.refresh_from_db()
                self.assertEqual(game.status, 'active')
                step.delete()

    def test_completed_or_paused_game_still_blocks_until_last_player_returns(self):
        from host_points.models import HostPointsParticipant
        player = HostPointsParticipant.objects.create(quiz=self.game, name='Alice', hub_session_code=self.session.code, is_active=True)
        for status in ['completed', 'inactive']:
            with self.subTest(status=status):
                self.game.status = status
                self.game.save(update_fields=['status'])
                result = start_readiness_check(self.session)
                self.assertFalse(result['success'])
                self.assertIn('Lobby', result['error'])
                self.assertFalse(self.session.readiness_checks.exists())
        player.is_active = False
        player.save(update_fields=['is_active'])
        self.assertTrue(start_readiness_check(self.session)['success'])

    def test_other_session_using_same_room_does_not_block_readiness(self):
        from host_points.models import HostPointsParticipant
        self.game.status = 'active'
        self.game.active_hub_session_code = 'OTHER'
        self.game.save(update_fields=['status', 'active_hub_session_code'])
        HostPointsParticipant.objects.create(quiz=self.game, name='Alice', hub_session_code='OTHER', is_active=True)
        self.assertTrue(start_readiness_check(self.session)['success'])

    def test_rejected_restart_preserves_existing_check_and_ready_marks(self):
        first = start_readiness_check(self.session)
        mark_participant_ready(self.session, 'Alice')
        self.game.status = 'active'
        self.game.save(update_fields=['status'])
        self.assertFalse(start_readiness_check(self.session)['success'])
        state = get_readiness_state(self.session)
        self.assertEqual(state['readiness_check']['id'], first['readiness_check']['id'])
        self.assertEqual(state['readiness_counts']['ready'], 1)

    def test_game_start_waits_until_host_closes_readiness_even_when_everyone_is_ready(self):
        from games_hub.active_game_guard import resolve_session_game_activation
        start_readiness_check(self.session)
        for name in ['Alice', 'Bob']:
            mark_participant_ready(self.session, name)
        for check_only in [True, False]:
            result = resolve_session_game_activation(self.session.code, 'host_points', self.game.room_code, check_only=check_only)
            self.assertFalse(result['success'])
            self.assertIn('Bereitschaftscheck', result['error'])
            self.game.refresh_from_db()
            self.assertEqual(self.game.status, 'waiting')
            self.assertFalse(self.step.participant_snapshots.exists())
        end_readiness_check(self.session)
        self.assertTrue(resolve_session_game_activation(self.session.code, 'host_points', self.game.room_code)['success'])

    def test_ended_session_cannot_start_check(self):
        self.session.ended_at = timezone.now()
        self.session.save(update_fields=['ended_at'])
        self.assertFalse(start_readiness_check(self.session)['success'])
        self.assertFalse(self.session.readiness_checks.exists())


class ReadinessCheckTemplateTests(TestCase):
    def test_lobby_renders_participant_readiness_popup(self):
        session = HubSession.objects.create(code='READY3', name='Readiness UI')

        response = self.client.get(reverse('games_hub:lobby', args=[session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="readinessOverlay"')
        self.assertContains(response, 'Bereitschaftscheck: Bitte bestätige, dass du bereit bist.')
        self.assertContains(response, "type: 'participant_ready'")
        self.assertContains(response, 'renderReadinessState(data)')

    def test_shared_play_overlay_renders_readiness_handler(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / 'templates' / 'includes' / '_game_tutorial_overlay.html').read_text(encoding='utf-8')

        self.assertIn('id="sessionReadinessOverlay"', content)
        self.assertIn('sessionReadinessReadyBtn', content)
        self.assertIn("type: 'participant_ready'", content)
        self.assertIn('readiness_started', content)
        self.assertIn('connectReadinessSocket', content)
