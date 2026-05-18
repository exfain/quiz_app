import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

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
