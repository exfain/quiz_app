import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from black_jack_quiz import models as bj, runtime as blackjack
from black_jack_quiz.identity import issue_player_token
from clue_rush import models as clue
from clue_rush.consumers import ClueRushGameConsumer
from clue_rush.runtime import start_question_schedule, reconcile_clue_schedule
from .authoritative_state import current_snapshot, observe_snapshot, validate_and_reserve_action
from .models import HubSession, HubGameStep, HubParticipant


class SessionConcurrencyRepairs(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('concurrent-host', is_staff=True)
        self.hub = HubSession.objects.create(code='LOAD25', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=25)
        self.names = [f'Team {n:02}' for n in range(25)]
        for name in self.names:
            HubParticipant.objects.create(session=self.hub, nickname=name, scoring_eligible=True)

    def parallel(self, operation):
        barrier = Barrier(25)
        def work(n):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return operation(n)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=25) as pool:
            return list(pool.map(work, range(25)))

    def test_25_blackjack_http_answers_and_reveal_acknowledgements(self):
        game = bj.BlackJackQuiz.objects.create(creator=self.user, title='25 Clients', total_questions=2)
        questions = [bj.BlackJackQuestion.objects.create(created_by=self.user,
            question_text=f'Frage {n}', correct_answer=10, time_limit=120) for n in range(2)]
        game.selected_questions.set(questions)
        game.question_order = [[q.pk for q in questions]]
        game.save()
        session = bj.BlackJackSession.objects.create(quiz=game)
        HubGameStep.objects.create(session=self.hub, game_key='blackjack', room_code=game.room_code, order=0)
        for name in self.names:
            bj.BlackJackParticipant.objects.create(quiz=game, name=name, hub_session_code=self.hub.code)
        game.start_quiz(self.hub.code)
        session.refresh_from_db()
        session.quiz = game
        session.send_question(questions[0])
        players = list(game.participants.order_by('name'))
        self.assertEqual(len(players), 25)
        state = blackjack.build_state(game, self.hub.code, players[0])
        context = {'game_id': state['game_id'], 'question_id': state['current_question_id'],
            'round_id': state['current_round_id'], 'set_id': state['current_set_id'], 'state_revision': state['state_revision']}
        def answer(n):
            p = players[n]
            response = Client().post(reverse('black_jack_quiz:submit_answer', args=[game.room_code, p.name]),
                json.dumps({**context, 'hub_session': self.hub.code, 'player_token': issue_player_token(p),
                    'client_action_id': str(uuid.uuid4()), 'user_answer': 9}), content_type='application/json')
            return response.status_code
        self.assertEqual(self.parallel(answer), [200] * 25)
        self.assertEqual(game.blackjack_answers.count(), 25)
        blackjack.end_question(game)
        def acknowledge(n):
            p = players[n]
            response = Client().post(reverse('black_jack_quiz:acknowledge_reveal', args=[game.room_code, p.name]),
                json.dumps({'hub_session': self.hub.code, 'player_token': issue_player_token(p),
                    'question_id': questions[0].pk, 'set_id': 1}), content_type='application/json')
            return response.status_code
        self.assertEqual(self.parallel(acknowledge), [200] * 25)

    def test_clue_draft_context_is_stable_across_first_hint_and_later_hints(self):
        game = clue.ClueRushGame.objects.create(creator=self.user, status='active', started_at=timezone.now())
        question = clue.ClueQuestion.objects.create(created_by=self.user, question_text='Wer?', answer='Ada Lovelace')
        game.selected_questions.add(question)
        for n in range(3):
            clue.Clue.objects.create(clue_question=question, clue_text=f'Hinweis {n}', order=n + 1, duration=30)
        clue.ClueRushSession.objects.create(quiz=game)
        HubGameStep.objects.create(session=self.hub, game_key='clue_rush', room_code=game.room_code, order=0)
        for name in self.names:
            clue.ClueRushParticipant.objects.create(quiz=game, name=name, hub_session_code=self.hub.code)
        start_question_schedule(quiz_id=game.pk, question_id=question.pk)
        consumer = ClueRushGameConsumer()
        consumer.room_code = game.room_code
        consumer._authoritative_session = self.hub.code
        consumer._authoritative_participant = self.names[0]
        snapshot = async_to_sync(consumer.get_state_snapshot)(self.names[0], self.hub.code)
        self.assertEqual(snapshot['current_round_id'], 1)
        observe_snapshot('clue_rush', game.room_code, snapshot, self.hub.code)
        # Publishing the first hint must not turn a draft into an old-round action.
        reconcile_clue_schedule(game.room_code)
        state = current_snapshot('clue_rush', game.room_code, self.hub.code)
        self.assertEqual(str(state['current_round_id']), '1')
        for name in self.names:
            decision = validate_and_reserve_action(game_key='clue_rush', room_code=game.room_code,
                session_code=self.hub.code, participant_name=name, action_type='participant_input_changed',
                action={'game_id': state['game_id'], 'question_id': state['current_question_id'],
                    'round_id': '1', 'state_revision': state['state_revision'], 'client_action_id': str(uuid.uuid4())})
            self.assertTrue(decision.accepted, decision)
            self.assertTrue(async_to_sync(consumer.store_pending_input)(name, self.hub.code, 'Ada Lovelace', question.pk, 1))
        self.assertEqual(game.pending_inputs.count(), 25)
        self.assertFalse(async_to_sync(consumer.store_pending_input)(self.names[0], self.hub.code, 'Alt', question.pk, 0))
