import json
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from QuizGame.models import Quiz, QuizParticipant
from games_hub.check_in import (
    complete_session_check_in,
    get_check_in_state,
    participant_check_in,
    reset_session_check_in,
    start_session_check_in,
)
from django.utils import timezone

from games_hub.models import HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession
from games_hub.views import get_leaderboard_data, get_post_game_results


class OverallScoringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='admin', password='pw')

    def _session(self, **kwargs):
        defaults = {
            'code': 'SCORE1',
            'name': 'Score Session',
            'is_active': False,
        }
        defaults.update(kwargs)
        return HubSession.objects.create(**defaults)

    def _quiz_step(self, session, room_code, order, title, status='completed'):
        quiz = Quiz.objects.create(
            title=title,
            room_code=room_code,
            creator=self.user,
            status=status,
        )
        step = HubGameStep.objects.create(
            session=session,
            order=order,
            game_key='quiz',
            room_code=room_code,
            title=title,
        )
        return quiz, step

    def _hub_participants(self, session, names):
        for name in names:
            HubParticipant.objects.create(session=session, nickname=name)

    def _complete_check_in(self, session, names):
        self._hub_participants(session, names)
        self.assertTrue(start_session_check_in(session)['success'])
        for name in names:
            self.assertTrue(participant_check_in(session, name)['success'])
        result = complete_session_check_in(session)
        self.assertTrue(result['success'], result)
        session.refresh_from_db()
        return session

    def _quiz_participant(self, quiz, session, name, score):
        return QuizParticipant.objects.create(
            quiz=quiz,
            name=name,
            total_score=score,
            hub_session_code=session.code,
        )

    def test_simple_mode_uses_concrete_game_names_and_linear_weighting(self):
        session = self._session(
            overall_scoring_mode=HubSession.OVERALL_SCORING_SIMPLE,
            overall_weighting_mode=HubSession.OVERALL_WEIGHTING_LINEAR_CAP,
        )
        self._hub_participants(session, ['Alice', 'Bob'])
        quiz_one, _ = self._quiz_step(session, '9101', 0, 'Geography Night')
        quiz_two, _ = self._quiz_step(session, '9102', 1, 'Music Round')
        self._quiz_participant(quiz_one, session, 'Alice', 10)
        self._quiz_participant(quiz_one, session, 'Bob', 5)
        self._quiz_participant(quiz_two, session, 'Alice', 2)
        self._quiz_participant(quiz_two, session, 'Bob', 8)

        data = get_leaderboard_data(session)
        first_key = data['games'][0]['key']
        second_key = data['games'][1]['key']
        alice = next(player for player in data['participants'] if player['name'] == 'Alice')
        bob = next(player for player in data['participants'] if player['name'] == 'Bob')

        self.assertEqual(data['games'][0]['title'], 'Geography Night')
        self.assertEqual(data['games'][0]['type'], 'Quick Quiz')
        self.assertEqual(data['games'][1]['title'], 'Music Round')
        self.assertEqual(data['games'][0]['weight'], 1)
        self.assertEqual(data['games'][1]['weight'], 1.15)
        self.assertEqual(alice['game_scores'][first_key], 10)
        self.assertEqual(alice['game_overall_scores'][second_key], 2.3)
        self.assertEqual(alice['weighted_score'], 12.3)
        self.assertEqual(bob['weighted_score'], 14.2)

    def test_ranking_mode_handles_ties_and_includes_zero_point_session_players(self):
        session = self._session(
            code='SCORE2',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        self._hub_participants(session, ['A', 'B', 'C', 'D'])
        quiz, _ = self._quiz_step(session, '9103', 0, 'Tie Round')
        self._quiz_participant(quiz, session, 'A', 10)
        self._quiz_participant(quiz, session, 'B', 8)
        self._quiz_participant(quiz, session, 'C', 8)

        data = get_leaderboard_data(session)
        game_key = data['games'][0]['key']
        by_name = {player['name']: player for player in data['participants']}

        self.assertEqual(by_name['A']['game_ranks'][game_key], 1)
        self.assertEqual(by_name['B']['game_ranks'][game_key], 2)
        self.assertEqual(by_name['C']['game_ranks'][game_key], 2)
        self.assertEqual(by_name['D']['game_ranks'][game_key], 4)
        self.assertEqual(by_name['A']['game_base_scores'][game_key], 4)
        self.assertEqual(by_name['B']['game_base_scores'][game_key], 3)
        self.assertEqual(by_name['C']['game_base_scores'][game_key], 3)
        self.assertEqual(by_name['D']['game_base_scores'][game_key], 1)
        self.assertEqual(by_name['D']['game_scores'][game_key], 0)

    def test_post_game_results_ranking_mode_without_weighting(self):
        session = self._session(
            code='POST1',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        self._hub_participants(session, ['Anna', 'Ben', 'Carla', 'David'])
        quiz, step = self._quiz_step(session, '9201', 0, 'Musikquiz Runde 1')
        self._quiz_participant(quiz, session, 'Anna', 10)
        self._quiz_participant(quiz, session, 'Ben', 8)
        self._quiz_participant(quiz, session, 'Carla', 8)

        data = get_post_game_results(session, step, participant_name='Ben')
        by_name = {row['participant']: row for row in data['rows']}

        self.assertTrue(data['available'])
        self.assertEqual(data['game']['title'], 'Musikquiz Runde 1')
        self.assertEqual(data['game']['type'], 'Quick Quiz')
        self.assertEqual(data['scoring_mode'], HubSession.OVERALL_SCORING_RANKING)
        self.assertEqual(data['weight_display'], '\u00d71.00')
        self.assertEqual(by_name['Anna']['rank'], 1)
        self.assertEqual(by_name['Ben']['rank'], 2)
        self.assertEqual(by_name['Carla']['rank'], 2)
        self.assertEqual(by_name['David']['rank'], 4)
        self.assertEqual(by_name['Anna']['overall_points'], 4)
        self.assertEqual(by_name['Ben']['overall_points'], 3)
        self.assertEqual(by_name['Carla']['overall_points'], 3)
        self.assertEqual(by_name['David']['overall_points'], 1)
        self.assertTrue(by_name['Ben']['is_current_participant'])

    def test_post_game_results_ranking_mode_with_linear_weighting(self):
        session = self._session(
            code='POST2',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
            overall_weighting_mode=HubSession.OVERALL_WEIGHTING_LINEAR_CAP,
        )
        self._hub_participants(session, ['Anna', 'Ben', 'Carla', 'David'])
        quiz, step = self._quiz_step(session, '9202', 1, 'Gewichtetes Spiel')
        self._quiz_participant(quiz, session, 'Anna', 10)
        self._quiz_participant(quiz, session, 'Ben', 8)
        self._quiz_participant(quiz, session, 'Carla', 8)

        data = get_post_game_results(session, step)
        by_name = {row['participant']: row for row in data['rows']}

        self.assertEqual(data['weight'], 1.15)
        self.assertEqual(data['weight_display'], '\u00d71.15')
        self.assertEqual(by_name['Anna']['overall_points'], 4.6)
        self.assertEqual(by_name['Ben']['overall_points'], 3.45)
        self.assertEqual(by_name['Carla']['overall_points'], 3.45)
        self.assertEqual(by_name['David']['overall_points'], 1.15)

    def test_post_game_results_simple_mode_uses_game_points_and_shows_rank(self):
        session = self._session(
            code='POST3',
            overall_scoring_mode=HubSession.OVERALL_SCORING_SIMPLE,
            overall_weighting_mode=HubSession.OVERALL_WEIGHTING_LINEAR_CAP,
        )
        self._hub_participants(session, ['Anna', 'Ben', 'Carla', 'David'])
        quiz, step = self._quiz_step(session, '9203', 1, 'Simple Gewichtung')
        self._quiz_participant(quiz, session, 'Anna', 10)
        self._quiz_participant(quiz, session, 'Ben', 8)
        self._quiz_participant(quiz, session, 'Carla', 8)

        data = get_post_game_results(session, step)
        by_name = {row['participant']: row for row in data['rows']}

        self.assertEqual(data['scoring_mode'], HubSession.OVERALL_SCORING_SIMPLE)
        self.assertEqual(by_name['Anna']['rank'], 1)
        self.assertEqual(by_name['Ben']['rank'], 2)
        self.assertEqual(by_name['Carla']['rank'], 2)
        self.assertEqual(by_name['David']['rank'], 4)
        self.assertEqual(by_name['Anna']['overall_points'], 11.5)
        self.assertEqual(by_name['Ben']['overall_points'], 9.2)
        self.assertEqual(by_name['Carla']['overall_points'], 9.2)
        self.assertEqual(by_name['David']['overall_points'], 0)

    def test_post_game_results_uses_snapshot_for_missing_auto_zero_and_late_join(self):
        session = self._session(
            code='POST4',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla', 'David'])
        HubParticipant.objects.filter(session=session, nickname='David').update(left_permanently_at=timezone.now())
        quiz, step = self._quiz_step(session, '9204', 0, 'Snapshot Spiel')
        HubGameParticipantSnapshot.create_for_step(step)
        HubParticipant.objects.create(session=session, nickname='Eva')
        self._quiz_participant(quiz, session, 'Anna', 10)
        self._quiz_participant(quiz, session, 'Ben', 8)
        self._quiz_participant(quiz, session, 'Carla', 8)
        self._quiz_participant(quiz, session, 'Eva', 99)

        data = get_post_game_results(session, step)
        by_name = {row['participant']: row for row in data['rows']}

        self.assertEqual(set(by_name.keys()), {'Anna', 'Ben', 'Carla', 'David'})
        self.assertEqual(by_name['David']['game_points'], 0)
        self.assertEqual(by_name['David']['overall_points'], 1)
        self.assertNotIn('Eva', by_name)

    def test_post_game_results_api_marks_current_participant(self):
        session = self._session(
            code='POST5',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        self._hub_participants(session, ['Anna', 'Ben'])
        quiz, step = self._quiz_step(session, '9205', 0, 'API Spiel')
        self._quiz_participant(quiz, session, 'Anna', 5)
        self._quiz_participant(quiz, session, 'Ben', 1)

        response = self.client.get(
            reverse('games_hub:post_game_results_api', args=[session.code]),
            {'game_key': 'quiz', 'room_code': step.room_code, 'participant': 'Anna'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        anna = next(row for row in payload['rows'] if row['participant'] == 'Anna')

        self.assertTrue(payload['available'])
        self.assertEqual(payload['game']['title'], 'API Spiel')
        self.assertTrue(anna['is_current_participant'])

    def test_post_game_results_include_contains_table_and_current_row_hook(self):
        template_path = Path(__file__).resolve().parents[1] / 'templates' / 'includes' / '_post_game_results.html'
        source = template_path.read_text(encoding='utf-8')

        self.assertIn('data-post-game-results', source)
        self.assertIn('post-game-results-table', source)
        self.assertIn('is-current-player', source)
        self.assertIn('Punkte im Spiel', source)
        self.assertIn('Punkte fuers Gesamtkonto', source)

    def test_scoring_settings_api_locks_after_first_game_start(self):
        self.client.force_login(self.user)
        session = self._session(code='SCORE3')
        url = reverse('games_hub:update_session_scoring_settings', args=[session.code])

        response = self.client.post(
            url,
            data=json.dumps({
                'overall_scoring_mode': 'ranking',
                'overall_weighting_mode': 'linear_cap',
                'weighting_step': 0.25,
                'weighting_cap': 1.75,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.overall_scoring_mode, HubSession.OVERALL_SCORING_RANKING)
        self.assertEqual(session.overall_weighting_mode, HubSession.OVERALL_WEIGHTING_LINEAR_CAP)
        self.assertEqual(session.weighting_step, 0.25)
        self.assertEqual(session.weighting_cap, 1.75)

        quiz, _ = self._quiz_step(session, '9104', 0, 'Started Round', status='active')
        self.assertTrue(quiz.status != 'waiting')
        response = self.client.post(
            url,
            data=json.dumps({'overall_scoring_mode': 'simple'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 409)
        session.refresh_from_db()
        self.assertEqual(session.overall_scoring_mode, HubSession.OVERALL_SCORING_RANKING)

    def test_check_in_completion_locks_official_pool_for_ranking(self):
        session = self._session(
            code='SCORE4',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        self._hub_participants(session, ['A', 'B', 'C'])

        self.assertTrue(start_session_check_in(session)['success'])
        self.assertTrue(participant_check_in(session, 'A')['success'])
        self.assertTrue(participant_check_in(session, 'B')['success'])
        result = complete_session_check_in(session)
        self.assertTrue(result['success'], result)
        session.refresh_from_db()
        self.assertEqual(session.check_in_status, HubSession.CHECK_IN_COMPLETED)
        self.assertEqual(session.locked_participant_count, 2)

        late = HubParticipant.objects.create(session=session, nickname='Late')
        self.assertFalse(late.scoring_eligible)
        check_in_state = get_check_in_state(session)
        late_entry = next(item for item in check_in_state['participants'] if item['nickname'] == 'Late')
        self.assertEqual(late_entry['official_status'], 'late')
        quiz, _ = self._quiz_step(session, '9105', 0, 'Locked Ranking')
        self._quiz_participant(quiz, session, 'A', 10)
        self._quiz_participant(quiz, session, 'B', 5)
        self._quiz_participant(quiz, session, 'Late', 99)

        data = get_leaderboard_data(session)
        game_key = data['games'][0]['key']
        by_name = {player['name']: player for player in data['participants']}

        self.assertEqual(set(by_name.keys()), {'A', 'B'})
        self.assertEqual(by_name['A']['game_base_scores'][game_key], 2)
        self.assertEqual(by_name['B']['game_base_scores'][game_key], 1)
        self.assertNotIn('Late', by_name)

    def test_completed_check_in_keeps_ranking_range_stable_after_disconnect_and_late_join(self):
        session = self._session(
            code='SCORE7',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        self._complete_check_in(session, ['A', 'B', 'C', 'D'])
        self.assertEqual(session.locked_participant_count, 4)

        HubParticipant.objects.filter(session=session, nickname='C').update(is_active=False)
        HubParticipant.objects.filter(session=session, nickname='C').update(is_active=True)
        late = HubParticipant.objects.create(session=session, nickname='Late')
        self.assertFalse(late.scoring_eligible)

        quiz, _ = self._quiz_step(session, '9107', 0, 'Stable Ranking Pool')
        self._quiz_participant(quiz, session, 'A', 10)
        self._quiz_participant(quiz, session, 'B', 8)
        self._quiz_participant(quiz, session, 'C', 8)
        self._quiz_participant(quiz, session, 'Late', 99)

        data = get_leaderboard_data(session)
        game_key = data['games'][0]['key']
        by_name = {player['name']: player for player in data['participants']}
        score_range = data['instances'][game_key]['score_range']

        self.assertEqual(set(by_name.keys()), {'A', 'B', 'C', 'D'})
        self.assertEqual(score_range['basis'], 'check_in')
        self.assertEqual(score_range['participant_count'], 4)
        self.assertEqual(score_range['min'], 1)
        self.assertEqual(score_range['max'], 4)
        self.assertEqual(data['settings']['ranking_pool']['participant_count'], 4)
        self.assertEqual(by_name['A']['game_base_scores'][game_key], 4)
        self.assertEqual(by_name['B']['game_base_scores'][game_key], 3)
        self.assertEqual(by_name['C']['game_base_scores'][game_key], 3)
        self.assertEqual(by_name['D']['game_base_scores'][game_key], 1)
        self.assertNotIn('Late', by_name)

    def test_simple_mode_uses_official_check_in_pool_and_ignores_late_game_scores(self):
        session = self._session(
            code='SCORE8',
            overall_scoring_mode=HubSession.OVERALL_SCORING_SIMPLE,
        )
        self._complete_check_in(session, ['Alice', 'Bob'])
        HubParticipant.objects.create(session=session, nickname='Late')

        quiz, _ = self._quiz_step(session, '9108', 0, 'Official Simple Pool')
        self._quiz_participant(quiz, session, 'Alice', 6)
        self._quiz_participant(quiz, session, 'Bob', 4)
        self._quiz_participant(quiz, session, 'Late', 100)

        data = get_leaderboard_data(session)
        game_key = data['games'][0]['key']
        by_name = {player['name']: player for player in data['participants']}

        self.assertEqual(set(by_name.keys()), {'Alice', 'Bob'})
        self.assertEqual(by_name['Alice']['game_scores'][game_key], 6)
        self.assertEqual(by_name['Bob']['game_scores'][game_key], 4)
        self.assertEqual(by_name['Alice']['weighted_score'], 6)
        self.assertEqual(by_name['Bob']['weighted_score'], 4)
        self.assertNotIn('Late', by_name)

    def test_ranking_range_preview_uses_theoretical_count_before_check_in(self):
        session = self._session(
            code='SCORE9',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        _, step = self._quiz_step(session, '9109', 0, 'Preview Round', status='waiting')

        data_without_preview = get_leaderboard_data(session)
        range_without_preview = data_without_preview['instances'][f'step:{step.id}']['score_range']
        self.assertFalse(range_without_preview['available'])
        self.assertIsNone(range_without_preview['max'])

        data_with_preview = get_leaderboard_data(session, participant_count_override=8)
        range_with_preview = data_with_preview['instances'][f'step:{step.id}']['score_range']
        self.assertTrue(range_with_preview['available'])
        self.assertEqual(range_with_preview['basis'], 'preview')
        self.assertEqual(range_with_preview['participant_count'], 8)
        self.assertEqual(range_with_preview['min'], 1)
        self.assertEqual(range_with_preview['max'], 8)
        self.assertIsNone(session.locked_participant_count)

    def test_ranking_range_preview_is_ignored_after_completed_check_in(self):
        session = self._session(
            code='SCORE10',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        self._complete_check_in(session, ['A', 'B', 'C', 'D', 'E'])
        _, step = self._quiz_step(session, '9110', 0, 'Locked Preview Round', status='waiting')

        data = get_leaderboard_data(session, participant_count_override=8)
        score_range = data['instances'][f'step:{step.id}']['score_range']

        self.assertEqual(session.locked_participant_count, 5)
        self.assertEqual(score_range['basis'], 'check_in')
        self.assertEqual(score_range['participant_count'], 5)
        self.assertEqual(score_range['max'], 5)
        self.assertEqual(data['settings']['ranking_pool']['participant_count'], 5)

    def test_leaderboard_api_rejects_invalid_preview_participant_count(self):
        session = self._session(
            code='SCORE11',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        response = self.client.get(
            f"{reverse('games_hub:session_leaderboard_api', args=[session.code])}?participant_count_override=0"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Teilnehmerzahl', response.json()['error'])

    def test_check_in_cannot_change_after_first_game_started(self):
        session = self._session(code='SCORE5')
        self._hub_participants(session, ['A'])
        self.assertTrue(start_session_check_in(session)['success'])
        quiz, _ = self._quiz_step(session, '9106', 0, 'Active Round', status='active')
        self.assertEqual(quiz.status, 'active')

        reset_result = reset_session_check_in(session)
        complete_result = complete_session_check_in(session)

        self.assertFalse(reset_result['success'])
        self.assertFalse(complete_result['success'])

    def test_check_in_http_flow_sets_locked_count(self):
        self.client.force_login(self.user)
        session = self._session(code='SCORE6')
        self._hub_participants(session, ['Alice', 'Bob'])

        response = self.client.post(reverse('games_hub:start_check_in', args=[session.code]))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            reverse('games_hub:participant_check_in_api', args=[session.code]),
            data=json.dumps({'nickname': 'Alice'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse('games_hub:complete_check_in', args=[session.code]))
        self.assertEqual(response.status_code, 200)

        session.refresh_from_db()
        alice = HubParticipant.objects.get(session=session, nickname='Alice')
        bob = HubParticipant.objects.get(session=session, nickname='Bob')
        self.assertEqual(session.locked_participant_count, 1)
        self.assertTrue(alice.scoring_eligible)
        self.assertFalse(bob.scoring_eligible)

    def test_check_in_start_api_exposes_open_state_to_host_and_participants(self):
        self.client.force_login(self.user)
        session = self._session(code='SCORE12')
        self._hub_participants(session, ['Alice', 'Bob'])

        response = self.client.post(reverse('games_hub:start_check_in', args=[session.code]))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        session.refresh_from_db()

        self.assertEqual(session.check_in_status, HubSession.CHECK_IN_OPEN)
        self.assertEqual(payload['check_in']['status'], HubSession.CHECK_IN_OPEN)
        self.assertEqual(payload['counts']['total_participants'], 2)
        self.assertEqual(payload['counts']['checked_in'], 0)

        state_response = self.client.get(reverse('games_hub:session_check_in_state_api', args=[session.code]))
        self.assertEqual(state_response.status_code, 200)
        state = state_response.json()
        self.assertEqual(state['check_in']['status'], HubSession.CHECK_IN_OPEN)
        self.assertEqual(
            {participant['nickname']: participant['official_status'] for participant in state['participants']},
            {'Alice': 'pending', 'Bob': 'pending'},
        )

    def test_participant_check_in_api_marks_player_and_updates_host_state(self):
        self.client.force_login(self.user)
        session = self._session(code='SCORE13')
        self._hub_participants(session, ['Alice'])
        self.client.post(reverse('games_hub:start_check_in', args=[session.code]))

        response = self.client.post(
            reverse('games_hub:participant_check_in_api', args=[session.code]),
            data=json.dumps({'nickname': 'Alice'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        alice = HubParticipant.objects.get(session=session, nickname='Alice')
        self.assertIsNotNone(alice.checked_in_at)
        self.assertTrue(alice.scoring_eligible)
        payload = response.json()
        alice_state = next(participant for participant in payload['participants'] if participant['nickname'] == 'Alice')
        self.assertTrue(alice_state['checked_in'])
        self.assertTrue(alice_state['scoring_eligible'])
        self.assertEqual(alice_state['official_status'], 'checked_in')
        self.assertEqual(payload['counts']['checked_in'], 1)

    def test_completed_check_in_state_reconstructs_official_pool_after_reload(self):
        self.client.force_login(self.user)
        session = self._session(code='SCORE14')
        self._hub_participants(session, ['Alice', 'Bob'])
        self.client.post(reverse('games_hub:start_check_in', args=[session.code]))
        self.client.post(
            reverse('games_hub:participant_check_in_api', args=[session.code]),
            data=json.dumps({'nickname': 'Alice'}),
            content_type='application/json',
        )

        response = self.client.post(reverse('games_hub:complete_check_in', args=[session.code]))
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()

        self.assertEqual(session.check_in_status, HubSession.CHECK_IN_COMPLETED)
        self.assertEqual(session.locked_participant_count, 1)
        self.assertEqual(
            list(session.get_official_participants().values_list('nickname', flat=True)),
            ['Alice'],
        )

        reload_response = self.client.get(reverse('games_hub:session_check_in_state_api', args=[session.code]))
        self.assertEqual(reload_response.status_code, 200)
        state = reload_response.json()
        self.assertEqual(state['check_in']['status'], HubSession.CHECK_IN_COMPLETED)
        self.assertEqual(state['check_in']['locked_participant_count'], 1)
        by_name = {participant['nickname']: participant for participant in state['participants']}
        self.assertEqual(by_name['Alice']['official_status'], 'official')
        self.assertEqual(by_name['Bob']['official_status'], 'not_official')

    def test_monitor_and_lobby_reload_render_check_in_controls(self):
        self.client.force_login(self.user)
        session = self._session(code='SCORE15', name='Reload Check-in')

        monitor_response = self.client.get(reverse('games_hub:monitor', args=[session.code]))
        self.assertEqual(monitor_response.status_code, 200)
        self.assertContains(monitor_response, 'startCheckInBtn')
        self.assertContains(monitor_response, 'completeCheckInBtn')
        self.assertContains(monitor_response, 'checkInParticipantRows')

        lobby_response = self.client.get(reverse('games_hub:lobby', args=[session.code]))
        self.assertEqual(lobby_response.status_code, 200)
        self.assertContains(lobby_response, 'checkInCard')
        self.assertContains(lobby_response, 'readyCheckInBtn')

    def test_leaderboard_api_preview_override_is_display_only_and_locked_after_check_in(self):
        session = self._session(
            code='SCORE16',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        _, step = self._quiz_step(session, '9116', 0, 'API Preview', status='waiting')

        preview_response = self.client.get(
            f"{reverse('games_hub:session_leaderboard_api', args=[session.code])}?participant_count_override=8"
        )
        self.assertEqual(preview_response.status_code, 200)
        preview_payload = preview_response.json()
        preview_range = preview_payload['instances'][f'step:{step.id}']['score_range']
        self.assertEqual(preview_range['basis'], 'preview')
        self.assertEqual(preview_range['max'], 8)
        session.refresh_from_db()
        self.assertIsNone(session.locked_participant_count)

        self._complete_check_in(session, ['A', 'B', 'C', 'D', 'E'])
        locked_response = self.client.get(
            f"{reverse('games_hub:session_leaderboard_api', args=[session.code])}?participant_count_override=8"
        )
        self.assertEqual(locked_response.status_code, 200)
        locked_payload = locked_response.json()
        locked_range = locked_payload['instances'][f'step:{step.id}']['score_range']

        self.assertEqual(session.locked_participant_count, 5)
        self.assertEqual(locked_range['basis'], 'check_in')
        self.assertEqual(locked_range['max'], 5)
