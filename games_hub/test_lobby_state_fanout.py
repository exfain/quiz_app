"""Joining 25 devices must not enqueue 25 identical database snapshots."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from autobahn.exception import Disconnected
from channels.exceptions import StopConsumer

from asgiref.sync import async_to_sync
from django.test import TransactionTestCase

from .consumers import HubConsumer
from .authoritative_consumer import AuthoritativeGameConsumerMixin
from .models import HubParticipant, HubSession


class LobbyStateFanoutTests(TransactionTestCase):
    def setUp(self):
        self.hub=HubSession.objects.create(code='FANOUT25')
        for n in range(24):
            HubParticipant.objects.create(session=self.hub,nickname=f'Team {n+1:02d}')

    def consumer(self):
        consumer=HubConsumer()
        consumer.session_code=self.hub.code
        consumer.group_name='hub_'+self.hub.code
        consumer.hub_participant_id=None
        consumer.hub_participant_name=None
        consumer.channel_layer=SimpleNamespace(group_send=AsyncMock())
        consumer.send=AsyncMock()
        return consumer

    def test_closed_transport_does_not_skip_normal_disconnect_cleanup(self):
        consumer=self.consumer();consumer.channel_name='closed-device'
        consumer.channel_layer.group_discard=AsyncMock()
        for error in (Disconnected('closed protocol'),OSError('closed socket')):
            with self.subTest(error=type(error).__name__),patch.object(AuthoritativeGameConsumerMixin,'send',new=AsyncMock(side_effect=error)):
                async_to_sync(HubConsumer.send)(consumer,text_data='{"type":"state"}')
        with patch('games_hub.authoritative_consumer.disconnect_socket_connection',return_value=True) as cleanup:
            with self.assertRaises(StopConsumer):async_to_sync(consumer.websocket_disconnect)({'code':1001})
            cleanup.assert_called_once_with('closed-device')
        consumer.channel_layer.group_discard.assert_awaited_once_with(consumer.group_name,'closed-device')

    def test_non_transport_send_errors_are_not_silenced(self):
        consumer=self.consumer()
        with patch.object(AuthoritativeGameConsumerMixin,'send',new=AsyncMock(side_effect=RuntimeError('unexpected'))):
            with self.assertRaisesRegex(RuntimeError,'unexpected'):
                async_to_sync(HubConsumer.send)(consumer,text_data='{"type":"state"}')

    def test_join_broadcasts_one_public_snapshot_to_25_devices(self):
        joining=self.consumer()
        original=HubConsumer.__dict__['get_state']
        snapshots=[]
        async def counted(consumer):
            result=await original.__get__(consumer,HubConsumer)()
            snapshots.append(result)
            return result
        async def exercise():
            with patch.object(HubConsumer,'get_state',new=counted):
                await joining.handle_join({'nickname':'Team 25'})
            _,event=joining.channel_layer.group_send.await_args.args
            self.assertEqual(event['type'],'lobby_update')
            self.assertEqual(len(event['state']['participants']),25)
            self.assertEqual(len(snapshots),1)
            self.assertNotIn('rejoin_token',json.dumps(event))
            for _ in range(25):
                recipient=self.consumer()
                recipient.get_state=AsyncMock(side_effect=AssertionError('Repeated database snapshot'))
                await recipient.lobby_update(event)
                payload=json.loads(recipient.send.await_args.kwargs['text_data'])
                self.assertEqual(payload,{'type':'state',**snapshots[0]})
                recipient.get_state.assert_not_awaited()
        async_to_sync(exercise)()
        self.assertEqual(self.hub.participants.count(),25)

    def test_older_queued_broadcast_cannot_replace_a_newer_direct_state(self):
        consumer=self.consumer()
        consumer.get_state=AsyncMock(return_value={'participants':[{'nickname':'New'}],'lobby_state_at':'2026-11-16T10:00:02+00:00'})
        async def exercise():
            await consumer.send_state()
            await consumer.lobby_update({'type':'lobby_update','state':{'participants':[],'lobby_state_at':'2026-11-16T10:00:01+00:00'}})
        async_to_sync(exercise)()
        consumer.send.assert_awaited_once()
        self.assertEqual(json.loads(consumer.send.await_args.kwargs['text_data'])['participants'],[{'nickname':'New'}])

    def test_legacy_notification_still_loads_a_fresh_state(self):
        consumer=self.consumer()
        consumer.get_state=AsyncMock(return_value={'participants':[{'nickname':'Current'}]})
        async_to_sync(consumer.lobby_update)({'type':'lobby_update'})
        consumer.get_state.assert_awaited_once()
        self.assertEqual(json.loads(consumer.send.await_args.kwargs['text_data'])['participants'],[{'nickname':'Current'}])

    def test_rejected_join_never_broadcasts_a_state_or_credential(self):
        consumer=self.consumer()
        consumer.get_state=AsyncMock(side_effect=AssertionError('Rejected join loaded state'))
        async_to_sync(consumer.handle_join)({'nickname':'<invalid>'})
        consumer.channel_layer.group_send.assert_not_awaited()
        consumer.get_state.assert_not_awaited()
        self.assertEqual(json.loads(consumer.send.await_args.kwargs['text_data'])['type'],'join_error')

    def test_explicit_snapshot_remains_fresh_after_roster_changes(self):
        consumer=self.consumer()
        async_to_sync(consumer.send_state)()
        first=json.loads(consumer.send.await_args.kwargs['text_data'])
        HubParticipant.objects.create(session=self.hub,nickname='Team 25')
        async_to_sync(consumer.send_state)()
        second=json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(len(first['participants']),24)
        self.assertEqual(len(second['participants']),25)
