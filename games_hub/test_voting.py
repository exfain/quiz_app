import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant
from games_hub.check_in import complete_session_check_in, participant_check_in, start_session_check_in
from games_hub.models import GameVote, HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession
from games_hub.voting import get_voting_state, open_session_voting


class HubVotingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='voting_admin', password='pw')
        self.client.force_login(self.user)

    def _session(self, code='VOTE1'):
        return HubSession.objects.create(code=code, name=f'Session {code}')

    def _complete_check_in(self, session, names):
        for name in names:
            HubParticipant.objects.get_or_create(session=session, nickname=name)
        self.assertTrue(start_session_check_in(session)['success'])
        for name in names:
            self.assertTrue(participant_check_in(session, name)['success'])
        result = complete_session_check_in(session)
        self.assertTrue(result['success'], result)
        session.refresh_from_db()
        return session

    def _add_quiz_step(self, session, order=0, title='Musikquiz Runde 1', status='waiting', scores=None):
        quiz = Quiz.objects.create(
            title=title,
            creator=self.user,
            status=status,
        )
        step = HubGameStep.objects.create(
            session=session,
            order=order,
            game_key='quiz',
            room_code=quiz.room_code,
            title=title,
        )
        if scores is not None:
            HubGameParticipantSnapshot.create_for_step(step)
            for name, score in scores.items():
                QuizParticipant.objects.create(
                    quiz=quiz,
                    name=name,
                    total_score=score,
                    hub_session_code=session.code,
                )
        return quiz, step

    def _configure_voting(self, session, mode):
        return self.client.post(
            reverse('games_hub:configure_voting', args=[session.code]),
            data=json.dumps({'mode': mode}),
            content_type='application/json',
        )

    def _submit_vote(self, session, nickname, step_order=0):
        return self.client.post(
            reverse('games_hub:submit_vote', args=[session.code]),
            data=json.dumps({'nickname': nickname, 'step_order': step_order}),
            content_type='application/json',
        )

    def test_default_voting_is_off_and_participant_state_has_no_open_voting(self):
        session = self._session()
        self._complete_check_in(session, ['Anna'])
        self._add_quiz_step(session)

        payload = self.client.get(
            f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Anna"
        ).json()

        self.assertFalse(payload['voting']['enabled'])
        self.assertFalse(payload['voting']['open'])
        self.assertFalse(payload['voting']['can_vote'])
        self.assertEqual(payload['voting']['mode'], HubSession.VOTING_OFF)

    def test_normal_voting_uses_game_titles_and_counts_only_official_voters(self):
        session = self._session('VOTE2')
        self._complete_check_in(session, ['Anna', 'Ben'])
        self._add_quiz_step(session, title='Musikquiz Runde 1')
        late = HubParticipant.objects.create(session=session, nickname='Late')
        self.assertFalse(late.scoring_eligible)

        response = self._configure_voting(session, HubSession.VOTING_NORMAL)
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.current_voting_round, 1)

        state = self.client.get(
            f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Anna"
        ).json()
        self.assertTrue(state['voting']['can_vote'])
        self.assertEqual(state['votes'][0]['display_name'], 'Musikquiz Runde 1')
        self.assertEqual(state['votes'][0]['title'], 'Musikquiz Runde 1')
        self.assertEqual(state['eligible_participants'], ['Anna', 'Ben'])

        vote_response = self._submit_vote(session, 'Anna')
        self.assertEqual(vote_response.status_code, 200)
        self.assertEqual(vote_response.json()['votes'][0]['count'], 1)

        late_response = self._submit_vote(session, 'Late')
        self.assertEqual(late_response.status_code, 403)
        self.assertEqual(GameVote.objects.filter(session=session).count(), 1)

    def test_loser_voting_allows_unique_last_place_only(self):
        session = self._session('VOTE3')
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla'])
        quiz, _ = self._add_quiz_step(
            session,
            title='Spiel 1',
            status='completed',
            scores={'Anna': 10, 'Ben': 6, 'Carla': 0},
        )
        self._add_quiz_step(session, order=1, title='Spiel 2', status='waiting')
        quiz.status = 'completed'
        quiz.save(update_fields=['status'])

        response = self._configure_voting(session, HubSession.VOTING_LOSER)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['eligible_participants'], ['Carla'])
        self.assertFalse(
            self.client.get(f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Anna").json()['voting']['can_vote']
        )
        self.assertTrue(
            self.client.get(f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Carla").json()['voting']['can_vote']
        )

    def test_loser_voting_allows_tied_last_place(self):
        session = self._session('VOTE4')
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla'])
        quiz, _ = self._add_quiz_step(
            session,
            title='Spiel 1',
            status='completed',
            scores={'Anna': 10, 'Ben': 0, 'Carla': 0},
        )
        self._add_quiz_step(session, order=1, title='Spiel 2', status='waiting')
        quiz.status = 'completed'
        quiz.save(update_fields=['status'])

        payload = self._configure_voting(session, HubSession.VOTING_LOSER).json()

        self.assertEqual(set(payload['eligible_participants']), {'Ben', 'Carla'})

    def test_winner_voting_allows_unique_winner_and_tied_winners(self):
        session = self._session('VOTE5')
        self._complete_check_in(session, ['Anna', 'Ben', 'Carla'])
        quiz, _ = self._add_quiz_step(
            session,
            title='Spiel 1',
            status='completed',
            scores={'Anna': 10, 'Ben': 10, 'Carla': 3},
        )
        self._add_quiz_step(session, order=1, title='Spiel 2', status='waiting')
        quiz.status = 'completed'
        quiz.save(update_fields=['status'])

        payload = self._configure_voting(session, HubSession.VOTING_WINNER).json()

        self.assertEqual(set(payload['eligible_participants']), {'Anna', 'Ben'})
        self.assertTrue(
            self.client.get(f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Anna").json()['voting']['can_vote']
        )
        self.assertFalse(
            self.client.get(f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Carla").json()['voting']['can_vote']
        )

    def test_winner_or_loser_voting_requires_completed_game(self):
        session = self._session('VOTE6')
        self._complete_check_in(session, ['Anna'])
        self._add_quiz_step(session, status='waiting')

        response = self._configure_voting(session, HubSession.VOTING_WINNER)

        self.assertEqual(response.status_code, 400)
        self.assertIn('abgeschlossenen Spiel', response.json()['error'])
        session.refresh_from_db()
        self.assertEqual(session.voting_mode, HubSession.VOTING_OFF)
        self.assertFalse(session.voting_open)

    def test_spectator_late_pending_and_permanent_leaver_cannot_vote(self):
        session = self._session('VOTE7')
        self._complete_check_in(session, ['Anna', 'Ben'])
        self._add_quiz_step(session)
        HubParticipant.objects.filter(session=session, nickname='Ben').update(left_permanently_at=timezone.now())
        HubParticipant.objects.create(session=session, nickname='Late')

        response = self._configure_voting(session, HubSession.VOTING_NORMAL)
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload['eligible_participants'], ['Anna'])
        self.assertEqual(self._submit_vote(session, 'Ben').status_code, 403)
        self.assertEqual(self._submit_vote(session, 'Late').status_code, 403)
        self.assertEqual(self._submit_vote(session, 'Spectator').status_code, 403)

    def test_new_voting_round_does_not_reuse_old_votes(self):
        session = self._session('VOTE8')
        self._complete_check_in(session, ['Anna'])
        self._add_quiz_step(session)

        self.assertEqual(self._configure_voting(session, HubSession.VOTING_NORMAL).status_code, 200)
        self.assertEqual(self._submit_vote(session, 'Anna').status_code, 200)
        session.refresh_from_db()
        first_state = get_voting_state(session, participant_nickname='Anna')
        self.assertEqual(first_state['votes'][0]['count'], 1)

        self.assertEqual(self._configure_voting(session, HubSession.VOTING_NORMAL).status_code, 200)
        session.refresh_from_db()
        second_state = get_voting_state(session, participant_nickname='Anna')

        self.assertEqual(session.current_voting_round, 2)
        self.assertEqual(second_state['votes'][0]['count'], 0)
        self.assertIsNone(second_state['voting']['my_vote_step_order'])
        self.assertEqual(GameVote.objects.filter(session=session).count(), 1)

    def test_disabling_voting_hides_current_round_from_participants(self):
        session = self._session('VOTE9')
        self._complete_check_in(session, ['Anna'])
        self._add_quiz_step(session)
        self.assertEqual(open_session_voting(session, HubSession.VOTING_NORMAL)['success'], True)

        response = self._configure_voting(session, HubSession.VOTING_OFF)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['voting']['enabled'])
        self.assertFalse(payload['voting']['open'])

    def test_voting_options_exclude_completed_and_active_steps(self):
        session = self._session('VOTE10')
        self._complete_check_in(session, ['Anna'])
        _, completed_step = self._add_quiz_step(
            session,
            order=0,
            title='Schon gespielt',
            status='completed',
        )
        _, inactive_step = self._add_quiz_step(
            session,
            order=1,
            title='Wieder startbar',
            status='inactive',
        )
        self._add_quiz_step(
            session,
            order=2,
            title='Noch offen',
            status='waiting',
        )
        _, active_step = self._add_quiz_step(
            session,
            order=3,
            title='Laeuft gerade',
            status='active',
        )

        response = self._configure_voting(session, HubSession.VOTING_NORMAL)
        self.assertEqual(response.status_code, 200)
        state = self.client.get(
            f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Anna"
        ).json()

        self.assertTrue(state['voting']['can_vote'])
        self.assertEqual(
            [option['display_name'] for option in state['votes']],
            ['Wieder startbar', 'Noch offen'],
        )
        self.assertEqual(
            [option['status'] for option in state['votes']],
            ['inactive', 'waiting'],
        )
        self.assertEqual(self._submit_vote(session, 'Anna', completed_step.order).status_code, 403)
        self.assertEqual(self._submit_vote(session, 'Anna', active_step.order).status_code, 403)
        self.assertEqual(self._submit_vote(session, 'Anna', inactive_step.order).status_code, 200)

    def test_completed_game_from_previous_voting_phase_does_not_affect_next_phase(self):
        session = self._session('VOTE11')
        self._complete_check_in(session, ['Anna'])
        _, played_step = self._add_quiz_step(
            session,
            order=0,
            title='Spiel 1',
            status='completed',
        )
        game_two, step_two = self._add_quiz_step(
            session,
            order=1,
            title='Spiel 2',
            status='waiting',
        )
        self._add_quiz_step(
            session,
            order=2,
            title='Spiel 3',
            status='waiting',
        )

        self.assertEqual(self._configure_voting(session, HubSession.VOTING_NORMAL).status_code, 200)
        first_state = self.client.get(
            f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Anna"
        ).json()
        self.assertEqual([option['display_name'] for option in first_state['votes']], ['Spiel 2', 'Spiel 3'])
        self.assertEqual(self._submit_vote(session, 'Anna', step_two.order).status_code, 200)

        game_two.status = 'completed'
        game_two.save(update_fields=['status'])

        self.assertEqual(self._configure_voting(session, HubSession.VOTING_NORMAL).status_code, 200)
        session.refresh_from_db()
        second_state = get_voting_state(session, participant_nickname='Anna')

        self.assertEqual(session.current_voting_round, 2)
        self.assertEqual([option['display_name'] for option in second_state['votes']], ['Spiel 3'])
        self.assertEqual(second_state['votes'][0]['count'], 0)
        self.assertIsNone(second_state['voting']['my_vote_step_order'])
        self.assertNotIn(played_step.id, [option['step_id'] for option in second_state['votes']])

    def test_open_voting_with_no_remaining_games_shows_waiting_message(self):
        session = self._session('VOTE12')
        self._complete_check_in(session, ['Anna'])
        self._add_quiz_step(
            session,
            order=0,
            title='Spiel 1',
            status='completed',
        )
        self._add_quiz_step(
            session,
            order=1,
            title='Spiel 2',
            status='active',
        )

        response = self._configure_voting(session, HubSession.VOTING_NORMAL)
        self.assertEqual(response.status_code, 200)
        state = self.client.get(
            f"{reverse('games_hub:get_votes', args=[session.code])}?nickname=Anna"
        ).json()

        self.assertTrue(state['voting']['open'])
        self.assertFalse(state['voting']['can_vote'])
        self.assertEqual(state['voting']['available_option_count'], 0)
        self.assertEqual(state['votes'], [])
        self.assertIn('Keine weiteren Spiele', state['voting']['message'])
