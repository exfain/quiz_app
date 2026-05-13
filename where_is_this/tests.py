import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .consumers import WhereConsumer
from .models import WhereAnswer, WhereParticipant, WhereQuestion, WhereQuiz, WhereSession


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class WhereScoreBoxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='where-scorebox-user')
        self.quiz = WhereQuiz.objects.create(
            title='Where Score Box Quiz',
            room_code='9200',
            creator=self.user,
            status='active',
        )
        self.participant = WhereParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUB1',
        )
        self.session = WhereSession.objects.create(quiz=self.quiz)

    def _create_question(self, text, points, lat):
        return WhereQuestion.objects.create(
            question_text=text,
            correct_latitude=lat,
            correct_longitude=lat + 1,
            points=points,
            time_limit=45,
            created_by=self.user,
        )

    def test_play_view_builds_hydrated_scorebox_for_current_and_future_questions(self):
        question_one = self._create_question('Q1', 80, 10)
        question_two = self._create_question('Q2', 60, 20)
        question_three = self._create_question('Q3', 40, 30)
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_two
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 2
        self.session.current_question_number = 2
        self.session.is_question_active = True
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_latitude=question_one.correct_latitude,
            user_longitude=question_one.correct_longitude,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_one.id,
                    'number': 1,
                    'earned_points': 80,
                    'max_points': 80,
                    'status': 'played',
                },
                {
                    'id': question_two.id,
                    'number': 2,
                    'earned_points': None,
                    'max_points': 60,
                    'status': 'current',
                },
                {
                    'id': question_three.id,
                    'number': 3,
                    'earned_points': None,
                    'max_points': 40,
                    'status': 'upcoming',
                },
            ],
        )
        self.assertEqual(
            response.context['initial_progress_history'],
            [
                {
                    'question_id': question_one.id,
                    'question_number': 1,
                    'points': 80,
                    'max_points': 80,
                }
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 80)
        self.assertEqual(response.context['score_total_max'], 80)
        self.assertContains(response, 'whereScoreBox')
        self.assertContains(response, 'whereScoreTotal')
        self.assertContains(response, 'whereQuestionScoreboardData')
        self.assertContains(response, 'whereInitialProgressData')
        self.assertContains(response, 'sessionCode: \'HUB1\'')

    def test_play_view_marks_sent_but_unanswered_question_as_zero_on_reload(self):
        question_one = self._create_question('Q1', 80, 10)
        question_two = self._create_question('Q2', 60, 20)
        question_three = self._create_question('Q3', 50, 30)
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = None
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 2
        self.session.current_question_number = 2
        self.session.is_question_active = False
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_latitude=question_one.correct_latitude,
            user_longitude=question_one.correct_longitude,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_one.id,
                    'number': 1,
                    'earned_points': 80,
                    'max_points': 80,
                    'status': 'played',
                },
                {
                    'id': question_two.id,
                    'number': 2,
                    'earned_points': 0,
                    'max_points': 60,
                    'status': 'played',
                },
                {
                    'id': question_three.id,
                    'number': 3,
                    'earned_points': None,
                    'max_points': 50,
                    'status': 'upcoming',
                },
            ],
        )
        self.assertEqual(response.context['score_total_earned'], 80)
        self.assertEqual(response.context['score_total_max'], 140)
        self.assertContains(response, '>80/140<', html=False)

    def test_play_view_uses_actual_send_order_for_out_of_order_current_question(self):
        question_one = self._create_question('Q1', 80, 10)
        question_two = self._create_question('Q2', 60, 20)
        question_three = self._create_question('Q3', 50, 30)
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_three
        self.quiz.save(update_fields=['question_order', 'current_question'])

        self.session.total_questions_sent = 2
        self.session.current_question_number = 2
        self.session.is_question_active = True
        self.session.save(update_fields=['total_questions_sent', 'current_question_number', 'is_question_active'])

        WhereAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_latitude=question_one.correct_latitude,
            user_longitude=question_one.correct_longitude,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('where_is_this:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry['id'] for entry in response.context['question_scoreboard']],
            [question_one.id, question_three.id, question_two.id],
        )
        self.assertEqual(response.context['current_question_id'], question_three.id)
        self.assertContains(response, 'moveQuestionToNextFreeScoreSlot(questionId, maxPoints = null)')


class WhereStartSyncTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = WhereConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'where_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_active_current_question_is_sent_on_participant_join(self):
        user = User.objects.create_user(username='where-player')
        question = WhereQuestion.objects.create(
            question_text='Where is the Eiffel Tower?',
            correct_latitude=48.8584,
            correct_longitude=2.2945,
            points=80,
            time_limit=45,
            hint_text='Paris landmark',
            created_by=user,
        )
        quiz = WhereQuiz.objects.create(
            title='Active Where',
            room_code='9201',
            creator=user,
            status='active',
            current_question=question,
        )
        WhereParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB1',
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_participant_join)({
            'participant_name': 'Ada',
            'hub_session': 'HUB1',
        })

        self.assertEqual([message['type'] for message in sent_messages], ['quiz_started', 'question_started'])
        self.assertEqual(sent_messages[1]['question']['id'], question.id)
        self.assertEqual(sent_messages[1]['question']['question_text'], question.question_text)
        self.assertEqual(sent_messages[1]['question']['points'], 80)
        self.assertNotIn('difficulty', sent_messages[1]['question'])
        self.assertEqual(sent_messages[1]['question']['time_limit'], 45)
        self.assertIsNone(sent_messages[1]['question']['image_url'])

    def test_admin_send_question_updates_session_progress_for_reload_safe_scorebox(self):
        user = User.objects.create_user(username='where-host-progress')
        first_question = WhereQuestion.objects.create(
            question_text='Where is Berlin?',
            correct_latitude=52.52,
            correct_longitude=13.405,
            points=100,
            time_limit=60,
            created_by=user,
        )
        second_question = WhereQuestion.objects.create(
            question_text='Where is Rome?',
            correct_latitude=41.9028,
            correct_longitude=12.4964,
            points=90,
            time_limit=45,
            created_by=user,
        )
        quiz = WhereQuiz.objects.create(
            title='Progress Where',
            room_code='9202',
            creator=user,
            status='active',
        )
        WhereSession.objects.create(quiz=quiz)
        consumer, _ = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({
            'question_id': first_question.id,
        })
        async_to_sync(consumer.handle_admin_send_question)({
            'question_id': second_question.id,
        })

        quiz.refresh_from_db()
        quiz.session.refresh_from_db()

        self.assertEqual(quiz.current_question_id, second_question.id)
        self.assertEqual(quiz.session.current_question_number, 2)
        self.assertEqual(quiz.session.total_questions_sent, 2)
        self.assertTrue(quiz.session.is_question_active)
