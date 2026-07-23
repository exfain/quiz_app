import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from games_hub.models import HubGameStep, HubParticipant, HubSession

from .consumers import EstimationConsumer
from .models import EstimationAnswer, EstimationParticipant, EstimationQuestion, EstimationQuiz, EstimationSession


class FakeChannelLayer:
    def __init__(self):
        self.group_messages = []

    async def group_send(self, group, message):
        self.group_messages.append((group, message))


class EstimationStartSyncTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = EstimationConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'estimation_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        sent_messages = []

        async def fake_send(text_data=None, **kwargs):
            sent_messages.append(json.loads(text_data))

        consumer.send = fake_send
        return consumer, sent_messages

    def test_host_start_broadcasts_quiz_started_to_waiting_clients(self):
        user = User.objects.create_user(username='estimation-host')
        quiz = EstimationQuiz.objects.create(
            title='Sync Estimation',
            room_code='9101',
            creator=user,
            status='waiting',
        )
        session = HubSession.objects.create(code='HUBSTART', name='Hub Start')
        session.check_in_status = HubSession.CHECK_IN_COMPLETED
        session.check_in_completed_at = timezone.now()
        session.locked_participant_count = 1
        session.save(update_fields=['check_in_status', 'check_in_completed_at', 'locked_participant_count'])
        HubParticipant.objects.create(
            session=session,
            nickname='Alice',
            checked_in_at=timezone.now(),
            scoring_eligible=True,
        )
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='estimation',
            room_code=quiz.room_code,
        )
        consumer, _ = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_start_quiz)({})

        quiz.refresh_from_db()
        self.assertEqual(quiz.status, 'active')
        self.assertEqual(
            consumer.channel_layer.group_messages[0],
            (
                f'estimation_{quiz.room_code}',
                {
                    'type': 'quiz_started',
                    'message': 'Estimation Quiz has started!'
                }
            )
        )
        self.assertEqual(consumer.channel_layer.group_messages[1][0], 'hub_HUBSTART')
        self.assertEqual(
            consumer.channel_layer.group_messages[1][1]['event'],
            {
                'type': 'quiz_started',
                'final_scores': [],
                'room_code': quiz.room_code,
                'game_key': 'estimation',
                'message': 'Estimation Quiz has started!',
            }
        )

    def test_fresh_start_clears_stale_reveal_runtime_before_first_question(self):
        user = User.objects.create_user(username='estimation-restart-host')
        question = EstimationQuestion.objects.create(
            question_text='Old revealed estimate',
            correct_answer=42,
            unit='number',
            max_points=5,
            zone_count=5,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Restarted Estimation',
            room_code='9199',
            creator=user,
            status='active',
            question_order=[question.id],
        )
        quiz.selected_questions.set([question])
        participant = EstimationParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUBRESET',
            total_score=7,
        )
        EstimationAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer=40,
            points_earned=3,
            time_taken=2.0,
        )
        participant.total_score = 7
        participant.save(update_fields=['total_score'])
        runtime = EstimationSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
            question_end_time=timezone.now() - timezone.timedelta(seconds=10),
            pending_answers={'Ada': {'user_answer': 40}},
            total_responses_current_question=1,
            average_score_current_question=3,
            average_accuracy_current_question=95,
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.start_quiz_db)(quiz.id)
        async_to_sync(consumer.handle_participant_join)({
            'participant_name': participant.name,
            'hub_session': participant.hub_session_code,
        })

        quiz.refresh_from_db()
        runtime.refresh_from_db()
        participant.refresh_from_db()
        response = self.client.get(
            reverse('estimation:play', args=[quiz.room_code, participant.name]),
            {'hub_session': participant.hub_session_code},
        )

        self.assertEqual(quiz.status, 'active')
        self.assertIsNone(quiz.current_question_id)
        self.assertIsNone(quiz.question_start_time)
        self.assertEqual(runtime.current_question_number, 0)
        self.assertEqual(runtime.total_questions_sent, 0)
        self.assertFalse(runtime.is_question_active)
        self.assertIsNone(runtime.question_end_time)
        self.assertEqual(runtime.pending_answers, {})
        self.assertEqual(runtime.total_responses_current_question, 0)
        self.assertEqual(runtime.average_score_current_question, 0)
        self.assertEqual(runtime.average_accuracy_current_question, 0)
        self.assertEqual([message['type'] for message in sent_messages], ['quiz_started'])
        self.assertEqual(response.context['initial_participant_phase'], 'waiting')
        self.assertContains(response, 'id="waitingQuizState" class="game-state "', html=False)
        self.assertContains(response, 'id="correctAnswerState" class="game-state d-none"', html=False)
        self.assertContains(response, 'this.onQuizStarted(data);')
        self.assertContains(response, 'data.resume_existing')
        self.assertContains(response, 'Ignoring stale question_ended without an active estimation question.')
        self.assertEqual(participant.total_score, 7)
        self.assertEqual(EstimationAnswer.objects.filter(quiz=quiz, participant=participant).count(), 1)

    def test_inactive_resume_preserves_active_round_runtime(self):
        user = User.objects.create_user(username='estimation-resume-host')
        question = EstimationQuestion.objects.create(
            question_text='Paused estimate',
            correct_answer=12,
            unit='number',
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Paused Estimation',
            room_code='9198',
            creator=user,
            status='inactive',
            started_at=timezone.now() - timezone.timedelta(minutes=1),
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=5),
        )
        runtime = EstimationSession.objects.create(
            quiz=quiz,
            current_question_number=2,
            total_questions_sent=2,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
            pending_answers={'Ada': {'user_answer': 10}},
        )
        consumer, _ = self.make_consumer(quiz)

        async_to_sync(consumer.start_quiz_db)(quiz.id)

        quiz.refresh_from_db()
        runtime.refresh_from_db()
        self.assertEqual(quiz.status, 'active')
        self.assertEqual(quiz.current_question_id, question.id)
        self.assertEqual(runtime.current_question_number, 2)
        self.assertEqual(runtime.total_questions_sent, 2)
        self.assertTrue(runtime.is_question_active)
        self.assertEqual(runtime.pending_answers, {'Ada': {'user_answer': 10}})

    def test_active_current_question_is_sent_on_participant_join(self):
        user = User.objects.create_user(username='estimation-player')
        question = EstimationQuestion.objects.create(
            question_text='How many people live in Paris?',
            correct_answer=2148000,
            unit='people',
            max_points=42,
            zone_count=42,
            hint_text='City proper',
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Active Estimation',
            room_code='9102',
            creator=user,
            status='active',
            current_question=question,
        )
        EstimationParticipant.objects.create(
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
        self.assertTrue(sent_messages[0]['resume_existing'])
        self.assertEqual(sent_messages[1]['question']['id'], question.id)
        self.assertEqual(sent_messages[1]['question']['question_text'], question.question_text)
        self.assertEqual(sent_messages[1]['question']['unit'], 'people')
        self.assertEqual(sent_messages[1]['question']['unit_display'], 'Personen')
        self.assertEqual(sent_messages[1]['question']['question_number'], 1)
        self.assertEqual(sent_messages[1]['question']['max_points'], 42)
        self.assertEqual(sent_messages[1]['question']['time_limit'], 90)
        self.assertFalse(sent_messages[1]['question']['has_answered'])

    def test_active_current_question_join_marks_existing_answer_as_answered(self):
        user = User.objects.create_user(username='estimation-rejoin-player')
        question = EstimationQuestion.objects.create(
            question_text='Estimate the distance',
            correct_answer=100,
            unit='meters',
            max_points=5,
            zone_count=5,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Rejoin Estimation',
            room_code='9103',
            creator=user,
            status='active',
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=5),
        )
        participant = EstimationParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code='HUB1',
        )
        EstimationSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=15),
        )
        EstimationAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer=100,
            time_taken=2.0,
        )
        consumer, sent_messages = self.make_consumer(quiz)

        async_to_sync(consumer.handle_participant_join)({
            'participant_name': 'Ada',
            'hub_session': 'HUB1',
        })

        question_payload = sent_messages[1]['question']
        self.assertTrue(question_payload['has_answered'])
        self.assertEqual(question_payload['existing_answer']['formatted_answer'], '100 m')
        self.assertGreaterEqual(question_payload['time_limit'], 0)
        self.assertGreaterEqual(question_payload['elapsed_seconds'], 0)

    def test_admin_send_question_uses_actual_send_order_for_question_number(self):
        user = User.objects.create_user(username='estimation-send-order')
        first_question = EstimationQuestion.objects.create(
            question_text='First',
            correct_answer=10,
            unit='number',
            max_points=5,
            zone_count=5,
            created_by=user,
        )
        second_question = EstimationQuestion.objects.create(
            question_text='Second',
            correct_answer=20,
            unit='number',
            max_points=5,
            zone_count=5,
            created_by=user,
        )
        third_question = EstimationQuestion.objects.create(
            question_text='Third',
            correct_answer=30,
            unit='number',
            max_points=5,
            zone_count=5,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Out of Order Estimation',
            room_code='9105',
            creator=user,
            status='active',
            question_order=[first_question.id, second_question.id, third_question.id],
        )
        quiz.selected_questions.set([first_question, second_question, third_question])
        consumer, _ = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_send_question)({'question_id': first_question.id})
        async_to_sync(consumer.handle_admin_send_question)({'question_id': third_question.id})

        first_payload = consumer.channel_layer.group_messages[0][1]
        second_payload = consumer.channel_layer.group_messages[1][1]
        self.assertEqual(first_payload['question']['question_number'], 1)
        self.assertEqual(second_payload['question']['question_number'], 2)

    def test_zone_mode_reveal_payload_includes_zone_scoring_breakdown(self):
        user = User.objects.create_user(username='zone-reveal-host')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100',
            correct_answer=100,
            tolerance_percentage=10,
            zone_count=3,
            max_points=3,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Zone Reveal Estimation',
            room_code='9103',
            creator=user,
            status='active',
            scoring_mode='zones',
            current_question=question,
        )
        consumer, _ = self.make_consumer(quiz)

        payload = async_to_sync(consumer.get_current_question_answer)(quiz)

        self.assertEqual(payload['scoring_mode'], 'zones')
        self.assertIsNotNone(payload['zone_scoring'])
        self.assertEqual(payload['zone_scoring']['outside_points'], 0)
        self.assertEqual(
            payload['zone_scoring']['zones'],
            [
                {
                    'zone_number': 1,
                    'min_percentage': 0.0,
                    'max_percentage': 10.0,
                    'absolute_min_value': 90,
                    'absolute_max_value': 110,
                    'absolute_range_display': '90-110',
                    'points': 3,
                },
                {
                    'zone_number': 2,
                    'min_percentage': 10.0,
                    'max_percentage': 20.0,
                    'absolute_min_value': 80,
                    'absolute_max_value': 120,
                    'absolute_range_display': '80-120',
                    'points': 2,
                },
                {
                    'zone_number': 3,
                    'min_percentage': 20.0,
                    'max_percentage': 30.0,
                    'absolute_min_value': 70,
                    'absolute_max_value': 130,
                    'absolute_range_display': '70-130',
                    'points': 1,
                },
            ]
        )

    def test_rank_mode_question_end_payload_hides_other_answer_values(self):
        user = User.objects.create_user(username='rank-payload-host')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100 rank payload',
            correct_answer=100,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Rank Payload Estimation',
            room_code='9104',
            creator=user,
            status='active',
            scoring_mode='rank',
            current_question=question,
        )
        participants = [
            EstimationParticipant.objects.create(quiz=quiz, name='Exact'),
            EstimationParticipant.objects.create(quiz=quiz, name='Close'),
        ]
        for participant, user_answer in zip(participants, [100, 103]):
            EstimationAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=question,
                user_answer=user_answer,
            )

        consumer, _ = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_end_question)({})

        payload = consumer.channel_layer.group_messages[-1][1]
        self.assertEqual(payload['type'], 'question_ended')
        self.assertIn('rank_results', payload)
        self.assertEqual(
            payload['rank_results'][0],
            {
                'participant_name': 'Exact',
                'points_earned': 2,
                'rank_position': 1,
            },
        )
        self.assertNotIn('user_answer', payload['rank_results'][0])
        self.assertNotIn('formatted_answer', payload['rank_results'][0])
        self.assertNotIn('accuracy_percentage', payload['rank_results'][0])


class EstimationScoringTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = EstimationConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'estimation_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        return consumer

    def test_zone_scoring_uses_percentage_zones_and_zone_count_as_max_points(self):
        user = User.objects.create_user(username='zones-host')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100',
            correct_answer=100,
            tolerance_percentage=10,
            zone_count=5,
            max_points=100,
            created_by=user,
        )

        self.assertEqual(question.calculate_score(100), 5)
        self.assertEqual(question.calculate_score(110), 5)
        self.assertEqual(question.calculate_score(80), 4)
        self.assertEqual(question.calculate_score(70), 3)
        self.assertEqual(question.calculate_score(50), 1)
        self.assertEqual(question.calculate_score(49), 0)

    def test_manual_zone_points_use_max_points_as_inner_zone_value(self):
        user = User.objects.create_user(username='manual-zones-host')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100 manual',
            correct_answer=100,
            tolerance_percentage=10,
            zone_count=5,
            max_points=8,
            use_manual_points=True,
            created_by=user,
        )

        self.assertEqual(question.get_max_points_for_mode('zones'), 8)
        self.assertEqual(question.calculate_score(100), 8)
        self.assertEqual(question.calculate_score(110), 8)
        self.assertEqual(question.calculate_score(80), 7)
        self.assertEqual(question.calculate_score(70), 6)
        self.assertEqual(question.calculate_score(50), 4)
        self.assertEqual(question.calculate_score(49), 0)

    def test_zone_reveal_data_includes_rounded_absolute_ranges(self):
        user = User.objects.create_user(username='zone-range-host')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 12.34',
            correct_answer=12.34,
            tolerance_percentage=10,
            zone_count=2,
            max_points=2,
            created_by=user,
        )

        zone_data = question.get_zone_reveal_data()

        self.assertEqual(
            zone_data['zones'],
            [
                {
                    'zone_number': 1,
                    'min_percentage': 0.0,
                    'max_percentage': 10.0,
                    'absolute_min_value': 11,
                    'absolute_max_value': 14,
                    'absolute_range_display': '11-14',
                    'points': 2,
                },
                {
                    'zone_number': 2,
                    'min_percentage': 10.0,
                    'max_percentage': 20.0,
                    'absolute_min_value': 10,
                    'absolute_max_value': 15,
                    'absolute_range_display': '10-15',
                    'points': 1,
                },
            ],
        )

    def test_legacy_tolerance_mode_is_treated_as_zone_mode_when_saving_answer(self):
        user = User.objects.create_user(username='legacy-zones-host')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100 legacy',
            correct_answer=100,
            tolerance_percentage=10,
            zone_count=5,
            max_points=100,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Legacy Zone Estimation',
            room_code='9301',
            creator=user,
            status='active',
            scoring_mode='tolerance',
            current_question=question,
        )
        participant = EstimationParticipant.objects.create(quiz=quiz, name='Ada')

        answer = EstimationAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer=120,
        )

        self.assertEqual(answer.points_earned, 4)

    def test_ranking_scores_by_absolute_deviation_descending_from_participant_count(self):
        user = User.objects.create_user(username='rank-host')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100 ranking',
            correct_answer=100,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Rank Estimation',
            room_code='9302',
            creator=user,
            status='active',
            scoring_mode='rank',
            current_question=question,
        )
        participants = [
            EstimationParticipant.objects.create(quiz=quiz, name='Exact'),
            EstimationParticipant.objects.create(quiz=quiz, name='Close'),
            EstimationParticipant.objects.create(quiz=quiz, name='Far'),
            EstimationParticipant.objects.create(quiz=quiz, name='Farthest'),
        ]
        for participant, user_answer in zip(participants, [100, 98, 90, 130]):
            EstimationAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=question,
                user_answer=user_answer,
            )
        consumer = self.make_consumer(quiz)

        results = async_to_sync(consumer.compute_rank_points_for_current_question)(quiz.id)

        self.assertEqual(
            [(result['participant_name'], result['points_earned']) for result in results],
            [('Exact', 4), ('Close', 3), ('Far', 2), ('Farthest', 1)],
        )
        for participant in participants:
            participant.refresh_from_db()
        self.assertEqual([participant.total_score for participant in participants], [4, 3, 2, 1])


class EstimationRankingSessionScopeTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='rank-scope-host')
        self.question = EstimationQuestion.objects.create(
            question_text='Scoped ranking question',
            correct_answer=100,
            created_by=self.user,
        )
        self.quiz = EstimationQuiz.objects.create(
            title='Scoped Rank Estimation',
            room_code='9310',
            creator=self.user,
            status='active',
            scoring_mode='rank',
            current_question=self.question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=5),
        )
        EstimationSession.objects.create(
            quiz=self.quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )
        self.old_hub = HubSession.objects.create(code='RANKOLD1', name='Old rank session')
        self.current_hub = HubSession.objects.create(code='RANKNOW1', name='Current rank session')
        HubGameStep.objects.create(
            session=self.old_hub,
            order=0,
            game_key='estimation',
            room_code=self.quiz.room_code,
        )
        HubGameStep.objects.create(
            session=self.current_hub,
            order=0,
            game_key='estimation',
            room_code=self.quiz.room_code,
        )

    def make_consumer(self):
        consumer = EstimationConsumer()
        consumer.room_code = self.quiz.room_code
        consumer.room_group_name = f'estimation_{self.quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        return consumer

    def create_answer(self, name, hub_session_code, estimate, time_taken=1.0):
        participant = EstimationParticipant.objects.create(
            quiz=self.quiz,
            name=name,
            hub_session_code=hub_session_code,
        )
        answer = EstimationAnswer.objects.create(
            quiz=self.quiz,
            participant=participant,
            question=self.question,
            user_answer=estimate,
            time_taken=time_taken,
        )
        return participant, answer

    def test_question_ranking_payload_contains_only_current_hub_session(self):
        current, _ = self.create_answer('Alex', self.current_hub.code, 101)
        old, old_answer = self.create_answer('Alex', self.old_hub.code, 100)
        consumer = self.make_consumer()

        async_to_sync(consumer.handle_admin_end_question)({'hub_session': self.current_hub.code})

        payload = consumer.channel_layer.group_messages[-1][1]
        old_answer.refresh_from_db()
        current.refresh_from_db()
        old.refresh_from_db()

        self.assertEqual(
            payload['rank_results'],
            [{'participant_name': 'Alex', 'points_earned': 1, 'rank_position': 1}],
        )
        self.assertEqual(current.total_score, 1)
        self.assertEqual(old_answer.points_earned, 0)
        self.assertEqual(old.total_score, 0)

    def test_question_ranking_sorts_only_multiple_current_participants(self):
        self.create_answer('Current Far', self.current_hub.code, 120, time_taken=1.0)
        self.create_answer('Current Exact', self.current_hub.code, 100, time_taken=2.0)
        self.create_answer('Other Session', self.old_hub.code, 100, time_taken=0.5)

        results = async_to_sync(self.make_consumer().compute_rank_points_for_current_question)(
            self.quiz.id,
            self.current_hub.code,
        )

        self.assertEqual(
            [(result['participant_name'], result['points_earned']) for result in results],
            [('Current Exact', 2), ('Current Far', 1)],
        )

    def test_final_result_leaderboard_contains_only_current_hub_session(self):
        current, _ = self.create_answer('Current Player', self.current_hub.code, 100)
        self.create_answer('Old Player', self.old_hub.code, 99)

        response = self.client.get(
            reverse('estimation:result', args=[self.quiz.room_code, current.name]),
            {'hub_session': self.current_hub.code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['leaderboard']), [current])
        self.assertEqual(response.context['total_participants'], 1)
        self.assertEqual(response.context['participant_rank'], 1)
        self.assertContains(response, 'FINALE RANGLISTE')
        self.assertContains(response, 'class="leaderboard-item current-user"', html=False)
        self.assertNotContains(response, 'Old Player')

    def test_final_score_payload_contains_only_current_hub_session(self):
        current, _ = self.create_answer('Current Final', self.current_hub.code, 100)
        self.create_answer('Old Final', self.old_hub.code, 99)
        current.total_score = 3
        current.save(update_fields=['total_score'])

        final_scores = async_to_sync(self.make_consumer().get_final_scores)()

        self.assertEqual(final_scores, [{'name': 'Current Final', 'total_score': 3}])


