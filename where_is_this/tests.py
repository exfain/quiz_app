import json
import uuid
from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.authoritative_state import attach_snapshot_metadata
from games_hub.views import get_leaderboard_data
from .consumers import WhereConsumer
from .geo import (
    WebMercatorCoordinateError,
    web_mercator_lat_lng_to_norm,
    web_mercator_norm_to_lat_lng,
)
from .models import (
    WhereAnswer,
    WhereDistanceZone,
    WhereParticipant,
    WhereQuestion,
    WhereQuiz,
    WhereSession,
)


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class WhereScoreBoxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='where-scorebox-user')
        self.quiz = WhereQuiz.objects.create(
            title='Where Score Box Quiz',
            room_code='9200',
            creator=self.user,
            status='active',
        )
        self.participant = WhereParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUB1',
        )
        self.session = WhereSession.objects.create(quiz=self.quiz)

    def _create_question(self, text, points, lat):
        return WhereQuestion.objects.create(
            question_text=text,
            correct_latitude=lat,
            correct_longitude=lat + 1,
            points=points,
            time_limit=45,
            created_by=self.user,
        )

    def test_play_view_builds_hydrated_scorebox_for_played_and_current_questions(self):
        question_one = self._create_question('Q1', 80, 10)
        question_two = self._create_question('Q2', 60, 20)
        question_three = self._create_question('Q3', 40, 30)
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_two
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 2
        self.session.current_question_number = 2
        self.session.is_question_active = True
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_latitude=question_one.correct_latitude,
            user_longitude=question_one.correct_longitude,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_one.id,
                    'number': 1,
                    'earned_points': 80,
                    'max_points': 80,
                    'status': 'played',
                },
                {
                    'id': question_two.id,
                    'number': 2,
                    'earned_points': None,
                    'max_points': 60,
                    'status': 'current',
                },
            ],
        )
        self.assertEqual(
            response.context['initial_progress_history'],
            [
                {
                    'question_id': question_one.id,
                    'question_number': 1,
                    'points': 80,
                    'max_points': 80,
                }
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 80)
        self.assertEqual(response.context['score_total_max'], 80)
        self.assertContains(response, 'whereScoreBox')
        self.assertContains(response, 'whereScoreTotal')
        self.assertContains(response, 'whereQuestionScoreboardData')
        self.assertContains(response, 'whereInitialProgressData')
        self.assertContains(response, 'sessionCode: \'HUB1\'')

    def test_play_view_does_not_trust_stale_sent_counter_for_unanswered_question(self):
        question_one = self._create_question('Q1', 80, 10)
        question_two = self._create_question('Q2', 60, 20)
        question_three = self._create_question('Q3', 50, 30)
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = None
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 2
        self.session.current_question_number = 2
        self.session.is_question_active = False
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_latitude=question_one.correct_latitude,
            user_longitude=question_one.correct_longitude,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_one.id,
                    'number': 1,
                    'earned_points': 80,
                    'max_points': 80,
                    'status': 'played',
                },
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 80)
        self.assertEqual(response.context['score_total_max'], 80)
        self.assertContains(response, '>80/80<', html=False)

    def test_play_view_keeps_scorebox_neutral_when_only_stale_session_counter_exists(self):
        question_one = self._create_question('Q1', 80, 10)
        question_two = self._create_question('Q2', 60, 20)
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_one.id, question_two.id]
        self.quiz.current_question = None
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 2
        self.session.current_question_number = 2
        self.session.is_question_active = False
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['question_scoreboard'], [])
        self.assertEqual(response.context['initial_progress_history'], [])
        self.assertEqual(response.context['score_total_earned'], 0)
        self.assertEqual(response.context['score_total_max'], 0)

    def test_play_view_uses_actual_send_order_for_out_of_order_current_question(self):
        question_one = self._create_question('Q1', 80, 10)
        question_two = self._create_question('Q2', 60, 20)
        question_three = self._create_question('Q3', 50, 30)
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_three
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 2
        self.session.current_question_number = 2
        self.session.is_question_active = True
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_latitude=question_one.correct_latitude,
            user_longitude=question_one.correct_longitude,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry['id'] for entry in response.context['question_scoreboard']],
            [question_one.id, question_three.id],
        )
        self.assertEqual(response.context['current_question_id'], question_three.id)
        self.assertContains(response, 'moveQuestionToNextFreeScoreSlot(questionId, maxPoints = null)')

    def test_play_view_does_not_show_points_for_active_submitted_question(self):
        question = self._create_question('Active Q', 80, 10)
        self.quiz.selected_questions.set([question])
        self.quiz.question_order = [question.id]
        self.quiz.current_question = question
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 1
        self.session.current_question_number = 1
        self.session.is_question_active = True
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            user_latitude=question.correct_latitude,
            user_longitude=question.correct_longitude,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['question_scoreboard'][0]['earned_points'], None)
        self.assertEqual(response.context['question_scoreboard'][0]['status'], 'current')
        self.assertEqual(response.context['initial_progress_history'], [])


