from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from games_hub.authoritative_state import current_snapshot
from games_hub.models import HubGameStep, HubParticipant, HubSession
from .models import WannWarDasGame, WannWarDasParticipant, WannWarDasQuestion


class WannWarDasSessionRepairs(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('wann-repair-host', is_staff=True)
        self.hub = HubSession.objects.create(code='WANN25', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=25)
        self.game = WannWarDasGame.objects.create(creator=self.user)
        self.q = WannWarDasQuestion.objects.create(created_by=self.user, question_text='Welches Jahr?',
            correct_answer=2000, time_limit=120, max_tolerance=20, seconds_per_step=5)
        self.other = WannWarDasQuestion.objects.create(created_by=self.user, question_text='Nächstes Jahr?', correct_answer=2001)
        self.game.selected_questions.set([self.q, self.other])
        HubGameStep.objects.create(session=self.hub, order=0, game_key='wann_war_das', room_code=self.game.room_code)
        for n in range(25):
            HubParticipant.objects.create(session=self.hub, nickname=f'Team {n:02}', scoring_eligible=True)
        self.game.start_quiz(self.hub.code)
        self.players = list(self.game.participants.order_by('name'))
        self.game.start_question(self.q, self.hub.code)

    def test_solution_grades_and_current_points_are_hidden_until_common_reveal(self):
        self.game.submit_answer(self.players[0], '2000')
        state = self.game.serialize_state(self.hub.code, self.players[1].name)
        self.assertNotIn('correct_answer', state['question'])
        self.assertNotIn('formatted_correct_answer', state['question'])
        self.assertNotIn('explanation', state['question'])
        self.assertFalse(state['answers'])
        self.assertEqual(next(p['score'] for p in state['participants'] if p['name'] == self.players[0].name), 0)
        row = next(r for r in state['scorebox']['rows'] if r['participant_id'] == self.players[0].pk)
        self.assertIsNone(row['entries'][0]['correct'])
        self.assertIsNone(row['entries'][0]['points'])
        owner = self.game.serialize_state(self.hub.code, for_host=True)
        self.assertEqual(owner['question']['correct_answer'], 2000)
        self.game.reveal_current_question(self.hub.code)
        state = self.game.serialize_state(self.hub.code, self.players[1].name)
        self.assertEqual(state['question']['correct_answer'], 2000)
        self.assertEqual(state['scorebox']['rows'][0]['entries'][0]['points'], 10)

    def test_25_saved_drafts_are_finalized_at_deadline_values_exactly_once(self):
        for player in self.players:
            self.assertTrue(self.game.store_pending_answer(player, '2019', self.q.pk, 1))
        deadline = self.game.question_started_at + timedelta(seconds=120)
        self.game.reveal_current_question(self.hub.code, at=deadline + timedelta(seconds=20))
        self.assertEqual(self.game.answers.count(), 25)
        self.assertEqual(set(self.game.answers.values_list('points_earned', flat=True)), {1})
        self.assertEqual(set(self.game.answers.values_list('tolerance_at_submit', flat=True)), {20})
        self.game.reveal_current_question(self.hub.code, at=deadline + timedelta(seconds=30))
        self.assertEqual(self.game.answers.count(), 25)

    def test_manual_end_uses_current_tolerance_for_open_drafts(self):
        player = self.players[0]
        self.game.store_pending_answer(player, '2001', self.q.pk, 1)
        self.game.reveal_current_question(self.hub.code, at=self.game.question_started_at + timedelta(seconds=5))
        answer = self.game.answers.get(participant=player)
        self.assertEqual(answer.tolerance_at_submit, 1)
        self.assertEqual(answer.points_earned, self.q.points_for_step(1))

    def test_participant_and_spectator_reads_do_not_reveal_after_timeout(self):
        self.game.question_started_at = timezone.now() - timedelta(seconds=121)
        self.game.save(update_fields=['question_started_at'])
        for name in [self.players[0].name, None]:
            state = self.game.serialize_state(self.hub.code, name)
            self.assertEqual(state['question_state'], 'active')
            self.assertFalse(state['can_answer'])
            self.assertNotIn('correct_answer', state['question'])
        self.assertFalse(self.game.answers.exists())

    def test_legacy_active_question_publishes_a_real_deadline_for_host_reload(self):
        state = self.game.serialize_state(self.hub.code, for_host=True)
        from django.utils.dateparse import parse_datetime
        self.assertEqual(parse_datetime(state['ends_at']), self.game.question_started_at + timedelta(seconds=120))
        self.assertGreater(state['timer']['remaining_seconds'], 110)

    def test_pause_resume_keeps_time_question_and_score(self):
        self.game.submit_answer(self.players[0], '2000')
        before = self.game.question_started_at
        started = self.game.started_at
        paused = timezone.now()
        self.game.set_paused(True, at=paused)
        state = self.game.serialize_state(self.hub.code, self.players[1].name)
        self.assertFalse(state['can_answer'])
        self.assertFalse(state['timer_running'])
        self.game.set_paused(False, at=paused + timedelta(seconds=90))
        self.players[0].refresh_from_db()
        self.assertEqual(self.game.question_started_at, before + timedelta(seconds=90))
        self.assertEqual(self.game.started_at, started)
        self.assertEqual(self.game.current_question_id, self.q.pk)
        self.assertEqual(self.game.current_question_number, 1)
        self.assertEqual(self.players[0].total_score, 10)

    def test_repetition_adds_independent_round_answers_and_total(self):
        for _ in range(2):
            for player in self.players:
                answer, error = self.game.submit_answer(player, '2000')
                self.assertIsNone(error)
            self.game.reveal_current_question(self.hub.code)
            if self.game.get_current_round_number() == 1:
                self.game.start_question(self.q, self.hub.code)
        self.players[0].refresh_from_db()
        self.assertEqual(self.game.answers.count(), 50)
        self.assertEqual(self.players[0].total_score, 20)
        self.assertEqual(self.game.get_question_progress(), {'kind': 'question', 'current': 2, 'total': 3})
        scorebox = self.game.build_scorebox(self.hub.code)
        self.assertEqual([e['points'] for e in scorebox['rows'][0]['entries']], [10, 10, None])

    def test_old_round_draft_cannot_overwrite_repeated_question(self):
        self.game.reveal_current_question(self.hub.code)
        self.game.start_question(self.q, self.hub.code)
        self.assertFalse(self.game.store_pending_answer(self.players[0], '2000', self.q.pk, 1))
        self.assertTrue(self.game.store_pending_answer(self.players[0], '2001', self.q.pk, 2))

    def test_premature_game_end_keeps_common_reveal_and_finalizes_pending(self):
        self.game.store_pending_answer(self.players[0], '2000', self.q.pk, 1)
        self.game.end_quiz()
        state = self.game.serialize_state(self.hub.code, self.players[0].name)
        self.assertEqual(state['game']['status'], 'completed')
        self.assertEqual(state['question_state'], 'revealed')
        self.assertEqual(state['question']['correct_answer'], 2000)
        self.assertEqual(state['own_answer']['points_earned'], 10)
        self.assertFalse(self.game.end_quiz())
