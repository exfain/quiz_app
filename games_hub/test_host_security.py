import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from Assign.consumers import AssignConsumer
from Estimation.consumers import EstimationConsumer
from QuizGame.consumers import QuizConsumer
from black_jack_quiz.consumers import BlackJackConsumer
from buzzer.consumers import BuzzerConsumer
from buzzer.models import BuzzerGame, BuzzerParticipant
from clue_rush.consumers import ClueRushGameConsumer
from host_points.consumers import HostPointsConsumer
from host_points.models import HostPointsGame, HostPointsParticipant
from sorting_ladder.consumers import SortingLadderGameConsumer
from wann_war_das.consumers import WannWarDasConsumer
from wann_war_das.models import WannWarDasGame
from wer_weiss_mehr.consumers import WerWeissMehrConsumer
from wer_weiss_mehr.models import WerWeissMehrGame
from where_is_this.consumers import WhereConsumer
from who_is_lying.consumers import WhoConsumer
from who_is_that.consumers import WhoThatConsumer

from .authoritative_consumer import AuthoritativeGameConsumerMixin
from .authoritative_state import current_snapshot, observe_snapshot
from .host_permissions import authorize_game_host, user_can_manage_hub_session
from .models import (
    HubGameParticipantSnapshot,
    HubGameStep,
    HubParticipant,
    HubSession,
    ProcessedClientAction,
)


