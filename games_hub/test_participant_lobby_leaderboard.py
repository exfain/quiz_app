from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant
from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.views import get_participant_leaderboard_data


def leaderboard_fixture(names_and_scores):
    games = [
        {'key': 'step:1', 'game_number': 1, 'status': 'completed'},
        {'key': 'step:2', 'game_number': 2, 'status': 'active'},
    ]
    participants = []
    for participant_id, (name, score) in enumerate(names_and_scores, start=1):
        participants.append({
            'name': name,
            'hub_participant_id': participant_id,
            'weighted_score': score,
            'total_with_adjustment': score,
            'game_scores': {'step:1': score},
            'game_overall_scores': {'step:1': score},
        })
    return {'games': games, 'participants': participants, 'instances': {}, 'settings': {}}


class ParticipantLeaderboardProjectionTests(SimpleTestCase):
    def project(self, rows, participant_name):
        leaderboard = leaderboard_fixture(rows)
        current_row = next(
            (row for row in leaderboard['participants'] if row['name'] == participant_name),
            None,
        )
        session = Mock(code='CURRENT')
        session.get_official_participants.return_value.filter.return_value.only.return_value.first.return_value = (
            SimpleNamespace(id=current_row['hub_participant_id']) if current_row else None
        )
        with patch('games_hub.views.get_leaderboard_data', return_value=leaderboard):
            return get_participant_leaderboard_data(session, participant_name)

    def test_only_participant_is_the_only_visible_row(self):
        data = self.project([('Anna', 12)], 'Anna')

        self.assertEqual([row['name'] for row in data['participants']], ['Anna'])
        self.assertTrue(data['participants'][0]['is_current_participant'])
        self.assertEqual(data['participants'][0]['rank'], 1)

    def test_middle_participant_gets_direct_neighbours_only(self):
        rows = [('Anna', 50), ('Ben', 40), ('Cara', 30), ('Dora', 20), ('Emil', 10)]

        data = self.project(rows, 'Cara')

        self.assertEqual([row['name'] for row in data['participants']], ['Ben', 'Cara', 'Dora'])
        self.assertEqual([row['rank'] for row in data['participants']], [2, 3, 4])

    def test_first_and_last_participants_have_no_artificial_empty_neighbour(self):
        rows = [('Anna', 30), ('Ben', 20), ('Cara', 10)]

        first = self.project(rows, 'Anna')
        last = self.project(rows, 'Cara')

        self.assertEqual([row['name'] for row in first['participants']], ['Anna', 'Ben'])
        self.assertEqual([row['name'] for row in last['participants']], ['Ben', 'Cara'])

    def test_two_participants_keep_the_authoritative_order(self):
        data = self.project([('Anna', 20), ('Ben', 10)], 'Ben')

        self.assertEqual([row['name'] for row in data['participants']], ['Anna', 'Ben'])

    def test_ties_keep_the_existing_authoritative_order(self):
        data = self.project([('Anna', 20), ('Ben', 20), ('Cara', 5)], 'Ben')

        self.assertEqual([row['name'] for row in data['participants']], ['Anna', 'Ben', 'Cara'])
        self.assertEqual([row['rank'] for row in data['participants']], [1, 2, 3])

    def test_only_completed_games_are_returned_to_participant_lobby(self):
        data = self.project([('Anna', 20)], 'Anna')

        self.assertEqual([game['game_number'] for game in data['games']], [1])

    def test_missing_current_participant_returns_no_foreign_rows(self):
        with self.assertLogs('games_hub.views', level='WARNING'):
            data = self.project([('Anna', 20)], 'Unbekannt')

        self.assertFalse(data['participant_found'])
        self.assertEqual(data['participants'], [])

    def test_name_match_without_authoritative_hub_participant_id_is_not_marked_as_self(self):
        leaderboard = leaderboard_fixture([('Anna', 20), ('Cara', 10)])
        session = Mock(code='CURRENT')
        session.get_official_participants.return_value.filter.return_value.only.return_value.first.return_value = (
            SimpleNamespace(id=999)
        )

        with patch('games_hub.views.get_leaderboard_data', return_value=leaderboard):
            with self.assertLogs('games_hub.views', level='WARNING'):
                data = get_participant_leaderboard_data(session, 'Cara')

        self.assertFalse(data['participant_found'])
        self.assertEqual(data['participants'], [])