class WhereWebMercatorTests(TestCase):
    def test_web_mercator_norm_to_lat_lng_uses_expected_world_bounds(self):
        lat, lng = web_mercator_norm_to_lat_lng(0.5, 0.5)
        self.assertAlmostEqual(lat, 0.0, places=6)
        self.assertAlmostEqual(lng, 0.0, places=6)

        equator_lat, west_lng = web_mercator_norm_to_lat_lng(0, 0.5)
        self.assertAlmostEqual(equator_lat, 0.0, places=6)
        self.assertAlmostEqual(west_lng, -180.0, places=6)

        equator_lat, east_lng = web_mercator_norm_to_lat_lng(1, 0.5)
        self.assertAlmostEqual(equator_lat, 0.0, places=6)
        self.assertAlmostEqual(east_lng, 180.0, places=6)

        north_lat, center_lng = web_mercator_norm_to_lat_lng(0.5, 0)
        self.assertAlmostEqual(north_lat, 85.05112878, places=6)
        self.assertAlmostEqual(center_lng, 0.0, places=6)

        south_lat, center_lng = web_mercator_norm_to_lat_lng(0.5, 1)
        self.assertAlmostEqual(south_lat, -85.05112878, places=6)
        self.assertAlmostEqual(center_lng, 0.0, places=6)

    def test_web_mercator_rejects_invalid_normalized_coordinates(self):
        with self.assertRaises(WebMercatorCoordinateError):
            web_mercator_norm_to_lat_lng(-0.01, 0.5)
        with self.assertRaises(WebMercatorCoordinateError):
            web_mercator_norm_to_lat_lng(0.5, 1.01)
        with self.assertRaises(WebMercatorCoordinateError):
            web_mercator_norm_to_lat_lng('not-a-number', 0.5)

    def test_lat_lng_round_trips_to_normalized_coordinates(self):
        landmarks = [
            (52.5200, 13.4050),     # Berlin
            (48.8566, 2.3522),      # Paris
            (40.7128, -74.0060),    # New York
            (-33.8688, 151.2093),   # Sydney
        ]
        for expected_lat, expected_lng in landmarks:
            with self.subTest(lat=expected_lat, lng=expected_lng):
                x_norm, y_norm = web_mercator_lat_lng_to_norm(expected_lat, expected_lng)
                lat, lng = web_mercator_norm_to_lat_lng(x_norm, y_norm)

                self.assertAlmostEqual(lat, expected_lat, places=6)
                self.assertAlmostEqual(lng, expected_lng, places=6)


class WhereHaversineDistanceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='where-distance-user')

    def _question_at(self, latitude, longitude):
        return WhereQuestion.objects.create(
            question_text='Distance check',
            correct_latitude=latitude,
            correct_longitude=longitude,
            created_by=self.user,
        )

    def assertDistanceAlmost(self, actual, expected, tolerance_km):
        self.assertLessEqual(abs(actual - expected), tolerance_km)

    def test_haversine_distance_uses_offline_calculation_for_known_landmarks(self):
        berlin = self._question_at(52.5200, 13.4050)
        self.assertDistanceAlmost(berlin.calculate_distance(52.5200, 13.4050), 0, 0.001)
        self.assertDistanceAlmost(berlin.calculate_distance(48.8566, 2.3522), 878, 10)

        london = self._question_at(51.5074, -0.1278)
        self.assertDistanceAlmost(london.calculate_distance(40.7128, -74.0060), 5570, 25)

        equator = self._question_at(0, 0)
        self.assertDistanceAlmost(equator.calculate_distance(0, 1), 111, 2)


class WhereScoringModeTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = WhereConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'where_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        return consumer

    def setUp(self):
        self.user = User.objects.create_user(username='where-scoring-user')
        self.question = WhereQuestion.objects.create(
            question_text='Where is Null Island?',
            correct_latitude=0,
            correct_longitude=0,
            points=100,
            time_limit=45,
            created_by=self.user,
        )

    def test_answer_saves_normalized_click_and_server_calculated_lat_lng(self):
        quiz = WhereQuiz.objects.create(
            title='Zone Where',
            room_code='9300',
            creator=self.user,
            status='active',
            current_question=self.question,
            scoring_mode='zones',
        )
        participant = WhereParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code='HUB1')
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=0,
            max_distance_km=1,
            points=7,
            label='Exact',
        )

        answer = WhereAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.question,
            x_norm=0.5,
            y_norm=0.5,
            user_latitude=42,
            user_longitude=42,
            time_taken=2,
        )

        self.assertAlmostEqual(answer.user_latitude, 0.0, places=6)
        self.assertAlmostEqual(answer.user_longitude, 0.0, places=6)
        self.assertLess(answer.distance_km, 1)
        self.assertEqual(answer.points_earned, 7)
        self.assertEqual(answer.zone_label, 'Exact')

    def test_websocket_submit_rejects_missing_or_invalid_normalized_marker(self):
        quiz = WhereQuiz.objects.create(
            title='Submit Where',
            room_code='9302',
            creator=self.user,
            status='active',
            current_question=self.question,
        )
        WhereParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code='HUB1')
        consumer = self.make_consumer(quiz)

        missing = async_to_sync(consumer.save_participant_answer)(
            'Ada',
            'HUB1',
            None,
            None,
            None,
            None,
            1,
        )
        invalid = async_to_sync(consumer.save_participant_answer)(
            'Ada',
            'HUB1',
            1.2,
            0.5,
            None,
            None,
            1,
        )

        self.assertIsNone(missing)
        self.assertIsNone(invalid)
        self.assertFalse(WhereAnswer.objects.filter(quiz=quiz).exists())

    def test_http_submit_uses_normalized_coordinates_over_client_lat_lng(self):
        quiz = WhereQuiz.objects.create(
            title='HTTP Submit Where',
            room_code='9303',
            creator=self.user,
            status='active',
            current_question=self.question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=1),
        )
        WhereSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        WhereParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code='HUB1')

        snapshot = attach_snapshot_metadata(
            {
                'phase': 'question_active',
                'game': {'id': quiz.id, 'status': 'active'},
                'question': {'id': self.question.id},
                'starts_at': quiz.question_start_time,
                'ends_at': quiz.session.question_end_time,
            },
            game_key='where',
            room_code=quiz.room_code,
            session_code='HUB1',
        )
        response = self.client.post(
            reverse('where_is_this:submit_answer', args=[quiz.room_code, 'Ada']),
            data=json.dumps({
                'x_norm': 0.5,
                'y_norm': 0.5,
                'latitude': 42,
                'longitude': 42,
                'time_taken': 1,
                'hub_session': 'HUB1',
                'game_id': snapshot['game_id'],
                'question_id': snapshot['current_question_id'],
                'round_id': snapshot.get('current_round_id'),
                'set_id': snapshot.get('current_set_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            }),
            content_type='application/json',
            QUERY_STRING='hub_session=HUB1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        answer = WhereAnswer.objects.get(quiz=quiz)
        self.assertEqual(answer.x_norm, 0.5)
        self.assertEqual(answer.y_norm, 0.5)
        self.assertAlmostEqual(answer.user_latitude, 0.0, places=6)
        self.assertAlmostEqual(answer.user_longitude, 0.0, places=6)
        self.assertLess(answer.distance_km, 1)

    def test_http_submit_without_marker_is_not_valid_answer(self):
        quiz = WhereQuiz.objects.create(
            title='Missing Marker Where',
            room_code='9304',
            creator=self.user,
            status='active',
            current_question=self.question,
        )
        WhereParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code='HUB1')

        response = self.client.post(
            reverse('where_is_this:submit_answer', args=[quiz.room_code, 'Ada']),
            data=json.dumps({'time_taken': 1, 'hub_session': 'HUB1'}),
            content_type='application/json',
            QUERY_STRING='hub_session=HUB1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertFalse(WhereAnswer.objects.filter(quiz=quiz).exists())

    def test_zone_scoring_uses_inclusive_lower_and_exclusive_upper_bounds(self):
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=0,
            max_distance_km=100,
            points=3,
            label='Near',
        )
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=100,
            max_distance_km=250,
            points=2,
            label='Close',
        )

        self.assertEqual(self.question.calculate_zone_score(99.999), 3)
        self.assertEqual(self.question.calculate_zone_score(100), 2)
        self.assertEqual(self.question.calculate_zone_score(250), 0)

    def test_zone_scoring_covers_boundaries_gaps_and_outside_distances(self):
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=0,
            max_distance_km=100,
            points=3,
            label='Zone 1',
        )
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=100,
            max_distance_km=250,
            points=2,
            label='Zone 2',
        )
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=250,
            max_distance_km=500,
            points=1,
            label='Zone 3',
        )

        expectations = {
            0: 3,
            50: 3,
            100: 2,
            249.9: 2,
            250: 1,
            499.9: 1,
            500: 0,
            800: 0,
        }
        for distance, points in expectations.items():
            with self.subTest(distance=distance):
                self.assertEqual(self.question.calculate_zone_score(distance), points)

    def test_zone_validation_rejects_invalid_and_overlapping_ranges(self):
        invalid_negative_min = WhereDistanceZone(
            question=self.question,
            min_distance_km=-1,
            max_distance_km=100,
            points=1,
        )
        invalid_range = WhereDistanceZone(
            question=self.question,
            min_distance_km=100,
            max_distance_km=100,
            points=1,
        )
        invalid_points = WhereDistanceZone(
            question=self.question,
            min_distance_km=500,
            max_distance_km=600,
            points=-1,
        )

        with self.assertRaises(ValidationError):
            invalid_negative_min.full_clean()
        with self.assertRaises(ValidationError):
            invalid_range.full_clean()
        with self.assertRaises(ValidationError):
            invalid_points.full_clean()

        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=0,
            max_distance_km=100,
            points=1,
        )
        overlapping = WhereDistanceZone(
            question=self.question,
            min_distance_km=50,
            max_distance_km=150,
            points=1,
        )
        with self.assertRaises(ValidationError):
            overlapping.full_clean()

    def test_zone_scoring_sorts_unsorted_zones_and_allows_gaps(self):
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=200,
            max_distance_km=300,
            points=2,
            label='Second',
        )
        WhereDistanceZone.objects.create(
            question=self.question,
            min_distance_km=0,
            max_distance_km=100,
            points=3,
            label='First',
        )

        zones = self.question.get_effective_distance_zones()
        self.assertEqual([zone['label'] for zone in zones], ['First', 'Second'])
        self.assertEqual(self.question.calculate_zone_score(150), 0)

    def test_ranking_mode_finalizes_points_by_distance_with_skipped_tie_rank(self):
        quiz = WhereQuiz.objects.create(
            title='Rank Where',
            room_code='9301',
            creator=self.user,
            status='active',
            current_question=self.question,
            scoring_mode='rank',
        )
        participants = [
            WhereParticipant.objects.create(quiz=quiz, name=name, hub_session_code='HUB1')
            for name in ['Anna', 'Ben', 'Carla', 'David']
        ]

        answers = [
            (participants[0], 0.5, 0.5),
            (participants[1], (1 + 180) / 360, 0.5),
            (participants[2], (1 + 180) / 360, 0.5),
            (participants[3], (10 + 180) / 360, 0.5),
        ]
        for participant, x_norm, y_norm in answers:
            WhereAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=self.question,
                x_norm=x_norm,
                y_norm=y_norm,
                user_latitude=0,
                user_longitude=0,
                time_taken=1,
            )

        consumer = self.make_consumer(quiz)
        payload = async_to_sync(consumer.finalize_current_question)(quiz.id, 'HUB1', False)

        refreshed = {
            answer.participant.name: answer
            for answer in WhereAnswer.objects.filter(quiz=quiz).select_related('participant')
        }
        self.assertEqual((refreshed['Anna'].rank_position, refreshed['Anna'].points_earned), (1, 4))
        self.assertEqual((refreshed['Ben'].rank_position, refreshed['Ben'].points_earned), (2, 3))
        self.assertEqual((refreshed['Carla'].rank_position, refreshed['Carla'].points_earned), (2, 3))
        self.assertEqual((refreshed['David'].rank_position, refreshed['David'].points_earned), (4, 1))
        self.assertEqual(payload['scoring_mode'], 'rank')
        self.assertEqual(
            [(entry['participant_name'], entry['rank_position'], entry['points_earned']) for entry in payload['answers']],
            [('Anna', 1, 4), ('Ben', 2, 3), ('Carla', 2, 3), ('David', 4, 1)],
        )

    def test_ranking_mode_finalizes_points_by_distance_without_ties_and_keeps_missing_answer_zero(self):
        quiz = WhereQuiz.objects.create(
            title='Rank No Tie Where',
            room_code='9305',
            creator=self.user,
            status='active',
            current_question=self.question,
            scoring_mode='rank',
        )
        participants = {
            name: WhereParticipant.objects.create(quiz=quiz, name=name, hub_session_code='HUB1')
            for name in ['A', 'B', 'C', 'D', 'Missing']
        }
        answer_longitudes = {
            'A': 0.09,
            'B': 0.45,
            'C': 1.8,
            'D': 4.5,
        }
        for name, longitude in answer_longitudes.items():
            WhereAnswer.objects.create(
                quiz=quiz,
                participant=participants[name],
                question=self.question,
                x_norm=(longitude + 180) / 360,
                y_norm=0.5,
                user_latitude=0,
                user_longitude=0,
                time_taken=1,
            )

        consumer = self.make_consumer(quiz)
        async_to_sync(consumer.finalize_current_question)(quiz.id, 'HUB1', False)

        refreshed_answers = {
            answer.participant.name: answer
            for answer in WhereAnswer.objects.filter(quiz=quiz).select_related('participant')
        }
        self.assertEqual((refreshed_answers['A'].rank_position, refreshed_answers['A'].points_earned), (1, 5))
        self.assertEqual((refreshed_answers['B'].rank_position, refreshed_answers['B'].points_earned), (2, 4))
        self.assertEqual((refreshed_answers['C'].rank_position, refreshed_answers['C'].points_earned), (3, 3))
        self.assertEqual((refreshed_answers['D'].rank_position, refreshed_answers['D'].points_earned), (4, 2))

        participants['Missing'].refresh_from_db()
        self.assertEqual(participants['Missing'].total_score, 0)
        self.assertFalse(WhereAnswer.objects.filter(participant=participants['Missing']).exists())

    def test_ranking_points_are_finalized_only_after_question_end_and_scorebox_uses_final_points(self):
        quiz = WhereQuiz.objects.create(
            title='Rank Scorebox Where',
            room_code='9306',
            creator=self.user,
            status='active',
            current_question=self.question,
            scoring_mode='rank',
        )
        WhereSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
        )
        quiz.selected_questions.set([self.question])
        quiz.question_order = [self.question.id]
        quiz.save(update_fields=['question_order'])
        participant = WhereParticipant.objects.create(quiz=quiz, name='Ada', hub_session_code='HUB1')

        answer = WhereAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.question,
            x_norm=0.5,
            y_norm=0.5,
            user_latitude=0,
            user_longitude=0,
            time_taken=1,
        )
        self.assertEqual(answer.points_earned, 0)

        active_response = self.client.get(
            reverse('where_is_this:play', args=[quiz.room_code, participant.name]),
            {'hub_session': participant.hub_session_code},
        )
        self.assertEqual(active_response.context['question_scoreboard'][0]['earned_points'], None)

        consumer = self.make_consumer(quiz)
        async_to_sync(consumer.finalize_current_question)(quiz.id, 'HUB1', False)
        quiz.session.is_question_active = False
        quiz.session.save(update_fields=['is_question_active'])
        quiz.current_question = None
        quiz.save(update_fields=['current_question'])

        reveal_response = self.client.get(
            reverse('where_is_this:play', args=[quiz.room_code, participant.name]),
            {'hub_session': participant.hub_session_code},
        )
        self.assertEqual(reveal_response.context['question_scoreboard'][0]['earned_points'], 1)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, 1)


class WhereManagementIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='where-admin',
            password='pw',
            is_staff=True,
        )
        self.client.force_login(self.user)

    def test_custom_quiz_can_store_scoring_modes(self):
        question = WhereQuestion.objects.create(
            question_text='Where is Berlin?',
            correct_latitude=52.52,
            correct_longitude=13.405,
            created_by=self.user,
        )

        response = self.client.post(
            reverse('admin_dashboard:create_where_custom_quiz'),
            data=json.dumps({
                'title': 'World Map Ranking',
                'question_ids': [question.id],
                'scoring_mode': 'rank',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        quiz = WhereQuiz.objects.get(id=response.json()['quiz_id'])
        self.assertEqual(quiz.scoring_mode, 'rank')
        self.assertEqual(list(quiz.selected_questions.values_list('id', flat=True)), [question.id])

    def test_add_question_stores_world_map_and_distance_zones(self):
        zones = [
            {'min_distance_km': 100, 'max_distance_km': 250, 'points': 2, 'label': 'Zone 2'},
            {'min_distance_km': 0, 'max_distance_km': 100, 'points': 3, 'label': 'Zone 1'},
        ]

        response = self.client.post(
            reverse('admin_dashboard:add_where_question'),
            data={
                'question_text': 'Where is Paris?',
                'time_limit': '60',
                'points': '100',
                'perfect_distance': '10',
                'good_distance': '100',
                'fair_distance': '500',
                'poor_distance': '2000',
                'latitude': '48.8566',
                'longitude': '2.3522',
                'map_type': 'world_mercator',
                'distance_zones': json.dumps(zones),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        question = WhereQuestion.objects.get(question_text='Where is Paris?')
        self.assertEqual(question.map_type, 'world_mercator')
        self.assertEqual(
            list(question.distance_zones.values_list('label', 'min_distance_km', 'max_distance_km', 'points')),
            [('Zone 1', 0.0, 100.0, 3), ('Zone 2', 100.0, 250.0, 2)],
        )

    def test_add_question_rejects_invalid_world_map_and_zone_payloads(self):
        invalid_payloads = [
            {'latitude': '86', 'longitude': '0', 'distance_zones': '[]'},
            {'latitude': '0', 'longitude': '181', 'distance_zones': '[]'},
            {
                'latitude': '0',
                'longitude': '0',
                'distance_zones': json.dumps([
                    {'min_distance_km': -1, 'max_distance_km': 100, 'points': 1},
                ]),
            },
            {
                'latitude': '0',
                'longitude': '0',
                'distance_zones': json.dumps([
                    {'min_distance_km': 100, 'max_distance_km': 100, 'points': 1},
                ]),
            },
            {
                'latitude': '0',
                'longitude': '0',
                'distance_zones': json.dumps([
                    {'min_distance_km': 0, 'max_distance_km': 100, 'points': -1},
                ]),
            },
            {
                'latitude': '0',
                'longitude': '0',
                'distance_zones': json.dumps([
                    {'min_distance_km': 0, 'max_distance_km': 100, 'points': 1},
                    {'min_distance_km': 50, 'max_distance_km': 150, 'points': 1},
                ]),
            },
        ]

        for payload in invalid_payloads:
            data = {
                'question_text': 'Invalid world map question',
                'time_limit': '60',
                'points': '100',
                'perfect_distance': '10',
                'good_distance': '100',
                'fair_distance': '500',
                'poor_distance': '2000',
                'map_type': 'world_mercator',
                **payload,
            }
            with self.subTest(payload=payload):
                response = self.client.post(reverse('admin_dashboard:add_where_question'), data=data)
                self.assertEqual(response.status_code, 400)


class WhereExternalResourceTemplateTests(TestCase):
    def test_where_templates_do_not_reference_external_map_resources(self):
        repo_root = Path(__file__).resolve().parents[1]
        template_paths = [
            repo_root / 'templates' / 'where_is_this' / 'play.html',
            repo_root / 'templates' / 'admin_dashboard' / 'where_monitor.html',
            repo_root / 'templates' / 'admin_dashboard' / 'where_management.html',
        ]
        forbidden_tokens = [
            'leaflet',
            'openstreetmap',
            'cartocdn',
            'leaflet-color-markers',
            'tileLayer',
            'L.map',
        ]
        for template_path in template_paths:
            content = template_path.read_text(encoding='utf-8').lower()
            for token in forbidden_tokens:
                with self.subTest(template=template_path.name, token=token):
                    self.assertNotIn(token.lower(), content)
            self.assertIn('world_web_mercator_blank_landmasses.svg', content)

    def test_where_host_send_question_button_has_payload_and_error_handling(self):
        repo_root = Path(__file__).resolve().parents[1]
        content = (repo_root / 'templates' / 'admin_dashboard' / 'where_monitor.html').read_text(encoding='utf-8')

        self.assertIn('type="button" class="btn btn-secondary send-question-btn"', content)
        self.assertIn("type: 'admin_send_question'", content)
        self.assertIn('question_id: questionId', content)
        self.assertIn('hub_session: this.getHubSession()', content)
        self.assertIn("case 'error':", content)
        self.assertIn('showHostError', content)
        self.assertIn('Verbindung zum Server ist nicht bereit', content)

    def test_where_host_end_quiz_uses_live_end_state_without_reload(self):
        repo_root = Path(__file__).resolve().parents[1]
        content = (repo_root / 'templates' / 'admin_dashboard' / 'where_monitor.html').read_text(encoding='utf-8')
        quiz_ended_start = content.find("case 'quiz_ended':")
        quiz_ended_end = content.find("case 'quiz_inactive':", quiz_ended_start)
        block = content[quiz_ended_start:quiz_ended_end]

        self.assertGreaterEqual(quiz_ended_start, 0)
        self.assertIn("type: 'admin_end_quiz'", content)
        self.assertIn('window.hostEndState?.apply', block)
        self.assertNotIn('location.reload()', block)


class WhereStartSyncTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = WhereConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'where_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_active_current_question_is_sent_on_participant_join(self):
        user = User.objects.create_user(username='where-player')
        question = WhereQuestion.objects.create(
            question_text='Where is the Eiffel Tower?',
            correct_latitude=48.8584,
            correct_longitude=2.2945,
            points=80,
            time_limit=45,
            hint_text='Paris landmark',
            created_by=user,
        )
        quiz = WhereQuiz.objects.create(
            title='Active Where',
            room_code='9201',
            creator=user,
            status='active',
            current_question=question,
        )
        session = WhereSession.objects.create(quiz=quiz)
        session.send_question(question)
        WhereParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB1',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_participant_join)({
            'participant_name': 'Ada',
            'hub_session': 'HUB1',
        })

        self.assertEqual([message['type'] for message in sent_messages], ['quiz_started', 'question_started'])
        self.assertEqual(sent_messages[1]['question']['id'], question.id)
        self.assertEqual(sent_messages[1]['question']['question_text'], question.question_text)
        self.assertEqual(sent_messages[1]['question']['points'], 80)
        self.assertNotIn('difficulty', sent_messages[1]['question'])
        self.assertEqual(sent_messages[1]['question']['time_limit'], 45)
        self.assertIsNone(sent_messages[1]['question']['image_url'])

    def test_admin_send_question_updates_session_progress_for_reload_safe_scorebox(self):
        user = User.objects.create_user(username='where-host-progress')
        first_question = WhereQuestion.objects.create(
            question_text='Where is Berlin?',
            correct_latitude=52.52,
            correct_longitude=13.405,
            points=100,
            time_limit=60,
            created_by=user,
        )
        second_question = WhereQuestion.objects.create(
            question_text='Where is Rome?',
            correct_latitude=41.9028,
            correct_longitude=12.4964,
            points=90,
            time_limit=45,
            created_by=user,
        )
        quiz = WhereQuiz.objects.create(
            title='Progress Where',
            room_code='9202',
            creator=user,
            status='active',
        )
        WhereSession.objects.create(quiz=quiz)
        consumer, _ = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({
            'question_id': first_question.id,
        })
        async_to_sync(consumer.handle_admin_send_question)({
            'question_id': second_question.id,
        })

        quiz.refresh_from_db()
        quiz.session.refresh_from_db()

        self.assertEqual(quiz.current_question_id, second_question.id)
        self.assertEqual(quiz.session.current_question_number, 2)
        self.assertEqual(quiz.session.total_questions_sent, 2)
        self.assertTrue(quiz.session.is_question_active)

    def test_admin_send_question_broadcasts_question_started_payload(self):
        user = User.objects.create_user(username='where-host-broadcast')
        question = WhereQuestion.objects.create(
            question_text='Where is Oslo?',
            correct_latitude=59.9139,
            correct_longitude=10.7522,
            points=70,
            time_limit=30,
            created_by=user,
        )
        quiz = WhereQuiz.objects.create(
            title='Broadcast Where',
            room_code='9203',
            creator=user,
            status='active',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({'question_id': question.id})

        quiz.refresh_from_db()
        self.assertEqual(quiz.current_question_id, question.id)
        self.assertEqual(sent_messages, [])
        group_messages = [message for _, message in consumer.channel_layer.group_messages]
        question_started = next(message for message in group_messages if message['type'] == 'question_started')
        self.assertEqual(question_started['question']['id'], question.id)
        self.assertEqual(question_started['question']['question_text'], 'Where is Oslo?')
        self.assertEqual(question_started['question']['time_limit'], 30)

    def test_admin_send_question_without_question_id_returns_visible_error(self):
        user = User.objects.create_user(username='where-host-missing-id')
        quiz = WhereQuiz.objects.create(
            title='Missing Id Where',
            room_code='9204',
            creator=user,
            status='active',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({})

        self.assertEqual(sent_messages[-1]['type'], 'error')
        self.assertIn('Frage-ID fehlt', sent_messages[-1]['message'])
        self.assertEqual(consumer.channel_layer.group_messages, [])

    def test_admin_send_question_with_unknown_question_returns_visible_error(self):
        user = User.objects.create_user(username='where-host-unknown-id')
        quiz = WhereQuiz.objects.create(
            title='Unknown Id Where',
            room_code='9205',
            creator=user,
            status='active',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({'question_id': 999999})

        self.assertEqual(sent_messages[-1]['type'], 'error')
        self.assertIn('nicht gefunden', sent_messages[-1]['message'])
        self.assertEqual(sent_messages[-1]['question_id'], 999999)
        self.assertEqual(consumer.channel_layer.group_messages, [])

    def test_admin_send_question_before_start_returns_visible_error(self):
        user = User.objects.create_user(username='where-host-before-start')
        question = WhereQuestion.objects.create(
            question_text='Where is Lisbon?',
            correct_latitude=38.7223,
            correct_longitude=-9.1393,
            created_by=user,
        )
        quiz = WhereQuiz.objects.create(
            title='Waiting Where',
            room_code='9206',
            creator=user,
            status='waiting',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({'question_id': question.id})

        self.assertEqual(sent_messages[-1]['type'], 'error')
        self.assertIn('Start the quiz', sent_messages[-1]['message'])
        self.assertEqual(sent_messages[-1]['question_id'], question.id)
        self.assertEqual(consumer.channel_layer.group_messages, [])

    def test_unknown_websocket_action_returns_visible_error(self):
        user = User.objects.create_user(username='where-host-unknown-action')
        quiz = WhereQuiz.objects.create(
            title='Unknown Action Where',
            room_code='9207',
            creator=user,
            status='active',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.receive)(json.dumps({'type': 'send_question'}))

        self.assertEqual(sent_messages[-1]['type'], 'error')
        self.assertIn('Unknown action: send_question', sent_messages[-1]['message'])


class WhereEndQuizTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='where-end-host', password='pw', is_staff=True)
        self.question = WhereQuestion.objects.create(
            question_text='Where is Null Island?',
            correct_latitude=0,
            correct_longitude=0,
            points=5,
            time_limit=60,
            created_by=self.user,
        )
        self.quiz = WhereQuiz.objects.create(
            title='Where End Flow',
            room_code='9299',
            creator=self.user,
            status='active',
            started_at=timezone.now(),
            current_question=self.question,
            scoring_mode='rank',
        )
        self.runtime = WhereSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
        )
        self.session = HubSession.objects.create(
            code='WHEREEND',
            name='Where End Session',
            is_active=True,
            started_at=timezone.now(),
        )
        HubParticipant.objects.create(session=self.session, nickname='Ada', scoring_eligible=True)
        HubParticipant.objects.create(session=self.session, nickname='Missing', scoring_eligible=True)
        self.step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='where',
            room_code=self.quiz.room_code,
            title=self.quiz.title,
        )
        self.ada = WhereParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code=self.session.code,
        )
        self.missing = WhereParticipant.objects.create(
            quiz=self.quiz,
            name='Missing',
            hub_session_code=self.session.code,
        )
        self.answer = WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.ada,
            question=self.question,
            x_norm=0.5,
            y_norm=0.5,
            user_latitude=0,
            user_longitude=0,
            time_taken=1,
        )

    def make_consumer(self):
        consumer = WhereConsumer()
        consumer.room_code = self.quiz.room_code
        consumer.room_group_name = f'where_{self.quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_end_quiz_finalizes_active_question_and_broadcasts_existing_end_flow(self):
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_quiz)({'hub_session': self.session.code})

        self.quiz.refresh_from_db()
        self.runtime.refresh_from_db()
        self.answer.refresh_from_db()
        self.ada.refresh_from_db()
        self.missing.refresh_from_db()
        self.assertEqual(sent_messages[-1]['type'], 'quiz_ended')
        self.assertEqual(sent_messages[-1]['status'], 'completed')
        self.assertEqual(sent_messages[-1]['quiz_id'], self.quiz.id)
        self.assertEqual(sent_messages[-1]['room_code'], self.quiz.room_code)
        self.assertFalse(sent_messages[-1]['can_start_questions'])
        self.assertFalse(sent_messages[-1]['can_answer'])
        self.assertEqual(
            sent_messages[-1]['final_scores'],
            [{'name': 'Ada', 'total_score': 2}, {'name': 'Missing', 'total_score': 0}],
        )
        self.assertEqual(self.quiz.status, 'completed')
        self.assertIsNotNone(self.quiz.ended_at)
        self.assertIsNone(self.quiz.current_question)
        self.assertIsNone(self.quiz.question_start_time)
        self.assertFalse(self.runtime.is_question_active)
        self.assertEqual(self.answer.rank_position, 1)
        self.assertEqual(self.answer.points_earned, 2)
        self.assertEqual(self.ada.total_score, 2)
        self.assertEqual(self.missing.total_score, 0)

        quiz_event = next(
            message for group, message in consumer.channel_layer.group_messages
            if group == consumer.room_group_name
        )
        self.assertEqual(quiz_event['type'], 'quiz_ended')
        self.assertEqual(
            quiz_event['final_scores'],
            [{'name': 'Ada', 'total_score': 2}, {'name': 'Missing', 'total_score': 0}],
        )
        hub_group, hub_event = next(
            (group, message) for group, message in consumer.channel_layer.group_messages
            if group.startswith('hub_')
        )
        self.assertEqual(hub_group, f'hub_{self.session.code}')
        self.assertEqual(hub_event['event']['type'], 'game_ended')
        self.assertEqual(hub_event['event']['final_scores'], sent_messages[-1]['final_scores'])

    def test_completed_quiz_rejects_questions_and_answers_and_rejoins_completed(self):
        consumer, sent_messages = self.make_consumer()
        async_to_sync(consumer.handle_admin_end_quiz)({'hub_session': self.session.code})
        consumer.channel_layer.group_messages.clear()

        async_to_sync(consumer.handle_admin_send_question)({'question_id': self.question.id})
        submitted = async_to_sync(consumer.save_participant_answer)(
            'Missing',
            self.session.code,
            0.5,
            0.5,
            None,
            None,
            1,
        )
        player_response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.ada.name]),
            {'hub_session': self.session.code},
        )

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'completed')
        self.assertIn('Start the quiz', sent_messages[-1]['message'])
        self.assertIsNone(submitted)
        self.assertContains(player_response, 'quizEndedState')
        self.assertFalse(WhereAnswer.objects.filter(participant=self.missing).exists())

    def test_end_quiz_keeps_final_scores_available_to_session_leaderboard(self):
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_quiz)({'hub_session': self.session.code})
        leaderboard = get_leaderboard_data(self.session)
        ada = next(row for row in leaderboard['participants'] if row['name'] == 'Ada')
        missing = next(row for row in leaderboard['participants'] if row['name'] == 'Missing')

        self.assertEqual(ada['game_scores'][f'step:{self.step.id}'], 2)
        self.assertEqual(missing['game_scores'][f'step:{self.step.id}'], 0)

    def test_end_quiz_without_started_question_still_completes_cleanly(self):
        self.runtime.end_current_question()
        self.quiz.refresh_from_db()
        consumer, _ = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_quiz)({'hub_session': self.session.code})

        self.quiz.refresh_from_db()
        self.runtime.refresh_from_db()
        self.assertEqual(self.quiz.status, 'completed')
        self.assertIsNone(self.quiz.current_question)
        self.assertFalse(self.runtime.is_question_active)

    def test_end_quiz_is_idempotent_and_does_not_broadcast_twice(self):
        consumer, sent_messages = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_quiz)({'hub_session': self.session.code})
        first_broadcast_count = len(consumer.channel_layer.group_messages)
        async_to_sync(consumer.handle_admin_end_quiz)({'hub_session': self.session.code})

        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.status, 'completed')
        self.assertEqual(len(consumer.channel_layer.group_messages), first_broadcast_count)
        self.assertEqual(sent_messages[-1]['type'], 'quiz_ended')
        self.assertEqual(sent_messages[-1]['status'], 'completed')
        self.assertFalse(sent_messages[-1]['can_start_questions'])

    def test_end_quiz_for_unknown_room_returns_error_without_broadcast(self):
        consumer, sent_messages = self.make_consumer()
        consumer.room_code = '0000'
        consumer.room_group_name = 'where_0000'

        async_to_sync(consumer.handle_admin_end_quiz)({'hub_session': self.session.code})

        self.assertEqual(sent_messages[-1], {'type': 'error', 'message': 'Quiz not found.'})
        self.assertEqual(consumer.channel_layer.group_messages, [])
