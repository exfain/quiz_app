import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wer_weiss_mehr.models import (
    WerWeissMehrAnswerOption,
    WerWeissMehrGame,
    WerWeissMehrQuestion,
)


class WerWeissMehrQuestionDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user('question-owner', is_staff=True)
        cls.other = User.objects.create_user('other-host', is_staff=True)
        cls.superuser = User.objects.create_user('question-admin', is_staff=True, is_superuser=True)
        cls.question = WerWeissMehrQuestion.objects.create(
            question_text='Shared question bank question', created_by=cls.owner,
        )
        cls.answer = WerWeissMehrAnswerOption.objects.create(
            question=cls.question, canonical_text='An answer',
        )
        cls.game = WerWeissMehrGame.objects.create(creator=cls.owner)
        cls.game.selected_questions.add(cls.question)
        cls.url = reverse('admin_dashboard:delete_wer_weiss_mehr_question')

    def delete_as(self, user, question_id=None):
        self.client.force_login(user)
        return self.client.post(
            self.url,
            json.dumps({'question_id': self.question.pk if question_id is None else question_id}),
            content_type='application/json',
        )

    def assert_question_preserved(self, active):
        self.question.refresh_from_db()
        self.assertEqual(self.question.is_active, active)
        self.assertTrue(WerWeissMehrAnswerOption.objects.filter(pk=self.answer.pk).exists())
        self.assertTrue(self.game.selected_questions.filter(pk=self.question.pk).exists())

    def test_owner_deactivates_question_and_preserves_answers_and_game(self):
        response = self.delete_as(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assert_question_preserved(active=False)

    def test_superuser_can_deactivate_question_created_by_another_host(self):
        response = self.delete_as(self.superuser)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assert_question_preserved(active=False)

    def test_other_staff_cannot_delete_and_receives_json_error(self):
        response = self.delete_as(self.other)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['success'])
        self.assertTrue(response.json()['error'])
        self.assert_question_preserved(active=True)

    def test_missing_question_returns_json_404(self):
        response = self.delete_as(self.superuser, self.question.pk + 1000)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()['success'])
        self.assert_question_preserved(active=True)

    def test_invalid_payloads_return_json_400_without_changing_data(self):
        self.client.force_login(self.owner)
        payloads = [
            '{', '[]', 'null', '{}', '{"question_id": null}',
            '{"question_id": true}', '{"question_id": 1.5}',
            '{"question_id": "invalid"}', '{"question_id": 0}',
            '{"question_id": -1}', '{"question_id": 9223372036854775808}',
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post(self.url, payload, content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()['success'])
                self.assert_question_preserved(active=True)

    def test_repeated_delete_remains_successful(self):
        for _ in range(2):
            response = self.delete_as(self.owner)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['success'])
        self.assert_question_preserved(active=False)

    def test_anonymous_request_still_requires_login(self):
        response = self.client.post(
            self.url, json.dumps({'question_id': self.question.pk}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 302)
        self.assert_question_preserved(active=True)