class EstimationPendingAnswerFinalizationTests(TransactionTestCase):
    def make_consumer(self, quiz):
        consumer = EstimationConsumer()
        consumer.room_code = quiz.room_code
        consumer.room_group_name = f'estimation_{quiz.room_code}'
        consumer.channel_layer = FakeChannelLayer()
        return consumer

    def test_handle_admin_end_question_finalizes_pending_zone_answer(self):
        user = User.objects.create_user(username='estimation-pending-zones')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100 zones',
            correct_answer=100,
            tolerance_percentage=10,
            zone_count=5,
            max_points=5,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Pending Zones',
            room_code='9303',
            creator=user,
            status='active',
            scoring_mode='zones',
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=8),
        )
        participant = EstimationParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='hub-zones',
        )
        session = EstimationSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=12),
            pending_answers={
                str(participant.id): {
                    'question_id': question.id,
                    'user_answer': '110',
                    'updated_at': timezone.now().isoformat(),
                    'participant_name': participant.name,
                    'hub_session_code': participant.hub_session_code,
                }
            },
        )
        consumer = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_end_question)({})

        answer = EstimationAnswer.objects.get(quiz=quiz, participant=participant, question=question)
        payload = consumer.channel_layer.group_messages[-1][1]
        participant.refresh_from_db()
        quiz.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(answer.user_answer, 110)
        self.assertEqual(answer.points_earned, 5)
        self.assertEqual(participant.total_score, 5)
        self.assertEqual(payload['evaluated_pending_answers'][0]['participant_name'], 'Alice')
        self.assertEqual(payload['evaluated_pending_answers'][0]['points_earned'], 5)
        self.assertIsNone(quiz.current_question)
        self.assertEqual(session.pending_answers, {})

    def test_handle_admin_end_question_finalizes_pending_rank_answer_before_ranking(self):
        user = User.objects.create_user(username='estimation-pending-rank')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100 rank pending',
            correct_answer=100,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Pending Rank',
            room_code='9304',
            creator=user,
            status='active',
            scoring_mode='rank',
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=10),
        )
        alice = EstimationParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='hub-rank',
        )
        bob = EstimationParticipant.objects.create(
            quiz=quiz,
            name='Bob',
            hub_session_code='hub-rank',
        )
        EstimationAnswer.objects.create(
            quiz=quiz,
            participant=bob,
            question=question,
            user_answer=100,
            time_taken=1.0,
        )
        session = EstimationSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=15),
            pending_answers={
                str(alice.id): {
                    'question_id': question.id,
                    'user_answer': '101',
                    'updated_at': timezone.now().isoformat(),
                    'participant_name': alice.name,
                    'hub_session_code': alice.hub_session_code,
                }
            },
        )
        consumer = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_end_question)({})

        payload = consumer.channel_layer.group_messages[-1][1]
        alice_answer = EstimationAnswer.objects.get(quiz=quiz, participant=alice, question=question)
        alice.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(alice_answer.user_answer, 101)
        self.assertEqual(alice.total_score, 1)
        self.assertEqual(
            payload['rank_results'],
            [
                {
                    'participant_name': 'Bob',
                    'points_earned': 2,
                    'rank_position': 1,
                },
                {
                    'participant_name': 'Alice',
                    'points_earned': 1,
                    'rank_position': 2,
                },
            ],
        )
        self.assertEqual(payload['evaluated_pending_answers'][0]['points_earned'], 1)
        self.assertEqual(session.pending_answers, {})

    def test_pending_answer_does_not_override_existing_submitted_answer(self):
        user = User.objects.create_user(username='estimation-pending-lock')
        question = EstimationQuestion.objects.create(
            question_text='Estimate 100 lock',
            correct_answer=100,
            tolerance_percentage=10,
            zone_count=5,
            max_points=5,
            created_by=user,
        )
        quiz = EstimationQuiz.objects.create(
            title='Pending Lock',
            room_code='9305',
            creator=user,
            status='active',
            scoring_mode='zones',
            current_question=question,
            question_start_time=timezone.now() - timezone.timedelta(seconds=10),
        )
        participant = EstimationParticipant.objects.create(
            quiz=quiz,
            name='Alice',
            hub_session_code='hub-lock',
        )
        existing_answer = EstimationAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_answer=100,
            time_taken=2.0,
        )
        session = EstimationSession.objects.create(
            quiz=quiz,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=10),
            pending_answers={
                str(participant.id): {
                    'question_id': question.id,
                    'user_answer': '130',
                    'updated_at': timezone.now().isoformat(),
                    'participant_name': participant.name,
                    'hub_session_code': participant.hub_session_code,
                }
            },
        )
        consumer = self.make_consumer(quiz)

        async_to_sync(consumer.handle_admin_end_question)({})

        participant.refresh_from_db()
        session.refresh_from_db()
        answers = list(EstimationAnswer.objects.filter(quiz=quiz, participant=participant, question=question))

        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].id, existing_answer.id)
        self.assertEqual(answers[0].user_answer, 100)
        self.assertEqual(participant.total_score, 5)
        self.assertEqual(session.pending_answers, {})


class EstimationRevealRenderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='estimation-reveal-player')
        self.quiz = EstimationQuiz.objects.create(
            title='Reveal Estimation',
            room_code='9401',
            creator=self.user,
            status='active',
            scoring_mode='zones',
        )
        self.participant = EstimationParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code=None,
        )

    def test_estimation_play_page_contains_zone_reveal_explanation_hooks(self):
        response = self.client.get(reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="zoneExplanationCard"')
        self.assertContains(response, 'id="zoneExplanationSummary"')
        self.assertContains(response, 'id="zoneRangeList"')
        self.assertContains(response, 'renderZoneScoringExplanation')
        self.assertContains(response, 'zone.absolute_range_display')
        self.assertContains(response, 'Schätzung')
        self.assertContains(response, 'Korrekte Antwort')
        self.assertContains(response, 'Abweichung:')
        self.assertContains(response, 'getDeviationPercentage(correctAnswerData)')
        self.assertContains(response, 'formatPointLabel(pointsForQuestion)')
        self.assertContains(response, 'Wertebereich:')
        self.assertContains(response, '% Abweichung')
        self.assertContains(response, 'background: var(--participant-effective-accent-color')
        self.assertContains(response, 'border: 2px solid currentColor')
        self.assertContains(response, 'background: rgba(255, 255, 255, 0.2)')
        self.assertNotContains(response, '<h2>Correct Answer Revealed!</h2>', html=True)
        self.assertNotContains(response, 'data-lucide="target"')
        self.assertNotContains(response, 'id="correctAnswerDisplay"')
        self.assertNotContains(response, 'Approx. values:')
        self.assertNotContains(response, 'Ungefähre Werte:')
        self.assertNotContains(response, 'Zone mode gives fewer points')

    def test_zone_mode_submit_stays_on_question_screen_with_waiting_hint(self):
        response = self.client.get(reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this.scoringMode = 'zones';")
        self.assertContains(response, "this.showSubmittedWaitingState(userAnswer);")
        self.assertContains(response, 'id="zoneSubmitFeedback"')
        self.assertContains(response, 'id="zoneSubmittedEstimate"')
        self.assertContains(response, 'Waiting for the other participants...')
        self.assertContains(response, "submitBtn.classList.add('d-none');")

    def test_rank_mode_submit_uses_waiting_hint_and_reveal_ranking(self):
        self.quiz.scoring_mode = 'rank'
        self.quiz.save(update_fields=['scoring_mode'])

        response = self.client.get(reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this.scoringMode = 'rank';")
        self.assertContains(response, "this.showSubmittedWaitingState(userAnswer);")
        self.assertContains(response, 'id="rankResultsCard"')
        self.assertContains(response, 'id="rankResultsList"')
        self.assertContains(response, 'renderRankResults(rankResults)')
        self.assertContains(response, "performanceText.innerHTML")
        self.assertContains(response, "formatPointLabel(pointsForQuestion)")

    def test_play_page_syncs_pending_answers_and_consumes_evaluated_pending_results(self):
        response = self.client.get(reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "type: 'participant_update_pending_answer'")
        self.assertContains(response, 'question_id: this.currentQuestionId')
        self.assertContains(response, 'getOwnEvaluatedPendingAnswer(data.evaluated_pending_answers)')
        self.assertContains(response, 'applyEvaluatedPendingAnswer(answer)')


class EstimationScoreBoxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='estimation-scorebox-user')
        self.quiz = EstimationQuiz.objects.create(
            title='Score Box Estimation',
            room_code='9402',
            creator=self.user,
            status='waiting',
            scoring_mode='zones',
        )
        self.participant = EstimationParticipant.objects.create(
            quiz=self.quiz,
            name='Ada',
            hub_session_code='HUB1',
        )

    def _create_question(self, text, answer, max_points=5, zone_count=5):
        return EstimationQuestion.objects.create(
            question_text=text,
            correct_answer=answer,
            unit='number',
            max_points=max_points,
            zone_count=zone_count,
            tolerance_percentage=10,
            created_by=self.user,
        )

    def test_estimation_end_screen_uses_reduced_german_theme_layout(self):
        self.participant.total_score = 1
        self.participant.save(update_fields=['total_score'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Score Box Estimation beendet!')
        self.assertContains(response, '<span class="score-label">Punkt</span>', html=True)
        self.assertContains(response, 'updateFinalScore(score)')
        self.assertContains(response, "scoreLabelEl.textContent = Number(score) === 1 ? 'Punkt' : 'Punkte'")
        self.assertContains(response, "returnToLobbyButton.textContent = '\\u2190 Zur Lobby zur\\u00fcckkehren';")
        self.assertContains(response, '.post-game-results-card[data-game-key="estimation"]')
        self.assertContains(response, "content.innerHTML = renderTable(payload);")
        self.assertContains(response, 'min-width: min(100%, 32rem)')
        self.assertContains(response, 'background: var(--participant-effective-accent-color')
        self.assertContains(response, 'background: rgba(255, 255, 255, 0.2)')
        self.assertContains(response, 'border: 2px solid currentColor')
        self.assertNotContains(response, 'Estimation Quiz Completed!')
        self.assertNotContains(response, 'Great job with all those estimates!')
        self.assertNotContains(response, 'trophy-icon')
        self.assertNotContains(response, 'ended-animation')
        self.assertNotContains(response, '<span class="score-label">points</span>', html=True)

    def test_completed_estimation_reload_renders_finished_state(self):
        self.quiz.status = 'completed'
        self.quiz.current_question = None
        self.quiz.save(update_fields=['status', 'current_question'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['initial_participant_phase'], 'finished')
        self.assertEqual(response.context['estimation_initial_state']['phase'], 'finished')
        self.assertContains(response, 'id="quizEndedState" class="game-state "', html=False)
        self.assertNotContains(response, 'id="quizEndedState" class="game-state d-none"', html=False)

    def test_inactive_participant_reload_returns_to_lobby_without_reactivating(self):
        self.participant.is_active = False
        self.participant.save(update_fields=['is_active'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.participant.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/hub/lobby/HUB1/', response['Location'])
        self.assertIn('nickname=Ada', response['Location'])
        self.assertFalse(self.participant.is_active)

    def test_active_estimation_reload_keeps_unanswered_question_open(self):
        question = self._create_question('Active question', 100, max_points=5, zone_count=5)
        self.quiz.status = 'active'
        self.quiz.current_question = question
        self.quiz.question_start_time = timezone.now() - timezone.timedelta(seconds=7)
        self.quiz.save(update_fields=['status', 'current_question', 'question_start_time'])
        EstimationSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=20),
        )

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        state = response.context['estimation_initial_state']
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['initial_participant_phase'], 'answering')
        self.assertFalse(state['current_question']['has_answered'])
        self.assertGreaterEqual(state['current_question']['time_limit'], 0)
        self.assertGreaterEqual(state['current_question']['elapsed_seconds'], 0)
        self.assertContains(response, 'id="questionState" class="game-state "', html=False)

    def test_active_estimation_reload_locks_already_answered_question(self):
        question = self._create_question('Answered question', 100, max_points=5, zone_count=5)
        self.quiz.status = 'active'
        self.quiz.current_question = question
        self.quiz.question_start_time = timezone.now() - timezone.timedelta(seconds=5)
        self.quiz.save(update_fields=['status', 'current_question', 'question_start_time'])
        EstimationSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=15),
        )
        EstimationAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            user_answer=100,
            time_taken=2.0,
        )

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        state = response.context['estimation_initial_state']
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['initial_participant_phase'], 'answered_waiting')
        self.assertTrue(state['current_question']['has_answered'])
        self.assertEqual(state['current_question']['existing_answer']['formatted_answer'], '100')
        self.assertContains(response, 'has_answered')
        self.assertContains(response, 'existing_answer')

    def test_reveal_estimation_reload_renders_reveal_state(self):
        question = self._create_question('Reveal question', 100, max_points=5, zone_count=5)
        self.quiz.status = 'active'
        self.quiz.question_order = [question.id]
        self.quiz.current_question = None
        self.quiz.save(update_fields=['status', 'question_order', 'current_question'])
        self.quiz.selected_questions.set([question])
        EstimationSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        EstimationAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question,
            user_answer=110,
            time_taken=3.0,
        )

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        state = response.context['estimation_initial_state']
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['initial_participant_phase'], 'reveal')
        self.assertEqual(state['correct_answer']['formatted_answer'], '100')
        self.assertEqual(state['participant_answer']['formatted_answer'], '110')
        self.assertContains(response, 'id="correctAnswerState" class="game-state "', html=False)
        self.assertNotContains(response, 'id="correctAnswerState" class="game-state d-none"', html=False)

    def test_estimation_play_builds_hydrated_scoreboard_from_start(self):
        question_one = self._create_question('Question one', 100, max_points=4, zone_count=4)
        question_two = self._create_question('Question two', 200, max_points=6, zone_count=6)
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_two.id, question_one.id]
        self.quiz.current_question = None
        self.quiz.save(update_fields=['question_order', 'current_question'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'],
            [
                {
                    'id': question_two.id,
                    'number': 1,
                    'earned_points': None,
                    'max_points': None,
                    'status': 'current',
                },
                {
                    'id': question_one.id,
                    'number': 2,
                    'earned_points': None,
                    'max_points': None,
                    'status': 'upcoming',
                },
            ],
        )
        self.assertEqual(response.context['initial_progress_history'], [])
        self.assertContains(response, 'scoreHistoryTotal')
        self.assertContains(response, 'estimationQuestionScoreboardData')
        self.assertContains(response, 'estimationInitialProgressData')
        self.assertContains(response, 'score-history-empty score-box__empty')

    def test_estimation_rank_mode_uses_awarded_max_points_for_played_question(self):
        self.quiz.scoring_mode = 'rank'
        self.quiz.save(update_fields=['scoring_mode'])
        question_one = self._create_question('Question one', 100, max_points=8, zone_count=8)
        question_two = self._create_question('Question two', 200, max_points=8, zone_count=8)
        self.quiz.selected_questions.set([question_one, question_two])
        self.quiz.question_order = [question_one.id, question_two.id]
        self.quiz.current_question = question_two
        self.quiz.save(update_fields=['question_order', 'current_question'])

        second_participant = EstimationParticipant.objects.create(
            quiz=self.quiz,
            name='Grace',
            hub_session_code='HUB1',
        )
        third_participant = EstimationParticipant.objects.create(
            quiz=self.quiz,
            name='Linus',
            hub_session_code='HUB1',
        )

        own_answer = EstimationAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_answer=110,
            time_taken=1.0,
        )
        own_answer.points_earned = 2
        own_answer.save(update_fields=['points_earned'])

        top_answer = EstimationAnswer.objects.create(
            quiz=self.quiz,
            participant=second_participant,
            question=question_one,
            user_answer=100,
            time_taken=1.0,
        )
        top_answer.points_earned = 3
        top_answer.save(update_fields=['points_earned'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['question_scoreboard'][0],
            {
                'id': question_one.id,
                'number': 1,
                'earned_points': 2,
                'max_points': 3,
                'status': 'played',
            },
        )
        self.assertEqual(
            response.context['initial_progress_history'],
            [
                {
                    'question_id': question_one.id,
                    'question_number': 1,
                    'points': 2,
                    'max_points': 3,
                }
            ],
        )
        self.assertEqual(response.context['current_question_max_points'], 3)
        self.assertContains(response, 'score-box__row')

    def test_estimation_play_uses_actual_send_order_for_out_of_order_current_question(self):
        question_one = self._create_question('Question one', 100, max_points=4, zone_count=4)
        question_two = self._create_question('Question two', 200, max_points=6, zone_count=6)
        question_three = self._create_question('Question three', 300, max_points=8, zone_count=8)
        self.quiz.selected_questions.set([question_one, question_two, question_three])
        self.quiz.question_order = [question_one.id, question_two.id, question_three.id]
        self.quiz.current_question = question_three
        self.quiz.save(update_fields=['question_order', 'current_question'])

        EstimationAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=question_one,
            user_answer=100,
            time_taken=1.0,
        )

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry['id'] for entry in response.context['question_scoreboard']],
            [question_one.id, question_three.id, question_two.id],
        )
        self.assertEqual(response.context['current_question_number'], 2)
        self.assertContains(response, 'moveQuestionToNextFreeScoreSlot(question.id, question.max_points);')

    def test_estimation_play_renders_current_question_unit_directly_next_to_input(self):
        question = EstimationQuestion.objects.create(
            question_text='How tall is the tower?',
            correct_answer=324,
            unit='meters',
            max_points=5,
            zone_count=5,
            tolerance_percentage=10,
            created_by=self.user,
        )
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="unit-display" id="unitDisplay">m</span>', html=False)
        self.assertNotContains(response, 'class="unit-display d-none" id="unitDisplay"', html=False)

    def test_estimation_active_answer_screen_uses_compact_themed_ui(self):
        question = EstimationQuestion.objects.create(
            question_text='How tall is the tower?',
            correct_answer=324,
            unit='meters',
            max_points=5,
            zone_count=5,
            tolerance_percentage=10,
            created_by=self.user,
        )
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="question-card qa-active-answer-card estimation-answer-card"', html=False)
        self.assertContains(response, 'class="question-number d-none" aria-hidden="true"', html=False)
        self.assertContains(response, 'id="playerTimerCircle"')
        self.assertContains(response, 'placeholder="Antwort"')
        self.assertContains(response, 'Einloggen')
        self.assertContains(response, '--participant-effective-accent-color')
        self.assertContains(response, 'width: fit-content')
        self.assertContains(response, 'font-size: clamp(2rem, 4.5vw, 3rem)')
        self.assertNotContains(response, '<label for="estimateInput" class="answer-label">Your Estimate:</label>', html=False)
        self.assertNotContains(response, 'Enter numbers only. Decimals are allowed.')
        self.assertNotContains(response, 'data-lucide="send"')
        self.assertNotContains(response, 'Submit Estimate')

    def test_estimation_play_hides_unit_slot_when_question_has_no_unit(self):
        question = EstimationQuestion.objects.create(
            question_text='How many items?',
            correct_answer=42,
            unit='number',
            max_points=5,
            zone_count=5,
            tolerance_percentage=10,
            created_by=self.user,
        )
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="unit-display d-none" id="unitDisplay"></span>', html=False)

    def test_estimation_play_localizes_years_in_active_question(self):
        question = EstimationQuestion.objects.create(
            question_text='Wie alt ist die Erde?',
            correct_answer=4_540_000_000,
            unit='years',
            max_points=5,
            zone_count=5,
            tolerance_percentage=10,
            created_by=self.user,
        )
        self.quiz.status = 'active'
        self.quiz.current_question = question
        self.quiz.question_start_time = timezone.now()
        self.quiz.save(update_fields=['status', 'current_question', 'question_start_time'])
        EstimationSession.objects.create(
            quiz=self.quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
            question_end_time=timezone.now() + timezone.timedelta(seconds=90),
        )

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="unitDisplay">Jahre</span>', html=False)
        self.assertEqual(response.context['estimation_initial_state']['current_question']['unit'], 'years')
        self.assertEqual(response.context['estimation_initial_state']['current_question']['unit_display'], 'Jahre')

    def test_estimation_play_keeps_legacy_unit_values_visible(self):
        question = EstimationQuestion.objects.create(
            question_text='What does it cost?',
            correct_answer=42,
            unit='EUR',
            max_points=5,
            zone_count=5,
            tolerance_percentage=10,
            created_by=self.user,
        )
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])

        response = self.client.get(
            reverse('estimation:play', args=[self.quiz.room_code, self.participant.name]),
            {'hub_session': self.participant.hub_session_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="unitDisplay">\u20ac</span>', html=False)
        self.assertNotContains(response, 'class="unit-display d-none" id="unitDisplay"', html=False)


class EstimationUnitAdminFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='estimation-admin-unit',
            password='secret',
            is_staff=True,
        )
        self.client.force_login(self.user)

    def test_manage_games_estimation_unit_select_uses_model_values(self):
        response = self.client.get(reverse('admin_dashboard:create_game'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="meters">m</option>', html=False)
        self.assertContains(response, '<option value="euros">€</option>', html=False)
        self.assertContains(response, '<option value="years">Jahre</option>', html=False)
        self.assertNotContains(response, '<option value="m">m</option>', html=False)
        self.assertNotContains(response, '<option value="EUR">EUR</option>', html=False)
        self.assertNotContains(response, '<option value="year">Jahr</option>', html=False)

    def test_estimation_management_and_api_use_localized_unit_labels(self):
        question = EstimationQuestion.objects.create(
            question_text='Wie viele Jahre?',
            correct_answer=10,
            unit='years',
            created_by=self.user,
        )

        management_response = self.client.get(reverse('admin_dashboard:estimation_management'))
        api_response = self.client.get(reverse('admin_dashboard:get_estimation_questions'))

        self.assertEqual(management_response.status_code, 200)
        self.assertContains(management_response, '<option value="years">Jahre</option>', html=False)
        self.assertNotContains(management_response, '<option value="years">Years</option>', html=False)
        payload = next(item for item in api_response.json()['questions'] if item['id'] == question.id)
        self.assertEqual(payload['unit'], 'years')
        self.assertEqual(payload['unit_display'], 'Jahre')

    def test_add_estimation_question_stores_model_conform_unit_values(self):
        response = self.client.post(
            reverse('admin_dashboard:add_estimation_question'),
            data=json.dumps({
                'question_text': 'How tall is the tower?',
                'correct_answer': 324,
                'unit': 'meters',
                'zone_count': 5,
                'tolerance_percentage': 10,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        question = EstimationQuestion.objects.get(question_text='How tall is the tower?')
        self.assertEqual(question.unit, 'meters')

    def test_add_and_edit_estimation_question_normalize_legacy_unit_values(self):
        create_response = self.client.post(
            reverse('admin_dashboard:add_estimation_question'),
            data=json.dumps({
                'question_text': 'Legacy unit question',
                'correct_answer': 7,
                'unit': 'm',
                'zone_count': 5,
                'tolerance_percentage': 10,
            }),
            content_type='application/json',
        )

        self.assertEqual(create_response.status_code, 200)
        question = EstimationQuestion.objects.get(question_text='Legacy unit question')
        self.assertEqual(question.unit, 'meters')

        legacy_question = EstimationQuestion.objects.create(
            question_text='What does it cost?',
            correct_answer=12,
            unit='EUR',
            zone_count=5,
            tolerance_percentage=10,
            created_by=self.user,
        )

        detail_response = self.client.get(
            reverse('admin_dashboard:get_estimation_question_detail', args=[legacy_question.id])
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()['question']['unit'], 'euros')

        update_response = self.client.post(
            reverse('admin_dashboard:update_estimation_question'),
            data=json.dumps({
                'question_id': legacy_question.id,
                'unit': 'year',
            }),
            content_type='application/json',
        )

        self.assertEqual(update_response.status_code, 200)
        legacy_question.refresh_from_db()
        self.assertEqual(legacy_question.unit, 'years')


class EstimationUnitDisplayTests(TestCase):
    def test_all_registered_units_have_localized_visible_presentations(self):
        expected_display = {
            'number': '',
            'meters': 'm',
            'kilometers': 'km',
            'feet': 'ft',
            'inches': 'in',
            'centimeters': 'cm',
            'years': 'Jahre',
            'months': 'Monate',
            'days': 'Tage',
            'hours': 'Stunden',
            'minutes': 'Minuten',
            'seconds': 'Sekunden',
            'kilograms': 'kg',
            'pounds': 'lbs',
            'grams': 'g',
            'tons': 'Tonnen',
            'liters': 'L',
            'gallons': 'gal',
            'milliliters': 'mL',
            'degrees': '\u00b0',
            'fahrenheit': '\u00b0F',
            'celsius': '\u00b0C',
            'dollars': '$',
            'euros': '\u20ac',
            'percent': '%',
            'people': 'Personen',
            'calories': 'cal',
            'watts': 'W',
            'miles': 'mi',
            'mph': 'mph',
            'kmh': 'km/h',
        }

        self.assertEqual(
            set(expected_display),
            {value for value, _label in EstimationQuestion.UNIT_CHOICES},
        )
        for unit, display in expected_display.items():
            with self.subTest(unit=unit):
                self.assertEqual(EstimationQuestion(unit=unit).get_unit_display_text(), display)

    def test_formatted_answers_share_localized_unit_and_legacy_singular(self):
        user = User.objects.create_user(username='estimation-localized-unit-user')
        years_question = EstimationQuestion.objects.create(
            question_text='Wie viele Jahre?',
            correct_answer=12,
            unit='years',
            created_by=user,
        )
        participant = EstimationParticipant.objects.create(
            quiz=EstimationQuiz.objects.create(
                title='Einheiten',
                room_code='9177',
                creator=user,
            ),
            name='Ada',
        )
        answer = EstimationAnswer.objects.create(
            quiz=participant.quiz,
            participant=participant,
            question=years_question,
            user_answer=10,
        )

        self.assertEqual(years_question.get_unit_display(), 'Jahre')
        self.assertEqual(years_question.get_formatted_correct_answer(), '12 Jahre')
        self.assertEqual(answer.get_formatted_user_answer(), '10 Jahre')
        self.assertEqual(EstimationQuestion(unit='year').get_unit_display_text(), 'Jahr')
        self.assertEqual(EstimationQuestion(unit='year').get_unit_display(), 'Jahr')

    def test_estimation_question_returns_expected_special_unit_symbols(self):
        user = User.objects.create_user(username='estimation-unit-user')

        euro_question = EstimationQuestion.objects.create(
            question_text='What does it cost?',
            correct_answer=12,
            unit='euros',
            created_by=user,
        )
        degree_question = EstimationQuestion.objects.create(
            question_text='How warm is it?',
            correct_answer=20,
            unit='celsius',
            created_by=user,
        )

        self.assertEqual(euro_question.get_unit_display_text(), '€')
        self.assertEqual(degree_question.get_unit_display_text(), '°C')
        return

        self.assertEqual(euro_question.get_unit_display_text(), '€')
        self.assertEqual(degree_question.get_unit_display_text(), '°C')
class EstimationLegacyUnitDisplayTests(TestCase):
    def test_estimation_question_normalizes_legacy_unit_symbols(self):
        user = User.objects.create_user(username='estimation-legacy-unit-user')

        meter_question = EstimationQuestion.objects.create(
            question_text='How high?',
            correct_answer=12,
            unit='m',
            created_by=user,
        )
        euro_question = EstimationQuestion.objects.create(
            question_text='How expensive?',
            correct_answer=12,
            unit='EUR',
            created_by=user,
        )

        self.assertEqual(meter_question.get_unit_display_text(), 'm')
        self.assertEqual(euro_question.get_unit_display_text(), '\u20ac')
