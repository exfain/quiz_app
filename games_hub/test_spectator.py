import json
import uuid
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from Assign.models import AssignQuestion, AssignQuiz, AssignSession
from admin_dashboard.models import DashboardSettings
from QuizGame.models import Quiz, QuizParticipant, QuizQuestion, QuizSession
from Estimation.models import EstimationQuestion, EstimationQuiz, EstimationSession
from black_jack_quiz.models import BlackJackQuestion, BlackJackQuiz, BlackJackSession
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
from games_hub.spectator import _public_spectator_payload, build_spectator_state
from sorting_ladder.models import (
    SortingItem,
    SortingLadderGame,
    SortingLadderSession,
    SortingQuestion,
)
from where_is_this.models import (
    WhereAnswer,
    WhereParticipant,
    WhereQuestion,
    WhereQuiz,
    WhereSession,
)
from who_is_lying.models import WhoQuestion, WhoQuiz, WhoSession
from who_is_that.models import WhoThatQuestion, WhoThatQuiz, WhoThatSession


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

    def test_assign_manual_start_does_not_reveal_stale_completed_question(self):
        question = AssignQuestion.objects.create(
            created_by=self.user,
            question_text='Ordne die alten Werte zu',
            left_items=['Alt A', 'Alt B'],
            right_items=['Alt X', 'Alt Y'],
            correct_matches={'0': 0, '1': 1},
            explanation='Geheime alte Aufloesung',
        )
        quiz = AssignQuiz.objects.create(
            creator=self.user,
            title='Fresh Assign Run',
            status='active',
            started_at=self.session.started_at,
            current_question=question,
        )
        quiz.selected_questions.add(question)
        AssignSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='assign',
            room_code=quiz.room_code,
        )
        reset_question_flow(
            game_key='assign',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

        game = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']

        self.assertEqual(game['phase'], 'game_waiting')
        self.assertIsNone(game['question'])

        observe_snapshot(
            'assign',
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

        revealed_game = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']

        self.assertEqual(revealed_game['phase'], 'reveal')
        self.assertEqual(
            revealed_game['question']['correct_pairs'],
            [
                {'left': 'Alt A', 'right': 'Alt X'},
                {'left': 'Alt B', 'right': 'Alt Y'},
            ],
        )
        self.assertEqual(
            revealed_game['question']['explanation'],
            'Geheime alte Aufloesung',
        )

    def test_manual_game_starts_ignore_stale_legacy_reveals(self):
        scenarios = []

        who_question = WhoQuestion.objects.create(
            created_by=self.user,
            statement='Geheime alte Behauptung',
            people=[
                {'name': 'Ada', 'is_lying': True},
                {'name': 'Grace', 'is_lying': False},
            ],
        )
        who_quiz = WhoQuiz.objects.create(
            creator=self.user,
            title='Fresh Who Run',
            status='active',
            started_at=self.session.started_at,
            current_question=who_question,
        )
        who_quiz.selected_questions.add(who_question)
        WhoSession.objects.create(
            quiz=who_quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        scenarios.append(('who', who_quiz, 'Ada'))

        who_that_question = WhoThatQuestion.objects.create(
            created_by=self.user,
            question_text='Geheime alte Person',
            image='',
            correct_answer='Alte Loesung',
        )
        who_that_quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            title='Fresh Who That Run',
            status='active',
            started_at=self.session.started_at,
            current_question=who_that_question,
        )
        who_that_quiz.selected_questions.add(who_that_question)
        WhoThatSession.objects.create(
            quiz=who_that_quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
        )
        scenarios.append(('who_that', who_that_quiz, 'Alte Loesung'))

        blackjack_question = BlackJackQuestion.objects.create(
            created_by=self.user,
            question_text='Geheime alte Zahl',
            correct_answer=73,
        )
        blackjack_quiz = BlackJackQuiz.objects.create(
            creator=self.user,
            title='Fresh Black Jack Run',
            status='active',
            started_at=self.session.started_at,
            current_question=blackjack_question,
            current_question_number=1,
            question_order=[[blackjack_question.id]],
        )
        blackjack_quiz.selected_questions.add(blackjack_question)
        BlackJackSession.objects.create(
            quiz=blackjack_quiz,
            current_question_number=1,
            total_questions_sent=1,
            asked_question_ids=[blackjack_question.id],
            is_question_active=False,
        )
        scenarios.append(('blackjack', blackjack_quiz, '73'))

        clue_question = ClueQuestion.objects.create(
            created_by=self.user,
            question_text='Geheime alte Clue-Frage',
            answer='Geheime Clue-Loesung',
        )
        clue_quiz = ClueRushGame.objects.create(
            creator=self.user,
            title='Fresh Clue Rush Run',
            status='active',
            started_at=self.session.started_at,
            current_question=clue_question,
            question_order=[clue_question.id],
        )
        clue_quiz.selected_questions.add(clue_question)
        ClueRushSession.objects.create(
            quiz=clue_quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=False,
            is_clue_active=False,
        )
        scenarios.append(('clue_rush', clue_quiz, 'Geheime Clue-Loesung'))

        for index, (game_key, game, secret) in enumerate(scenarios):
            with self.subTest(game_key=game_key):
                hub = HubSession.objects.create(
                    code=f'STALE{index}',
                    name=f'Stale {game_key}',
                    is_active=True,
                    started_at=self.session.started_at,
                )
                HubGameStep.objects.create(
                    session=hub,
                    order=0,
                    game_key=game_key,
                    room_code=game.room_code,
                )
                reset_question_flow(
                    game_key=game_key,
                    room_code=game.room_code,
                    session_code=hub.code,
                    mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
                )

                spectator_game = build_spectator_state(hub)['game']

                self.assertEqual(spectator_game['phase'], 'game_waiting')
                self.assertIsNone(spectator_game['question'])
                self.assertNotIn(secret, json.dumps(spectator_game))

        who_hub = HubSession.objects.get(code='STALE0')
        observe_snapshot(
            'who',
            who_quiz.room_code,
            {
                'type': 'question_ended',
                'phase': 'question_result',
                'revealed': True,
                'current_question_id': who_question.id,
                'question': {'id': who_question.id},
            },
            who_hub.code,
        )
        revealed_who = build_spectator_state(who_hub)['game']
        self.assertEqual(revealed_who['phase'], 'reveal')
        self.assertEqual(revealed_who['question']['liars'], ['Ada'])

    def test_fresh_where_session_does_not_reuse_question_from_stale_answer(self):
        question = WhereQuestion.objects.create(
            created_by=self.user,
            question_text='Alter geheimer Ort',
            correct_latitude=50,
            correct_longitude=8,
        )
        quiz = WhereQuiz.objects.create(
            creator=self.user,
            title='Fresh Where Run',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.add(question)
        WhereSession.objects.create(
            quiz=quiz,
            current_question_number=0,
            total_questions_sent=0,
            is_question_active=False,
        )
        participant = WhereParticipant.objects.create(
            quiz=quiz,
            name='Ada',
            hub_session_code=self.session.code,
        )
        WhereAnswer.objects.create(
            quiz=quiz,
            participant=participant,
            question=question,
            user_latitude=49,
            user_longitude=9,
            time_taken=2,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='where',
            room_code=quiz.room_code,
        )

        spectator_game = build_spectator_state(self.session)['game']

        self.assertEqual(spectator_game['phase'], 'game_waiting')
        self.assertIsNone(spectator_game['question'])
        self.assertNotIn(question.question_text, json.dumps(spectator_game))

    def test_who_that_spectator_exposes_image_without_question_text(self):
        question = WhoThatQuestion.objects.create(
            created_by=self.user,
            question_text='Dieser Text gehoert nicht auf den Spectator.',
            image='who_that_images/spectator-person.jpg',
            correct_answer='Ada Lovelace',
            category='Wissenschaft',
        )
        quiz = WhoThatQuiz.objects.create(
            creator=self.user,
            title='Who That Image',
            status='active',
            started_at=self.session.started_at,
            current_question=question,
        )
        quiz.selected_questions.set([question])
        WhoThatSession.objects.create(
            quiz=quiz,
            current_question_number=1,
            total_questions_sent=1,
            is_question_active=True,
        )
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='who_that',
            room_code=quiz.room_code,
            title=quiz.title,
        )

        spectator_game = build_spectator_state(self.session)['game']

        self.assertEqual(spectator_game['question']['category'], 'Wissenschaft')
        self.assertTrue(
            spectator_game['question']['image_url'].endswith(
                '/media/who_that_images/spectator-person.jpg'
            )
        )
        self.assertNotIn('text', spectator_game['question'])
        self.assertNotIn(question.question_text, json.dumps(spectator_game))

    def test_who_spectator_uses_same_randomized_person_order_as_participants(self):
        question = WhoQuestion.objects.create(
            created_by=self.user,
            statement='Wer sagt die Wahrheit?',
            people=[
                {'name': 'Ada', 'is_lying': False},
                {'name': 'Grace', 'is_lying': True},
                {'name': 'Katherine', 'is_lying': False},
                {'name': 'Margaret', 'is_lying': True},
            ],
            time_limit=10,
        )
        original_names = [person['name'] for person in question.people]
        room_code = next(
            code
            for code in (f'WHOORDER{index}' for index in range(20))
            if [
                person['name']
                for person in question.get_randomized_people(room_code=code)['people']
            ] != original_names
        )
        quiz = WhoQuiz.objects.create(
            creator=self.user,
            title='Ordered Who',
            room_code=room_code,
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        quiz_session = WhoSession.objects.create(quiz=quiz)
        quiz_session.prepare_question(question)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='who',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        snapshot = reset_question_flow(
            game_key='who',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        started_at = timezone.now() - timedelta(seconds=11)
        presented = present_question(
            game_key='who',
            room_code=quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': snapshot.get('game_id'),
                'state_revision': snapshot['state_revision'],
                'client_action_id': str(uuid.uuid4()),
            },
            answer_duration_seconds=40,
            at=started_at - timedelta(seconds=1),
        )
        opened = open_answering(
            game_key='who',
            room_code=quiz.room_code,
            session_code=self.session.code,
            action={
                'question_id': question.id,
                'game_id': presented.snapshot.get('game_id'),
                'state_revision': presented.state_revision,
                'client_action_id': str(uuid.uuid4()),
            },
            at=started_at,
        )
        self.assertTrue(opened.accepted)
        quiz_session.open_answering(
            question,
            started_at=started_at,
            answer_duration_seconds=40,
        )

        spectator_game = build_spectator_state(self.session)['game']
        randomized_people = question.get_randomized_people(room_code=room_code)['people']

        self.assertEqual(spectator_game['question']['timer_state']['current_person_index'], 1)
        self.assertEqual(
            spectator_game['question']['current_person'],
            randomized_people[1]['name'],
        )

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

    def test_prepared_quick_quiz_renders_blank_spectator_question_shell(self):
        question = QuizQuestion.objects.create(
            created_by=self.user,
            question_text='Noch nicht oeffentliche Frage',
            question_type='true_false',
            correct_answer='True',
        )
        quiz = Quiz.objects.create(
            creator=self.user,
            title='Prepared Spectator Quiz',
            status='active',
            started_at=self.session.started_at,
        )
        quiz.selected_questions.set([question])
        QuizSession.objects.create(quiz=quiz)
        HubGameStep.objects.create(
            session=self.session,
            order=0,
            game_key='quiz',
            room_code=quiz.room_code,
            title=quiz.title,
        )
        reset_question_flow(
            game_key='quiz',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        runtime = GameRuntimeState.objects.get(
            session=self.session,
            game_key='quiz',
            room_code=quiz.room_code,
        )
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        runtime.public_snapshot = {
            **runtime.public_snapshot,
            'prepared_question_id': str(question.id),
            'question_shell_prepared': True,
        }
        runtime.save(update_fields=[
            'state_revision',
            'context_revision',
            'public_snapshot',
            'updated_at',
        ])

        game = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']

        self.assertEqual(game['phase'], 'question_prepared')
        self.assertEqual(game['message'], 'Frage wird vorbereitet')
        self.assertIsNone(game['question'])
        self.assertFalse(game['timer']['active'])
        self.assertNotIn(question.question_text, json.dumps(game))
        self.assertNotIn('correct_answer', json.dumps(game))

    def test_manual_quick_quiz_spectator_hides_options_until_content_phase(self):
        settings = DashboardSettings.load()
        settings.quiz_mc_answer_reveal_stagger_ms = 520
        settings.save(update_fields=['quiz_mc_answer_reveal_stagger_ms'])
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
        self.assertEqual(delayed['question']['text'], 'Nur die Frage zuerst?')
        self.assertNotIn('options', delayed['question'])
        self.assertNotIn('correct_answer', delayed['question'])

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
        self.assertEqual(content['answer_reveal_stagger_ms'], 520)
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

        reset_question_flow(
            game_key='quiz',
            room_code=quiz.room_code,
            session_code=self.session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
        restarted = self.client.get(
            reverse('games_hub:spectate_session_state', args=[self.session.code])
        ).json()['game']
        self.assertEqual(restarted['phase'], 'game_waiting')
        self.assertIsNone(restarted['question'])
        self.assertNotIn('correct_answer', json.dumps(restarted))

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
