import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import escapejs

from QuizGame.consumers import QuizConsumer
from QuizGame.models import Quiz, QuizAnswer, QuizParticipant, QuizQuestion, QuizSession
from who_is_lying.consumers import WhoConsumer
from who_is_lying.models import WhoQuiz, WhoAnswer, WhoParticipant, WhoQuestion, WhoSession
from .authoritative_state import current_snapshot, finish_question_flow, open_answering, reset_question_flow, reveal_question_content
from .models import GameRuntimeState, HubGameStep, HubSession
from .quiz_pause import pause, resume
from .quiz_rounds import current_round, participant_history, progress
from .spectator import build_spectator_state


class QuizRepairTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='repairs', is_staff=True)
        self.now = timezone.now()
        self.hub = HubSession.objects.create(code='REPAIR', is_active=True, started_at=self.now - timedelta(minutes=1))
        self.games = {}
        for key, model, question_model, participant_model, session_model, consumer_class in (
            ('quiz', Quiz, QuizQuestion, QuizParticipant, QuizSession, QuizConsumer),
            ('who', WhoQuiz, WhoQuestion, WhoParticipant, WhoSession, WhoConsumer),
        ):
            quiz = model.objects.create(creator=self.user, status='active', started_at=self.now)
            kwargs = {'question_text': 'True?', 'question_type': 'true_false', 'correct_answer': 'True'} if key == 'quiz' else {
                'statement': 'Who lies?', 'people': [{'name': 'Truth', 'is_lying': False}, {'name': 'Liar', 'is_lying': True}]}
            questions = [question_model.objects.create(created_by=self.user, time_limit=20, **kwargs) for _ in range(5)]
            quiz.selected_questions.set(questions)
            session_model.objects.create(quiz=quiz)
            participant = participant_model.objects.create(quiz=quiz, name="Team <'&\"\\", hub_session_code=self.hub.code)
            consumer = consumer_class()
            consumer.room_code = quiz.room_code
            consumer.room_group_name = f'{key}_{quiz.room_code}'
            consumer.send = AsyncMock()
            consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
            reset_question_flow(game_key=key, room_code=quiz.room_code, session_code=self.hub.code, mode='manual_three_phase')
            self.games[key] = (quiz, questions, participant, consumer)

    def action(self, key, question):
        quiz, _, _, _ = self.games[key]
        snapshot = current_snapshot(key, quiz.room_code, self.hub.code)
        return {'question_id': question.id, 'game_id': snapshot['game_id'],
                'state_revision': snapshot['state_revision'], 'client_action_id': str(uuid.uuid4())}

    def start(self, key, question):
        quiz, _, _, consumer = self.games[key]
        if key == 'who':
            result = async_to_sync(consumer.present_who_question)(quiz.pk, question.pk, self.hub.code,
                self.action(key, question), 20, at=self.now - timedelta(seconds=30))
            self.assertTrue(result.accepted, (result.code, result.message))
            result = async_to_sync(consumer.open_who_answering)(quiz.pk, question.pk, self.hub.code,
                self.action(key, question), at=self.now - timedelta(seconds=1))
            self.assertTrue(result.accepted, (result.code, result.message))
        else:
            result = async_to_sync(consumer.present_quiz_question)(quiz.pk, question.pk, self.hub.code, self.action(key, question))
            self.assertTrue(result.accepted, (result.code, result.message))
            GameRuntimeState.objects.filter(game_key=key, room_code=quiz.room_code).update(question_presented_at=self.now - timedelta(seconds=30))
            result = reveal_question_content(game_key=key, room_code=quiz.room_code, session_code=self.hub.code,
                action=self.action(key, question), at=self.now - timedelta(seconds=10))
            self.assertTrue(result.accepted, (result.code, result.message))
            result = open_answering(game_key=key, room_code=quiz.room_code, session_code=self.hub.code,
                action=self.action(key, question), answer_duration_seconds=20, at=self.now - timedelta(seconds=1))
            self.assertTrue(result.accepted, (result.code, result.message))
            quiz.refresh_from_db()
            quiz.session.open_answering(question, started_at=self.now - timedelta(seconds=1), answer_duration_seconds=20)
        quiz.refresh_from_db()

    def finish(self, key):
        quiz, _, _, consumer = self.games[key]
        question_id = quiz.current_question_id
        if key == 'who':
            async_to_sync(consumer.finalize_person_choices)(quiz.pk, self.hub.code)
        else:
            async_to_sync(consumer.finalize_pending_answers_and_close_question)(quiz.pk, self.hub.code)
            finish_question_flow(game_key=key, room_code=quiz.room_code, session_code=self.hub.code, question_id=question_id)
        quiz.refresh_from_db()

    def test_repeated_questions_keep_both_scores_and_have_separate_progress_slots(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            with self.subTest(key=key):
                for _ in range(2):
                    self.start(key, questions[0])
                    value = 'True' if key == 'quiz' else [next(person['id'] for person in questions[0].get_randomized_people(quiz.room_code)['people'] if person['original_index'] == 1)]
                    answer = async_to_sync(consumer.save_participant_answer)(participant.name, self.hub.code, value, 0, questions[0].id)
                    self.assertIsNotNone(answer)
                    self.assertIsNone(async_to_sync(consumer.save_participant_answer)(participant.name, self.hub.code, value, 0, questions[0].id))
                    self.finish(key)
                participant.refresh_from_db()
                self.assertEqual(participant.total_score, 2)
                history = participant_history(quiz, participant, key, self.hub.code)
                self.assertEqual([row['question_number'] for row in history], [1, 2])
                self.assertEqual([row['points_earned'] for row in history], [1, 1])
                self.assertEqual(progress(quiz, key, self.hub.code, question_id=questions[0].id)['total'], 6)

    def test_unanswered_rounds_and_reveal_use_actual_send_order(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            with self.subTest(key=key):
                HubGameStep.objects.all().delete()
                HubGameStep.objects.create(session=self.hub, game_key=key, room_code=quiz.room_code, order=0)
                reset_question_flow(game_key=key, room_code=quiz.room_code, session_code=self.hub.code, mode='manual_three_phase')
                for question in (questions[3], questions[1]):
                    self.start(key, question)
                    self.finish(key)
                self.start(key, questions[4])
                payload = async_to_sync(consumer.get_current_question_data)(self.hub.code)
                self.assertEqual(payload['question_number'], 3)
                self.assertEqual(payload['total_questions' if key == 'quiz' else 'total_sets'], 5)
                self.assertEqual(build_spectator_state(self.hub)['game']['progress']['current'], 3)
                self.finish(key)
                history = participant_history(quiz, participant, key, self.hub.code)
                self.assertEqual([row['question_number'] for row in history], [1, 2, 3])
                self.assertEqual([row['points_earned'] for row in history], [0, 0, 0])
                if key == 'who':
                    reveal = async_to_sync(consumer.get_recent_participant_reveal)(participant.name, self.hub.code)
                    self.assertEqual(reveal['question_number'], 3)
                self.assertEqual(build_spectator_state(self.hub)['game']['progress']['current'], 3)

    def test_pause_freezes_deadline_choices_and_score_and_resume_is_idempotent(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            with self.subTest(key=key):
                self.start(key, questions[0])
                if key == 'quiz':
                    self.assertTrue(async_to_sync(consumer.save_pending_answer)(participant.name, self.hub.code, 'True', questions[0].id))
                else:
                    self.assertTrue(async_to_sync(consumer.sync_person_choices)(participant.name, self.hub.code, questions[0].id,
                        person_id=0, selected=True, question_started_at=quiz.question_start_time.isoformat(), update=True)['accepted'])
                session = type(quiz.session).objects.get(quiz=quiz)
                deadline, start = session.question_end_time, quiz.question_start_time
                pending = dict(session.pending_answers)
                self.assertTrue(pause(quiz, key, self.hub.code, at=self.now))
                self.assertFalse(pause(quiz, key, self.hub.code, at=self.now + timedelta(seconds=5)))
                snapshot = current_snapshot(key, quiz.room_code, self.hub.code)
                self.assertFalse(snapshot['answering_allowed'])
                self.assertFalse(snapshot['timer_running'])
                self.assertEqual(snapshot['remaining_answer_time'], int((deadline - self.now).total_seconds() + .999))
                value = 'True' if key == 'quiz' else [0]
                self.assertIsNone(async_to_sync(consumer.save_participant_answer)(participant.name, self.hub.code, value, 0, questions[0].id))
                self.assertTrue(resume(quiz, key, self.hub.code, at=self.now + timedelta(seconds=100)))
                self.assertFalse(resume(quiz, key, self.hub.code, at=self.now + timedelta(seconds=200)))
                quiz.refresh_from_db()
                session.refresh_from_db()
                self.assertEqual(quiz.question_start_time, start + timedelta(seconds=100))
                self.assertEqual(session.question_end_time, deadline + timedelta(seconds=100))
                self.assertEqual(current_round(quiz), 1)
                saved = session.pending_answers[str(participant.pk)]
                self.assertEqual(saved.get('answer', saved.get('choices')), pending[str(participant.pk)].get('answer', pending[str(participant.pk)].get('choices')))
                if key == 'who':
                    self.assertEqual(saved['question_started_at'], quiz.question_start_time.isoformat())

    def test_result_rank_and_public_lists_are_scoped_to_hub(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            with self.subTest(key=key):
                type(participant).objects.create(quiz=quiz, name=participant.name, hub_session_code='OTHER', total_score=99)
                self.assertEqual(participant.get_rank(), 1)
                namespace = 'quiz' if key == 'quiz' else 'who_is_lying'
                response = self.client.get(reverse(f'{namespace}:result', args=[quiz.room_code, participant.name]), {'hub_session': self.hub.code})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['participant'].id, participant.id)
                self.assertEqual([row.id for row in response.context['leaderboard']], [participant.id])
                for api in ('api_leaderboard', 'api_participants'):
                    response = self.client.get(reverse(f'{namespace}:{api}', args=[quiz.room_code]), {'hub_session': self.hub.code}).json()
                    self.assertEqual(len(response['leaderboard' if api == 'api_leaderboard' else 'participants']), 1)

    def test_who_player_script_keeps_special_name_and_standalone_hub_empty(self):
        quiz, _, participant, _ = self.games['who']
        participant.hub_session_code = None
        participant.save()
        response = self.client.get(reverse('who_is_lying:play', args=[quiz.room_code, participant.name]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this.participantName = '" + str(escapejs(participant.name)) + "';")
        self.assertContains(response, "this.hubSession = '';")

    def test_live_participant_join_keeps_the_id_needed_by_host_score_controls(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            async_to_sync(consumer.handle_participant_join)({'participant_name': participant.name, 'hub_session': self.hub.code})
            events = [call.args[1] for call in consumer.channel_layer.group_send.await_args_list]
            joined = next(event for event in events if event['type'] == 'participant_joined')
            self.assertEqual(joined['participant']['id'], participant.id)
            self.assertEqual(joined['participant']['name'], participant.name)

    def test_http_submit_uses_displayed_positions_and_never_reveals_solution(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            with self.subTest(key=key):
                self.start(key, questions[0])
                namespace = 'quiz' if key == 'quiz' else 'who_is_lying'
                data = self.action(key, questions[0])
                data['time_taken'] = 999
                if key == 'quiz':
                    data['answer'] = 'True'
                else:
                    data['selected_liars'] = [0]
                response = self.client.post(reverse(f'{namespace}:submit_answer', args=[quiz.room_code, participant.name]) + '?hub_session=' + self.hub.code,
                    json.dumps(data), content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                payload = response.json()
                self.assertTrue(payload['success'])
                for private in ('is_correct', 'correct_answer', 'analysis', 'actual_liars_names', 'points_earned'):
                    self.assertNotIn(private, payload)
                model = QuizAnswer if key == 'quiz' else WhoAnswer
                answer = model.objects.get(quiz=quiz)
                self.assertGreaterEqual(answer.time_taken, 0)
                self.assertLess(answer.time_taken, 20)
                if key == 'who':
                    self.assertEqual(answer.selected_liars, [questions[0].get_randomized_people(quiz.room_code)['position_to_original'][0]])

    def test_http_status_hides_options_and_future_people_in_prompt_phase(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            self.start(key, questions[0])
            GameRuntimeState.objects.filter(game_key=key, room_code=quiz.room_code).update(question_phase='prompt_visible')
            namespace = 'quiz' if key == 'quiz' else 'who_is_lying'
            response = self.client.get(reverse(f'{namespace}:quiz_status', args=[quiz.room_code, participant.name]), {'hub_session': self.hub.code})
            payload = response.json()['current_question']
            self.assertEqual(payload.get('options', []), [])
            self.assertEqual(payload.get('people', []), [])

    def test_http_host_end_evaluates_saved_choices_and_whole_game_end_discards_them(self):
        self.client.force_login(self.user)
        for key, (quiz, questions, participant, consumer) in self.games.items():
            with self.subTest(key=key):
                self.start(key, questions[0])
                if key == 'quiz':
                    self.assertTrue(async_to_sync(consumer.save_pending_answer)(participant.name, self.hub.code, 'True', questions[0].id))
                else:
                    self.assertTrue(async_to_sync(consumer.sync_person_choices)(participant.name, self.hub.code, questions[0].id,
                        person_id=0, selected=True, question_started_at=quiz.question_start_time.isoformat(), update=True)['accepted'])
                route = 'end_question' if key == 'quiz' else 'end_who_question'
                response = self.client.post(reverse('admin_dashboard:' + route, args=[quiz.room_code]), json.dumps({'hub_session': self.hub.code}), content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                model = QuizAnswer if key == 'quiz' else WhoAnswer
                self.assertEqual(model.objects.filter(quiz=quiz).count(), 1)
                quiz.refresh_from_db()
                self.start(key, questions[1])
                if key == 'quiz':
                    async_to_sync(consumer.save_pending_answer)(participant.name, self.hub.code, 'True', questions[1].id)
                else:
                    async_to_sync(consumer.sync_person_choices)(participant.name, self.hub.code, questions[1].id,
                        person_id=0, selected=True, question_started_at=quiz.question_start_time.isoformat(), update=True)
                route = 'end_quiz_by_room_code' if key == 'quiz' else 'end_who_quiz_by_room_code'
                response = self.client.post(reverse('admin_dashboard:' + route, args=[quiz.room_code]), json.dumps({'hub_session': self.hub.code}), content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(model.objects.filter(quiz=quiz).count(), 1)
                quiz.refresh_from_db()
                self.assertEqual(quiz.status, 'completed')
                self.assertFalse(quiz.session.is_question_active)
                self.assertEqual(quiz.session.pending_answers, {})

    def test_legacy_answer_survives_first_replay_with_original_points(self):
        for key, (quiz, questions, participant, consumer) in self.games.items():
            HubGameStep.objects.all().delete()
            HubGameStep.objects.create(session=self.hub, game_key=key, room_code=quiz.room_code, order=0)
            reset_question_flow(game_key=key, room_code=quiz.room_code, session_code=self.hub.code, mode='manual_three_phase')
            model = QuizAnswer if key == 'quiz' else WhoAnswer
            kwargs = {'answer_text': 'True'} if key == 'quiz' else {'selected_liars': [1]}
            original = model.objects.create(quiz=quiz, participant=participant, question=questions[0], **kwargs)
            session = quiz.session
            session.current_question_number = 1
            session.total_questions_sent = 1
            session.save()
            self.start(key, questions[0])
            self.finish(key)
            original.refresh_from_db()
            self.assertEqual(original.points_earned, 1)
            participant.refresh_from_db()
            self.assertEqual(participant.total_score, 1)
            history = participant_history(quiz, participant, key, self.hub.code)
            self.assertEqual(history[0]['points_earned'], 1)
            self.assertEqual(progress(quiz, key, self.hub.code, question_id=questions[0].id)['total'], 6)


class QuizRoundMigrationTests(TransactionTestCase):
    def test_existing_quiz_answers_and_scores_survive_round_migration(self):
        old = [('QuizGame', '0017_quizsession_pending_answers_and_last_result')]
        target = [('QuizGame', '0018_alter_quizanswer_unique_together_and_more')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old)
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-round-host')
            quiz = apps.get_model('QuizGame', 'Quiz').objects.create(creator_id=user.pk)
            question = apps.get_model('QuizGame', 'QuizQuestion').objects.create(
                created_by_id=user.pk, question_text='Legacy', question_type='true_false', correct_answer='True')
            participant = apps.get_model('QuizGame', 'QuizParticipant').objects.create(
                quiz_id=quiz.pk, name='Legacy team', total_score=1, questions_answered=1)
            answer = apps.get_model('QuizGame', 'QuizAnswer').objects.create(
                quiz_id=quiz.pk, question_id=question.pk, participant_id=participant.pk,
                answer_text='True', is_correct=True, points_earned=1)
            session = apps.get_model('QuizGame', 'QuizSession').objects.create(
                quiz_id=quiz.pk, current_question_number=1, total_questions_sent=1)
            MigrationExecutor(connection).migrate(target)
            restored = QuizAnswer.objects.get(pk=answer.pk)
            self.assertEqual((restored.answer_text, restored.points_earned, restored.round_number), ('True', 1, 0))
            self.assertEqual(QuizParticipant.objects.get(pk=participant.pk).total_score, 1)
            restored_session = QuizSession.objects.get(pk=session.pk)
            self.assertEqual(restored_session.current_question_number, 1)
            self.assertEqual(restored_session.question_rounds, [])
        finally:
            MigrationExecutor(connection).migrate(latest)
