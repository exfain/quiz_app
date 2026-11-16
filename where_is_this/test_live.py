import json
import math
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User
from django.db import connections
from django.test import TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.authoritative_state import current_snapshot
from . import runtime as r
from .consumers import WhereConsumer
from .identity import issue_player_token
from .models import WhereQuiz, WhereQuestion, WhereParticipant


class WhereLiveTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('where-live-host', is_staff=True)
        self.hub = HubSession.objects.create(code='WHERE25', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=25)
        for i in range(25):
            HubParticipant.objects.create(session=self.hub, nickname=f'Team {i:02}', scoring_eligible=True)
        self.game = WhereQuiz.objects.create(creator=self.user, scoring_mode='rank', zone_count=5, zone_width_km=10)
        self.q = WhereQuestion.objects.create(created_by=self.user, question_text='Wo liegt Berlin?', correct_latitude=52.52,
            correct_longitude=13.405, time_limit=120, map_type='germany')
        self.other = WhereQuestion.objects.create(created_by=self.user, question_text='Wo liegt Paris?', correct_latitude=48.85,
            correct_longitude=2.35, time_limit=120, map_type='europe')
        self.game.selected_questions.set([self.q, self.other])
        HubGameStep.objects.create(session=self.hub, game_key='where', room_code=self.game.room_code, order=0)
        r.start(self.game, self.hub.code)
        self.players = list(self.game.participants.order_by('name'))
        r.send_question(self.game, self.q)

    def data(self, *, offset=0, question=None, round_number=None):
        state = r.state(self.game, self.hub.code, self.players[0])
        return {'question_id': question or state['current_question_id'], 'round_id': round_number or state['current_round_id'],
                'game_id': state['game_id'], 'state_revision': state['state_revision'], 'client_action_id': str(uuid.uuid4()),
                'latitude': self.q.correct_latitude + offset, 'longitude': self.q.correct_longitude}

    def test_ranking_uses_whole_round_roster_and_earlier_submission_breaks_equal_distance(self):
        for player in self.players[:3]:
            r.save_input(self.game, player, self.data(), confirm=True)
        self.assertEqual(list(self.game.participants.values_list('total_score', flat=True)), [0] * 25)
        r.reveal(self.game)
        answers = list(self.game.where_answers.order_by('rank_position'))
        self.assertEqual([a.points_earned for a in answers], [25, 24, 23])
        self.assertEqual([a.participant_id for a in answers], [p.pk for p in self.players[:3]])
        self.assertFalse(r.reveal(self.game))

    def test_equal_width_zones_points_and_outside(self):
        self.game.scoring_mode = 'zones'
        self.game.save(update_fields=['scoring_mode'])
        r.reveal(self.game)
        r.send_question(self.game, self.q)
        for player, distance in zip(self.players, [0, 15, 25, 35, 45, 55]):
            offset = distance / 6371 * 180 / math.pi
            r.save_input(self.game, player, self.data(offset=offset), confirm=True)
        r.reveal(self.game)
        answers = list(self.game.where_answers.filter(round_number=2).order_by('distance_km'))
        self.assertEqual([a.points_earned for a in answers], [5, 4, 3, 2, 1, 0])
        self.assertEqual(len(r.state(self.game, self.hub.code)['zone_scoring']['zones']), 5)

    def test_draft_reload_and_manual_end_finalize_latest_marker_and_keep_locked_answer(self):
        for offset in [.2, .1, 0]:
            r.save_input(self.game, self.players[0], self.data(offset=offset))
        self.assertEqual(r.state(self.game, self.hub.code, self.players[0])['pending_input']['user_latitude'], self.q.correct_latitude)
        r.save_input(self.game, self.players[1], self.data(offset=.3), confirm=True)
        with self.assertRaises(r.ActionError):
            r.save_input(self.game, self.players[1], self.data(offset=0))
        r.reveal(self.game)
        self.assertEqual(self.game.where_answers.count(), 2)
        self.assertAlmostEqual(self.game.where_answers.get(participant=self.players[0]).distance_km, 0)

    def test_expired_drafts_wait_for_host_and_receive_deadline_time(self):
        r.save_input(self.game, self.players[0], self.data())
        session = self.game.session
        session.question_end_time = timezone.now() - timezone.timedelta(seconds=1)
        session.save(update_fields=['question_end_time'])
        for _ in range(2):
            state = r.state(self.game, self.hub.code, self.players[0])
            self.assertFalse(state['revealed'])
            self.assertFalse(state['can_answer'])
        with self.assertRaises(r.ActionError):
            r.save_input(self.game, self.players[1], self.data(), confirm=True)
        self.assertEqual(self.game.where_answers.count(), 0)
        r.reveal(self.game)
        self.assertEqual(self.game.where_answers.count(), 1)

    def test_no_solution_or_foreign_marker_or_grading_before_common_reveal_even_after_host_snapshot(self):
        r.save_input(self.game, self.players[0], self.data(), confirm=True)
        host = r.state(self.game, self.hub.code, host=True)
        self.assertIn('correct_location', host['question'])
        for state in [r.state(self.game, self.hub.code, self.players[1]), current_snapshot('where', self.game.room_code, self.hub.code)]:
            self.assertNotIn('correct_location', state.get('question') or {})
        state = r.state(self.game, self.hub.code, self.players[1])
        self.assertEqual(state['answers'], [])
        self.assertTrue(all(p['score'] == 0 for p in state['participants']))
        self.assertIsNone(state['scoreboard'][0]['points'])
        r.reveal(self.game)
        state = r.state(self.game, self.hub.code, self.players[1])
        self.assertIn('correct_location', state['question'])
        self.assertEqual(len(state['answers']), 1)

    def test_replay_has_own_answers_points_and_dynamic_total(self):
        for _ in range(2):
            r.save_input(self.game, self.players[0], self.data(), confirm=True)
            r.reveal(self.game)
            if self.game.where_answers.count() == 1:
                r.send_question(self.game, self.q)
        self.assertEqual(self.game.where_answers.count(), 2)
        state = r.state(self.game, self.hub.code, self.players[0])
        self.assertEqual(state['progress'], {'kind': 'question', 'current': 2, 'total': 3})
        self.assertEqual([row['points'] for row in state['scoreboard']], [25, 25])

    def test_stale_round_cannot_overwrite_replay(self):
        old = self.data()
        r.reveal(self.game)
        r.send_question(self.game, self.q)
        with self.assertRaises(r.ActionError):
            r.save_input(self.game, self.players[0], old)

    def test_pause_resume_freezes_time_and_keeps_draft_locked_answer_and_scores(self):
        r.save_input(self.game, self.players[0], self.data())
        r.save_input(self.game, self.players[1], self.data(), confirm=True)
        r.set_paused(self.game, True)
        self.game.refresh_from_db()
        paused = self.game.paused_at
        before = self.game.session.question_end_time
        self.game.paused_at = paused - timezone.timedelta(seconds=60)
        self.game.save(update_fields=['paused_at'])
        r.set_paused(self.game, False)
        self.assertGreater(r.state(self.game, self.hub.code)['remaining_seconds'], 160)
        self.assertEqual(len(self.game.session.pending_answers), 1)
        self.assertEqual(self.game.where_answers.count(), 1)
        self.game.refresh_from_db()
        self.assertGreater(self.game.session.question_end_time, before)

    def test_late_joiner_waits_until_next_question(self):
        player = WhereParticipant.objects.create(quiz=self.game, name='Late', hub_session_code=self.hub.code)
        self.assertFalse(r.state(self.game, self.hub.code, player)['can_answer'])
        with self.assertRaises(r.ActionError):
            r.save_input(self.game, player, self.data())
        r.reveal(self.game)
        r.send_question(self.game, self.other)
        self.assertTrue(r.state(self.game, self.hub.code, player)['can_answer'])

    def test_ending_game_retains_reveal_and_grades_pending(self):
        r.save_input(self.game, self.players[0], self.data())
        r.end_game(self.game)
        state = r.state(self.game, self.hub.code, self.players[0])
        self.assertEqual(state['game']['status'], 'completed')
        self.assertTrue(state['revealed'])
        self.assertEqual(state['answers'][0]['points_earned'], 25)
        self.assertFalse(r.end_game(self.game))

    def test_invalid_and_nonfinite_coordinates_never_persist(self):
        for patch in [{'latitude': float('nan')}, {'longitude': float('inf')}, {'x_norm': 2, 'y_norm': .5}, {'latitude': 90}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                r.save_input(self.game, self.players[0], {**self.data(), **patch})
        self.assertEqual(self.game.where_answers.count(), 0)

    def test_editing_question_only_affects_future_rounds(self):
        data = self.data()
        original = self.q.correct_latitude
        self.q.correct_latitude, self.q.map_type = 60, 'world_mercator'
        self.q.question_text = 'Geänderte Frage'
        self.q.save()
        r.save_input(self.game, self.players[0], data, confirm=True)
        r.reveal(self.game)
        state = r.state(self.game, self.hub.code, self.players[0])
        self.assertEqual(state['question']['correct_location']['latitude'], original)
        self.assertEqual(state['question']['map_type'], 'germany')
        self.assertAlmostEqual(state['answers'][0]['distance_km'], 0)

    def test_25_simultaneous_http_answers_and_public_scores_after_reveal(self):
        barrier = Barrier(25)
        data = self.data()
        def submit(player):
            try:
                barrier.wait(timeout=20)
                return Client().post(reverse('where_is_this:submit_answer', args=[self.game.room_code, player.name])
                    + f'?hub_session={self.hub.code}&player_token={issue_player_token(player)}',
                    json.dumps(data), content_type='application/json').status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=25) as executor:
            statuses = list(executor.map(submit, self.players))
        self.assertEqual(statuses, [200] * 25)
        self.assertEqual(self.game.where_answers.count(), 25)
        r.reveal(self.game)
        scores = sorted(self.game.participants.values_list('total_score', flat=True))
        self.assertEqual(scores, list(range(1, 26)))

    def test_http_requires_matching_capability(self):
        url = reverse('where_is_this:submit_answer', args=[self.game.room_code, self.players[1].name])
        self.assertEqual(self.client.post(url + f'?hub_session={self.hub.code}&player_token={issue_player_token(self.players[0])}',
            json.dumps(self.data()), content_type='application/json').status_code, 403)

    def test_draft_ack_identifies_exact_saved_marker_and_round(self):
        data = self.data()
        # Draft messages use the saved round identity, without final-answer ids.
        data.pop('client_action_id')
        async def scenario():
            socket = WebsocketCommunicator(WhereConsumer.as_asgi(), '/ws/where/test/')
            socket.scope.update(user=AnonymousUser(), url_route={'kwargs': {'room_code': self.game.room_code}})
            await socket.connect()
            await socket.receive_json_from()
            await socket.send_json_to({'type': 'participant_join', 'participant_name': self.players[0].name,
                'hub_session': self.hub.code, 'player_token': issue_player_token(self.players[0])})
            self.assertEqual((await socket.receive_json_from())['type'], 'where_state')
            await socket.send_json_to({'type': 'participant_input_changed', **data})
            for _ in range(4):
                message = await socket.receive_json_from()
                if message['type'] == 'input_saved':
                    self.assertEqual(message['round_id'], data['round_id'])
                    self.assertEqual(message['question_id'], data['question_id'])
                    self.assertTrue(0 <= message['x_norm'] <= 1)
                    self.assertTrue(0 <= message['y_norm'] <= 1)
                    break
            else:
                self.fail('No marker acknowledgement received')
            await socket.disconnect()
        async_to_sync(scenario)()

    def test_configured_zones_and_map_selection_survive_game_edit(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:create_where_custom_quiz'),
            json.dumps({'title': 'Zonen', 'question_ids': [self.q.pk], 'scoring_mode': 'zones',
                        'zone_count': 7, 'zone_width_km': 25}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        game = WhereQuiz.objects.get(pk=response.json()['quiz_id'])
        self.assertEqual((game.zone_count, game.zone_width_km), (7, 25))
        detail = self.client.get(reverse('admin_dashboard:get_where_selected_questions', args=[game.pk])).json()
        self.assertEqual((detail['zone_count'], detail['zone_width_km'], detail['scoring_mode']), (7, 25, 'zones'))
        self.assertEqual(self.q.map_type, 'germany')

    def test_invalid_map_location_in_editor_never_overwrites_question(self):
        self.client.force_login(self.user)
        original = self.q.correct_latitude
        response = self.client.post(reverse('admin_dashboard:update_where_question'),
            {'question_id': self.q.pk, 'map_type': 'germany', 'latitude': 0, 'longitude': 0})
        self.assertEqual(response.status_code, 400)
        self.q.refresh_from_db()
        self.assertEqual(self.q.correct_latitude, original)

    def test_anonymous_socket_cannot_control_host_or_read_host_solution(self):
        async def scenario():
            socket = WebsocketCommunicator(WhereConsumer.as_asgi(), '/ws/where/test/')
            socket.scope.update(user=AnonymousUser(), url_route={'kwargs': {'room_code': self.game.room_code}})
            await socket.connect()
            await socket.receive_json_from()
            await socket.send_json_to({'type': 'host_join', 'hub_session': self.hub.code})
            self.assertEqual((await socket.receive_json_from())['type'], 'error')
            await socket.send_json_to({'type': 'admin_end_quiz', 'hub_session': self.hub.code})
            self.assertEqual((await socket.receive_json_from())['type'], 'action_rejected')
            await socket.disconnect()
        async_to_sync(scenario)()
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')

    def test_spectator_reveal_has_all_pins_and_winner_and_zone_config(self):
        from games_hub.spectator import _serialize_where
        for player in self.players:
            r.save_input(self.game, player, self.data(), confirm=True)
        public = _serialize_where(self.game, self.hub)
        self.assertNotIn('correct_location', public['question'])
        self.assertEqual(public['answers'], [])
        r.reveal(self.game)
        public = _serialize_where(self.game, self.hub)
        self.assertEqual(len(public['answers']), 25)
        self.assertEqual(sum(a['rank_position'] == 1 for a in public['answers']), 1)
        self.assertIsNone(public['zone_scoring'])
