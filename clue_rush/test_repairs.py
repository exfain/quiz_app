import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import AnonymousUser, User
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .consumers import ClueRushGameConsumer
from .models import Clue, ClueAnswer, CluePendingInput, ClueQuestion, ClueRushGame, ClueRushParticipant, ClueRushSession
from .progress import current_round, question_progress, score_history
from .runtime import reconcile_clue_schedule, finalize_clue_question, set_paused, end_game
from games_hub.authoritative_state import current_snapshot, reset_question_flow
from games_hub.models import HubGameStep, HubSession
from games_hub.spectator import _serialize_clue_rush
from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime, start_unit_tutorial_if_needed


class ClueRushRepairTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('clue-repairs', is_staff=True)
        self.hub = HubSession.objects.create(code='CLREPAIR', creator=self.user, is_active=True, started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED, locked_participant_count=2)
        self.quiz = ClueRushGame.objects.create(creator=self.user, status='active', started_at=timezone.now())
        self.questions = []
        for n in range(5):
            question = ClueQuestion.objects.create(created_by=self.user, question_text=f'Question {n}', answer='Ada Lovelace')
            for i in range(3):
                Clue.objects.create(clue_question=question, clue_text=f'Hint {i}', order=i + 1, duration=10)
            self.questions.append(question)
        self.question = self.questions[0]
        self.quiz.selected_questions.set(self.questions)
        self.session = ClueRushSession.objects.create(quiz=self.quiz)
        HubGameStep.objects.create(session=self.hub, game_key='clue_rush', room_code=self.quiz.room_code, order=0)
        self.alice = ClueRushParticipant.objects.create(quiz=self.quiz, name='Alice', hub_session_code=self.hub.code)
        self.bob = ClueRushParticipant.objects.create(quiz=self.quiz, name='Bob', hub_session_code=self.hub.code)
        self.consumer = self.make_consumer()
        reset_question_flow(game_key='clue_rush', room_code=self.quiz.room_code, session_code=self.hub.code, mode='manual_three_phase')

    def make_consumer(self, user=None, name='Alice'):
        consumer = ClueRushGameConsumer()
        consumer.room_code = self.quiz.room_code
        consumer.room_group_name = f'cluerush_{self.quiz.room_code}'
        consumer.scope = {'user': user or self.user}
        consumer._clue_host_socket = True
        consumer.send = AsyncMock()
        consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
        consumer._authoritative_participant = name
        consumer._authoritative_session = self.hub.code
        return consumer

    def action(self, question=None):
        state = current_snapshot('clue_rush', self.quiz.room_code, self.hub.code)
        return {'question_id': (question or self.question).pk, 'game_id': str(self.quiz.pk),
                'state_revision': state['state_revision'], 'client_action_id': str(uuid.uuid4()), 'hub_session': self.hub.code}

    def start(self, question=None, age=2):
        question = question or self.question
        now = timezone.now()
        result = async_to_sync(self.consumer.present_clue_question)(self.quiz.pk, question.pk, self.hub.code,
            self.action(question), None, now - timedelta(seconds=age + 10))
        self.assertTrue(result.accepted, result.message)
        result = async_to_sync(self.consumer.open_clue_answering)(self.quiz.pk, question.pk, self.hub.code,
            self.action(question), now - timedelta(seconds=age))
        self.assertTrue(result.accepted, result.message)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()

    def save(self, name='Alice', text='Ada Lovelace'):
        return async_to_sync(self.consumer.save_participant_answer)(name, self.hub.code, text, self.quiz.current_question_id)

    def pending(self, text='Ada Lovelace'):
        return async_to_sync(self.consumer.store_pending_input)('Alice', self.hub.code, text, self.quiz.current_question_id)

    def finish(self):
        result = finalize_clue_question(self.quiz.room_code, hub_session_code=self.hub.code, force=True)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        return result

    def test_both_players_can_answer_and_repeated_question_has_independent_scores(self):
        self.start()
        self.assertEqual(self.save()['points_earned'], 3)
        self.assertEqual(self.save('Bob')['points_earned'], 3)
        self.assertEqual(self.save()['_error'], 'already_submitted')
        self.finish()
        self.start()
        self.assertEqual(self.save()['points_earned'], 3)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 6)
        self.assertEqual(list(self.alice.clue_answers.order_by('round_number').values_list('round_number', flat=True)), [1, 2])
        self.assertEqual(question_progress(self.quiz, self.hub.code, self.question.pk), {'kind': 'question', 'current': 2, 'total': 6})

    def test_http_and_rejoined_player_do_not_receive_grading_before_common_reveal(self):
        self.start()
        snapshot = async_to_sync(self.consumer.get_state_snapshot)('Alice', self.hub.code)
        from games_hub.authoritative_state import observe_snapshot
        state = observe_snapshot('clue_rush', self.quiz.room_code, snapshot, self.hub.code)
        data = {'guess': 'Ada Lovelace', 'game_id': state['game_id'], 'question_id': self.question.pk,
            'round_id': state['current_round_id'], 'state_revision': state['state_revision'], 'client_action_id': str(uuid.uuid4())}
        response = self.client.post(reverse('clue_rush:submit_guess', args=[self.quiz.room_code, 'Alice']) + '?hub_session=' + self.hub.code,
            json.dumps(data), content_type='application/json')
        self.assertTrue(response.json()['success'], response.content)
        self.assertNotIn('correct', response.json())
        self.assertNotIn('points_earned', response.json())
        player = self.make_consumer(AnonymousUser())
        before = async_to_sync(player.get_state_snapshot)('Alice', self.hub.code)
        self.assertNotIn('is_correct', before['participant_state']['answer'])
        self.assertNotIn('points_earned', before['participant_state']['answer'])
        self.assertIsNone(before['participant_state']['points'])
        status = self.client.get(reverse('clue_rush:game_status', args=[self.quiz.room_code, 'Alice']), {'hub_session': self.hub.code})
        self.assertEqual(status.json()['participant_score'], 0)
        self.finish()
        after = async_to_sync(player.get_state_snapshot)('Alice', self.hub.code)
        self.assertTrue(after['revealed_answer']['is_correct'])
        self.assertEqual(after['revealed_answer']['points_earned'], 3)

    def test_reads_at_deadline_wait_for_host_without_finalizing(self):
        self.start(age=31)
        result = reconcile_clue_schedule(self.quiz.room_code)
        self.assertTrue(result['deadline_reached'])
        self.assertNotIn('finalization', result)
        spectator = _serialize_clue_rush(ClueRushGame.objects.get(pk=self.quiz.pk), self.hub)
        self.assertEqual(spectator['phase'], 'question')
        self.assertNotIn('correct_answer', spectator['question'])
        self.assertFalse(ClueAnswer.objects.exists())

    def test_connected_host_finishes_expired_question_and_spectator_reveals(self):
        self.start(age=31)
        result = async_to_sync(self.consumer.reconcile_clues_db)()
        self.assertTrue(result['finalization']['newly_finalized'])
        self.quiz.refresh_from_db()
        self.assertEqual(current_snapshot('clue_rush', self.quiz.room_code, self.hub.code)['question_phase'], None)
        spectator = _serialize_clue_rush(self.quiz, self.hub)
        self.assertEqual(spectator['phase'], 'reveal')
        self.assertEqual(spectator['question']['correct_answer'], 'Ada Lovelace')
        self.start(self.questions[1])

    def test_participant_scheduler_cannot_finish_without_host(self):
        self.start(age=31)
        player = self.make_consumer(AnonymousUser())
        result = async_to_sync(player.reconcile_clues_db)()
        self.assertNotIn('finalization', result)
        self.assertFalse(ClueAnswer.objects.exists())
        signed_in_player = self.make_consumer()
        signed_in_player._clue_participant_socket = True
        result = async_to_sync(signed_in_player.reconcile_clues_db)()
        self.assertNotIn('finalization', result)
        self.assertFalse(ClueAnswer.objects.exists())

    def test_read_publishes_each_hint_transition_once(self):
        self.start(age=12)
        layer = type('Layer', (), {'group_send': AsyncMock()})()
        with patch('channels.layers.get_channel_layer', return_value=layer):
            reconcile_clue_schedule(self.quiz.room_code)
            reconcile_clue_schedule(self.quiz.room_code)
        events = [call.args[1] for call in layer.group_send.call_args_list]
        self.assertEqual([event['clue']['order'] for event in events], [1, 2])

    def test_submission_uses_timestamp_at_hint_boundary_without_worker(self):
        self.start(age=12)
        self.assertEqual(self.save()['points_earned'], 2)

    def test_timeout_draft_uses_last_hint_points(self):
        self.start()
        self.pending()
        result = finalize_clue_question(self.quiz.room_code, at=self.session.answer_deadline, hub_session_code=self.hub.code)
        self.assertTrue(result['newly_finalized'])
        answer = ClueAnswer.objects.get(participant=self.alice)
        self.assertEqual((answer.submitted_clue_number, answer.points_earned), (3, 1))

    def test_pause_freezes_answering_and_resume_retains_schedule_and_drafts(self):
        self.start()
        self.pending()
        now = timezone.now()
        deadline, started_at = self.session.answer_deadline, self.quiz.started_at
        set_paused(self.quiz.pk, True, at=now)
        paused = current_snapshot('clue_rush', self.quiz.room_code, self.hub.code)
        self.assertFalse(paused['answering_allowed'])
        self.assertFalse(paused['timer_running'])
        self.assertEqual(self.save()['_error'], 'invalid_phase')
        set_paused(self.quiz.pk, False, at=now + timedelta(seconds=90))
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.quiz.started_at, started_at)
        self.assertEqual(self.session.answer_deadline, deadline + timedelta(seconds=90))
        self.assertEqual(self.session.current_question_number, 1)
        self.assertTrue(CluePendingInput.objects.filter(participant=self.alice).exists())

    def test_duplicate_start_does_not_reset_flow_or_clock(self):
        self.start()
        previous = current_snapshot('clue_rush', self.quiz.room_code, self.hub.code)
        async_to_sync(self.consumer.handle_admin_start_quiz)({})
        self.assertEqual(current_snapshot('clue_rush', self.quiz.room_code, self.hub.code)['answering_started_at'], previous['answering_started_at'])

    def test_stale_end_cannot_end_next_question(self):
        self.start()
        stale = self.action()
        self.finish()
        self.start(self.questions[1])
        async_to_sync(self.consumer.handle_admin_end_question)(stale)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.questions[1].pk)

    def test_stale_end_cannot_end_repeat_of_same_question(self):
        self.start()
        stale = self.action()
        self.finish()
        self.start()
        async_to_sync(self.consumer.handle_admin_end_question)(stale)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.question.pk)

    def test_history_hides_solution_until_common_reveal(self):
        self.start()
        self.save(text='Wrong')
        self.assertEqual(score_history(self.quiz, self.alice), [])
        state = async_to_sync(self.consumer.get_state_snapshot)('Alice', self.hub.code)
        self.assertNotIn('Ada Lovelace', json.dumps(state))
        self.finish()
        self.assertEqual(score_history(self.quiz, self.alice)[0]['correct_answer'], 'Ada Lovelace')

    def test_live_answers_and_foreign_corrections_are_not_sent_to_players(self):
        player = self.make_consumer(AnonymousUser(), 'Bob')
        async_to_sync(player.participant_answered)({'answer': {'answer_text': 'secret'}})
        async_to_sync(player.answer_corrected)({'participant_name': 'Alice'})
        player.send.assert_not_called()

    def test_out_of_order_progress_matches_html_socket_and_spectator(self):
        self.start(self.questions[4])
        state = async_to_sync(self.consumer.get_state_snapshot)('Alice', self.hub.code)
        page = self.client.get(reverse('clue_rush:play', args=[self.quiz.room_code, 'Alice']), {'hub_session': self.hub.code})
        spectator = _serialize_clue_rush(self.quiz, self.hub)
        self.assertEqual(state['question']['question_number'], 1)
        self.assertEqual(page.context['current_question_number'], 1)
        self.assertEqual(spectator['progress']['current'], 1)

    def test_manual_score_adjustment_survives_next_answer(self):
        self.start()
        self.save()
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:set_clue_rush_participant_score'),
            json.dumps({'participant_id': self.alice.pk, 'score': 7}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.finish()
        self.start(self.questions[1])
        self.save()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 10)

    def test_tutorial_cannot_be_promoted_to_official_points(self):
        self.start()
        self.save(text='Wrong')
        answer = ClueAnswer.objects.get(participant=self.alice)
        answer.is_tutorial = True
        answer.save()
        self.finish()
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:promote_clue_rush_answer_correct', args=[self.quiz.room_code]),
            json.dumps({'answer_id': answer.pk}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 0)

    def test_end_game_discards_drafts_and_clears_all_running_state(self):
        self.start()
        self.pending()
        end_game(self.quiz.pk)
        self.quiz.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.quiz.status, 'completed')
        self.assertIsNone(self.quiz.current_clue_id)
        self.assertFalse(self.session.is_question_active)
        self.assertIsNone(self.session.answer_deadline)
        self.assertEqual(self.session.clue_schedule, [])
        self.assertFalse(CluePendingInput.objects.exists())
        self.assertFalse(ClueAnswer.objects.exists())

    def test_live_edit_is_rejected_without_breaking_schedule(self):
        self.start()
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_dashboard:update_clue_rush_question'), json.dumps({
            'question_id': self.question.pk, 'answer': 'Changed', 'clues': [{'clue_text': 'Replacement'}]}), content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.question.refresh_from_db()
        self.assertEqual(self.question.answer, 'Ada Lovelace')
        reconcile_clue_schedule(self.quiz.room_code)

    def test_answer_length_is_consistent_and_never_silently_truncated(self):
        self.start()
        self.question.answer = 'A' * 200
        self.question.save()
        self.assertIsNone(self.pending('A' * 205))
        self.assertEqual(self.save(text='A' * 205)['_error'], 'invalid_answer')
        self.finish()
        self.assertEqual(ClueAnswer.objects.get(participant=self.alice).points_earned, 0)

    def test_public_lists_results_and_admin_apis_are_scoped(self):
        ClueRushParticipant.objects.create(quiz=self.quiz, name='Alice', hub_session_code='OTHER')
        response = self.client.get(reverse('clue_rush:api_participants', args=[self.quiz.room_code]), {'hub_session': self.hub.code})
        self.assertEqual(response.json()['count'], 2)
        response = self.client.get(reverse('clue_rush:result', args=[self.quiz.room_code, 'Alice']), {'hub_session': self.hub.code})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['participant'], self.alice)
        self.client.force_login(self.user)
        for route in ['api_clue_rush_participants', 'api_clue_rush_stats', 'get_clue_rush_selected_questions']:
            args = [self.quiz.pk if route == 'get_clue_rush_selected_questions' else self.quiz.room_code]
            response = self.client.get(reverse('admin_dashboard:' + route, args=args), {'hub_session': self.hub.code})
            self.assertEqual(response.status_code, 200, response.content)

    def test_standalone_join_and_result_pages_render(self):
        self.assertEqual(self.client.get(reverse('clue_rush:join')).status_code, 200)
        participant = ClueRushParticipant.objects.create(quiz=self.quiz, name='Solo')
        self.assertEqual(self.client.get(reverse('clue_rush:result', args=[self.quiz.room_code, participant.name])).status_code, 200)

    def test_tutorial_is_excluded_from_all_question_counters(self):
        self.quiz.tutorial_question = self.question
        self.quiz.save()
        prepare_unit_tutorial_runtime('clue_rush', self.quiz.room_code, self.hub.code, True, validate=False)
        start_unit_tutorial_if_needed('clue_rush', self.quiz.room_code, self.hub.code)
        self.start()
        self.save()
        self.finish()
        self.start(self.questions[1])
        state = async_to_sync(self.consumer.get_state_snapshot)('Alice', self.hub.code)
        page = self.client.get(reverse('clue_rush:play', args=[self.quiz.room_code, 'Alice']), {'hub_session': self.hub.code})
        self.assertEqual((state['question']['question_number'], state['total_questions']), (1, 4))
        self.assertEqual((page.context['current_question_number'], page.context['total_question_count']), (1, 4))
        self.assertEqual(_serialize_clue_rush(self.quiz, self.hub)['progress']['current'], 1)
        self.assertEqual(score_history(self.quiz, self.alice), [])
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.total_score, 0)

    def test_hub_pause_resume_and_end_use_same_runtime(self):
        from games_hub.active_game_guard import _set_game_status, _end_game_cleanly
        self.start()
        self.pending()
        _set_game_status(self.quiz, 'inactive')
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.paused_at)
        self.assertEqual(self.save()['_error'], 'invalid_phase')
        _set_game_status(self.quiz, 'active')
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.current_question_id, self.question.pk)
        self.assertTrue(CluePendingInput.objects.exists())
        HubSession.pause_active_games_for_session_codes([self.hub.code])
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.paused_at)
        _set_game_status(self.quiz, 'active')
        _end_game_cleanly(self.quiz)
        self.session.refresh_from_db()
        self.assertFalse(self.session.is_question_active)
        self.assertIsNone(self.session.answer_deadline)
        self.assertFalse(CluePendingInput.objects.exists())
        self.assertFalse(ClueAnswer.objects.exists())

    def test_http_end_clears_runtime_and_notifies_players_and_hub(self):
        self.start()
        self.pending()
        self.client.force_login(self.user)
        layer = type('Layer', (), {'group_send': AsyncMock()})()
        with patch('channels.layers.get_channel_layer', return_value=layer), patch('admin_dashboard.views.get_channel_layer', return_value=layer):
            response = self.client.post(reverse('admin_dashboard:end_clue_rush_game_by_room_code', args=[self.quiz.room_code]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([call.args[0] for call in layer.group_send.call_args_list], [f'cluerush_{self.quiz.room_code}', f'hub_{self.hub.code}'])
        self.assertFalse(CluePendingInput.objects.exists())
        self.assertFalse(ClueAnswer.objects.exists())
        self.assertFalse(current_snapshot('clue_rush', self.quiz.room_code, self.hub.code)['timer_running'])
