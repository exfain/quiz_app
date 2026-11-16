import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games_hub.authoritative_state import current_snapshot, reset_question_flow
from games_hub.models import HubGameStep, HubParticipant, HubSession
from games_hub.unit_tutorial_runtime import prepare_unit_tutorial_runtime, start_unit_tutorial_if_needed
from .consumers import WhoConsumer
from .models import WhoAnswer, WhoParticipant, WhoQuestion, WhoQuiz, WhoSession


class WhoPersonChoiceTests(TransactionTestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username='who-choices')
        self.hub = HubSession.objects.create(code='CHOICES', is_active=True, started_at=timezone.now())
        self.quiz = WhoQuiz.objects.create(creator=user, status='active', started_at=timezone.now())
        HubGameStep.objects.create(session=self.hub, order=0, game_key='who', room_code=self.quiz.room_code)
        self.question = WhoQuestion.objects.create(
            created_by=user, statement='Who lies?', time_limit=10,
            people=[{'name': 'Liar', 'is_lying': True}, {'name': 'Truth', 'is_lying': False}],
        )
        self.quiz.selected_questions.add(self.question)
        self.session = WhoSession.objects.create(quiz=self.quiz)
        self.participants = []
        for name in ('Alice', 'Bob'):
            HubParticipant.objects.create(session=self.hub, nickname=name)
            self.participants.append(WhoParticipant.objects.create(
                quiz=self.quiz, name=name, hub_session_code=self.hub.code,
            ))
        self.consumer = WhoConsumer()
        self.consumer.room_code = self.quiz.room_code
        self.consumer.room_group_name = f'who_{self.quiz.room_code}'
        self.consumer.channel_layer = type('Layer', (), {'group_send': AsyncMock()})()
        self.consumer.send = AsyncMock()
        self.started_at = timezone.now()
        self._start()

    def _action(self):
        snapshot = current_snapshot('who', self.quiz.room_code, self.hub.code)
        return {'question_id': self.question.id, 'game_id': snapshot['game_id'],
                'state_revision': snapshot['state_revision'], 'client_action_id': str(uuid.uuid4())}

    def _start(self):
        reset_question_flow(game_key='who', room_code=self.quiz.room_code,
                            session_code=self.hub.code, mode='manual_three_phase')
        presented = async_to_sync(self.consumer.present_who_question)(
            self.quiz.id, self.question.id, self.hub.code, self._action(), 10,
            at=self.started_at - timedelta(seconds=10),
        )
        self.assertTrue(presented.accepted)
        opened = async_to_sync(self.consumer.open_who_answering)(
            self.quiz.id, self.question.id, self.hub.code, self._action(), at=self.started_at,
        )
        self.assertTrue(opened.accepted)

    def choose(self, *, participant='Alice', person_id=0, selected=True, seconds=1, started_at=None):
        with patch('who_is_lying.consumers.timezone.now', return_value=self.started_at + timedelta(seconds=seconds)):
            return async_to_sync(self.consumer.sync_person_choices)(
                participant, self.hub.code, self.question.id, person_id=person_id, selected=selected,
                question_started_at=started_at or self.started_at.isoformat(), update=True,
            )

    def finish(self, seconds=21):
        with patch('who_is_lying.consumers.timezone.now', return_value=self.started_at + timedelta(seconds=seconds)):
            return async_to_sync(self.consumer.finalize_person_choices)(self.quiz.id, self.hub.code)

    def test_two_players_are_finalized_once_after_deadline(self):
        self.assertTrue(self.choose()['accepted'])
        self.assertTrue(self.choose(participant='Bob', selected=False)['accepted'])
        self.assertEqual(WhoAnswer.objects.count(), 0)
        result = self.finish()
        self.assertEqual(result['finalized_count'], 2)
        original = self.question.get_randomized_people(self.quiz.room_code)['position_to_original'][0]
        alice, bob = [WhoAnswer.objects.get(participant=participant) for participant in self.participants]
        self.assertEqual(alice.selected_liars, [original])
        self.assertEqual(alice.points_earned, self.question.calculate_score([original]))
        self.assertEqual(bob.points_earned, 0)
        self.assertIsNone(self.finish())
        self.assertEqual(WhoAnswer.objects.count(), 2)
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_answers, {})

    def test_current_person_can_be_unmarked_and_marked_again(self):
        first = self.choose()
        second = self.choose(selected=False, seconds=2)
        third = self.choose(seconds=3)
        self.assertEqual([first['selected_people'], second['selected_people'], third['selected_people']], [[0], [], [0]])
        self.assertEqual([first['choices_revision'], second['choices_revision'], third['choices_revision']], [1, 2, 3])

    def test_past_and_future_people_cannot_be_changed(self):
        self.assertFalse(self.choose(person_id=1)['accepted'])
        self.assertTrue(self.choose()['accepted'])
        late = self.choose(selected=False, seconds=11)
        self.assertFalse(late['accepted'])
        self.assertEqual(late['selected_people'], [0])
        self.assertTrue(self.choose(person_id=1, seconds=11)['accepted'])

    def test_deadline_rejection_does_not_overwrite_saved_choice(self):
        self.choose()
        late = self.choose(selected=False, seconds=20)
        self.assertFalse(late['accepted'])
        self.assertEqual(late['selected_people'], [0])
        self.finish()
        self.assertEqual(len(WhoAnswer.objects.get(participant=self.participants[0]).selected_liars), 1)

    def test_stale_instance_and_invalid_values_are_rejected(self):
        self.assertFalse(self.choose(started_at='old')['accepted'])
        for value in ('true', 1, None):
            self.assertFalse(self.choose(selected=value)['accepted'])
        self.assertFalse(self.choose(person_id=True)['accepted'])
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_answers, {})

    def test_reload_and_join_restore_only_own_confirmed_choices(self):
        self.choose()
        self.choose(participant='Bob', selected=False)
        response = self.client.get(reverse('who_is_lying:play', args=[self.quiz.room_code, 'Alice']),
                                   {'hub_session': self.hub.code})
        self.assertEqual(response.context['current_person_choices']['selected_people'], [0])
        async_to_sync(self.consumer.handle_participant_join)({'participant_name': 'Bob', 'hub_session': self.hub.code})
        messages = [json.loads(call.kwargs['text_data']) for call in self.consumer.send.await_args_list]
        own = next(message for message in messages if message['type'] == 'person_choices_synced')
        self.assertEqual(own['selected_people'], [])
        self.assertNotIn('pending_answers', own)

    def test_early_host_end_values_saved_marks_only(self):
        self.choose()
        result = self.finish(seconds=2)
        self.assertEqual(result['finalized_count'], 1)
        self.assertEqual(WhoAnswer.objects.get().participant_id, self.participants[0].id)

    def test_disconnected_player_keeps_saved_marks(self):
        self.choose()
        self.participants[0].is_active = False
        self.participants[0].save(update_fields=['is_active'])
        self.finish()
        self.assertEqual(WhoAnswer.objects.filter(participant=self.participants[0]).count(), 1)

    def test_zero_click_player_is_registered_and_gets_zero_points(self):
        result = async_to_sync(self.consumer.sync_person_choices)('Alice', self.hub.code, self.question.id)
        self.assertTrue(result['accepted'])
        self.finish()
        answer = WhoAnswer.objects.get()
        self.assertEqual(answer.selected_liars, [])
        self.assertEqual(answer.points_earned, 0)

    def test_tutorial_choices_never_add_or_subtract_points(self):
        self.quiz.tutorial_question = self.question
        self.quiz.save(update_fields=['tutorial_question'])
        prepare_unit_tutorial_runtime('who', self.quiz.room_code, self.hub.code, True)
        start_unit_tutorial_if_needed('who', self.quiz.room_code, self.hub.code)
        self.choose()
        self.finish()
        self.assertEqual(WhoAnswer.objects.get().points_earned, 0)
        self.participants[0].refresh_from_db()
        self.assertEqual(self.participants[0].total_score, 0)

    def test_game_end_discards_unfinalized_marks(self):
        self.choose()
        async_to_sync(self.consumer.end_quiz_db)(self.quiz.id)
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_answers, {})
        self.assertEqual(WhoAnswer.objects.count(), 0)

    def test_inactive_game_rejects_new_marks(self):
        self.choose()
        self.quiz.status = 'inactive'
        self.quiz.save(update_fields=['status'])
        self.assertFalse(self.choose(selected=False)['accepted'])
        self.session.refresh_from_db()
        self.assertEqual(self.session.get_person_choice_state(self.participants[0])['selected_people'], [0])

    def test_choice_handler_requires_successful_participant_join(self):
        self.consumer.who_participant_id = None
        async_to_sync(self.consumer.handle_person_choice)({'choice_request_id': 'before-join'})
        message = json.loads(self.consumer.send.await_args.kwargs['text_data'])
        self.assertEqual(message['type'], 'person_choice_rejected')
        self.assertEqual(message['choice_request_id'], 'before-join')
        self.session.refresh_from_db()
        self.assertEqual(self.session.pending_answers, {})

    def test_stale_host_end_does_not_close_or_score_current_set(self):
        self.choose()
        for expected in (
            {'expected_question_id': self.question.id + 1},
            {'expected_started_at': self.started_at - timedelta(seconds=30)},
        ):
            self.assertIsNone(async_to_sync(self.consumer.finalize_person_choices)(self.quiz.id, self.hub.code, **expected))
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_question_active)
        self.assertTrue(self.session.pending_answers)
        self.assertEqual(WhoAnswer.objects.count(), 0)