class ParticipantLeaderboardApiTests(TestCase):
    def setUp(self):
        self.session = HubSession.objects.create(code='LBTEST', name='Lobbytest')
        self.url = reverse('games_hub:session_leaderboard_api', args=[self.session.code])

    @patch('games_hub.views.get_leaderboard_data')
    def test_participant_request_is_personalized_but_full_api_stays_complete(self, mocked_leaderboard):
        hub_participants = {
            name: HubParticipant.objects.create(session=self.session, nickname=name)
            for name in ('Anna', 'Ben', 'Cara', 'Dora', 'Emil')
        }
        source = leaderboard_fixture([
            ('Anna', 50),
            ('Ben', 40),
            ('Cara', 30),
            ('Dora', 20),
            ('Emil', 10),
        ])
        for row in source['participants']:
            row['hub_participant_id'] = hub_participants[row['name']].id
        mocked_leaderboard.side_effect = lambda *args, **kwargs: deepcopy(source)

        participant_response = self.client.get(self.url, {'participant_name': 'Cara'})
        full_response = self.client.get(self.url)

        self.assertEqual(participant_response.status_code, 200)
        self.assertEqual(
            [row['name'] for row in participant_response.json()['participants']],
            ['Ben', 'Cara', 'Dora'],
        )
        self.assertEqual(full_response.status_code, 200)
        self.assertEqual(len(full_response.json()['participants']), 5)
        self.assertNotIn('personalized', full_response.json())

    def test_current_session_excludes_old_session_participants_and_upcoming_games(self):
        creator = User.objects.create_user(username='leaderboard-owner')
        self.session.started_at = timezone.now() - timedelta(hours=1)
        self.session.save(update_fields=['started_at'])
        HubParticipant.objects.create(session=self.session, nickname='Aktuell')

        completed_quiz = Quiz.objects.create(
            creator=creator,
            room_code='9101',
            title='Abgeschlossen',
            status='completed',
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=completed_quiz.room_code,
        )
        QuizParticipant.objects.create(
            quiz=completed_quiz,
            name='Aktuell',
            hub_session_code=self.session.code,
            total_score=7,
        )

        upcoming_quiz = Quiz.objects.create(
            creator=creator,
            room_code='9102',
            title='Noch offen',
            status='waiting',
        )
        HubGameStep.objects.create(
            session=self.session,
            order=1,
            game_key='quiz',
            room_code=upcoming_quiz.room_code,
        )

        old_session = HubSession.objects.create(
            code='OLDLB',
            name='Alt',
            started_at=timezone.now() - timedelta(hours=2),
        )
        HubParticipant.objects.create(session=old_session, nickname='Altname')
        old_quiz = Quiz.objects.create(
            creator=creator,
            room_code='9103',
            title='Altes Spiel',
            status='completed',
        )
        HubGameStep.objects.create(
            session=old_session,
            order=0,
            game_key='quiz',
            room_code=old_quiz.room_code,
        )
        QuizParticipant.objects.create(
            quiz=old_quiz,
            name='Altname',
            hub_session_code=old_session.code,
            total_score=99,
        )

        response = self.client.get(self.url, {'participant_name': 'Aktuell'})
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['name'] for row in data['participants']], ['Aktuell'])
        self.assertEqual([game['game_number'] for game in data['games']], [1])
        self.assertEqual(data['participants'][0]['game_scores'][data['games'][0]['key']], 7)
        self.assertNotContains(response, 'Altname')

    def test_lobby_markup_uses_german_personalized_columns(self):
        response = self.client.get(reverse('games_hub:lobby', args=[self.session.code]))
        content = response.content.decode()

        self.assertContains(response, 'RANGLISTE')
        self.assertIn("'RANG'", content)
        self.assertIn("'NAME'", content)
        self.assertIn("'GESAMT'", content)
        self.assertIn('participant_name', content)
        self.assertIn('game_overall_scores', content)
        self.assertIn('nameCell.title = player.name;', content)
        self.assertNotIn("youLabel.textContent = 'DU'", content)
        self.assertNotIn('className = \'lb-you\'', content)
        self.assertNotIn('>Leaderboard<', content)
        self.assertNotIn('>PLAYER<', content)
        self.assertNotIn('>POINTS<', content)

    def test_vhs_leaderboard_styles_are_scoped_and_scroll_inside_the_panel(self):
        css = Path(settings.BASE_DIR, 'static', 'themes', 'vhs', 'vhs.css').read_text(encoding='utf-8')

        self.assertIn(
            'html[data-participant-theme="vhs"] .vhs-lobby-root .participant-leaderboard-panel',
            css,
        )
        self.assertIn('html[data-participant-theme="vhs"] .vhs-lobby-root .lb-table-scroll', css)
        self.assertIn('overflow-x: auto;', css)
        self.assertIn('border-radius: 0 !important;', css)
        self.assertIn('--participant-leaderboard-name-width: clamp(118px, 18vw, 176px);', css)
        self.assertIn('width: max-content;', css)
        self.assertIn('tbody tr:not(.is-current-player) .lb-name', css)
        self.assertIn('color: #9ba19c !important;', css)
        self.assertIn('tbody tr.is-current-player .lb-name', css)
        self.assertIn('color: #e9dfca !important;', css)
        self.assertNotIn('.vhs-lobby-root .lb-you', css)
