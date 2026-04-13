from django.test import TransactionTestCase
from django.contrib.auth.models import User
from asgiref.sync import async_to_sync
from .models import AssignQuiz, AssignQuestion, AssignParticipant, AssignAnswer
from .consumers import AssignConsumer


class CheckRoundAnswerTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='pass')
        self.quiz = AssignQuiz.objects.create(
            title='Test Quiz',
            creator=self.user,
            room_code='1234'
        )
        # Frage: 3 linke Items, 3 rechte Items, correct_matches = {"0": 2, "1": 0, "2": 1}
        self.question = AssignQuestion.objects.create(
            question_text='Match items',
            points=10,
            time_limit=60,
            left_items=['A', 'B', 'C'],
            right_items=['X', 'Y', 'Z'],
            correct_matches={'0': 2, '1': 0, '2': 1},
            created_by=self.user,
        )
        self.participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess1'
        )
        self.consumer = AssignConsumer()
        self.consumer.room_code = '1234'
        # Nur room_code wird benötigt; channel_name/channel_layer nicht gesetzt (kein WS-Kontext)

    def _get_shuffled_pos_for_original(self, original_idx):
        """Hilfsmethode: ermittelt shuffled Position für einen Original-Index."""
        randomized = self.question.get_randomized_items(room_code='1234')
        for shuffled_pos, orig in randomized['position_to_original'].items():
            if orig == original_idx:
                return shuffled_pos
        return None

    def test_correct_answer_round_0(self):
        """Richtige Zuordnung für Runde 0 ergibt True."""
        # correct für Runde 0 ist original_idx 2 (right_items[2] = 'Z')
        shuffled_pos = self._get_shuffled_pos_for_original(2)
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 0, {'0': shuffled_pos}
        )
        self.assertTrue(result)

    def test_wrong_answer_round_0(self):
        """Falsche Zuordnung für Runde 0 ergibt False."""
        # correct für Runde 0 ist original_idx 2; wir nehmen original_idx 0 (falsch)
        shuffled_pos = self._get_shuffled_pos_for_original(0)
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 0, {'0': shuffled_pos}
        )
        self.assertFalse(result)

    def test_empty_match_is_wrong(self):
        """Leeres user_match (kein Drop) zählt als falsch."""
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 0, {}
        )
        self.assertFalse(result)

    def test_distractor_round_is_always_wrong(self):
        """Eine Runde ohne Eintrag in correct_matches (Distractor) ergibt immer False."""
        # round_index 99 existiert nicht in correct_matches → immer False
        result = async_to_sync(self.consumer.check_round_answer)(
            self.question, 99, {'99': 0}
        )
        self.assertFalse(result)

    def test_save_participant_answer_correct_conversion(self):
        """save_participant_answer konvertiert shuffled→original korrekt und speichert AssignAnswer."""
        # current_question auf dem Quiz setzen (notwendig für save_participant_answer)
        self.quiz.current_question = self.question
        self.quiz.save()

        # Shuffled Matches für alle 3 korrekten Runden bauen
        shuffled_matches = {}
        for left_idx_str, correct_orig in self.question.correct_matches.items():
            shuffled_pos = self._get_shuffled_pos_for_original(correct_orig)
            shuffled_matches[left_idx_str] = shuffled_pos

        result = async_to_sync(self.consumer.save_participant_answer)(
            'Alice', 'sess1', shuffled_matches, 12.0
        )

        self.assertIsNotNone(result, "save_participant_answer sollte ein Ergebnis-Dict zurückgeben")
        self.assertEqual(result['correct_matches'], 3)
        self.assertEqual(result['points_earned'], 30)
        self.assertEqual(result['accuracy'], 100.0)

        # Sicherstellen dass AssignAnswer in der DB gespeichert wurde
        answer = AssignAnswer.objects.get(
            quiz=self.quiz, participant=self.participant, question=self.question
        )
        self.assertEqual(answer.user_matches, {'0': 2, '1': 0, '2': 1})

    def test_assign_answer_score_calculation(self):
        """AssignAnswer berechnet Punkte korrekt: 3 richtige × 10 Punkte = 30."""
        answer = AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            user_matches={'0': 2, '1': 0, '2': 1},  # alle korrekt (original indices)
            time_taken=15.0
        )
        self.assertEqual(answer.points_earned, 30)
        self.assertEqual(answer.get_correct_matches_count(), 3)
        self.assertEqual(answer.get_accuracy_percentage(), 100.0)

    def test_assign_answer_partial_score(self):
        """AssignAnswer berechnet Teilpunkte: 1 richtig × 10 Punkte = 10."""
        answer = AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            user_matches={'0': 2},  # nur Runde 0 korrekt
            time_taken=5.0
        )
        self.assertEqual(answer.points_earned, 10)
        self.assertEqual(answer.get_correct_matches_count(), 1)
        self.assertEqual(answer.get_total_matches_count(), 1)
        self.assertAlmostEqual(answer.get_accuracy_percentage(), 33.3, places=1)
