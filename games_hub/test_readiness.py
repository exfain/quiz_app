import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.urls import reverse
from django.test import TestCase, TransactionTestCase

from games_hub.consumers import HubConsumer
from games_hub.models import HubParticipant, HubReadinessCheck, HubSession
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
