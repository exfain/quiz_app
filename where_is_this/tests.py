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


class WhereLegacyZoneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('where-legacy-zone')
        self.question = WhereQuestion.objects.create(created_by=self.user, question_text='Legacy zones', correct_latitude=0, correct_longitude=0)

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
