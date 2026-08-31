from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Quiz, QuizParticipant, QuizQuestion, QuizSession


User = get_user_model()


class QuickQuizHostLayoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='quick-layout-host',
            email='',
            password='testpass123',
        )
        self.client.force_login(self.user)
        self.question = QuizQuestion.objects.create(
            question_text='What is the capital of Australia?',
            question_type='multiple_choice',
            correct_answer='A',
            option_a='Canberra',
            option_b='Sydney',
            option_c='Melbourne',
            option_d='Perth',
            time_limit=30,
            created_by=self.user,
        )
        self.quiz = Quiz.objects.create(
            title='Quick Quiz',
            creator=self.user,
            status='waiting',
            tutorial_enabled=True,
            tutorial_title='Tutorial',
            tutorial_text='Explanation',
        )
        self.quiz.selected_questions.set([self.question])
        QuizSession.objects.get_or_create(quiz=self.quiz)
        QuizParticipant.objects.create(
            quiz=self.quiz,
            name='Anna',
            total_score=4,
            is_active=True,
        )

    def render_monitor(self):
        return self.client.get(
            reverse('admin_dashboard:quiz_monitor', args=[self.quiz.room_code])
        )

    def test_compact_monitor_keeps_existing_global_and_question_controls(self):
        response = self.render_monitor()
        html = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count('data-quick-quiz-title'), 1)
        self.assertIn('Spieltyp: Quiz', html)
        self.assertIn('class="status-badge status-waiting"', html)
        self.assertIn('id="questionProgress"', html)
        self.assertIn('id="questionTimerWrapper"', html)
        self.assertIn('id="questionTimeLeft"', html)
        self.assertIn('id="participantsList"', html)
        self.assertIn('id="participantCount"', html)
        self.assertIn('data-participant-response-status', html)

        for hook in (
            'startQuizBtn',
            'showTutorialToggle',
            'playTutorialToggle',
            'showLeaderboardBtn',
            'backToHubBtn',
            'availableQuestions',
            'questionBankModal',
        ):
            self.assertIn(f'id="{hook}"', html, hook)
        self.assertIn('copy-lobby-btn-monitor', html)
        self.assertIn('question-time-input', html)
        self.assertIn('send-question-btn', html)
        self.assertIn('FRAGE SENDEN', html)
        self.assertIn('Question Bank', html)

    def test_waiting_layout_has_live_and_actions_grid_before_question_list(self):
        response = self.render_monitor()
        html = response.content.decode('utf-8')

        live_index = html.index('class="card question-panel host-monitor-current"')
        actions_index = html.index('class="card question-actions-card host-monitor-actions"')
        participants_index = html.index('class="quiz-participant-column host-monitor-participants"')
        question_list_index = html.index('class="card quiz-question-list-card host-monitor-question-list"')

        self.assertLess(live_index, actions_index)
        self.assertLess(actions_index, participants_index)
        self.assertLess(participants_index, question_list_index)
        self.assertIn('id="currentQuestionBody"', html)
        self.assertIn('id="questionActionsBody"', html)
        self.assertIn('Noch keine Frage aktiv', html)
        self.assertIn('Fragenliste', html)
        self.assertIn('id="questionSelection"', html)

    def test_participant_panel_shows_status_without_duplicate_quiz_answer(self):
        response = self.render_monitor()
        html = response.content.decode('utf-8')
        participant_panel = html.split('<aside class="quiz-participant-column ', 1)[1].split('</aside>', 1)[0]

        self.assertIn('Teilnehmer (', participant_panel)
        self.assertIn('Anna', participant_panel)
        self.assertIn('Offen', participant_panel)
        self.assertIn('pts', participant_panel)
        self.assertNotIn('Canberra', participant_panel)
        self.assertNotIn('response-answer', participant_panel)
        self.assertLess(html.index('id="liveResponses"'), html.index('<aside class="quiz-participant-column '))

    def test_layout_contains_no_redundant_phase_stats_or_invented_log(self):
        response = self.render_monitor()
        html = response.content.decode('utf-8')

        self.assertNotIn('Schnellaktionen', html)
        self.assertNotIn('Seit 00:', html)
        self.assertNotIn('Durchschnittszeit', html)
        self.assertNotIn('Aktueller Highscore', html)
        self.assertNotIn('Richtig beantwortet', html)
        self.assertNotIn('Spiel-Log', html)
        self.assertNotIn('participantCount', html.split('quiz-status-header', 1)[1].split('</header>', 1)[0])

    def test_active_question_keeps_all_phase_and_review_action_hooks(self):
        source = Path(
            settings.BASE_DIR / 'templates' / 'admin_dashboard' / 'quiz_monitor.html'
        ).read_text(encoding='utf-8')

        self.assertIn('id="questionActionsTitle"', source)
        self.assertIn('id="questionActionsBody"', source)
        self.assertIn('host-monitor-current', source)
        self.assertIn('host-monitor-actions', source)
        self.assertIn('host-monitor-question-list', source)
        for hook in (
            'revealQuestionContentBtn',
            'openAnsweringBtn',
            'endQuestionBtn',
            'returnToQuestionOverviewBtn',
            'liveResponses',
            'responseCount',
            'editScoreModal',
            'editScoreSave',
        ):
            self.assertIn(hook, source)
        self.assertIn('ANTWORTEN ANZEIGEN', source)
        self.assertIn('FRAGE FREIGEBEN', source)
        self.assertIn('promote-correct-btn', source)
        self.assertIn('promote-field-correct-btn', source)
        self.assertIn('renderParticipantResponseStatuses', source)

    def test_layout_css_has_two_column_desktop_and_stacked_narrow_contract(self):
        source = Path(
            settings.BASE_DIR / 'templates' / 'admin_dashboard' / 'quiz_monitor.html'
        ).read_text(encoding='utf-8')

        self.assertIn('grid-template-columns: minmax(0, 1.72fr) minmax(20rem, 1fr);', source)
        self.assertIn('grid-template-rows: minmax(13rem, 1fr) auto;', source)
        self.assertIn('.quiz-question-list-card', source)
        self.assertIn('@media (max-width: 991.98px)', source)
        self.assertIn('grid-template-columns: minmax(0, 1fr);', source)
        self.assertIn('overflow-wrap: anywhere;', source)
        self.assertIn('min-width: 0;', source)