REGISTERED_GAME_CONSUMERS = (
    QuizConsumer,
    AssignConsumer,
    EstimationConsumer,
    WhereConsumer,
    WhoConsumer,
    WhoThatConsumer,
    BlackJackConsumer,
    ClueRushGameConsumer,
    SortingLadderGameConsumer,
    BuzzerConsumer,
    HostPointsConsumer,
    WannWarDasConsumer,
    WerWeissMehrConsumer,
)


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class HostOwnershipTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user('security-owner', password='pw', is_staff=True)
        self.other = User.objects.create_user('security-other', password='pw', is_staff=True)
        self.superuser = User.objects.create_superuser('security-root', password='pw', email='')
        self.game = BuzzerGame.objects.create(title='Owned Buzzer', creator=self.owner)
        self.session = HubSession.objects.create(code='SEC001', creator=self.owner)
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        self.other_game = BuzzerGame.objects.create(title='Other Buzzer', creator=self.other)
        self.other_session = HubSession.objects.create(code='SEC002', creator=self.other)
        HubGameStep.objects.create(
            session=self.other_session,
            order=0,
            game_key='buzzer',
            room_code=self.other_game.room_code,
            title=self.other_game.title,
        )

    def test_session_owner_and_legacy_step_owner_are_authoritative(self):
        self.assertTrue(user_can_manage_hub_session(self.owner, self.session))
        self.assertFalse(user_can_manage_hub_session(self.other, self.session))

        legacy = HubSession.objects.create(code='SEC003')
        HubGameStep.objects.create(
            session=legacy,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        self.assertTrue(user_can_manage_hub_session(self.owner, legacy))
        self.assertFalse(user_can_manage_hub_session(self.other, legacy))

    def test_game_authorization_rejects_anonymous_non_owner_and_cross_session(self):
        self.assertEqual(
            authorize_game_host(AnonymousUser(), 'buzzer', self.game.room_code).code,
            'unauthorized',
        )
        self.assertEqual(
            authorize_game_host(self.other, 'buzzer', self.game.room_code).code,
            'unauthorized',
        )
        self.assertEqual(
            authorize_game_host(
                self.owner, 'buzzer', self.game.room_code, self.other_session.code
            ).code,
            'stale_action',
        )
        self.assertTrue(
            authorize_game_host(
                self.owner, 'buzzer', self.game.room_code, self.session.code
            ).allowed
        )
        self.assertTrue(
            authorize_game_host(
                self.superuser, 'buzzer', self.game.room_code, self.session.code
            ).allowed
        )

    def test_all_registered_game_consumers_share_the_admin_authorization_boundary(self):
        registered_keys = {key for key, _label in HubGameStep.GAME_CHOICES}
        consumer_keys = {consumer.authoritative_game_key for consumer in REGISTERED_GAME_CONSUMERS}
        self.assertEqual(consumer_keys, registered_keys)
        for consumer in REGISTERED_GAME_CONSUMERS:
            self.assertTrue(issubclass(consumer, AuthoritativeGameConsumerMixin), consumer.__name__)

    def _dispatch_admin_message(self, user, session_code):
        consumer = BuzzerConsumer()
        consumer.scope = {'user': user}
        consumer.room_code = self.game.room_code
        consumer.send = AsyncMock()
        consumer.receive = AsyncMock()
        async_to_sync(consumer.websocket_receive)({
            'type': 'websocket.receive',
            'text': json.dumps({
                'type': 'admin_start_game',
                'hub_session': session_code,
            }),
        })
        return consumer

    def test_websocket_admin_action_rejects_anonymous_and_non_owner(self):
        for user in (AnonymousUser(), self.other):
            with self.subTest(user=str(user)):
                consumer = self._dispatch_admin_message(user, self.session.code)
                consumer.receive.assert_not_awaited()
                payload = json.loads(consumer.send.await_args.kwargs['text_data'])
                self.assertEqual(payload['type'], 'action_rejected')
                self.assertEqual(payload['code'], 'unauthorized')

    def test_websocket_admin_action_rejects_cross_session_context(self):
        consumer = self._dispatch_admin_message(self.owner, self.other_session.code)
        consumer.receive.assert_not_awaited()
        payload = json.loads(consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(payload['code'], 'stale_action')

    def test_websocket_owner_reaches_the_domain_handler(self):
        consumer = self._dispatch_admin_message(self.owner, self.session.code)
        consumer.send.assert_not_awaited()
        consumer.receive.assert_awaited_once()


class HostHttpMutationPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('http-owner', password='pw', is_staff=True)
        self.other = User.objects.create_user('http-other', password='pw', is_staff=True)
        self.session = HubSession.objects.create(
            code='HTTP01',
            creator=self.owner,
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            locked_participant_count=0,
        )
        self.game = BuzzerGame.objects.create(title='HTTP Buzzer', creator=self.owner)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )

    def test_hub_mutations_require_authentication_and_session_owner(self):
        activate_url = reverse('games_hub:activate_session_game', args=[self.session.code])
        payload = json.dumps({'game_key': 'buzzer', 'room_code': self.game.room_code})
        self.assertEqual(
            Client().post(activate_url, data=payload, content_type='application/json').status_code,
            302,
        )

        client = Client()
        client.force_login(self.other)
        denied = client.post(activate_url, data=payload, content_type='application/json')
        self.assertEqual(denied.status_code, 403)
        self.session.refresh_from_db()
        self.assertFalse(self.game.status == 'active')

        client.force_login(self.owner)
        allowed = client.post(activate_url, data=payload, content_type='application/json')
        self.assertNotEqual(allowed.status_code, 403)

    def test_cross_session_recall_cannot_change_foreign_participants(self):
        participant = BuzzerParticipant.objects.create(
            quiz=self.game,
            name='Alice',
            hub_session_code=self.session.code,
            is_active=True,
        )
        client = Client()
        client.force_login(self.other)
        response = client.post(
            reverse('games_hub:recall_session_participants_to_lobby', args=[self.session.code])
        )
        participant.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertTrue(participant.is_active)

    def test_new_game_monitor_and_endpoints_reject_non_owner(self):
        cases = []
        for game_key, model, monitor_name, end_name in (
            ('buzzer', BuzzerGame, 'buzzer_monitor', 'end_buzzer_game_by_room_code'),
            ('host_points', HostPointsGame, 'host_points_monitor', 'end_host_points_game_by_room_code'),
            ('wann_war_das', WannWarDasGame, 'wann_war_das_monitor', 'end_wann_war_das_game_by_room_code'),
            ('wer_weiss_mehr', WerWeissMehrGame, 'wer_weiss_mehr_monitor', 'end_wer_weiss_mehr_game_by_room_code'),
        ):
            game = model.objects.create(title=f'{game_key} secured', creator=self.owner)
            session = HubSession.objects.create(code=f'H{len(cases):05}', creator=self.owner)
            HubGameStep.objects.create(
                session=session,
                order=0,
                game_key=game_key,
                room_code=game.room_code,
                title=game.title,
            )
            cases.append((game, session, monitor_name, end_name))

        client = Client()
        client.force_login(self.other)
        for game, session, monitor_name, end_name in cases:
            with self.subTest(game=game.__class__.__name__):
                monitor = client.get(
                    reverse(f'admin_dashboard:{monitor_name}', args=[game.room_code]),
                    {'hub_session': session.code},
                )
                end = client.post(
                    reverse(f'admin_dashboard:{end_name}', args=[game.room_code]),
                    data=json.dumps({'hub_session': session.code}),
                    content_type='application/json',
                )
                game.refresh_from_db()
                self.assertEqual(monitor.status_code, 302)
                self.assertEqual(end.status_code, 403)
                self.assertNotEqual(game.status, 'completed')

    def test_legacy_host_points_score_mutation_requires_owner_and_session_context(self):
        game = HostPointsGame.objects.create(
            title='Secured score',
            creator=self.owner,
            status='active',
            active_hub_session_code='HTTPS2',
        )
        session = HubSession.objects.create(code='HTTPS2', creator=self.owner)
        hub_participant = HubParticipant.objects.create(
            session=session,
            nickname='Alice',
            scoring_eligible=True,
        )
        step = HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='host_points',
            room_code=game.room_code,
            title=game.title,
        )
        HubGameParticipantSnapshot.objects.create(
            session=session,
            game_step=step,
            participant=hub_participant,
            active_player=True,
            included_in_scoring=True,
        )
        participant = game.ensure_snapshot_participants(session.code)[0]
        url = reverse('host_points:adjust_score', args=[game.room_code])
        payload = json.dumps({
            'participant_id': participant.id,
            'delta': 5,
            'hub_session': session.code,
        })

        self.assertEqual(Client().post(url, data=payload, content_type='application/json').status_code, 302)
        client = Client()
        client.force_login(self.other)
        self.assertEqual(client.post(url, data=payload, content_type='application/json').status_code, 403)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, 0)

        client.force_login(self.owner)
        self.assertEqual(client.post(url, data=payload, content_type='application/json').status_code, 200)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, 5)


class BuzzerHostIdempotencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_user('buzzer-idempotent', password='pw', is_staff=True)
        self.game = BuzzerGame.objects.create(
            title='Idempotent Buzzer',
            creator=self.owner,
            points_per_correct=3,
        )
        self.session = HubSession.objects.create(
            code='IDEMP1',
            creator=self.owner,
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(
                session=self.session,
                nickname=name,
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            )
        step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='buzzer',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubGameParticipantSnapshot.create_for_step(step)
        self.consumer = BuzzerConsumer()
        self.consumer.room_code = self.game.room_code
        self.consumer.room_group_name = f'buzzer_{self.game.room_code}'
        self.consumer.channel_layer = FakeChannelLayer()
        self.sent = []

        async def fake_send(text_data=None, **_kwargs):
            self.sent.append(json.loads(text_data))

        self.consumer.send = fake_send

    def _snapshot(self):
        self.game.refresh_from_db()
        observe_snapshot(
            'buzzer',
            self.game.room_code,
            self.game.serialize_state(self.session.code),
            self.session.code,
        )
        return current_snapshot('buzzer', self.game.room_code, self.session.code)

    def _action(self, action_type, **extra):
        snapshot = self._snapshot()
        return {
            'type': action_type,
            'hub_session': self.session.code,
            'client_action_id': str(uuid.uuid4()),
            'state_revision': snapshot['state_revision'],
            'game_id': snapshot['game_id'],
            'question_id': snapshot.get('current_question_id'),
            'round_id': snapshot.get('current_round_id'),
            'set_id': snapshot.get('current_set_id'),
            **extra,
        }

    def _send_twice(self, action):
        async_to_sync(self.consumer.receive)(json.dumps(action))
        async_to_sync(self.consumer.receive)(json.dumps(action))

    def test_every_buzzer_admin_transition_has_exactly_one_effect(self):
        start = self._action('admin_start_game')
        self._send_twice(start)
        self.game.refresh_from_db()
        started_at = self.game.started_at
        self.assertEqual(self.game.status, 'active')
        self.assertEqual(ProcessedClientAction.objects.filter(client_action_id=start['client_action_id']).count(), 1)
        self.assertEqual(self.game.started_at, started_at)

        start_round = self._action('admin_start_round')
        self._send_twice(start_round)
        self.assertEqual(self.game.rounds.count(), 1)

        open_buzzer = self._action('admin_open_buzzer')
        self._send_twice(open_buzzer)
        self.game.refresh_from_db()
        opened_at = self.game.current_round.opened_at
        self.game.current_round.refresh_from_db()
        self.assertEqual(self.game.current_round.opened_at, opened_at)

        alice = self.game.participants.get(name='Alice', hub_session_code=self.session.code)
        bob = self.game.participants.get(name='Bob', hub_session_code=self.session.code)
        self.assertTrue(self.game.accept_buzz(alice)[0])
        mark_wrong = self._action('admin_mark_wrong')
        self._send_twice(mark_wrong)
        self.assertEqual(self.game.current_round.buzzes.filter(status='wrong').count(), 1)

        self.assertTrue(self.game.accept_buzz(bob)[0])
        mark_correct = self._action('admin_mark_correct')
        self._send_twice(mark_correct)
        bob.refresh_from_db()
        self.assertEqual(bob.total_score, self.game.points_per_correct)

        next_round = self._action('admin_start_round')
        self._send_twice(next_round)
        self.assertEqual(self.game.rounds.count(), 2)

        end_round = self._action('admin_end_round')
        self._send_twice(end_round)
        self.game.refresh_from_db()
        ended_at = self.game.current_round.ended_at
        self.game.current_round.refresh_from_db()
        self.assertEqual(self.game.current_round.ended_at, ended_at)

        end_game = self._action('admin_end_game')
        self._send_twice(end_game)
        self.game.refresh_from_db()
        game_ended_at = self.game.ended_at
        self._send_twice(end_game)
        self.game.refresh_from_db()
        self.assertEqual(self.game.ended_at, game_ended_at)

    def test_parallel_duplicate_round_start_creates_one_round(self):
        self.game.start_quiz(self.session.code)
        action = self._action('admin_start_round')

        async def send_parallel():
            await asyncio.gather(
                self.consumer.receive(json.dumps(action)),
                self.consumer.receive(json.dumps(action)),
            )

        async_to_sync(send_parallel)()
        self.assertEqual(self.game.rounds.count(), 1)
        self.assertEqual(ProcessedClientAction.objects.filter(client_action_id=action['client_action_id']).count(), 1)

    def test_stale_round_context_cannot_end_the_current_round(self):
        self.game.start_quiz(self.session.code)
        self.game.start_round(self.session.code)
        stale_action = self._action('admin_end_round')
        self.game.end_current_round()
        self.game.start_round(self.session.code)
        self._snapshot()

        async_to_sync(self.consumer.receive)(json.dumps(stale_action))

        self.game.refresh_from_db()
        self.assertEqual(self.game.current_round_number, 2)
        self.assertNotEqual(self.game.current_round.status, 'ended')
        self.assertEqual(self.sent[-2]['code'], 'stale_action')

    def test_set_inactive_duplicate_changes_status_once(self):
        self.game.start_quiz(self.session.code)
        action = self._action('admin_set_inactive')
        self._send_twice(action)
        self.game.refresh_from_db()
        updated_at = self.game.updated_at
        self._send_twice(action)
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'inactive')
        self.assertEqual(self.game.updated_at, updated_at)
