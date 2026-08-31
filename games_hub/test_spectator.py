import json
import uuid
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from QuizGame.models import Quiz, QuizParticipant, QuizQuestion, QuizSession
from Estimation.models import EstimationQuestion, EstimationQuiz, EstimationSession
from clue_rush.models import Clue, ClueAnswer, ClueQuestion, ClueRushGame, ClueRushParticipant, ClueRushSession
from games_hub.authoritative_state import (
    finish_question_flow,
    observe_snapshot,
    open_answering,
    present_question,
    reset_question_flow,
    reveal_question_content,
)
from games_hub.game_intro import GAME_INTRO_DURATION_MS
from games_hub.models import GameRuntimeState, HubGameStep, HubParticipant, HubSession
from games_hub.spectator import _public_spectator_payload
from sorting_ladder.models import (
    SortingItem,
    SortingLadderGame,
    SortingLadderSession,
    SortingQuestion,
)


class HubSpectatorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='admin', password='pass')
        self.session = HubSession.objects.create(
            code='SPEC1',
            name='Spectator Session',
            is_active=True,
            started_at=timezone.now(),
        )

    def test_spectator_route_renders_without_creating_participant(self):
        response = self.client.get(reverse('games_hub:spectate_session', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'hub/spectate.html')
        self.assertContains(response, 'Spectator View')
        self.assertContains(response, reverse('games_hub:spectate_session_state', args=[self.session.code]))
        self.assertEqual(HubParticipant.objects.count(), 0)
    def test_spectator_uses_vhs_intro_and_contains_no_personal_inputs(self):
        response = self.client.get(reverse('games_hub:spectate_session', args=[self.session.code]))
        content = response.content.decode('utf-8')

        self.assertContains(response, 'themes/vhs/vhs.css')
        self.assertContains(response, 'themes/vhs/spectator.css')
        self.assertContains(response, 'id="unitTutorialNotice"', html=False)
        self.assertContains(response, 'class="vhs-theme-shell spectator-shell"')
        self.assertEqual(content.count('class="qa-vhs-intro__layer '), 5)
        self.assertEqual(content.count('class="qa-vhs-intro__scanline"'), 11)
        for forbidden_tag in ('<input', '<textarea', '<select', '<form', '<button'):
            self.assertNotIn(forbidden_tag, content.lower())
        self.assertNotIn('draggable="true"', content.lower())
        self.assertEqual(HubParticipant.objects.count(), 0)

    def test_every_registered_game_has_a_spectator_renderer(self):
        response = self.client.get(reverse('games_hub:spectate_session', args=[self.session.code]))
        content = response.content.decode('utf-8')

        for game_key, _label in HubGameStep.GAME_CHOICES:
            self.assertIn(f'{game_key}: render', content, game_key)

    def test_spectator_state_waits_without_active_game(self):
        response = self.client.get(reverse('games_hub:spectate_session_state', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['phase'], 'waiting')
        self.assertIsNone(payload['game'])
        self.assertEqual(HubParticipant.objects.count(), 0)

    def test_quick_quiz_spectator_uses_runtime_state_without_participant(self):
        question = QuizQuestion.objects.create(
            created_by=self.user,
            question_text='Was ist 2 + 2?',
            question_type='multiple_choice',
            option_a='4',
            option_b='5',
            correct_answer='A',
        )
        quiz = Quiz.objects.create(
            creator=self.user,
            title='Spectator Quick Quiz',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        quiz_session = QuizSession.objects.create(quiz=quiz)
        quiz_session.send_question(question)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
        )

        response = self.client.get(reverse('games_hub:spectate_session_state', args=[self.session.code]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['phase'], 'question')
        self.assertEqual(payload['game']['game_key'], 'quiz')
        self.assertEqual(payload['game']['question']['text'], 'Was ist 2 + 2?')
        self.assertEqual(
            payload['game']['question']['options'],
            [{'key': 'A', 'text': '4'}, {'key': 'B', 'text': '5'}],
        )
        self.assertNotIn('correct_answer', payload['game']['question'])
        self.assertNotIn('explanation', payload['game']['question'])
        self.assertEqual(HubParticipant.objects.count(), 0)
        self.assertEqual(QuizParticipant.objects.count(), 0)

    def test_true_false_spectator_exposes_both_options_without_solution(self):
        question = QuizQuestion.objects.create(
            created_by=self.user,
            question_text='Ist die Erde rund?',
            question_type='true_false',
            correct_answer='True',
        )
        quiz = Quiz.objects.create(
            creator=self.user,
            title='Spectator True False Quiz',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        QuizSession.objects.create(quiz=quiz).send_question(question)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
        )

        payload = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()

        self.assertEqual(
            payload['game']['question']['options'],
            [
                {'key': 'True', 'text': 'True'},
                {'key': 'False', 'text': 'False'},
            ],
        )
        self.assertNotIn('correct_answer', payload['game']['question'])

    def test_estimation_spectator_waits_without_authoritative_question(self):
        question = EstimationQuestion.objects.create(
            created_by=self.user,
            question_text='Geheime Schaetzfrage',
            correct_answer=5000,
            unit='kilograms',
        )
        quiz = EstimationQuiz.objects.create(
            creator=self.user,
            title='Estimation Waiting',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        EstimationSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='estimation',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        reset_question_flow(
            game_key='estimation',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

        for _reload in range(2):
            game = self.client.get(
                reverse('games_hub:spectate_session_state', args=[self.session.code])
            ).json()['game']

            self.assertEqual(game['phase'], 'game_waiting')
            self.assertIsNone(game['question'])
            self.assertNotIn('Geheime Schaetzfrage', json.dumps(game))
            self.assertNotIn('5000', json.dumps(game))

    def test_estimation_spectator_follows_authoritative_question_lifecycle(self):
        question = EstimationQuestion.objects.create(
            created_by=self.user,
            question_text='Wie schwer ist das Objekt?',
            correct_answer=5000,
            unit='kilograms',
        )
        quiz = EstimationQuiz.objects.create(
            creator=self.user,
            title='Estimation Lifecycle',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        quiz_session = EstimationSession.objects.create(quiz=quiz)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='estimation',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        snapshot = reset_question_flow(
            game_key='estimation',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        presented = present_question(
            game_key='estimation',
            room_code=quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': snapshot.get('game_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            at=timezone.now() - timedelta(seconds=2),
        )
        self.assertTrue(presented.accepted)
        quiz_session.prepare_question(question)

        prompt = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(prompt['phase'], 'question')
        self.assertEqual(prompt['question']['text'], question.question_text)
        self.assertNotIn('correct_answer', prompt['question'])
        self.assertNotIn('zones', prompt['question'])

        opened = open_answering(
            game_key='estimation',
            room_code=quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': presented.snapshot.get('game_id'),
                'state_revision': presented.state_revision,
                'client_action_id': str(uuid.uuid4()),
            },
            answer_duration_seconds=30,
        )
        self.assertTrue(opened.accepted)

        answering = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(answering['phase'], 'question')
        self.assertEqual(answering['question_phase'], 'answering_open')
        self.assertNotIn('correct_answer', answering['question'])
        self.assertNotIn('zones', answering['question'])

        quiz_session.end_current_question()
        finish_question_flow(
            game_key='estimation',
            room_code=quiz.room_code,
            session_code=self.session.code,
            question_id=question.id,
        )
        observe_snapshot(
            'estimation',
            quiz.room_code,
            {
                'type': 'question_ended',
                'phase': 'question_result',
                'revealed': True,
                'current_question_id': question.id,
                'question': {'id': question.id},
            },
            self.session.code,
        )
        result = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(result['phase'], 'reveal')
        self.assertIn('correct_answer', result['question'])
        self.assertIn('zones', result['question'])

        reset_question_flow(
            game_key='estimation',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        waiting = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(waiting['phase'], 'game_waiting')
        self.assertIsNone(waiting['question'])

    def test_sorting_ladder_does_not_expose_correct_ranks_before_resolution(self):
        question = SortingQuestion.objects.create(
            created_by=self.user,
            question_text='Sortiere die Elemente',
            upper_label='Oben',
            lower_label='Unten',
        )
        fixed = SortingItem.objects.create(topic=question, text='Fix', correct_rank=1)
        active = SortingItem.objects.create(topic=question, text='Aktiv', correct_rank=2)
        last = SortingItem.objects.create(topic=question, text='Spaet', correct_rank=3)
        question.starting_item = fixed
        question.save(update_fields=['starting_item'])
        game = SortingLadderGame.objects.create(
            creator=self.user,
            title='Public Sorting',
            status='active',
            started_at=self.session.started_at,
            current_question=question,
        )
        game.selected_questions.set([question])
        game_session = SortingLadderSession.objects.create(
            quiz=game,
            current_round=1,
            is_round_active=True,
            active_element=active,
            shuffled_item_ids=f'{last.id},{fixed.id},{active.id}',
        )
        game_session.placed_elements.add(fixed)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='sorting_ladder',
            room_code=game.room_code,
            title=game.title,
        )

        question_payload = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']['question']

        self.assertNotIn('rank', question_payload['active_element'])
        self.assertNotIn('rank', question_payload['fixed_element'])
        self.assertTrue(all('rank' not in item for item in question_payload['items']))
        self.assertTrue(all('rank' not in item for item in question_payload['placed_elements']))
        self.assertEqual(
            [item['text'] for item in question_payload['items']],
            ['Spaet', 'Aktiv'],
        )

        game_session.reveal_state = SortingLadderSession.REVEAL_REVEALED
        game_session.is_round_active = False
        game_session.save(update_fields=['reveal_state', 'is_round_active'])
        reveal_game = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']

        self.assertEqual(reveal_game['phase'], 'reveal')
        self.assertEqual(
            [item['rank'] for item in reveal_game['question']['final_order']],
            ['1.00', '2.00', '3.00'],
        )

    def test_non_reveal_payload_removes_every_solution_only_field(self):
        solution_fields = {
            'correct_answer': 'secret answer',
            'correct_location': {'latitude': 'secret latitude'},
            'correct_pairs': [{'left': 'secret pair'}],
            'explanation': 'secret explanation',
            'final_order': [{'text': 'secret order'}],
            'liars': ['secret liar'],
            'target': 'secret target',
            'truth_tellers': ['secret truth'],
            'zones': {'secret': 'zone'},
        }
        public = _public_spectator_payload({
            'phase': 'question',
            'question': {'id': 1, 'text': 'Public prompt', **solution_fields},
        })

        self.assertEqual(public['question'], {'id': 1, 'text': 'Public prompt'})
        revealed = _public_spectator_payload({
            'phase': 'reveal',
            'question': {'id': 1, **solution_fields},
        })
        self.assertEqual(
            set(solution_fields),
            set(revealed['question']) - {'id'},
        )

    def test_clue_rush_prompt_phase_is_not_misclassified_as_reveal(self):
        game = ClueRushGame.objects.create(
            creator=self.user,
            title='Protected Clue Rush',
            status='active',
            started_at=self.session.started_at,
        )
        question = ClueQuestion.objects.create(
            created_by=self.user,
            question_text='Gesuchter Begriff',
            answer='Geheime Loesung',
        )
        game.current_question = question
        game.save(update_fields=['current_question'])
        ClueRushSession.objects.create(
            quiz=game,
            total_questions_sent=1,
            current_question_number=1,
            is_question_active=False,
            is_clue_active=False,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='clue_rush',
            room_code=game.room_code,
            title=game.title,
        )
        snapshot = reset_question_flow(
            game_key='clue_rush',
            room_code=game.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        presented = present_question(
            game_key='clue_rush',
            room_code=game.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': snapshot.get('game_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            at=timezone.now() - timedelta(seconds=2),
        )
        self.assertTrue(presented.accepted)

        prompt = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(prompt['phase'], 'question')
        self.assertEqual(prompt['question_phase'], 'prompt_visible')
        self.assertNotIn('correct_answer', prompt['question'])

        opened = open_answering(
            game_key='clue_rush',
            room_code=game.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': presented.snapshot.get('game_id'),
                'state_revision': presented.state_revision,
                'client_action_id': str(uuid.uuid4()),
            },
            answer_duration_seconds=30,
        )
        self.assertTrue(opened.accepted)
        finish_question_flow(
            game_key='clue_rush',
            room_code=game.room_code,
            session_code=self.session.code,
            question_id=question.id,
        )

        revealed = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(revealed['phase'], 'reveal')
        self.assertEqual(revealed['question']['correct_answer'], 'Geheime Loesung')

    def test_manual_quick_quiz_spectator_hides_options_until_content_phase(self):
        question = QuizQuestion.objects.create(
            created_by=self.user,
            question_text='Nur die Frage zuerst?',
            question_type='multiple_choice',
            option_a='Ja',
            option_b='Nein',
            correct_answer='A',
        )
        quiz = Quiz.objects.create(
            creator=self.user,
            title='Manual Spectator Quiz',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        quiz_session = QuizSession.objects.create(quiz=quiz)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        snapshot = reset_question_flow(
            game_key='quiz',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        presented = present_question(
            game_key='quiz',
            room_code=quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': snapshot.get('game_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
        )
        self.assertTrue(presented.accepted)
        quiz_session.present_question(question)

        delayed = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(delayed['question_phase'], 'prompt_visible')
        self.assertIsNone(delayed['question'])

        revealed = reveal_question_content(
            game_key='quiz',
            room_code=quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': presented.snapshot.get('game_id'),
                'state_revision': presented.state_revision,
                'client_action_id': str(uuid.uuid4()),
            },
            at=datetime.fromisoformat(presented.snapshot['question_visible_at']),
        )
        self.assertTrue(revealed.accepted)
        content = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(content['question_phase'], 'content_visible')
        self.assertEqual(len(content['question']['options']), 2)
        self.assertEqual(content['phase'], 'question')
        self.assertNotIn('correct_answer', content['question'])
        self.assertNotIn('explanation', content['question'])

        answering_starts_at = timezone.now() + timedelta(seconds=5)
        opened = open_answering(
            game_key='quiz',
            room_code=quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': revealed.snapshot.get('game_id'),
                'state_revision': revealed.state_revision,
                'client_action_id': str(uuid.uuid4()),
            },
            answer_duration_seconds=30,
            at=answering_starts_at,
        )
        self.assertTrue(opened.accepted)
        answering = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(answering['question_phase'], 'answering_open')
        self.assertEqual(answering['phase'], 'question')
        self.assertFalse(answering['timer']['active'])
        self.assertEqual(
            datetime.fromisoformat(answering['timer']['starts_at']),
            answering_starts_at,
        )
        self.assertNotIn('correct_answer', answering['question'])

        runtime = GameRuntimeState.objects.get(
            session=self.session,
            game_key='quiz',
            room_code=quiz.room_code,
        )
        runtime.starts_at = timezone.now() - timedelta(seconds=1)
        runtime.ends_at = timezone.now() + timedelta(seconds=29)
        runtime.save(update_fields=['starts_at', 'ends_at', 'updated_at'])
        active_answering = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertTrue(active_answering['timer']['active'])

        finish_question_flow(
            game_key='quiz',
            room_code=quiz.room_code,
            session_code=self.session.code,
            question_id=question.id,
        )
        quiz_session.end_current_question()
        resolution = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(resolution['phase'], 'reveal')
        self.assertEqual(resolution['question']['correct_answer'], 'A. Ja')

    def test_spectator_state_exposes_shared_intro_timeline(self):
        quiz = Quiz.objects.create(
            creator=self.user,
            title='Intro Spectator Quiz',
            status='active',
            started_at=self.session.started_at,
        )
        started_at = timezone.now()
        step = HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
            intro_started_at=started_at,
            intro_ends_at=started_at + timedelta(milliseconds=GAME_INTRO_DURATION_MS),
            intro_state_revision=3,
        )

        game = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']

        self.assertEqual(game['game_instance_id'], f'quiz:{quiz.room_code}:{step.pk}')
        self.assertEqual(game['game_number'], 1)
        self.assertEqual(game['intro']['game_title'], 'INTRO SPECTATOR QUIZ')
        self.assertEqual(game['intro']['state_revision'], 3)
        self.assertEqual(game['intro']['intro_started_at'], started_at.isoformat())

    def test_completed_game_scoreboard_contains_only_single_game_points(self):
        quiz = Quiz.objects.create(
            creator=self.user,
            title='Single Game Result',
            status='completed',
            started_at=self.session.started_at,
            ended_at=timezone.now(),
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        for name, score in (('Anna', 9), ('Ben', 4)):
            HubParticipant.objects.create(session=self.session, nickname=name)
            QuizParticipant.objects.create(
                quiz=quiz,
                name=name,
                total_score=score,
                hub_session_code=self.session.code,
            )

        game = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        scoreboard = game['scoreboard']

        self.assertTrue(scoreboard['available'])
        self.assertEqual(
            scoreboard['rows'],
            [
                {'rank': 1, 'participant': 'Anna', 'game_points': 9},
                {'rank': 2, 'participant': 'Ben', 'game_points': 4},
            ],
        )
        self.assertNotIn('overall_points', json.dumps(scoreboard))
        self.assertNotIn('weight', json.dumps(scoreboard))

    def test_consecutive_game_scoreboards_never_mix_game_points(self):
        for name in ('Anna', 'Ben'):
            HubParticipant.objects.create(session=self.session, nickname=name)
        for order, (title, scores) in enumerate((
            ('Spiel A', {'Anna': 9, 'Ben': 3}),
            ('Spiel B', {'Anna': 1, 'Ben': 8}),
        )):
            quiz = Quiz.objects.create(
                creator=self.user,
                title=title,
                status='completed',
                started_at=self.session.started_at,
                ended_at=timezone.now(),
            )
            HubGameStep.objects.create(
                session=self.session,
                order=order,
                game_key='quiz',
                room_code=quiz.room_code,
                title=title,
            )
            for name, score in scores.items():
                QuizParticipant.objects.create(
                    quiz=quiz,
                    name=name,
                    total_score=score,
                    hub_session_code=self.session.code,
                )
        self.session.current_step_index = 0
        self.session.save(update_fields=['current_step_index'])
        scoreboard_a = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']['scoreboard']

        self.session.current_step_index = 1
        self.session.save(update_fields=['current_step_index'])
        scoreboard_b = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']['scoreboard']

        self.assertEqual(scoreboard_a['title'], 'Spiel A')
        self.assertEqual(
            [(row['participant'], row['game_points']) for row in scoreboard_a['rows']],
            [('Anna', 9), ('Ben', 3)],
        )
        self.assertEqual(scoreboard_b['title'], 'Spiel B')
        self.assertEqual(
            [(row['participant'], row['game_points']) for row in scoreboard_b['rows']],
            [('Ben', 8), ('Anna', 1)],
        )

    def test_clue_rush_spectator_shows_answer_lock_clue_without_answer_text(self):
        game = ClueRushGame.objects.create(
            creator=self.user,
            title='Spectator Clue Rush',
            status='active',
            started_at=self.session.started_at,
        )
        question = ClueQuestion.objects.create(
            created_by=self.user,
            question_text='Gesuchter Begriff',
            answer='Secret Answer',
        )
        clue_one = Clue.objects.create(clue_question=question, order=1, clue_text='Erster Hinweis')
        Clue.objects.create(clue_question=question, order=2, clue_text='Zweiter Hinweis')
        clue_three = Clue.objects.create(clue_question=question, order=3, clue_text='Dritter Hinweis')
        game.current_question = question
        game.current_clue = clue_three
        game.save(update_fields=['current_question', 'current_clue'])
        ClueRushSession.objects.create(
            quiz=game,
            total_questions_sent=1,
            current_question_number=1,
            is_question_active=True,
            current_clue_number=3,
            is_clue_active=True,
        )
        participant = ClueRushParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=self.session.code,
            is_active=True,
        )
        ClueAnswer.objects.create(
            quiz=game,
            participant=participant,
            question=question,
            answer_text='Secret Answer',
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='clue_rush',
            room_code=game.room_code,
            title=game.title,
        )

        response = self.client.get(reverse('games_hub:spectate_session_state', args=[self.session.code]))
        response_text = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response_text)
        self.assertEqual(payload['phase'], 'question')
        self.assertEqual(payload['game']['game_key'], 'clue_rush')
        self.assertEqual(
            payload['game']['question']['clues'],
            [
                {'number': 1, 'text': clue_one.clue_text},
                {'number': 2, 'text': 'Zweiter Hinweis'},
                {'number': 3, 'text': 'Dritter Hinweis'},
            ],
        )
        self.assertEqual(payload['game']['question']['answer_locks'], [{'participant': 'Lisa', 'clue_number': 3}])
        self.assertNotIn('Secret Answer', response_text)
        self.assertEqual(HubParticipant.objects.count(), 0)
