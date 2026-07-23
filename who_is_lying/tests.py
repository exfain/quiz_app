import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.active_game_guard import resolve_session_game_activation
from games_hub.consumers import HubConsumer
from games_hub.lobby_return_flow import (
    get_session_lobby_presence,
    mark_single_participant_inactive_for_lobby_return,
)
from games_hub.models import HubGameStep, HubParticipant, HubSession

from .consumers import WhoConsumer
from .models import WhoAnswer, WhoParticipant, WhoQuestion, WhoQuiz, WhoSession


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class WhoLyingScoringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-scoring')
        self.question = WhoQuestion.objects.create(
            statement='Ich spiele in der Nationalmannschaft.',
            points=10,
            time_limit=30,
            people=[
                {'name': 'Joshua Kimmich', 'is_lying': False},
                {'name': 'Kingsley Coman', 'is_lying': True},
                {'name': 'Oliver Baumann', 'is_lying': False},
            ],
            created_by=self.user,
        )

    def test_calculate_score_matches_plus_minus_zero_rules(self):
        self.assertEqual(self.question.calculate_score([1]), 1)
        self.assertEqual(self.question.calculate_score([0]), -1)
        self.assertEqual(self.question.calculate_score([]), 0)
        self.assertEqual(self.question.calculate_score([0, 1]), 0)

    def test_people_order_survives_a_process_hash_seed_change(self):
        with patch('builtins.hash', return_value=1):
            order_before_restart = self.question.get_randomized_people(room_code='7610')['people']
        with patch('builtins.hash', return_value=4):
            order_after_restart = self.question.get_randomized_people(room_code='7610')['people']

        self.assertEqual(order_after_restart, order_before_restart)

    def test_correct_identifications_count_includes_unselected_truth_tellers(self):
        quiz = WhoQuiz.objects.create(
            title='Who Accuracy',
            room_code='7611',
            creator=self.user,
            status='completed',
        )
        participant = WhoParticipant.objects.create(quiz=quiz, name='Ada')
        answer = WhoAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.question,
            selected_liars=[],
            time_taken=5.0,
        )

        self.assertEqual(answer.get_correct_identifications_count(), 2)
        self.assertEqual(answer.get_accuracy_percentage(), round((2 / 3) * 100, 1))

    def test_participant_total_score_can_be_negative_after_false_accusation(self):
        quiz = WhoQuiz.objects.create(
            title='Who Negative Score',
            room_code='7616',
            creator=self.user,
            status='completed',
        )
        participant = WhoParticipant.objects.create(quiz=quiz, name='Ada')
        answer = WhoAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=self.question,
            selected_liars=[0],
            time_taken=4.0,
        )

        participant.refresh_from_db()
        self.assertEqual(answer.points_earned, -1)
        self.assertEqual(participant.total_score, -1)

    def test_session_send_question_uses_total_duration_for_all_people(self):
        quiz = WhoQuiz.objects.create(
            title='Who Session',
            room_code='7612',
            creator=self.user,
            status='active',
        )
        session = WhoSession.objects.create(quiz=quiz)

        before = timezone.now()
        session.send_question(self.question, time_per_person=12)
        session.refresh_from_db()
        duration = (session.question_end_time - before).total_seconds()

        self.assertTrue(session.is_question_active)
        self.assertEqual(session.current_question_number, 1)
        self.assertEqual(session.total_questions_sent, 1)
        self.assertAlmostEqual(duration, 36, delta=1)


class WhoLyingConsumerTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = WhoConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'who_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_admin_send_question_sets_total_set_duration_but_broadcasts_per_person_time(self):
        user = User.objects.create_user(username='who-consumer')
        question = WhoQuestion.objects.create(
            statement='Wer würde lügen?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Live',
            room_code='7613',
            creator=user,
            status='active',
        )
        WhoSession.objects.create(quiz=quiz)
        consumer, _ = self.make_consumer(quiz)

        before = timezone.now()
        async_to_sync(consumer.handle_admin_send_question)({
            'question_id': question.id,
            'custom_time_limit': 15,
        })

        quiz.refresh_from_db()
        quiz.session.refresh_from_db()
        duration = (quiz.session.question_end_time - before).total_seconds()
        self.assertEqual(quiz.current_question_id, question.id)
        self.assertAlmostEqual(duration, 45, delta=1)
        self.assertEqual(
            consumer.channel_layer.group_messages[-1][1]['question']['time_limit'],
            15,
        )
        payload = consumer.channel_layer.group_messages[-1][1]['question']
        self.assertEqual(payload['time_per_person'], 15)
        self.assertEqual(payload['current_person_index'], 0)
        self.assertTrue(14 <= payload['current_person_time_left'] <= 15)
        self.assertIsNotNone(payload['question_started_at'])
        self.assertIsNotNone(payload['question_end_time'])
        self.assertIsNotNone(payload['server_now'])

    def test_save_participant_answer_returns_person_results_for_reveal(self):
        user = User.objects.create_user(username='who-reveal-consumer')
        question = WhoQuestion.objects.create(
            statement='Wer lügt wirklich?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': False},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Reveal',
            room_code='7617',
            creator=user,
            status='active',
            current_question=question,
        )
        WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB1',
        )
        consumer, _ = self.make_consumer(quiz)

        randomized = question.get_randomized_people(room_code=quiz.room_code)
        liar_displayed = next(
            person for person in randomized['people']
            if question.people[person['original_index']]['is_lying']
        )

        result = async_to_sync(consumer.save_participant_answer)(
            'Ada', 'HUB1', [liar_displayed['id']], 2.5
        )

        self.assertIn('person_results', result)
        self.assertEqual(
            [entry['name'] for entry in result['person_results']],
            [person['name'] for person in randomized['people']],
        )
        liar_result = next(entry for entry in result['person_results'] if entry['name'] == liar_displayed['name'])
        self.assertTrue(liar_result['is_lying'])
        self.assertTrue(liar_result['was_selected'])
        self.assertTrue(liar_result['was_correct'])
        self.assertEqual(liar_result['points_effect'], 1)
        truth_result = next(entry for entry in result['person_results'] if entry['name'] != liar_displayed['name'])
        self.assertFalse(truth_result['is_lying'])
        self.assertFalse(truth_result['was_selected'])
        self.assertTrue(truth_result['was_correct'])
        self.assertEqual(truth_result['points_effect'], 0)

    def test_answer_submitted_payload_includes_full_person_reveal_results(self):
        user = User.objects.create_user(username='who-reveal-payload')
        question = WhoQuestion.objects.create(
            statement='Wer luegt wirklich?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': False},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Reveal Payload',
            room_code='7619',
            creator=user,
            status='active',
            current_question=question,
        )
        WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB3',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        randomized = question.get_randomized_people(room_code=quiz.room_code)
        liar_displayed = next(
            person for person in randomized['people']
            if question.people[person['original_index']]['is_lying']
        )

        async_to_sync(consumer.handle_participant_submit_answer)({
            'participant_name': 'Ada',
            'hub_session': 'HUB3',
            'selected_liars': [liar_displayed['id']],
            'time_taken': 1.8,
        })

        payload = next(message for message in sent_messages if message['type'] == 'answer_submitted')
        self.assertEqual(len(payload['person_results']), len(question.people))
        liar_result = next(entry for entry in payload['person_results'] if entry['name'] == liar_displayed['name'])
        self.assertTrue(liar_result['is_lying'])
        self.assertTrue(liar_result['was_correct'])
        truth_result = next(entry for entry in payload['person_results'] if entry['name'] != liar_displayed['name'])
        self.assertFalse(truth_result['is_lying'])
        self.assertTrue(truth_result['was_correct'])

    def test_participant_join_receives_current_question_number(self):
        user = User.objects.create_user(username='who-current-question-number')
        first_question = WhoQuestion.objects.create(
            statement='Erste Runde',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        current_question = WhoQuestion.objects.create(
            statement='Zweite Runde',
            points=10,
            time_limit=25,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dave', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Current Question',
            room_code='7620',
            creator=user,
            status='active',
            current_question=current_question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=31),
            question_order=[first_question.id, current_question.id],
        )
        quiz.selected_questions.add(first_question, current_question)
        WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB4',
        )
        WhoSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=19),
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_participant_join)({
            'participant_name': 'Ada',
            'hub_session': 'HUB4',
        })

        payload = next(message for message in sent_messages if message['type'] == 'question_started')
        self.assertEqual(payload['question']['question_number'], 2)
        self.assertEqual(payload['question']['time_per_person'], 25)
        self.assertEqual(payload['question']['current_person_index'], 1)
        self.assertTrue(18 <= payload['question']['current_person_time_left'] <= 19)
        self.assertIsNotNone(payload['question']['question_started_at'])
        self.assertIsNotNone(payload['question']['question_end_time'])
        self.assertIsNotNone(payload['question']['server_now'])

    def test_admin_send_question_uses_actual_send_order_for_question_number(self):
        user = User.objects.create_user(username='who-send-order')
        first_question = WhoQuestion.objects.create(
            statement='First round',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        second_question = WhoQuestion.objects.create(
            statement='Second round',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=user,
        )
        third_question = WhoQuestion.objects.create(
            statement='Third round',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Eve', 'is_lying': False},
                {'name': 'Finn', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Send Order',
            room_code='7622',
            creator=user,
            status='active',
            question_order=[first_question.id, second_question.id, third_question.id],
        )
        quiz.selected_questions.set([first_question, second_question, third_question])
        WhoSession.objects.create(quiz=quiz)
        consumer, _ = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({'question_id': first_question.id})
        async_to_sync(consumer.handle_admin_send_question)({'question_id': third_question.id})

        first_payload = consumer.channel_layer.group_messages[0][1]
        second_payload = consumer.channel_layer.group_messages[1][1]
        self.assertEqual(first_payload['question']['question_number'], 1)
        self.assertEqual(second_payload['question']['question_number'], 2)

    def test_auto_submit_after_host_end_still_scores_recently_ended_question(self):
        user = User.objects.create_user(username='who-ended-submit')
        question = WhoQuestion.objects.create(
            statement='Wer luegt nach dem Host-Ende?',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Host End',
            room_code='7623',
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=5),
        )
        WhoSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=75),
        )
        participant = WhoParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB6',
        )
        consumer, sent_messages = self.make_consumer(quiz)
        randomized = question.get_randomized_people(room_code=quiz.room_code)
        truth_displayed = next(
            person for person in randomized['people']
            if not question.people[person['original_index']]['is_lying']
        )

        async_to_sync(consumer.handle_admin_end_question)({})
        async_to_sync(consumer.handle_participant_submit_answer)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
            'question_id': question.id,
            'selected_liars': [truth_displayed['id']],
            'time_taken': 3.6,
        })

        answer = WhoAnswer.objects.get(quiz=quiz, participant=participant, question=question)
        participant.refresh_from_db()
        quiz.refresh_from_db()

        self.assertIsNone(quiz.current_question_id)
        self.assertEqual(answer.points_earned, -1)
        self.assertEqual(participant.total_score, -1)
        payload = next(message for message in sent_messages if message['type'] == 'answer_submitted')
        self.assertEqual(payload['points_earned'], -1)

    def test_admin_set_time_per_person_is_blocked_during_running_question(self):
        user = User.objects.create_user(username='who-time-locked')
        question = WhoQuestion.objects.create(
            statement='Zeit sperren',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=user,
        )
        quiz = WhoQuiz.objects.create(
            title='Who Time Locked',
            room_code='7621',
            creator=user,
            status='active',
            current_question=question,
        )
        WhoSession.objects.create(quiz=quiz)
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_set_time_per_person)({
            'time_per_person': 25,
        })

        self.assertEqual(consumer.channel_layer.group_messages, [])
        self.assertEqual(sent_messages[-1]['type'], 'error')


class WhoLyingResultViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-result', password='secret')
        self.quiz = WhoQuiz.objects.create(
            title='Who Results',
            room_code='7614',
            creator=self.user,
            status='completed',
        )
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
        )
        self.question = WhoQuestion.objects.create(
            statement='Ich habe einen Oscar gewonnen.',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Robin', 'is_lying': False},
                {'name': 'Cristiano', 'is_lying': True},
            ],
            created_by=self.user,
        )

    def test_result_page_renders_negative_points_without_double_plus(self):
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            selected_liars=[0],
            time_taken=3.2,
        )

        response = self.client.get(
            reverse('who_is_lying:result', args=[self.quiz.room_code, self.participant.name])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '-1 pts')
        self.assertNotContains(response, '+-1 pts')


class WhoLyingMonitorViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='who-monitor',
            password='secret',
            is_staff=True,
        )
        self.quiz = WhoQuiz.objects.create(
            title='Who Monitor',
            room_code='7615',
            creator=self.user,
            status='active',
        )
        self.question = WhoQuestion.objects.create(
            statement='Wer lügt?',
            points=10,
            time_limit=18,
            people=[
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
                {'name': 'D', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.session = WhoSession.objects.create(quiz=self.quiz)
        self.session.send_question(self.question)
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            total_score=-2,
        )
        self.client.force_login(self.user)

    def test_monitor_uses_per_name_timer_and_marks_current_person(self):
        start_time = timezone.now() - timezone.timedelta(seconds=37)
        total_duration = self.question.get_total_duration_seconds()
        randomized_people = self.question.get_randomized_people(room_code=self.quiz.room_code)['people']
        expected_index = 2

        self.quiz.question_start_time = start_time
        self.quiz.save(update_fields=['question_start_time'])
        self.session.question_end_time = start_time + timezone.timedelta(seconds=total_duration)
        self.session.save(update_fields=['question_end_time'])

        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_time_per_person'], 18)
        self.assertEqual(response.context['current_person_index'], expected_index)
        self.assertEqual(response.context['current_person_name'], randomized_people[expected_index]['name'])
        self.assertAlmostEqual(response.context['current_question_time_left'], 17, delta=1)
        self.assertContains(response, 'id="questionTimeLeft"')
        self.assertContains(response, 'Current name')
        self.assertContains(response, 'id="currentPersonName"')
        self.assertContains(response, 'id="currentPersonProgress"')
        self.assertContains(response, f'data-current-person-index="{expected_index}"')
        self.assertContains(response, 'data-time-per-person="18"')
        self.assertContains(response, 'active-current')
        self.assertContains(response, randomized_people[expected_index]['name'])
        self.assertContains(response, 'updateCurrentPersonDisplay(index)')
        self.assertContains(response, 'data-question-started-at=')
        self.assertContains(response, 'data-server-now=')
        self.assertContains(response, 'getQuestionTimerState()')
        self.assertContains(response, "case 'time_per_person_updated':")

    def test_monitor_does_not_render_non_negative_score_clamp(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<input type="number" class="form-control" id="editScoreInput" step="1">', html=False)
        self.assertContains(response, 'Please enter a valid integer.')

    def test_monitor_does_not_render_points_per_id_badge(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'pts per ID')
        self.assertContains(response, 'people')

    def test_monitor_uses_actual_truth_status_for_current_question_people(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        current_people = response.context['current_question_people']
        self.assertTrue(any(person['is_lying'] for person in current_people))
        self.assertTrue(any(not person['is_lying'] for person in current_people))
        self.assertContains(response, '<span>Liar</span>', html=False)
        self.assertContains(response, '<span>Truth Teller</span>', html=False)

    def test_monitor_disables_live_time_controls_during_running_question(self):
        response = self.client.get(
            reverse('admin_dashboard:who_monitor', args=[self.quiz.room_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="timePerPersonInput"')
        self.assertContains(response, 'id="updateTimePerPersonBtn"')
        self.assertContains(response, 'Während einer laufenden Frage gesperrt')
        self.assertContains(response, 'id="timePerPersonInput" value="18" style="max-width:120px;" disabled', html=False)
        self.assertContains(response, 'id="updateTimePerPersonBtn" disabled', html=False)

    def test_update_question_blocks_running_question_content_changes(self):
        original_statement = self.question.statement
        response = self.client.post(
            reverse('admin_dashboard:update_who_question'),
            data=json.dumps({
                'question_id': self.question.id,
                'statement': 'Geaendert',
                'time_limit': 99,
                'people': [
                    {'name': 'X', 'is_lying': False},
                    {'name': 'Y', 'is_lying': True},
                ],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.question.refresh_from_db()
        self.assertEqual(self.question.statement, original_statement)
        self.assertEqual(self.question.time_limit, 18)
        self.assertEqual(
            self.question.people,
            [
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
                {'name': 'D', 'is_lying': True},
            ],
        )

    def test_admin_score_endpoint_accepts_negative_scores(self):
        response = self.client.post(
            reverse('admin_dashboard:set_who_participant_score'),
            data=json.dumps({
                'participant_id': self.participant.id,
                'score': -3,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.total_score, -3)
        self.assertJSONEqual(response.content, {'success': True, 'new_score': -3})


class WhoLyingPlayViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-play', password='secret')
        self.quiz = WhoQuiz.objects.create(
            title='Who Play',
            room_code='7618',
            creator=self.user,
            status='active',
        )
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUB2',
        )

    def test_play_template_contains_set_reveal_state_markup(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="setRevealState"')
        self.assertContains(response, 'showRevealState()')
        self.assertContains(response, 'reveal-row reveal-row-results')
        self.assertContains(response, "typeof person.was_correct === 'boolean'")
        self.assertContains(response, 'Dein Urteil:')
        self.assertContains(response, 'richtig')
        self.assertContains(response, 'falsch')
        self.assertContains(response, '.selection-instructions {')
        self.assertContains(response, 'display: none;')
        self.assertContains(response, 'question_id: this.activeQuestionId')
        self.assertContains(response, 'lügt nicht')
        self.assertContains(response, 'gedrückt')
        self.assertContains(response, 'Set-Auflösung')
        self.assertContains(response, 'class="who-reveal-vhs"')
        self.assertContains(response, 'DIE RICHTIGE ANTWORT IST')
        self.assertContains(response, 'GEGEBENE ANTWORT')
        self.assertContains(response, 'BEHAUPTUNG')
        self.assertContains(response, 'id="whoRevealCorrectValue"')
        self.assertContains(response, 'id="whoRevealGivenValue"')
        self.assertContains(response, 'id="whoRevealPromptValue"')
        self.assertContains(response, "if (!this.currentSetResult || !this.questionHasEnded) return;")
        self.assertContains(response, "return value ? 'Stimmt' : 'Stimmt nicht';")

        markup = response.content.decode('utf-8')
        self.assertEqual(markup.count('WhoPlayer.prototype.showRevealState = function()'), 1)
        self.assertEqual(markup.count('class="who-reveal-vhs"'), 1)
        self.assertNotIn('showLegacyRevealState', markup)

        vhs_css = (Path(__file__).resolve().parent.parent / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        self.assertIn('#setRevealState .who-reveal-default', vhs_css)
        self.assertIn('#setRevealState .who-reveal-vhs__correct', vhs_css)
        self.assertIn('color: #e9dfca;', vhs_css)

    def test_play_template_contains_vhs_current_person_layout(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="selection-summary who-person-position"')
        self.assertContains(response, 'id="personSwitchTimer"')
        self.assertContains(response, 'class="who-person-switch-timer__fill"')
        self.assertContains(response, 'class="who-person-actions"')
        self.assertContains(response, 'vhs-who-liar-button')
        self.assertContains(response, 'id="personProgressDefault"')
        self.assertContains(response, 'id="personProgressVhs"')
        self.assertContains(response, 'progressVhsEl.textContent = `${this.currentPersonPosition + 1} von ${this.peopleSequence.length}`')
        self.assertContains(response, '1 - (remaining / duration)')
        self.assertContains(response, '--who-person-progress')
        self.assertContains(response, "accuseBtn.classList.toggle('is-selected', isSelected)")
        self.assertContains(response, "accuseBtn.setAttribute('aria-pressed', isSelected ? 'true' : 'false')")

        markup = response.content.decode('utf-8')
        card_start = markup.index('id="currentPersonCard"')
        name_panel_start = markup.index('class="who-person-name-panel"', card_start)
        actions_start = markup.index('class="who-person-actions"', name_panel_start)
        button_position = markup.index('id="accuseLiarBtn"')
        self.assertGreater(button_position, actions_start)
        self.assertNotIn('id="accuseLiarBtn"', markup[name_panel_start:actions_start])

        vhs_css = (Path(__file__).resolve().parent.parent / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        self.assertIn('body.who-play-page .vhs-theme-shell #questionState .statement-text', vhs_css)
        self.assertIn('background: transparent !important;', vhs_css)
        self.assertIn('transform: scaleX(var(--who-person-progress, 0));', vhs_css)
        self.assertIn('transform-origin: left center;', vhs_css)
        self.assertIn('.vhs-who-liar-button:not(:disabled):hover', vhs_css)
        self.assertIn('.vhs-who-liar-button.is-selected', vhs_css)

    def test_vhs_set_timer_and_set_end_summary_use_who_specific_sources(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="playerSetTimeLeft"')
        self.assertContains(response, 'data-vhs-timer-key=""')
        self.assertContains(response, 'data-vhs-timer-maximum=""')
        self.assertContains(response, 'const endsAtMs = this.parseServerTimestamp(question?.question_end_time);')
        self.assertContains(response, 'const totalTimerElement = document.getElementById(\'playerSetTimeLeft\');')
        self.assertContains(response, 'const initialSetDuration = Math.max(1, peopleCount * this.currentTimePerPerson);')
        self.assertContains(response, "const setTimerKey = `${question?.id || this.currentQuestionIndex || 'active'}:${question?.question_started_at || ''}`;")
        self.assertContains(response, 'totalTimerElement.dataset.vhsTimerKey = setTimerKey;')
        self.assertContains(response, 'totalTimerElement.dataset.vhsTimerMaximum = String(initialSetDuration);')
        self.assertContains(response, 'totalTimerElement.textContent = timerState.totalTimeLeft')
        self.assertContains(response, 'personSwitchTimer.style.setProperty(\'--who-person-progress\', progress)')
        self.assertContains(response, 'class="submitted-details who-set-summary"')
        self.assertContains(response, 'class="who-set-ended-title__vhs">SET BEENDET</span>')
        self.assertContains(response, 'class="who-set-ended-waiting__vhs">Warte auf die n&auml;chste Runde...</span>')
        self.assertContains(response, 'NICHT ERKANNTE L&Uuml;GNER')
        self.assertContains(response, 'FALSCHE BESCHULDIGUNGEN')
        self.assertContains(response, 'id="whoSetEvaluatingState"')
        self.assertContains(response, 'SET WIRD AUSGEWERTET...')
        self.assertContains(response, 'data-who-set-result-ready="false"')
        self.assertContains(response, "this.showState('whoSetEvaluatingState')")
        self.assertContains(response, 'this.renderSetResult(data);')
        self.assertContains(response, 'this.setSetResultReady(true);')

        markup = response.content.decode('utf-8')
        self.assertEqual(markup.count('id="whoSetEvaluatingState"'), 1)
        self.assertEqual(markup.count('id="answerSubmittedState"'), 1)
        self.assertEqual(markup.count('this.questionTimer = setInterval(renderTimerState, 250);'), 1)
        submitted_handler = markup.index('onAnswerSubmitted(data) {')
        render_call = markup.index('this.renderSetResult(data);', submitted_handler)
        final_show = markup.index("this.showState('answerSubmittedState');", render_call)
        render_start = markup.index('renderSetResult(data) {')
        result_ready = markup.index('this.setSetResultReady(true);', render_start)
        state_switch = markup.index('showState(stateId) {')
        hide_all_states = markup.index("state.classList.add('d-none');", state_switch)
        show_target_state = markup.index("targetState.classList.remove('d-none');", hide_all_states)
        self.assertLess(render_call, final_show)
        self.assertLess(render_start, result_ready)
        self.assertLess(hide_all_states, show_target_state)

        project_root = Path(__file__).resolve().parent.parent
        vhs_css = (project_root / 'static' / 'themes' / 'vhs' / 'vhs.css').read_text(encoding='utf-8')
        accessibility = (project_root / 'templates' / 'includes' / 'accessibility_widget.html').read_text(encoding='utf-8')
        self.assertIn('body.who-play-page .vhs-theme-shell #answerSubmittedState .who-set-summary', vhs_css)
        self.assertIn('.who-set-ended-waiting {', vhs_css)
        self.assertIn('color: #9ba19c !important;', vhs_css)
        self.assertIn('.who-set-summary__group-values:empty::before', vhs_css)
        self.assertIn('content: "Keine";', vhs_css)
        self.assertIn('#answerSubmittedState:not([data-who-set-result-ready="true"])', vhs_css)
        self.assertIn('.who-set-evaluating-panel', vhs_css)
        self.assertIn("if (document.body.classList.contains('who-play-page')) return;", accessibility)
        self.assertIn("? document.getElementById('playerSetTimeLeft')", accessibility)
        self.assertIn("timerSource.dataset.vhsTimerMaximum", accessibility)
        self.assertIn("timerSource.dataset.vhsTimerKey", accessibility)
        self.assertIn("timerValue / vhsTimerMaximum", accessibility)
        self.assertIn("timerFill.style.transition = 'none';", accessibility)
        self.assertIn("timerFill.style.removeProperty('transition');", accessibility)

    def test_vhs_set_end_is_not_overwritten_by_automatic_reveal(self):
        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        markup = response.content.decode('utf-8')

        question_ended_start = markup.index('onQuestionEnded() {')
        question_ended_end = markup.index('onQuizEnded(data) {', question_ended_start)
        question_ended_handler = markup[question_ended_start:question_ended_end]
        self.assertIn("if (this.isVhsTheme()) {", question_ended_handler)
        self.assertIn('this.showVhsSetEndedState();', question_ended_handler)
        self.assertLess(
            question_ended_handler.index('this.showVhsSetEndedState();'),
            question_ended_handler.index('this.showRevealState();'),
        )
        self.assertIn('return;', question_ended_handler)

        answer_submitted_start = markup.index('onAnswerSubmitted(data) {')
        answer_submitted_end = markup.index('isVhsTheme() {', answer_submitted_start)
        answer_submitted_handler = markup[answer_submitted_start:answer_submitted_end]
        self.assertIn("if (this.isVhsTheme()) {", answer_submitted_handler)
        self.assertIn('this.showVhsSetEndedState();', answer_submitted_handler)
        self.assertIn('} else if (this.questionHasEnded) {', answer_submitted_handler)

        set_ended_start = markup.index('showVhsSetEndedState() {')
        set_ended_end = markup.index('renderSetResult(data) {', set_ended_start)
        set_ended_handler = markup[set_ended_start:set_ended_end]
        self.assertIn('if (!this.currentSetResult) return;', set_ended_handler)
        self.assertIn("this.showState('answerSubmittedState');", set_ended_handler)
        self.assertNotIn('showRevealState', set_ended_handler)

        self.assertIn("this.showState('questionState');", markup)
        self.assertIn("this.showState('quizEndedState');", markup)
        self.assertEqual(markup.count('id="answerSubmittedState"'), 1)
        self.assertEqual(markup.count('id="setRevealState"'), 1)

    def test_play_view_embeds_server_timer_sync_fields_for_active_question(self):
        question = WhoQuestion.objects.create(
            statement='Aktive Runde',
            points=10,
            time_limit=18,
            people=[
                {'name': 'A', 'is_lying': False},
                {'name': 'B', 'is_lying': True},
                {'name': 'C', 'is_lying': False},
            ],
            created_by=self.user,
        )
        start_time = timezone.now() - timezone.timedelta(seconds=19)
        self.quiz.current_question = question
        self.quiz.question_start_time = start_time
        self.quiz.save(update_fields=['current_question', 'question_start_time'])
        session = WhoSession.objects.create(quiz=self.quiz)
        session.question_end_time = start_time + timezone.timedelta(seconds=54)
        session.save(update_fields=['question_end_time'])

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_timer_state']['time_per_person'], 18)
        self.assertEqual(response.context['current_question_timer_state']['current_person_index'], 1)
        self.assertTrue(16 <= response.context['current_question_timer_state']['current_person_time_left'] <= 17)
        self.assertContains(response, 'question_started_at')
        self.assertContains(response, 'current_person_time_left')
        self.assertContains(response, 'server_now')
        self.assertContains(response, 'getQuestionTimerState(question)')
        self.assertContains(response, 'this.startQuestionTimer(activeQuestion);')


class WhoLyingHubParticipantLifecycleTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(username='who-hub-lifecycle')
        self.session = HubSession.objects.create(
            code='GI4XUE',
            name='Who lifecycle',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=2,
        )
        self.hub_participants = [
            HubParticipant.objects.create(
                session=self.session,
                nickname='jkh',
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            ),
            HubParticipant.objects.create(
                session=self.session,
                nickname='Ada',
                scoring_eligible=True,
                checked_in_at=timezone.now(),
            ),
        ]
        self.games = []
        self.steps = []
        for order, room_code in enumerate(('W123', 'W124', 'W125')):
            game = WhoQuiz.objects.create(
                creator=self.user,
                title=f'Who Lock Smoke {order + 1}',
                room_code=room_code,
                status='waiting',
            )
            step = HubGameStep.objects.create(
                session=self.session,
                order=order,
                game_key='who',
                room_code=room_code,
                title=game.title,
            )
            self.games.append(game)
            self.steps.append(step)

    def activate(self, index, action=None):
        result = resolve_session_game_activation(
            self.session.code,
            'who',
            self.games[index].room_code,
            action=action,
        )
        self.assertTrue(result['success'], result)
        return result

    def play_params(self, index, **overrides):
        params = {
            'hub_session': self.session.code,
            'game_start_intro': '1',
            'game_start_key': 'who',
            'game_start_room': self.games[index].room_code,
            'game_start_nonce': str(1784646760729 + index),
            'game_start_order': str(self.steps[index].order + 1),
            'game_start_title': self.games[index].title,
        }
        params.update(overrides)
        return params

    def open_play(self, index, nickname='jkh', **overrides):
        return self.client.get(
            reverse('who_is_lying:play', args=[self.games[index].room_code, nickname]),
            self.play_params(index, **overrides),
        )

    def test_three_consecutive_who_games_provision_current_participants(self):
        for index in range(3):
            self.activate(index, action='end' if index else None)

            response = self.open_play(index)

            self.assertEqual(response.status_code, 200)
            participant = WhoParticipant.objects.get(
                quiz=self.games[index],
                name='jkh',
                hub_session_code=self.session.code,
            )
            self.assertEqual(participant.quiz_id, self.games[index].id)
            self.assertEqual(
                WhoParticipant.objects.filter(
                    quiz=self.games[index],
                    hub_session_code=self.session.code,
                ).count(),
                2,
            )
            self.session.refresh_from_db()
            self.assertEqual(self.session.current_step_index, index)

        self.assertEqual(
            WhoParticipant.objects.filter(hub_session_code=self.session.code).count(),
            6,
        )

    def test_activation_provisions_bindings_without_moving_players_out_of_lobby(self):
        self.activate(0)

        presence = get_session_lobby_presence(self.session.code)
        participants = WhoParticipant.objects.filter(
            quiz=self.games[0],
            hub_session_code=self.session.code,
        )

        self.assertEqual(participants.count(), 2)
        self.assertFalse(participants.filter(is_active=True).exists())
        self.assertTrue(presence['all_in_lobby'])
        self.assertEqual(presence['not_in_lobby_count'], 0)

        join_response = self.client.post(
            reverse('who_is_lying:join'),
            json.dumps({
                'room_code': self.games[0].room_code,
                'participant_name': 'jkh',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )

        self.assertEqual(join_response.status_code, 200)
        self.assertTrue(join_response.json()['success'])
        self.assertTrue(participants.get(name='jkh').is_active)
        self.assertFalse(participants.get(name='Ada').is_active)

        second_join_response = self.client.post(
            reverse('who_is_lying:join'),
            json.dumps({
                'room_code': self.games[0].room_code,
                'participant_name': 'Ada',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )

        self.assertEqual(second_join_response.status_code, 200)
        self.assertTrue(second_join_response.json()['success'])
        self.assertEqual(participants.filter(is_active=True).count(), 2)
        self.assertEqual(participants.count(), 2)

    def test_lobby_disconnect_and_game_connect_order_cannot_overwrite_presence(self):
        self.activate(0)
        participant = WhoParticipant.objects.get(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        )
        hub_consumer = HubConsumer()
        hub_consumer.session_code = self.session.code
        hub_consumer.group_name = f'hub_{self.session.code}'
        hub_consumer.channel_name = 'hub-old-channel'
        hub_consumer.hub_participant_id = self.hub_participants[0].id
        hub_consumer.channel_layer = SimpleNamespace(group_discard=AsyncMock())

        async_to_sync(hub_consumer.disconnect)(1000)
        participant.refresh_from_db()
        self.assertFalse(participant.is_active)

        game_consumer = WhoConsumer()
        game_consumer.room_code = self.games[0].room_code
        game_consumer.room_group_name = f'who_{self.games[0].room_code}'
        game_consumer.who_participant_id = None
        game_consumer.channel_layer = SimpleNamespace(group_send=AsyncMock())
        game_consumer.send = AsyncMock()
        async_to_sync(game_consumer.handle_participant_join)({
            'participant_name': 'jkh',
            'hub_session': self.session.code,
        })

        async_to_sync(hub_consumer.disconnect)(1000)
        participant.refresh_from_db()
        self.assertTrue(participant.is_active)
        self.assertEqual(game_consumer.who_participant_id, participant.id)

    def test_reload_and_rejoin_in_third_game_use_current_server_step(self):
        for index in range(3):
            self.activate(index, action='end' if index else None)

        initial_response = self.open_play(2)
        reload_response = self.client.get(
            reverse('who_is_lying:play', args=[self.games[2].room_code, 'jkh']),
            {'hub_session': self.session.code},
        )
        participant = WhoParticipant.objects.get(
            quiz=self.games[2],
            name='jkh',
            hub_session_code=self.session.code,
        )
        participant.is_active = False
        participant.save(update_fields=['is_active'])
        rejoin_response = self.open_play(
            2,
            game_start_nonce=self.play_params(0)['game_start_nonce'],
            game_start_order=self.play_params(0)['game_start_order'],
        )

        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(reload_response.status_code, 200)
        self.assertEqual(rejoin_response.status_code, 200)
        participant.refresh_from_db()
        self.assertTrue(participant.is_active)
        self.assertEqual(
            WhoParticipant.objects.filter(
                quiz=self.games[2],
                name='jkh',
                hub_session_code=self.session.code,
            ).count(),
            1,
        )

    def test_duplicate_activation_join_and_rejoin_are_idempotent(self):
        self.activate(0)
        self.activate(0)

        join_url = reverse('who_is_lying:join')
        payload = {
            'room_code': self.games[0].room_code,
            'participant_name': 'jkh',
            'hub_session': self.session.code,
        }
        first_join = self.client.post(join_url, json.dumps(payload), content_type='application/json')
        second_join = self.client.post(join_url, json.dumps(payload), content_type='application/json')
        first_play = self.open_play(0)
        second_play = self.open_play(0)

        self.assertEqual(first_join.status_code, 200)
        self.assertEqual(second_join.status_code, 200)
        self.assertEqual(first_play.status_code, 200)
        self.assertEqual(second_play.status_code, 200)
        self.assertEqual(
            WhoParticipant.objects.filter(
                quiz=self.games[0],
                name='jkh',
                hub_session_code=self.session.code,
            ).count(),
            1,
        )

    def test_play_recovers_missing_binding_only_for_current_authorized_hub_identity(self):
        self.activate(0)
        WhoParticipant.objects.filter(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        ).delete()

        response = self.open_play(0)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WhoParticipant.objects.filter(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        ).exists())

    def test_stale_or_manipulated_start_context_cannot_create_participant(self):
        self.activate(0)
        self.activate(1, action='end')
        current_count = WhoParticipant.objects.filter(quiz=self.games[1]).count()

        stale_response = self.open_play(0)
        stale_rejoin_response = self.client.get(
            reverse('who_is_lying:play', args=[self.games[0].room_code, 'jkh']),
            {'hub_session': self.session.code},
        )
        unknown_name_response = self.open_play(1, nickname='Mallory')
        invalid_session_response = self.client.get(
            reverse('who_is_lying:play', args=[self.games[1].room_code, 'jkh']),
            self.play_params(1, hub_session='INVALID'),
        )

        self.assertEqual(stale_response.status_code, 302)
        self.assertIn(reverse('games_hub:lobby', args=[self.session.code]), stale_response.url)
        self.assertIn('who_join_error=', stale_response.url)
        self.assertEqual(stale_rejoin_response.status_code, 302)
        self.assertEqual(unknown_name_response.status_code, 302)
        self.assertEqual(invalid_session_response.status_code, 302)
        self.assertFalse(WhoParticipant.objects.filter(quiz=self.games[1], name='Mallory').exists())
        self.assertFalse(WhoParticipant.objects.filter(
            quiz=self.games[1],
            name='jkh',
            hub_session_code='INVALID',
        ).exists())
        self.assertEqual(WhoParticipant.objects.filter(quiz=self.games[1]).count(), current_count)

    def test_join_rejects_untrusted_hub_session_and_accepts_alphanumeric_room(self):
        self.activate(2)
        WhoParticipant.objects.filter(
            quiz=self.games[2],
            name='jkh',
            hub_session_code=self.session.code,
        ).delete()
        join_url = reverse('who_is_lying:join')

        valid_response = self.client.post(
            join_url,
            json.dumps({
                'room_code': 'W125',
                'participant_name': 'jkh',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )
        invalid_response = self.client.post(
            join_url,
            json.dumps({
                'room_code': 'W125',
                'participant_name': 'Mallory',
                'hub_session': self.session.code,
            }),
            content_type='application/json',
        )

        self.assertEqual(valid_response.status_code, 200)
        self.assertTrue(valid_response.json()['success'])
        self.assertEqual(invalid_response.status_code, 403)
        self.assertFalse(invalid_response.json()['success'])
        self.assertFalse(WhoParticipant.objects.filter(quiz=self.games[2], name='Mallory').exists())

    def test_participants_and_sessions_remain_isolated_and_cleanup_is_room_scoped(self):
        self.activate(0)
        self.activate(1, action='end')
        self.open_play(1, nickname='jkh')
        mark_single_participant_inactive_for_lobby_return(
            self.session.code,
            'who',
            self.games[0].room_code,
            'jkh',
        )

        previous = WhoParticipant.objects.get(
            quiz=self.games[0],
            name='jkh',
            hub_session_code=self.session.code,
        )
        current = WhoParticipant.objects.get(
            quiz=self.games[1],
            name='jkh',
            hub_session_code=self.session.code,
        )
        ada = WhoParticipant.objects.get(
            quiz=self.games[1],
            name='Ada',
            hub_session_code=self.session.code,
        )
        self.assertFalse(previous.is_active)
        self.assertTrue(current.is_active)
        self.assertFalse(ada.is_active)

        other_session = HubSession.objects.create(
            code='OTHER1',
            name='Other session',
            is_active=True,
            started_at=timezone.now(),
            check_in_status=HubSession.CHECK_IN_COMPLETED,
            check_in_completed_at=timezone.now(),
            locked_participant_count=1,
        )
        HubParticipant.objects.create(
            session=other_session,
            nickname='jkh',
            scoring_eligible=True,
            checked_in_at=timezone.now(),
        )
        other_game = WhoQuiz.objects.create(
            creator=self.user,
            title='Other Who',
            room_code='W126',
            status='waiting',
        )
        HubGameStep.objects.create(
            session=other_session,
            order=0,
            game_key='who',
            room_code=other_game.room_code,
            title=other_game.title,
        )

        result = resolve_session_game_activation(other_session.code, 'who', other_game.room_code)

        self.assertTrue(result['success'])
        self.assertTrue(WhoParticipant.objects.filter(
            quiz=other_game,
            name='jkh',
            hub_session_code=other_session.code,
        ).exists())
        self.assertEqual(
            WhoParticipant.objects.filter(name='jkh', hub_session_code=self.session.code).count(),
            2,
        )

    def test_lobby_does_not_redirect_who_after_failed_join(self):
        response = self.client.get(
            reverse('games_hub:lobby', args=[self.session.code]),
            {'nickname': 'jkh'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "step.game_key === 'who'")
        self.assertContains(response, 'Who join failed; keeping participant in the hub lobby.')

class WhoLyingScoreBoxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='who-score-box', password='secret')
        self.quiz = WhoQuiz.objects.create(
            title='Who Score Box',
            room_code='7621',
            creator=self.user,
            status='active',
        )
        self.participant = WhoParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUB5',
        )

    def test_play_view_hydrates_score_box_with_negative_history(self):
        first_question = WhoQuestion.objects.create(
            statement='Runde 1',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
                {'name': 'Cara', 'is_lying': True},
            ],
            created_by=self.user,
        )
        current_question = WhoQuestion.objects.create(
            statement='Runde 2',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Dan', 'is_lying': False},
                {'name': 'Eve', 'is_lying': True},
            ],
            created_by=self.user,
        )
        upcoming_question = WhoQuestion.objects.create(
            statement='Runde 3',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Finn', 'is_lying': False},
                {'name': 'Gina', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.quiz.question_order = [first_question.id, current_question.id, upcoming_question.id]
        self.quiz.current_question = current_question
        self.quiz.save(update_fields=['question_order', 'current_question'])
        self.quiz.selected_questions.add(first_question, current_question, upcoming_question)
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=first_question,
            selected_liars=[0],
            time_taken=2.0,
        )

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertEqual(
            response.context['initial_progress_history'],
            [{
                'question_id': first_question.id,
                'question_number': 1,
                'points': -1,
                'max_points': 2,
            }],
        )
        self.assertEqual(
            response.context['question_scoreboard'][0],
            {
                'id': first_question.id,
                'number': 1,
                'earned_points': -1,
                'max_points': 2,
                'status': 'played',
            },
        )
        self.assertEqual(response.context['question_scoreboard'][1]['status'], 'current')
        self.assertIsNone(response.context['question_scoreboard'][1]['max_points'])
        self.assertEqual(response.context['question_scoreboard'][2]['status'], 'upcoming')
        self.assertIsNone(response.context['question_scoreboard'][2]['max_points'])
        self.assertContains(response, 'score-box__row')
        self.assertContains(response, 'scoreHistoryTotal')
        self.assertContains(response, 'whoQuestionScoreboardData')
        self.assertContains(response, 'whoInitialProgressData')
        self.assertContains(response, 'whoCurrentQuestionNumber')
        self.assertContains(response, 'const hasMaxPoints = isPlayed && maxPoints !== undefined && maxPoints !== null;')

    def test_play_view_uses_actual_send_order_for_out_of_order_current_round(self):
        first_question = WhoQuestion.objects.create(
            statement='Runde 1',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=self.user,
        )
        second_question = WhoQuestion.objects.create(
            statement='Runde 2',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=self.user,
        )
        third_question = WhoQuestion.objects.create(
            statement='Runde 3',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Eve', 'is_lying': False},
                {'name': 'Finn', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.quiz.question_order = [first_question.id, second_question.id, third_question.id]
        self.quiz.current_question = third_question
        self.quiz.save(update_fields=['question_order', 'current_question'])
        self.quiz.selected_questions.add(first_question, second_question, third_question)
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=first_question,
            selected_liars=[0],
            time_taken=2.0,
        )

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry['id'] for entry in response.context['question_scoreboard']],
            [first_question.id, third_question.id, second_question.id],
        )
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertContains(response, 'moveQuestionToNextFreeScoreSlot(question?.id, question?.total_possible_points);')

    def test_play_view_rejoin_after_host_end_uses_saved_post_end_history(self):
        first_question = WhoQuestion.objects.create(
            statement='Runde 1',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Alice', 'is_lying': False},
                {'name': 'Bob', 'is_lying': True},
            ],
            created_by=self.user,
        )
        next_question = WhoQuestion.objects.create(
            statement='Runde 2',
            points=10,
            time_limit=20,
            people=[
                {'name': 'Cara', 'is_lying': False},
                {'name': 'Dan', 'is_lying': True},
            ],
            created_by=self.user,
        )
        self.quiz.question_order = [first_question.id, next_question.id]
        self.quiz.save(update_fields=['question_order'])
        self.quiz.selected_questions.add(first_question, next_question)
        WhoAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=first_question,
            selected_liars=[0],
            time_taken=2.5,
        )

        response = self.client.get(
            reverse('who_is_lying:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertEqual(
            response.context['initial_progress_history'],
            [{
                'question_id': first_question.id,
                'question_number': 1,
                'points': -1,
                'max_points': 1,
            }],
        )
        self.assertEqual(response.context['question_scoreboard'][0]['status'], 'played')
        self.assertEqual(response.context['question_scoreboard'][0]['earned_points'], -1)
        self.assertEqual(response.context['question_scoreboard'][1]['status'], 'current')
