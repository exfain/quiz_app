import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone
from games_hub.models import HubSession, HubGameStep, HubParticipant
from games_hub.authoritative_state import current_snapshot, reset_question_flow
from . import models as m, runtime as r
from .identity import issue_player_token
from .consumers import BlackJackConsumer


class BlackJackRepairs(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('bj-host', '', 'pw')
        self.hub = HubSession.objects.create(code='BJTEST', creator=self.user, is_active=True,
            started_at=timezone.now(), check_in_status='completed', locked_participant_count=2)
        self.quiz = m.BlackJackQuiz.objects.create(creator=self.user, title='Black Jack', total_questions=2)
        self.questions = [m.BlackJackQuestion.objects.create(created_by=self.user,
            question_text=f'Frage {n}', correct_answer=n * 10, time_limit=60) for n in range(1, 5)]
        self.quiz.question_order = [[q.pk for q in self.questions[:2]], [q.pk for q in self.questions[2:]]]
        self.quiz.save()
        self.quiz.selected_questions.set(self.questions)
        self.session = m.BlackJackSession.objects.create(quiz=self.quiz)
        HubGameStep.objects.create(session=self.hub, game_key='blackjack', room_code=self.quiz.room_code, order=0)
        self.p1 = m.BlackJackParticipant.objects.create(quiz=self.quiz, name='Lisa', hub_session_code=self.hub.code)
        self.p2 = m.BlackJackParticipant.objects.create(quiz=self.quiz, name='Max', hub_session_code=self.hub.code)
        for p in [self.p1, self.p2]:
            HubParticipant.objects.create(session=self.hub, nickname=p.name)
        self.quiz.start_quiz(self.hub.code)
        self.client.force_login(self.user)

    def start(self, index=0):
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.session.quiz = self.quiz
        self.session.send_question(self.questions[index])
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()

    def answer(self, participant=None, value=9):
        return r.submit(self.quiz, participant or self.p1, value)

    def end(self):
        result = r.end_question(self.quiz)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        return result

    def context(self):
        state = r.build_state(self.quiz, self.hub.code, self.p1)
        return {'game_id': state['game_id'], 'question_id': state['current_question_id'],
            'round_id': state['current_round_id'], 'set_id': state['current_set_id'],
            'state_revision': state['state_revision'], 'client_action_id': str(uuid.uuid4())}

    def post(self, route, participant=None, **data):
        p = participant or self.p1
        payload = {'hub_session': self.hub.code, 'player_token': issue_player_token(p), **self.context(), **data}
        return self.client.post(reverse('black_jack_quiz:' + route, args=[self.quiz.room_code, p.name]),
            json.dumps(payload), content_type='application/json')

    def complete_first_set(self):
        for index in [0, 1]:
            self.start(index)
            for p in [self.p1, self.p2]:
                self.answer(p, self.questions[index].correct_answer - 1)
            self.end()

    def test_two_players_score_independently_and_snapshot_context_is_stable(self):
        self.start()
        first = self.context()
        self.answer(self.p2, 8)
        r.build_state(self.quiz, self.hub.code, self.p2)
        r.build_state(self.quiz, self.hub.code, self.p1)
        response = self.post('submit_answer', **first, user_answer=9)
        self.assertEqual(response.status_code, 200, response.content)
        self.end()
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual([self.p1.total_points, self.p2.total_points], [1, 2])

    def test_missing_answer_elimination_is_retained_in_history(self):
        self.start()
        self.answer(value=5)
        self.answer(self.p2, 15)
        self.end()
        self.start(1)
        self.answer(self.p2, 20)
        self.end()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 0)
        self.assertEqual(r.scoreboard(self.quiz, self.session, self.p1, self.hub.code)[0]['earned_points'], 0)

    def test_incomplete_sets_restore_separate_stars_and_bust(self):
        self.start()
        self.answer(value=32)
        self.answer(self.p2, 3)
        self.end()
        r.select_set(self.quiz, 2)
        self.start(2)
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertFalse(self.p1.is_busted)
        self.assertEqual(self.p2.total_points, 0)
        self.answer(value=29)
        self.answer(self.p2, 29)
        self.end()
        r.select_set(self.quiz, 1)
        self.start(1)
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertTrue(self.p1.is_busted)
        self.assertEqual(self.p2.total_points, 7)

    def test_repetition_keeps_old_answers_and_adds_independent_points(self):
        self.complete_first_set()
        self.complete_first_set()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 4)
        self.assertEqual(self.p1.blackjack_answers.count(), 4)
        state = r.build_state(self.quiz, self.hub.code, self.p1)
        self.assertEqual(state['total_sets'], 3)
        self.assertEqual([row['earned_points'] for row in state['scoreboard']], [2, 2, None])

    def test_pause_freezes_time_and_resume_preserves_answers_and_started_at(self):
        self.start()
        self.answer()
        before, started = self.session.question_end_time, self.quiz.started_at
        at = timezone.now()
        r.set_paused(self.quiz, True, at=at)
        r.set_paused(self.quiz, False, at=at + timedelta(seconds=120))
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.session.question_end_time, before + timedelta(seconds=120))
        self.assertEqual(self.quiz.started_at, started)
        self.assertEqual(self.quiz.get_current_run_answers().count(), 1)

    def test_draft_saved_before_deadline_is_graded_after_deadline_even_disconnected(self):
        self.start()
        self.assertTrue(r.store_pending(self.quiz, self.p1, '9', self.context()))
        self.p1.is_active = False
        self.p1.save()
        self.session.refresh_from_db()
        self.session.question_end_time = timezone.now() - timedelta(seconds=1)
        self.session.save(update_fields=['question_end_time'])
        self.end()
        self.assertEqual(self.p1.blackjack_answers.get().user_answer, 9)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.total_points, 1)

    def test_late_and_stale_drafts_do_not_replace_saved_input(self):
        self.start()
        context = self.context()
        r.store_pending(self.quiz, self.p1, '9', context)
        self.session.question_end_time = timezone.now() - timedelta(seconds=1)
        self.session.save(update_fields=['question_end_time'])
        self.assertFalse(r.store_pending(self.quiz, self.p1, '99', context))
        self.end()
        self.start(1)
        self.assertFalse(r.store_pending(self.quiz, self.p1, '10', context))

    def test_participant_timeout_does_not_close_without_host(self):
        self.start()
        self.session.question_end_time = timezone.now() - timedelta(seconds=1)
        self.session.save()
        consumer = BlackJackConsumer()
        consumer.room_code = self.quiz.room_code
        consumer.send_state = AsyncMock()
        async_to_sync(consumer.handle_participant_question_timeout)({'question_id': self.questions[0].pk})
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_question_active)

    def test_stale_host_end_cannot_close_new_question(self):
        self.start()
        previous = self.context()
        self.answer()
        self.answer(self.p2, 9)
        self.end()
        self.start(1)
        with self.assertRaises(ValueError):
            r.end_question(self.quiz, code=self.hub.code, data=previous)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.questions[1].pk)

    def test_manual_star_correction_is_additive_and_kept_in_history(self):
        self.start()
        self.answer()
        self.answer(self.p2, 9)
        self.end()
        r.set_score(self.p1, 7)
        self.start(1)
        self.answer(value=19)
        self.answer(self.p2, 19)
        self.end()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 8)
        self.assertEqual(r.scoreboard(self.quiz, self.session, self.p1, self.hub.code)[0]['earned_points'], 8)

    def test_busted_players_receive_zero_in_ranking(self):
        self.quiz.scoring_mode = 'rank'
        self.quiz.save()
        self.start()
        self.answer(value=32)
        self.answer(self.p2, 9)
        self.end()
        self.start(1)
        self.answer(self.p2, 19)
        self.end()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 0)

    def test_foreign_pool_is_not_graded_or_reset(self):
        foreign = m.BlackJackParticipant.objects.create(quiz=self.quiz, name='Old', hub_session_code='OLD',
            total_points=19, overall_points=9, is_active=False)
        self.complete_first_set()
        foreign.refresh_from_db()
        self.assertEqual((foreign.total_points, foreign.overall_points), (19, 9))

    def test_future_hub_page_does_not_reset_current_game(self):
        self.start()
        future = HubSession.objects.create(code='FUTURE', creator=self.user)
        HubGameStep.objects.create(session=future, game_key='blackjack', room_code=self.quiz.room_code, order=0)
        m.BlackJackParticipant.objects.create(quiz=self.quiz, name='Next', hub_session_code=future.code)
        result = Client().get(reverse('black_jack_quiz:play', args=[self.quiz.room_code, 'Next']), {'hub_session': future.code})
        self.assertEqual(result.status_code, 404)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.questions[0].pk)

    def test_public_context_does_not_authorize_foreign_submission(self):
        self.start()
        result = Client().post(reverse('black_jack_quiz:submit_answer', args=[self.quiz.room_code, self.p1.name]),
            json.dumps({'hub_session': self.hub.code, 'user_answer': 9, **self.context()}), content_type='application/json')
        self.assertEqual(result.status_code, 403)
        wrong = self.post('submit_answer', player_token=issue_player_token(self.p2), user_answer=9)
        self.assertEqual(wrong.status_code, 403)

    def test_score_editor_rejects_foreign_host(self):
        other = User.objects.create_user('other', password='pw', is_staff=True)
        self.client.force_login(other)
        result = self.client.post(reverse('admin_dashboard:set_blackjack_participant_score'),
            json.dumps({'participant_id': self.p1.pk, 'score': 18}), content_type='application/json')
        self.assertEqual(result.status_code, 403)

    def test_live_question_edit_is_rejected(self):
        self.start()
        result = self.client.post(reverse('admin_dashboard:update_blackjack_question'),
            json.dumps({'question_id': self.questions[0].pk, 'correct_answer': 100}), content_type='application/json')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.answer(value=10).points_earned, 0)

    def test_late_join_waits_until_next_set(self):
        self.start()
        late = m.BlackJackParticipant.objects.create(quiz=self.quiz, name='Late', hub_session_code=self.hub.code)
        with self.assertRaisesMessage(ValueError, 'naechsten Set'):
            self.answer(late, 10)
        self.answer()
        self.answer(self.p2, 9)
        self.end()
        self.start(1)
        self.answer(value=19)
        self.answer(self.p2, 19)
        self.end()
        self.start(2)
        self.assertIsNotNone(self.answer(late, 30))

    def test_whole_game_end_discards_open_set_and_reveals_before_acknowledgment(self):
        self.complete_first_set()
        self.start(2)
        self.answer(value=25)
        event = r.end_game(self.quiz)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 2)
        self.assertFalse(self.session.is_question_active)
        state = r.build_state(self.quiz, self.hub.code, self.p1)
        self.assertTrue(state['own_reveal'])
        self.assertEqual(state['own_reveal']['correct_answer']['correct_answer'], 30)
        r.acknowledge(self.quiz, self.p1, {'question_id': self.questions[2].pk, 'set_id': event['set_number']})
        self.assertIsNone(r.build_state(self.quiz, self.hub.code, self.p1)['own_reveal'])

    def test_player_does_not_receive_own_or_foreign_grading_before_reveal(self):
        self.start()
        self.answer(value=32)
        state = r.build_state(self.quiz, self.hub.code, self.p1)
        self.assertEqual(state['participant_points'], 0)
        self.assertFalse(state['is_busted'])
        self.assertNotIn('correct_answer', state['question'])
        self.assertNotIn('responses', state)

    def test_invalid_numbers_are_rejected_without_breaking_the_session(self):
        self.start()
        for value in [True, 1.5, 2 ** 80, 'NaN', 'Infinity']:
            with self.assertRaises(ValueError):
                self.answer(value=value)
        self.assertEqual(self.answer(value=100).user_answer, 100)

    def test_all_tutorial_questions_play_before_regular_set_and_do_not_count(self):
        from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime, get_unit_tutorial_state
        self.quiz.tutorial_set_number = 1
        self.quiz.save(update_fields=['tutorial_set_number'])
        prepare_unit_tutorial_runtime('blackjack', self.quiz.room_code, self.hub.code, True)
        reset_question_flow(game_key='blackjack', room_code=self.quiz.room_code,
            session_code=self.hub.code, mode='manual_three_phase')
        for index in range(4):
            state = r.build_state(self.quiz, self.hub.code)
            action = {'game_id': state['game_id'], 'state_revision': state['state_revision'],
                'question_id': self.questions[index].pk, 'client_action_id': str(uuid.uuid4())}
            decision = r.present(self.quiz, self.questions[index].pk, self.hub.code, action,
                at=timezone.now() - timedelta(seconds=10))
            self.assertTrue(decision.accepted)
            state = decision.snapshot
            action.update(state_revision=state['state_revision'], client_action_id=str(uuid.uuid4()),
                round_id=state['current_round_id'], set_id=state['current_set_id'])
            self.assertTrue(r.open_question(self.quiz, self.hub.code, action).accepted)
            self.answer(value=self.questions[index].correct_answer - 1)
            self.answer(self.p2, self.questions[index].correct_answer - 1)
            event = self.end()
            self.assertEqual(event['quiz_complete'], index == 3)
            if index == 0:
                self.assertFalse(get_unit_tutorial_state('blackjack', self.quiz.room_code, self.hub.code)['tutorial_has_been_played'])
            if index == 1:
                self.assertTrue(get_unit_tutorial_state('blackjack', self.quiz.room_code, self.hub.code)['tutorial_has_been_played'])
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 2)
        self.assertEqual(r.build_state(self.quiz)['total_sets'], 1)

    def test_spectator_uses_played_order_and_snapshot_after_archive_and_repeat(self):
        from games_hub.spectator import _serialize_blackjack
        self.start(1)
        self.assertEqual(_serialize_blackjack(self.quiz, self.hub)['progress']['current'], 1)
        self.answer(value=19)
        self.answer(self.p2, 19)
        self.end()
        self.questions[1].question_text = 'Edited after reveal'
        self.questions[1].correct_answer = 999
        self.questions[1].is_active = False
        self.questions[1].save()
        state = _serialize_blackjack(self.quiz, self.hub)
        self.assertEqual(state['question']['correct_answer'], 20)
        self.assertEqual(state['question']['text'], 'Frage 2')
        self.assertEqual(state['question']['set_question_count'], 2)

    def test_duplicate_start_preserves_phase_and_saved_answer(self):
        self.start()
        self.answer()
        before = self.quiz.started_at
        r.start_game(self.quiz, self.hub.code)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.quiz.started_at, before)
        self.assertTrue(self.session.is_question_active)
        self.assertEqual(self.quiz.get_current_run_answers().count(), 1)

    def test_http_whole_game_stop_discards_open_set_like_socket_path(self):
        self.start()
        self.answer()
        response = self.client.post(reverse('admin_dashboard:end_blackjack_quiz_by_room_code', args=[self.quiz.room_code]),
            json.dumps({'hub_session': self.hub.code}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 0)
        self.assertTrue(r.build_state(self.quiz, self.hub.code, self.p1)['own_reveal'])

    def test_new_hub_start_preserves_previous_pool_results(self):
        self.complete_first_set()
        r.end_game(self.quiz)
        self.hub.ended_at = timezone.now()
        self.hub.save(update_fields=['ended_at'])
        next_hub = HubSession.objects.create(code='BJNEXT', creator=self.user,
            is_active=True, started_at=timezone.now())
        HubGameStep.objects.create(session=next_hub, game_key='blackjack', room_code=self.quiz.room_code, order=0)
        other = m.BlackJackParticipant.objects.create(quiz=self.quiz, name=self.p1.name,
            hub_session_code=next_hub.code)
        self.assertTrue(r.start_game(self.quiz, next_hub.code))
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.overall_points, 2)
        self.assertEqual(self.p1.blackjack_answers.count(), 2)
        self.assertEqual(other.total_points, 0)
        self.assertEqual(r.build_state(self.quiz, next_hub.code, other)['total_sets'], 2)
        self.assertTrue(any(entry.get('hub_session_code') == self.hub.code for entry in self.session.set_rounds))

    def test_signed_websocket_submit_still_works_and_masks_grading(self):
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser
        self.start()
        action = self.context()
        async def scenario():
            communicator = WebsocketCommunicator(BlackJackConsumer.as_asgi(), '/')
            communicator.scope.update(user=AnonymousUser(), url_route={'kwargs': {'room_code': self.quiz.room_code}})
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.receive_json_from()
            await communicator.send_json_to({'type': 'participant_join', 'participant_name': self.p1.name,
                'hub_session': self.hub.code, 'player_token': issue_player_token(self.p1)})
            state = await communicator.receive_json_from()
            self.assertEqual(state['type'], 'blackjack_state')
            await communicator.send_json_to({'type': 'participant_submit_answer', 'participant_name': self.p1.name,
                'hub_session': self.hub.code, 'user_answer': 9, **action})
            response = await communicator.receive_json_from()
            self.assertEqual(response['type'], 'answer_submitted')
            self.assertNotIn('points_earned', response)
            self.assertNotIn('correct_answer', response)
            await communicator.disconnect()
        async_to_sync(scenario)()
        self.assertEqual(self.p1.blackjack_answers.get().user_answer, 9)

    def test_end_paused_game_clears_pause_and_allows_independent_replay(self):
        self.start()
        r.set_paused(self.quiz, True)
        r.end_game(self.quiz)
        r.select_set(self.quiz, 1)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertIsNone(self.session.paused_at)
        self.assertNotIn('_paused_at', current_snapshot('blackjack', self.quiz.room_code, self.hub.code))
        self.start()
        self.assertEqual(self.answer(value=9).points_earned, 1)

    def test_imported_legacy_active_answer_keeps_grading_hidden(self):
        self.start()
        self.answer(value=5)
        self.answer(self.p2, 9)
        self.end()
        self.start(1)
        self.answer(value=19)
        m.BlackJackAnswer.objects.filter(quiz=self.quiz).update(set_round_number=0)
        m.BlackJackSession.objects.filter(quiz=self.quiz).update(set_rounds=[], current_set_round_number=0)
        state = r.build_state(self.quiz, self.hub.code, self.p1)
        self.assertEqual(state['participant_points'], 5)
        self.assertEqual(state['submitted_answer'], 19)
        self.assertNotIn('correct_answer', state['question'])

    def test_result_keeps_original_solution_after_later_edit_and_ignores_open_set(self):
        self.complete_first_set()
        self.start(2)
        self.answer(value=29)
        r.end_game(self.quiz)
        m.BlackJackQuestion.objects.filter(pk=self.questions[0].pk).update(
            question_text='Changed later', correct_answer=999, is_active=False)
        response = self.client.get(reverse('black_jack_quiz:result', args=[self.quiz.room_code, self.p1.name]),
            {'hub_session': self.hub.code})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_answers'], 2)
        first = response.context['participant_answers'][0]
        self.assertEqual(first.question.question_text, 'Frage 1')
        self.assertEqual(first.question.correct_answer, 10)