class WhoPersonChoiceMigrationTests(TransactionTestCase):
    def test_existing_scores_and_session_state_survive_additive_migration(self):
        old_target = [('who_is_lying', '0013_persist_recently_ended_question')]
        new_target = [('who_is_lying', '0015_alter_whoanswer_unique_together_and_more')]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(old_target)
            apps = executor.loader.project_state(old_target).apps
            user = apps.get_model('auth', 'User').objects.create(username='migration-who')
            quiz = apps.get_model('who_is_lying', 'WhoQuiz').objects.create(creator_id=user.id, status='active')
            question = apps.get_model('who_is_lying', 'WhoQuestion').objects.create(
                created_by_id=user.id, statement='Old question', people=[{'name': 'Old liar', 'is_lying': True}],
            )
            participant = apps.get_model('who_is_lying', 'WhoParticipant').objects.create(
                quiz_id=quiz.id, name='Old team', total_score=1, questions_answered=1,
            )
            answer = apps.get_model('who_is_lying', 'WhoAnswer').objects.create(
                quiz_id=quiz.id, participant_id=participant.id, question_id=question.id,
                selected_liars=[0], points_earned=1,
            )
            session = apps.get_model('who_is_lying', 'WhoSession').objects.create(
                quiz_id=quiz.id, current_question_number=4, total_questions_sent=4,
                recently_ended_question_id=question.id,
            )
            MigrationExecutor(connection).migrate(new_target)
            restored = WhoSession.objects.get(pk=session.id)
            self.assertEqual(restored.pending_answers, {})
            self.assertEqual(restored.current_question_number, 4)
            self.assertEqual(restored.recently_ended_question_id, question.id)
            self.assertEqual(WhoAnswer.objects.get(pk=answer.id).selected_liars, [0])
            self.assertEqual(WhoAnswer.objects.get(pk=answer.id).points_earned, 1)
            self.assertEqual(WhoParticipant.objects.get(pk=participant.id).total_score, 1)
        finally:
            MigrationExecutor(connection).migrate(new_target)
