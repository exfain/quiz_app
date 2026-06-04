from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant
from games_hub.check_in import (
    complete_session_check_in,
    participant_check_in,
    start_session_check_in,
)
from games_hub.active_game_guard import resolve_session_game_activation
from games_hub.models import HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession
from games_hub.views import get_leaderboard_data


class SessionEveningSimulationTests(TestCase):
    """Model/service-level simulations for complete multi-game hub sessions."""

    def setUp(self):
        self.user = User.objects.create_user(username='session_evening_admin', password='pw')

    def _session(self, code, scoring_mode=HubSession.OVERALL_SCORING_RANKING, weighting_mode=HubSession.OVERALL_WEIGHTING_NONE):
        return HubSession.objects.create(
            code=code,
            name=f'Session {code}',
            overall_scoring_mode=scoring_mode,
            overall_weighting_mode=weighting_mode,
        )

    def _complete_check_in(self, session, names):
        for name in names:
            HubParticipant.objects.get_or_create(session=session, nickname=name)
        result = start_session_check_in(session)
        self.assertTrue(result['success'], result)
        for name in names:
            result = participant_check_in(session, name)
            self.assertTrue(result['success'], result)
        result = complete_session_check_in(session)
        self.assertTrue(result['success'], result)
        session.refresh_from_db()
        return session

    def _add_completed_quiz_game(self, session, order, title, scores, create_snapshot=True):
        quiz = Quiz.objects.create(
            title=title,
            room_code=f'{session.code}{order:02d}',
            creator=self.user,
            status='active',
        )
        step = HubGameStep.objects.create(
            session=session,
            order=order,
            game_key='quiz',
            room_code=quiz.room_code,
            title=title,
        )
        if create_snapshot:
            HubGameParticipantSnapshot.create_for_step(step)
        for name, score in scores.items():
            QuizParticipant.objects.create(
                quiz=quiz,
                name=name,
                total_score=score,
                hub_session_code=session.code,
            )
        quiz.status = 'completed'
        quiz.save(update_fields=['status'])
        return quiz, step

    def _participants_by_name(self, leaderboard):
        return {participant['name']: participant for participant in leaderboard['participants']}

    def _instance_key(self, step):
        return f'step:{step.id}'

    def test_game_activation_creates_snapshot_once_and_keeps_it_stable(self):
        session = self._session('EVS')
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla', 'David'])
        quiz = Quiz.objects.create(
            title='Snapshot Start',
            room_code='EVS00',
            creator=self.user,
            status='waiting',
        )
        step = HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
        )

        result = resolve_session_game_activation(session.code, 'quiz', quiz.room_code)
        self.assertTrue(result['success'], result)
        self.assertEqual(HubGameParticipantSnapshot.objects.filter(game_step=step).count(), 4)

        eva = HubParticipant.objects.create(session=session, nickname='Eva', scoring_eligible=True)
        result = resolve_session_game_activation(session.code, 'quiz', quiz.room_code)
        self.assertTrue(result['success'], result)
        snapshot_names = set(
            HubGameParticipantSnapshot.objects
            .filter(game_step=step)
            .values_list('participant__nickname', flat=True)
        )

        self.assertEqual(HubGameParticipantSnapshot.objects.filter(game_step=step).count(), 4)
        self.assertNotIn(eva.nickname, snapshot_names)

    def test_scenario_a_normal_ranking_evening_with_two_games_and_ties(self):
        session = self._session('EVA')
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla', 'David'])

        _, step_one = self._add_completed_quiz_game(
            session,
            0,
            'Spiel 1: Wissen',
            {'Anna': 10, 'Ben': 8, 'Carla': 8, 'David': 0},
        )
        _, step_two = self._add_completed_quiz_game(
            session,
            1,
            'Spiel 2: Musik',
            {'Anna': 2, 'Ben': 12, 'Carla': 12, 'David': 6},
        )

        leaderboard = get_leaderboard_data(session)
        by_name = self._participants_by_name(leaderboard)
        key_one = self._instance_key(step_one)
        key_two = self._instance_key(step_two)

        self.assertEqual(set(by_name), {'Anna', 'Ben', 'Carla', 'David'})
        self.assertEqual(leaderboard['instances'][key_one]['score_range']['max'], 4)
        self.assertEqual(leaderboard['instances'][key_two]['score_range']['max'], 4)
        self.assertEqual(by_name['Anna']['game_base_scores'][key_one], 4)
        self.assertEqual(by_name['Ben']['game_base_scores'][key_one], 3)
        self.assertEqual(by_name['Carla']['game_base_scores'][key_one], 3)
        self.assertEqual(by_name['David']['game_base_scores'][key_one], 1)
        self.assertEqual(by_name['Anna']['game_base_scores'][key_two], 1)
        self.assertEqual(by_name['Ben']['game_base_scores'][key_two], 4)
        self.assertEqual(by_name['Carla']['game_base_scores'][key_two], 4)
        self.assertEqual(by_name['David']['game_base_scores'][key_two], 2)
        self.assertEqual(by_name['Anna']['weighted_score'], 5)
        self.assertEqual(by_name['Ben']['weighted_score'], 7)
        self.assertEqual(by_name['Carla']['weighted_score'], 7)
        self.assertEqual(by_name['David']['weighted_score'], 3)

    def test_scenario_b_late_join_future_game_snapshot_does_not_recalculate_past_games(self):
        session = self._session('EVB')
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla', 'David'])
        _, step_one = self._add_completed_quiz_game(
            session,
            0,
            'Spiel 1',
            {'Anna': 10, 'Ben': 8, 'Carla': 8, 'David': 0},
        )

        eva = HubParticipant.objects.create(session=session, nickname='Eva')
        eva.scoring_eligible = True
        eva.save(update_fields=['scoring_eligible'])
        _, step_two = self._add_completed_quiz_game(
            session,
            1,
            'Spiel 2',
            {'Anna': 5, 'Ben': 4, 'Carla': 3, 'David': 2, 'Eva': 12},
        )

        leaderboard = get_leaderboard_data(session)
        by_name = self._participants_by_name(leaderboard)
        key_one = self._instance_key(step_one)
        key_two = self._instance_key(step_two)
        step_one_snapshots = HubGameParticipantSnapshot.objects.filter(game_step=step_one, included_in_scoring=True)
        step_two_snapshots = HubGameParticipantSnapshot.objects.filter(game_step=step_two, included_in_scoring=True)

        self.assertEqual(step_one_snapshots.count(), 4)
        self.assertEqual(step_two_snapshots.count(), 5)
        self.assertNotIn('Eva', set(step_one_snapshots.values_list('participant__nickname', flat=True)))
        self.assertIn('Eva', set(step_two_snapshots.values_list('participant__nickname', flat=True)))
        self.assertNotIn(key_one, by_name['Eva']['game_scores'])
        self.assertIn(key_two, by_name['Eva']['game_scores'])
        self.assertEqual(leaderboard['instances'][key_one]['score_range']['max'], 4)
        self.assertEqual(leaderboard['instances'][key_two]['score_range']['max'], 5)

    def test_scenario_c_inactive_official_player_gets_zero_in_future_game_without_losing_old_points(self):
        session = self._session('EVC')
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla', 'David', 'Eva'])
        _, step_one = self._add_completed_quiz_game(
            session,
            0,
            'Spiel 1',
            {'Anna': 10, 'Ben': 9, 'Carla': 7, 'David': 4, 'Eva': 1},
        )
        HubParticipant.objects.filter(session=session, nickname='Ben').update(left_permanently_at=timezone.now())
        _, step_two = self._add_completed_quiz_game(
            session,
            1,
            'Spiel 2',
            {'Anna': 11, 'Carla': 8, 'David': 6, 'Eva': 2},
        )

        leaderboard = get_leaderboard_data(session)
        by_name = self._participants_by_name(leaderboard)
        key_one = self._instance_key(step_one)
        key_two = self._instance_key(step_two)
        ben_snapshot = HubGameParticipantSnapshot.objects.get(game_step=step_two, participant__nickname='Ben')

        self.assertTrue(ben_snapshot.included_in_scoring)
        self.assertFalse(ben_snapshot.active_player)
        self.assertTrue(ben_snapshot.auto_zero)
        self.assertEqual(ben_snapshot.reason, HubGameParticipantSnapshot.REASON_LEFT_PERMANENTLY)
        self.assertEqual(leaderboard['instances'][key_two]['score_range']['max'], 5)
        self.assertEqual(by_name['Ben']['game_scores'][key_one], 9)
        self.assertEqual(by_name['Ben']['game_scores'][key_two], 0)
        self.assertEqual(by_name['Ben']['game_base_scores'][key_two], 1)
        self.assertEqual(by_name['Ben']['weighted_score'], 5)

    def test_scenario_d_ranking_evening_uses_linear_weighting_per_game_number(self):
        session = self._session(
            'EVD',
            scoring_mode=HubSession.OVERALL_SCORING_RANKING,
            weighting_mode=HubSession.OVERALL_WEIGHTING_LINEAR_CAP,
        )
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla', 'David'])
        _, step_one = self._add_completed_quiz_game(
            session,
            0,
            'Spiel 1',
            {'Anna': 10, 'Ben': 7, 'Carla': 5, 'David': 0},
        )
        _, step_two = self._add_completed_quiz_game(
            session,
            1,
            'Spiel 2',
            {'Anna': 0, 'Ben': 10, 'Carla': 7, 'David': 5},
        )
        _, step_three = self._add_completed_quiz_game(
            session,
            2,
            'Spiel 3',
            {'Anna': 7, 'Ben': 5, 'Carla': 10, 'David': 0},
        )

        leaderboard = get_leaderboard_data(session)
        by_name = self._participants_by_name(leaderboard)
        key_one = self._instance_key(step_one)
        key_two = self._instance_key(step_two)
        key_three = self._instance_key(step_three)

        self.assertEqual(leaderboard['instances'][key_one]['weight'], 1)
        self.assertEqual(leaderboard['instances'][key_two]['weight'], 1.15)
        self.assertEqual(leaderboard['instances'][key_three]['weight'], 1.3)
        self.assertEqual(leaderboard['instances'][key_one]['score_range']['weighted_max'], 4)
        self.assertEqual(leaderboard['instances'][key_two]['score_range']['weighted_max'], 4.6)
        self.assertEqual(leaderboard['instances'][key_three]['score_range']['weighted_max'], 5.2)
        self.assertEqual(by_name['Anna']['weighted_score'], 9.05)
        self.assertEqual(by_name['Ben']['weighted_score'], 10.2)
        self.assertEqual(by_name['Carla']['weighted_score'], 10.65)
        self.assertEqual(by_name['David']['weighted_score'], 4.6)

    def test_scenario_e_simple_mode_adds_game_points_and_ignores_late_non_official_scores(self):
        session = self._session('EVE', scoring_mode=HubSession.OVERALL_SCORING_SIMPLE)
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla', 'David'])
        _, step_one = self._add_completed_quiz_game(
            session,
            0,
            'Spiel 1',
            {'Anna': 10, 'Ben': 8, 'Carla': 6, 'David': 4},
        )
        HubParticipant.objects.create(session=session, nickname='Eva')
        HubParticipant.objects.filter(session=session, nickname='Ben').update(left_permanently_at=timezone.now())
        _, step_two = self._add_completed_quiz_game(
            session,
            1,
            'Spiel 2',
            {'Anna': 1, 'Carla': 3, 'David': 5, 'Eva': 100},
        )

        leaderboard = get_leaderboard_data(session)
        by_name = self._participants_by_name(leaderboard)
        key_one = self._instance_key(step_one)
        key_two = self._instance_key(step_two)

        self.assertEqual(set(by_name), {'Anna', 'Ben', 'Carla', 'David'})
        self.assertEqual(by_name['Anna']['game_base_scores'][key_one], 10)
        self.assertEqual(by_name['Anna']['game_base_scores'][key_two], 1)
        self.assertEqual(by_name['Anna']['weighted_score'], 11)
        self.assertEqual(by_name['Ben']['game_scores'][key_two], 0)
        self.assertEqual(by_name['Ben']['weighted_score'], 8)
        self.assertEqual(by_name['Carla']['weighted_score'], 9)
        self.assertEqual(by_name['David']['weighted_score'], 9)
        self.assertNotIn('Eva', by_name)
