import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import LiveServerTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from games_hub.active_game_guard import (
    is_game_routable_for_hub_auto_redirect,
    resolve_session_game_activation,
)
from games_hub.check_in import complete_session_check_in, participant_check_in, start_session_check_in
from games_hub.authoritative_state import reset_question_flow
from games_hub.models import GameRuntimeState, HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession
from games_hub.playwright_e2e import install_browser_test_stubs, start_chromium_browser
from games_hub.tutorial_runtime import activate_tutorial_runtime, mark_tutorial_completed
from games_hub.unit_tutorial_runtime import get_unit_tutorial_state
from games_hub.views import get_leaderboard_data
from .consumers import WerWeissMehrConsumer
from .models import (
    WerWeissMehrAnswerOption,
    WerWeissMehrGame,
    WerWeissMehrParticipant,
    WerWeissMehrParticipantState,
    WerWeissMehrPendingInput,
    WerWeissMehrQuestion,
    WerWeissMehrRound,
    WerWeissMehrRoundResponse,
    WerWeissMehrSession,
    normalize_answer_text,
)
from .services import (
    apply_manual_correction,
    build_game_state,
    end_current_round,
    finish_set,
    next_round_or_finish,
    open_prepared_round,
    start_set,
    store_pending_input,
    submit_answer,
)

try:
    from channels.testing import ChannelsLiveServerTestCase as _BrowserTestBase
except ImportError:
    _BrowserTestBase = LiveServerTestCase


class DummyChannelLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group, payload):
        self.sent.append((group, payload))


class WerWeissMehrRoutingTests(TestCase):
    def test_asgi_routes_wer_weiss_mehr_websocket_path(self):
        from games_website.asgi import websocket_urlpatterns

        path = 'ws/wer-weiss-mehr/8600/'

        self.assertTrue(
            any(pattern.pattern.regex.match(path) for pattern in websocket_urlpatterns),
            'games_website.asgi must route /ws/wer-weiss-mehr/<room_code>/ websockets.',
        )


class WerWeissMehrRuntimeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='host')
        self.game = WerWeissMehrGame.objects.create(title='Test', creator=self.user, status='active')
        WerWeissMehrSession.objects.create(quiz=self.game)
        self.question = WerWeissMehrQuestion.objects.create(
            question_text='Wie heissen Bundeslaender?',
            round_time_limit=30,
            created_by=self.user,
        )
        self.bayern = WerWeissMehrAnswerOption.objects.create(
            question=self.question,
            canonical_text='Bayern',
        )
        self.saarland = WerWeissMehrAnswerOption.objects.create(
            question=self.question,
            canonical_text='Saarland',
        )
        self.thueringen = WerWeissMehrAnswerOption.objects.create(
            question=self.question,
            canonical_text='Thüringen',
            aliases=['Thueringen'],
        )
        self.question.recalculate_answer_sort_order()
        self.game.selected_questions.add(self.question)
        self.p1 = WerWeissMehrParticipant.objects.create(quiz=self.game, name='Lisa', hub_session_code='ABC')
        self.p2 = WerWeissMehrParticipant.objects.create(quiz=self.game, name='Max', hub_session_code='ABC')

    def test_vhs_player_chrome_uses_app_name_and_single_participant_location(self):
        response = self.client.get(
            reverse('wer_weiss_mehr:play', args=[self.game.room_code, self.p1.name]),
            {'hub_session': 'ABC'},
        )
        content = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('<title>Test - QuizMaster</title>', content)
        self.assertIn('data-participant-name="Lisa"', content)
        self.assertIn('class="wwm-game-type-label text-muted small"', content)
        self.assertNotIn('<div class="text-muted small">Wer weiß mehr?</div>', content)

        css_path = finders.find('themes/vhs/vhs.css')
        self.assertIsNotNone(css_path)
        css = Path(css_path).read_text(encoding='utf-8')
        self.assertIn(
            ':is(.wwm-game-type-label, .player-pill) {\n'
            '  display: none !important;',
            css,
        )
        self.assertIn(
            'body.wer-weiss-mehr-play-page .vhs-theme-shell\n'
            '  .wwm-header > div:first-child',
            css,
        )

    def test_host_correction_drafts_survive_live_response_renders(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / 'templates'
            / 'admin_dashboard'
            / 'wer_weiss_mehr_monitor.html'
        )
        source = template_path.read_text(encoding='utf-8')

        self.assertIn('const correctionDrafts = new Map();', source)

        self.assertIn(
            'correctionDrafts.set(Number(select.dataset.responseId), Number(select.value));',
            source,
        )
        self.assertIn('correctionDrafts.has(responseId)', source)
        self.assertIn('correctionDrafts.delete(responseId);', source)
        self.assertIn(
            "const nextDraftContext = `${s.question?.id || ''}:${s.current_round || 0}`;",
            source,
        )
        render_responses = source.split('function renderResponses(s) {', 1)[1].split(
            'function renderParticipants(participants) {',
            1,
        )[0]
        self.assertNotIn('responsesBox.innerHTML', render_responses)
        self.assertIn("tbody.querySelectorAll('tr[data-response-id]')", render_responses)
        self.assertIn('!correctionDrafts.has(responseId)', render_responses)
        self.assertIn('tbody.insertBefore(row, currentRow || null);', render_responses)
        self.assertIn("responsesBox.dataset.correctionEventsBound = 'true';", source)

    def test_manual_round_templates_use_automatic_field_reveal_without_board_button(self):
        templates_root = Path(__file__).resolve().parents[1] / 'templates'
        host_source = (
            templates_root / 'admin_dashboard' / 'wer_weiss_mehr_monitor.html'
        ).read_text(encoding='utf-8')
        participant_source = (
            templates_root / 'wer_weiss_mehr' / 'play.html'
        ).read_text(encoding='utf-8')

        self.assertIn('FRAGE FREIGEBEN', host_source)
        self.assertNotIn('ANTWORTTAFEL ANZEIGEN', host_source)
        self.assertNotIn('reveal_question_content', host_source)
        self.assertIn('field_reveal_ready_at', host_source)
        self.assertIn('field_reveal_stagger_ms', participant_source)
        self.assertIn('tile.presentation_index', participant_source)
        self.assertIn("latestState?.question_phase === 'answering_open'", participant_source)

    def test_vhs_answer_grid_uses_column_flow_and_shared_action_button(self):
        response = self.client.get(
            reverse('wer_weiss_mehr:play', args=[self.game.room_code, self.p1.name]),
            {'hub_session': 'ABC'},
        )
        content = response.content.decode('utf-8')
        template_path = Path(__file__).resolve().parents[1] / 'templates' / 'wer_weiss_mehr' / 'play.html'
        template_source = template_path.read_text(encoding='utf-8')
        css_path = finders.find('themes/vhs/vhs.css')
        css = Path(css_path).read_text(encoding='utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="tiles wwm-target-grid" role="list"', content)
        self.assertIn('class="btn btn-primary btn-lg vhs-action-button wwm-submit-button"', content)
        self.assertIn('class="wwm-answer-controls"', content)
        self.assertIn('function updateVhsTileLayout()', template_source)
        self.assertIn('Math.floor((availableHeight + rowGap) / (tileHeight + rowGap))', template_source)
        self.assertIn('Math.ceil(tileCount / maxColumns)', template_source)
        self.assertIn('const needsScroll = contentHeight > availableHeight + 2;', template_source)
        self.assertIn("tiles.dataset.singleColumn = columns === 1 ? 'true' : 'false';", template_source)
        self.assertIn("tiles.dataset.scrollable = needsScroll ? 'true' : 'false';", template_source)
        self.assertIn("tiles.style.removeProperty('--wwm-tile-viewport-height');", template_source)
        self.assertIn('grid-auto-flow: column;', css)
        self.assertIn('--wwm-tile-height: 48px;', css)
        self.assertIn('--wwm-tile-row-gap: 7px;', css)
        self.assertIn('row-gap: var(--wwm-tile-row-gap);', css)
        self.assertIn('column-gap: var(--wwm-tile-column-gap);', css)
        self.assertIn('.wwm-target-grid[data-single-column="true"]', css)
        self.assertIn('width: min(100%, 420px);', css)
        self.assertIn('.wwm-target-grid[data-scrollable="true"]', css)
        self.assertIn('overflow: visible;', css)
        self.assertIn('overflow-y: auto;', css)
        self.assertIn(
            'body.wer-weiss-mehr-play-page .vhs-theme-shell .wwm-answer-area .vhs-action-button',
            css,
        )

    def test_participant_tiles_keep_alphabetical_dom_order(self):
        self.game.start_quiz(hub_session_code='ABC')
        start_set(self.game, self.question.id, hub_session_code='ABC')
        self.game.refresh_from_db()

        state = build_game_state(
            self.game,
            hub_session_code='ABC',
            participant_name=self.p1.name,
        )

        self.assertEqual(
            [tile['id'] for tile in state['question']['tiles']],
            [self.bayern.id, self.saarland.id, self.thueringen.id],
        )

    def test_participant_snapshot_uses_configured_set_number(self):
        second_question = WerWeissMehrQuestion.objects.create(
            question_text='Welche Planeten kennst du?',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(
            question=second_question,
            canonical_text='Erde',
        )
        self.game.selected_questions.add(second_question)
        self.game.question_order = [self.question.id, second_question.id]
        self.game.save(update_fields=['question_order'])
        self.game.start_quiz(hub_session_code='ABC')
        start_set(
            self.game,
            second_question.id,
            hub_session_code='ABC',
        )
        self.game.refresh_from_db()

        state = build_game_state(
            self.game,
            hub_session_code='ABC',
            participant_name=self.p1.name,
        )

        self.assertEqual(state['current_set_number'], 2)

    def test_normalization_accepts_umlauts_and_aliases(self):
        self.assertEqual(normalize_answer_text(' Thüringen '), 'thueringen')
        self.assertEqual(normalize_answer_text('Thueringen'), 'thueringen')
        self.assertEqual(normalize_answer_text('Nordrhein-Westfalen'), 'nordrhein westfalen')
        self.assertEqual(self.question.find_matching_answer('Thueringen'), self.thueringen)

    def test_start_quiz_initializes_runtime_after_guard_activation(self):
        self.game.status = 'active'
        self.game.started_at = None
        self.game.current_question = self.question
        self.game.save(update_fields=['status', 'started_at', 'current_question'])
        session = self.game.session
        session.phase = WerWeissMehrSession.PHASE_REVIEW
        session.current_round = 3
        session.save(update_fields=['phase', 'current_round'])

        self.game.start_quiz()
        self.game.refresh_from_db()
        session.refresh_from_db()

        self.assertEqual(self.game.status, 'active')
        self.assertIsNotNone(self.game.started_at)
        self.assertIsNone(self.game.current_question)
        self.assertEqual(session.phase, WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(session.current_round, 0)

    def test_restart_clears_completed_runtime_for_current_hub(self):
        other_participant = WerWeissMehrParticipant.objects.create(
            quiz=self.game,
            name='Other Hub',
            hub_session_code='OTHER',
            total_score=7,
        )
        self.game.start_quiz(hub_session_code='ABC')
        start_set(self.game, self.question.id, hub_session_code='ABC')
        submit_answer(self.game, self.p1, 'Bayern')
        store_pending_input(self.game, self.p2, 'Saarland')
        other_state = WerWeissMehrParticipantState.objects.create(
            quiz=self.game,
            participant=other_participant,
            question=self.question,
            survived_rounds=2,
        )
        other_response = WerWeissMehrRoundResponse.objects.create(
            quiz=self.game,
            participant=other_participant,
            question=self.question,
            round_number=1,
            answer_text='Bayern',
            matched_answer=self.bayern,
            auto_status=WerWeissMehrRoundResponse.STATUS_CORRECT,
            final_status=WerWeissMehrRoundResponse.STATUS_CORRECT,
            is_correct=True,
        )
        session = self.game.session
        session.completed_question_ids = [self.question.id]
        session.revealed_answers.add(self.bayern)
        session.phase = WerWeissMehrSession.PHASE_SET_COMPLETED
        session.save(update_fields=['completed_question_ids', 'phase'])
        self.p1.total_score = 4
        self.p1.save(update_fields=['total_score'])
        self.game.end_quiz()
        self.game.status = 'waiting'
        self.game.save(update_fields=['status'])

        self.game.start_quiz(hub_session_code='ABC')

        self.game.refresh_from_db()
        session.refresh_from_db()
        self.p1.refresh_from_db()
        other_participant.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertIsNone(self.game.current_question)
        self.assertEqual(session.phase, WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(session.current_round, 0)
        self.assertEqual(session.completed_question_ids, [])
        self.assertFalse(session.revealed_answers.exists())
        self.assertFalse(WerWeissMehrRound.objects.filter(quiz=self.game).exists())
        self.assertFalse(WerWeissMehrParticipantState.objects.filter(quiz=self.game, participant=self.p1).exists())
        self.assertFalse(WerWeissMehrRoundResponse.objects.filter(quiz=self.game, participant=self.p1).exists())
        self.assertFalse(WerWeissMehrPendingInput.objects.filter(quiz=self.game, participant=self.p2).exists())
        self.assertEqual(self.p1.total_score, 0)
        self.assertTrue(WerWeissMehrParticipantState.objects.filter(pk=other_state.pk).exists())
        self.assertTrue(WerWeissMehrRoundResponse.objects.filter(pk=other_response.pk).exists())
        self.assertEqual(other_participant.total_score, 7)

    def test_duplicate_start_does_not_reset_running_instance(self):
        self.game.start_quiz(hub_session_code='ABC')
        start_set(self.game, self.question.id, hub_session_code='ABC')
        session = self.game.session
        started_at = self.game.started_at

        self.game.start_quiz(hub_session_code='ABC')

        self.game.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.game.started_at, started_at)
        self.assertEqual(self.game.current_question_id, self.question.id)
        self.assertEqual(session.phase, WerWeissMehrSession.PHASE_ROUND_ACTIVE)
        self.assertEqual(session.current_round, 1)
        self.assertTrue(WerWeissMehrRound.objects.filter(
            quiz=self.game,
            question=self.question,
            round_number=1,
        ).exists())

    def test_half_started_state_is_not_reported_as_running(self):
        self.game.status = 'active'
        self.game.started_at = None
        self.game.current_question = self.question
        self.game.save(update_fields=['status', 'started_at', 'current_question'])
        session = self.game.session
        session.phase = WerWeissMehrSession.PHASE_REVIEW
        session.current_round = 3
        session.save(update_fields=['phase', 'current_round'])

        state = build_game_state(self.game, hub_session_code='ABC')

        self.assertEqual(state['game_status'], 'waiting')
        self.assertEqual(state['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(state['current_round'], 0)
        self.assertIsNone(state['question'])
        self.assertEqual(len(state['available_questions']), 1)

    def test_duplicate_same_round_hidden_answer_survives_for_all_players(self):
        start_set(self.game, self.question.id, hub_session_code='ABC')
        submit_answer(self.game, self.p1, 'Thüringen')
        submit_answer(self.game, self.p2, 'Thueringen')

        end_current_round(self.game)
        next_round_or_finish(self.game)

        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual(self.p1.total_score, 1)
        self.assertEqual(self.p2.total_score, 1)
        self.assertTrue(self.game.session.revealed_answers.filter(id=self.thueringen.id).exists())

    def test_answer_revealed_before_round_start_eliminates_later_players(self):
        start_set(self.game, self.question.id, hub_session_code='ABC')
        submit_answer(self.game, self.p1, 'Thüringen')
        submit_answer(self.game, self.p2, 'Bayern')
        end_current_round(self.game)
        next_round_or_finish(self.game)

        submit_answer(self.game, self.p1, 'Thüringen')
        end_current_round(self.game)

        response = WerWeissMehrRoundResponse.objects.get(
            quiz=self.game,
            participant=self.p1,
            question=self.question,
            round_number=2,
        )
        self.assertEqual(response.final_status, WerWeissMehrRoundResponse.STATUS_WRONG)
        next_round_or_finish(self.game)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.total_score, 1)

    def test_manual_correction_maps_to_concrete_target_answer(self):
        start_set(self.game, self.question.id, hub_session_code='ABC')
        submit_answer(self.game, self.p1, 'Thüringn')
        end_current_round(self.game)
        response = WerWeissMehrRoundResponse.objects.get(
            quiz=self.game,
            participant=self.p1,
            question=self.question,
            round_number=1,
        )
        self.assertEqual(response.final_status, WerWeissMehrRoundResponse.STATUS_WRONG)

        apply_manual_correction(self.game, response.id, self.thueringen.id)

        response.refresh_from_db()
        self.p1.refresh_from_db()
        self.assertTrue(response.is_correct)
        self.assertTrue(response.is_manual_override)
        self.assertEqual(response.matched_answer_id, self.thueringen.id)
        self.assertEqual(self.p1.total_score, 1)
        state = build_game_state(self.game, hub_session_code='ABC')
        score_by_name = {
            score['participant_name']: score
            for score in state['scorebox'][0]['scores']
        }
        self.assertEqual(score_by_name['Lisa']['points'], 1)
        self.assertEqual(score_by_name['Lisa']['max_points'], 3)

        next_round_or_finish(self.game)

        response.refresh_from_db()
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.total_score, 1)
        self.assertTrue(self.game.session.revealed_answers.filter(id=self.thueringen.id).exists())

    def test_manual_correction_during_active_round_updates_live_score_and_survival(self):
        self.game.start_quiz()
        start_set(self.game, self.question.id, hub_session_code='ABC')
        submit_answer(self.game, self.p1, 'Thuringn')
        response = WerWeissMehrRoundResponse.objects.get(
            quiz=self.game,
            participant=self.p1,
            question=self.question,
            round_number=1,
        )
        self.assertEqual(response.final_status, WerWeissMehrRoundResponse.STATUS_WRONG)
        self.assertEqual(self.game.session.phase, WerWeissMehrSession.PHASE_ROUND_ACTIVE)

        apply_manual_correction(self.game, response.id, self.thueringen.id)

        response.refresh_from_db()
        self.p1.refresh_from_db()
        state = build_game_state(self.game, hub_session_code='ABC')
        lisa_response = next(item for item in state['responses'] if item['participant_name'] == 'Lisa')
        lisa_score = next(score for score in state['scorebox'][0]['scores'] if score['participant_name'] == 'Lisa')
        self.assertEqual(response.final_status, WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED)
        self.assertTrue(response.is_correct)
        self.assertTrue(response.is_manual_override)
        self.assertEqual(response.matched_answer_id, self.thueringen.id)
        self.assertEqual(self.p1.total_score, 1)
        self.assertEqual(lisa_response['final_status'], WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED)
        self.assertEqual(lisa_response['auto_status'], WerWeissMehrRoundResponse.STATUS_WRONG)
        self.assertEqual(lisa_score['points'], 1)
        self.assertTrue(any(
            tile['id'] == self.thueringen.id and tile['revealed']
            for tile in state['question']['tiles']
        ))

        end_current_round(self.game)
        response.refresh_from_db()
        self.p1.refresh_from_db()
        self.assertEqual(response.matched_answer_id, self.thueringen.id)
        self.assertEqual(response.final_status, WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED)
        self.assertEqual(self.p1.total_score, 1)

    def test_manual_correction_rejects_response_from_another_hub_session(self):
        other_participant = WerWeissMehrParticipant.objects.create(
            quiz=self.game,
            name='Other Hub',
            hub_session_code='OTHER',
        )
        self.game.start_quiz(hub_session_code='ABC')
        start_set(self.game, self.question.id, hub_session_code='ABC')
        response = WerWeissMehrRoundResponse.objects.create(
            quiz=self.game,
            participant=other_participant,
            question=self.question,
            round_number=1,
            answer_text='Bayernn',
        )

        with self.assertRaisesMessage(ValueError, 'aktuellen Hub-Session'):
            apply_manual_correction(
                self.game,
                response.id,
                self.bayern.id,
                hub_session_code='ABC',
            )

        response.refresh_from_db()
        self.assertFalse(response.is_correct)
        self.assertFalse(response.is_manual_override)

    def test_manual_correction_rejects_answer_revealed_before_current_round(self):
        start_set(self.game, self.question.id, hub_session_code='ABC')
        submit_answer(self.game, self.p1, 'Bayern')
        submit_answer(self.game, self.p2, 'Saarland')
        end_current_round(self.game)
        next_round_or_finish(self.game)

        submit_answer(self.game, self.p1, 'Bayernn')
        end_current_round(self.game)
        response = WerWeissMehrRoundResponse.objects.get(
            quiz=self.game,
            participant=self.p1,
            question=self.question,
            round_number=2,
        )

        with self.assertRaisesMessage(ValueError, 'bereits vor Beginn dieser Runde'):
            apply_manual_correction(self.game, response.id, self.bayern.id)

        response.refresh_from_db()
        self.assertFalse(response.is_correct)
        self.assertEqual(response.final_status, WerWeissMehrRoundResponse.STATUS_WRONG)

    def test_inactive_game_rejects_pending_and_submit(self):
        start_set(self.game, self.question.id, hub_session_code='ABC')
        self.game.status = 'inactive'
        self.game.save(update_fields=['status'])

        pending = store_pending_input(self.game, self.p1, 'Bayern')

        self.assertIsNone(pending)
        self.assertFalse(WerWeissMehrPendingInput.objects.filter(quiz=self.game, participant=self.p1).exists())
        with self.assertRaisesMessage(ValueError, 'Das Spiel ist nicht aktiv.'):
            submit_answer(self.game, self.p1, 'Bayern')
        self.assertFalse(WerWeissMehrRoundResponse.objects.filter(quiz=self.game, participant=self.p1).exists())

        payload = build_game_state(self.game, hub_session_code='ABC', participant_name='Lisa')
        self.assertEqual(payload['game_status'], 'inactive')
        self.assertFalse(payload['participant_state']['can_answer'])


class WerWeissMehrConsumerTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='host')
        self.game = WerWeissMehrGame.objects.create(title='Live Start', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=self.game)
        self.hub_session = HubSession.objects.create(code='WWMSTART', name='WWM Start')
        HubGameStep.objects.create(
            session=self.hub_session,
            order=1,
            game_key='wer_weiss_mehr',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        HubParticipant.objects.create(session=self.hub_session, nickname='Lisa')
        start_session_check_in(self.hub_session)
        participant_check_in(self.hub_session, 'Lisa')
        complete_session_check_in(self.hub_session)

    def test_admin_start_quiz_sends_current_server_state_to_host(self):
        consumer = WerWeissMehrConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'werweissmehr_{self.game.room_code}'
        consumer.channel_layer = DummyChannelLayer()
        consumer.send = AsyncMock()

        async_to_sync(consumer.handle_admin_start_quiz)({})

        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'active')
        self.assertIsNotNone(self.game.started_at)

        payloads = [
            json.loads(call.kwargs['text_data'])
            for call in consumer.send.await_args_list
        ]
        state_payload = next(payload for payload in payloads if payload.get('type') == 'state')
        self.assertEqual(state_payload['game_status'], 'active')
        self.assertEqual(state_payload['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertTrue(
            any(payload.get('type') == 'quiz_started' for _, payload in consumer.channel_layer.sent)
        )
        self.assertTrue(
            any(
                group == f'hub_{self.hub_session.code}'
                and payload.get('type') == 'navigate'
                and payload.get('step', {}).get('game_key') == 'wer_weiss_mehr'
                and payload.get('step', {}).get('room_code') == self.game.room_code
                for group, payload in consumer.channel_layer.sent
            )
        )

    def test_admin_start_quiz_routes_to_explicit_hub_session(self):
        stale_session = HubSession.objects.create(code='STALEWWM', name='Stale WWM')
        HubGameStep.objects.create(
            session=stale_session,
            order=1,
            game_key='wer_weiss_mehr',
            room_code=self.game.room_code,
            title='Stale step',
        )
        consumer = WerWeissMehrConsumer()
        consumer.room_code = self.game.room_code
        consumer.room_group_name = f'werweissmehr_{self.game.room_code}'
        consumer.channel_layer = DummyChannelLayer()
        consumer.send = AsyncMock()

        async_to_sync(consumer.handle_admin_start_quiz)({'hub_session_code': self.hub_session.code})

        navigate_groups = [
            group for group, payload in consumer.channel_layer.sent
            if payload.get('type') == 'navigate'
            and payload.get('step', {}).get('game_key') == 'wer_weiss_mehr'
        ]
        self.assertIn(f'hub_{self.hub_session.code}', navigate_groups)
        self.assertNotIn(f'hub_{stale_session.code}', navigate_groups)


class WerWeissMehrAdminIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='admin', password='testpass123', email='')
        self.client.force_login(self.user)

    def _complete_check_in(self, session, nickname='Lisa'):
        HubParticipant.objects.create(session=session, nickname=nickname)
        start_session_check_in(session)
        participant_check_in(session, nickname)
        complete_session_check_in(session)

    def _link_game_to_session(self, game, session):
        HubGameStep.objects.get_or_create(
            session=session,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            defaults={
                'order': session.steps.count(),
                'title': game.title,
            },
        )

    def _action_context(self, game, session_code, participant_name=None):
        state = build_game_state(
            game,
            hub_session_code=session_code,
            participant_name=participant_name,
        )
        return {
            'client_action_id': str(uuid.uuid4()),
            'state_revision': state['state_revision'],
            'game_id': state['game_id'],
            'question_id': state['current_question_id'],
            'round_id': state['current_round_id'],
            'set_id': state.get('current_set_id'),
        }

    def _enable_manual_question_flow(self, game, session_code):
        return reset_question_flow(
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            session_code=session_code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

    def _open_presented_round(self, game, session_code):
        game.refresh_from_db()
        state = build_game_state(game, hub_session_code=session_code)
        ready_at = parse_datetime(state['field_reveal_ready_at'])
        presented_at = parse_datetime(state['question_presented_at'])
        self.assertIsNotNone(ready_at)
        self.assertIsNotNone(presented_at)
        runtime = GameRuntimeState.objects.get(
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            session__code=session_code,
        )
        runtime.question_presented_at = timezone.now() - (ready_at - presented_at) - timedelta(milliseconds=10)
        runtime.save(update_fields=['question_presented_at', 'updated_at'])
        action_context = self._action_context(game, session_code)
        self.assertEqual(str(action_context['set_id']), str(game.current_question_id))
        response = self.client.post(
            reverse('admin_dashboard:open_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session_code,
                **action_context,
            }),
            content_type='application/json',
        )
        if response.status_code != 200:
            self.fail(response.json())
        return response

    def _create_tutorial_set_game(self, session_code='WWMTSET'):
        game = WerWeissMehrGame.objects.create(
            title='Tutorial Flow',
            creator=self.user,
            status='waiting',
            tutorial_enabled=True,
        )
        WerWeissMehrSession.objects.create(quiz=game)
        tutorial = WerWeissMehrQuestion.objects.create(
            question_text='Tutorialset',
            round_time_limit=15,
            created_by=self.user,
        )
        normal = WerWeissMehrQuestion.objects.create(
            question_text='Normales Set',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=tutorial, canonical_text='Probe')
        WerWeissMehrAnswerOption.objects.create(question=normal, canonical_text='Bayern')
        tutorial.recalculate_answer_sort_order()
        normal.recalculate_answer_sort_order()
        game.tutorial_question = tutorial
        game.selected_questions.add(normal)
        game.question_order = [normal.id]
        game.save(update_fields=['tutorial_question', 'question_order'])
        session = HubSession.objects.create(code=session_code, name='Tutorial Flow')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )
        self._complete_check_in(session)
        return game, session, tutorial, normal

    def _start_game_with_tutorial_set(self, game, session):
        response = self.client.post(
            reverse('wer_weiss_mehr:start_game', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code, 'play_tutorial': True}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_create_custom_game_can_create_and_select_inline_set(self):
        response = self.client.post(
            reverse('admin_dashboard:create_wer_weiss_mehr_custom_game'),
            data=json.dumps({
                'title': 'Bundesländer',
                'question_text': 'Wie heißen die Bundesländer von Deutschland?',
                'round_time_limit': 20,
                'answers': [
                    {'canonical_text': 'Thüringen', 'aliases': ['Thueringen']},
                    {'canonical_text': 'Bayern', 'aliases': []},
                ],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])

        game = WerWeissMehrGame.objects.get(id=payload['quiz_id'])
        self.assertEqual(game.selected_questions.count(), 1)
        question = game.selected_questions.get()
        self.assertEqual(question.answers.count(), 2)

        state = build_game_state(game)
        self.assertEqual(len(state['available_questions']), 1)
        self.assertEqual(state['available_questions'][0]['answer_count'], 2)
        self.assertNotIn('answers_preview', state['available_questions'][0])

    def test_hub_activation_initializes_wwm_runtime(self):
        game = WerWeissMehrGame.objects.create(title='Guard Start', creator=self.user, status='active')
        WerWeissMehrSession.objects.create(quiz=game)
        session = HubSession.objects.create(code='WWMGUARD', name='Guard Start')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )

        result = resolve_session_game_activation(session.code, 'wer_weiss_mehr', game.room_code)

        self.assertTrue(result['success'])
        game.refresh_from_db()
        self.assertEqual(game.status, 'active')
        self.assertIsNotNone(game.started_at)
        self.assertTrue(is_game_routable_for_hub_auto_redirect(game))

    def test_start_endpoint_initializes_runtime_and_returns_state(self):
        game = WerWeissMehrGame.objects.create(title='HTTP Start', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        game.selected_questions.add(question)
        game.question_order = [question.id]
        game.save(update_fields=['question_order'])
        session = HubSession.objects.create(code='WWMHTTP', name='HTTP Start')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )
        HubParticipant.objects.create(session=session, nickname='Lisa')
        HubParticipant.objects.create(session=session, nickname='Ben')
        start_session_check_in(session)
        participant_check_in(session, 'Lisa')
        participant_check_in(session, 'Ben')
        complete_session_check_in(session)

        response = self.client.post(
            f'/wer-weiss-mehr/start/{game.room_code}/',
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['game_status'], 'active')
        self.assertEqual(len(payload['available_questions']), 1)
        game.refresh_from_db()
        self.assertEqual(game.status, 'active')
        self.assertIsNotNone(game.started_at)
        self.assertTrue(is_game_routable_for_hub_auto_redirect(game))

    def test_start_set_endpoint_initializes_first_round_and_returns_state(self):
        game = WerWeissMehrGame.objects.create(title='Set Start', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        game.question_order = [question.id]
        game.save(update_fields=['question_order'])
        session = HubSession.objects.create(code='WWMSET', name='Set Start')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )
        WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        self._enable_manual_question_flow(game, session.code)

        response = self.client.post(
            f'/wer-weiss-mehr/start-set/{game.room_code}/',
            data=json.dumps({
                'hub_session': session.code,
                'question_id': question.id,
                'time_limit_seconds': 30,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(payload['question_phase'], GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE)
        self.assertEqual(payload['current_round'], 1)
        self.assertIsNone(payload['answering_deadline_at'])
        self.assertIsNone(payload['timer']['ends_at'])
        self.assertEqual(payload['question']['id'], question.id)
        self.assertEqual(payload['question']['answer_count'], 2)
        self.assertEqual([tile['text'] for tile in payload['question']['tiles']], ['Bayern', 'Saarland'])
        self.assertEqual([tile['presentation_index'] for tile in payload['question']['tiles']], [0, 1])
        self.assertFalse(any(tile['revealed'] for tile in payload['question']['tiles']))
        self.assertEqual(
            sorted(answer['text'] for answer in payload['target_answers']),
            ['Bayern', 'Saarland'],
        )

        runtime_session = game.session
        runtime_session.refresh_from_db()
        game.refresh_from_db()
        self.assertEqual(game.current_question_id, question.id)
        self.assertEqual(runtime_session.phase, WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(runtime_session.current_round, 1)
        self.assertIsNone(runtime_session.round_end_time)

        participant_payload = build_game_state(
            game,
            hub_session_code=session.code,
            participant_name='Lisa',
        )
        participant_state = participant_payload['participant_state']
        self.assertFalse(participant_state['can_answer'])
        self.assertEqual(participant_payload['available_questions'], [])
        self.assertEqual(participant_payload['target_answers'], [])
        self.assertEqual(participant_payload['responses'], [])
        self.assertTrue(all(tile['text'] == '' for tile in participant_payload['question']['tiles']))

        early_submit = self.client.post(
            reverse('wer_weiss_mehr:participant_submit_answer', args=[game.room_code]),
            data=json.dumps({
                'participant_name': 'Lisa',
                'hub_session': session.code,
                'answer_text': 'Bayern',
                **self._action_context(game, session.code, 'Lisa'),
            }),
            content_type='application/json',
        )
        self.assertEqual(early_submit.status_code, 409)
        self.assertEqual(early_submit.json()['code'], 'invalid_phase')
        self.assertFalse(WerWeissMehrRoundResponse.objects.exists())

        early_state = build_game_state(game, hub_session_code=session.code)
        early_decision = open_prepared_round(
            game,
            hub_session_code=session.code,
            action={
                'client_action_id': str(uuid.uuid4()),
                'state_revision': early_state['state_revision'],
                'game_id': early_state['game_id'],
                'question_id': early_state['current_question_id'],
                'round_id': early_state['current_round_id'],
                'set_id': early_state['current_set_id'],
            },
            at=parse_datetime(early_state['question_presented_at']),
        )
        self.assertFalse(early_decision.accepted)
        self.assertEqual(early_decision.code, 'content_reveal_incomplete')

        open_response = self._open_presented_round(game, session.code)
        self.assertEqual(open_response.status_code, 200)
        open_payload = open_response.json()
        self.assertEqual(open_payload['phase'], WerWeissMehrSession.PHASE_ROUND_ACTIVE)
        self.assertEqual(open_payload['question_phase'], GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN)
        self.assertIsNotNone(open_payload['answering_deadline_at'])
        open_participant_state = build_game_state(
            game,
            hub_session_code=session.code,
            participant_name='Lisa',
        )
        self.assertTrue(open_participant_state['participant_state']['can_answer'])
        runtime_session.refresh_from_db()
        self.assertIsNotNone(runtime_session.round_end_time)

        first_deadline = runtime_session.round_end_time
        duplicate_open = self.client.post(
            reverse('admin_dashboard:open_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                **self._action_context(game, session.code),
            }),
            content_type='application/json',
        )
        self.assertEqual(duplicate_open.status_code, 200)
        runtime_session.refresh_from_db()
        self.assertEqual(runtime_session.round_end_time, first_deadline)

        accepted_submit = self.client.post(
            reverse('wer_weiss_mehr:participant_submit_answer', args=[game.room_code]),
            data=json.dumps({
                'participant_name': 'Lisa',
                'hub_session': session.code,
                'answer_text': 'Bayern',
                **self._action_context(game, session.code, 'Lisa'),
            }),
            content_type='application/json',
        )
        self.assertEqual(accepted_submit.status_code, 200)
        self.assertEqual(WerWeissMehrRoundResponse.objects.count(), 1)

    def test_tutorial_set_is_visible_and_regular_sets_locked_until_completed(self):
        game, session, tutorial, normal = self._create_tutorial_set_game('WWMTVIS')

        payload = self._start_game_with_tutorial_set(game, session)

        self.assertTrue(payload['success'])
        self.assertEqual(payload['game_status'], 'active')
        self.assertEqual([item['id'] for item in payload['available_questions']], [tutorial.id, normal.id])
        tutorial_row = payload['available_questions'][0]
        normal_row = payload['available_questions'][1]
        self.assertTrue(tutorial_row['is_tutorial_set'])
        self.assertEqual(tutorial_row['status'], 'tutorial_pending')
        self.assertFalse(tutorial_row['is_start_disabled'])
        self.assertFalse(normal_row['is_tutorial_set'])
        self.assertEqual(normal_row['status'], 'locked_until_tutorial')
        self.assertTrue(normal_row['is_start_disabled'])
        self.assertIn('Tutorialset', normal_row['disabled_reason'])
        self.assertEqual([row['question_id'] for row in payload['scorebox']], [normal.id])
        self.assertEqual(payload['scorebox'][0]['label'], '#1')
        self.assertNotIn('question_text', payload['scorebox'][0])

    def test_monitor_renders_tutorial_set_host_controls(self):
        game, _, _, _ = self._create_tutorial_set_game('WWMTTPL')

        response = self.client.get(reverse('admin_dashboard:wer_weiss_mehr_monitor', args=[game.room_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tutorialset – keine Wertung')
        self.assertContains(response, 'Tutorialset starten')
        self.assertContains(response, 'Tutorialset überspringen')
        self.assertContains(response, 'SKIP_TUTORIAL_SET_URL')

    def test_regular_set_start_does_not_trigger_pending_tutorial_set(self):
        game, session, tutorial, normal = self._create_tutorial_set_game('WWMTBLOCK')
        self._start_game_with_tutorial_set(game, session)

        response = self.client.post(
            reverse('wer_weiss_mehr:start_game_set', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'question_id': normal.id,
                'time_limit_seconds': 30,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Tutorialset', response.json()['error'])
        game.refresh_from_db()
        self.assertIsNone(game.current_question_id)
        tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', game.room_code, session.code)
        self.assertFalse(tutorial_state['current_unit_is_tutorial'])
        self.assertFalse(tutorial_state['tutorial_has_been_played'])
        self.assertEqual(tutorial_state['tutorial_question_id'], tutorial.id)

    def test_explicit_tutorial_set_start_keeps_tutorial_unscored(self):
        game, session, tutorial, normal = self._create_tutorial_set_game('WWMTSTART')
        self._start_game_with_tutorial_set(game, session)

        response = self.client.post(
            reverse('wer_weiss_mehr:start_game_set', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'question_id': tutorial.id,
                'time_limit_seconds': 15,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['question']['id'], tutorial.id)
        self.assertTrue(payload['question']['is_tutorial_round'])
        self.assertEqual([row['question_id'] for row in payload['scorebox']], [normal.id])
        tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', game.room_code, session.code)
        self.assertTrue(tutorial_state['current_unit_is_tutorial'])
        self.assertFalse(tutorial_state['tutorial_has_been_played'])

    def test_tutorial_set_host_correction_is_allowed_but_unscored(self):
        game, session, tutorial, normal = self._create_tutorial_set_game('WWMTCORR')
        self._start_game_with_tutorial_set(game, session)
        participant = WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        self.client.post(
            reverse('wer_weiss_mehr:start_game_set', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'question_id': tutorial.id,
                'time_limit_seconds': 15,
            }),
            content_type='application/json',
        )
        open_response = self._open_presented_round(game, session.code)
        self.assertEqual(open_response.status_code, 200)
        submit_answer(game, participant, 'falsch')
        response = WerWeissMehrRoundResponse.objects.get(
            quiz=game,
            participant=participant,
            question=tutorial,
            round_number=1,
        )
        target = tutorial.answers.get()

        correction_response = self.client.post(
            reverse('admin_dashboard:apply_wer_weiss_mehr_correction', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'response_id': response.id,
                'target_answer_id': target.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(correction_response.status_code, 200)
        payload = correction_response.json()
        self.assertTrue(payload['success'])
        corrected = WerWeissMehrRoundResponse.objects.get(id=response.id)
        participant.refresh_from_db()
        self.assertTrue(corrected.is_correct)
        self.assertTrue(corrected.is_manual_override)
        self.assertEqual(corrected.matched_answer_id, target.id)
        self.assertEqual(participant.total_score, 0)
        self.assertEqual([row['question_id'] for row in payload['scorebox']], [normal.id])
        self.assertIsNone(payload['scorebox'][0]['scores'][0]['points'])

    def test_skip_tutorial_set_unlocks_regular_sets_without_scoring_tutorial(self):
        game, session, tutorial, normal = self._create_tutorial_set_game('WWMTSKIP')
        self._start_game_with_tutorial_set(game, session)

        response = self.client.post(
            reverse('wer_weiss_mehr:skip_tutorial_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        rows = {item['id']: item for item in payload['available_questions']}
        self.assertEqual(rows[tutorial.id]['status'], 'tutorial_completed')
        self.assertTrue(rows[tutorial.id]['is_start_disabled'])
        self.assertEqual(rows[normal.id]['status'], 'available')
        self.assertFalse(rows[normal.id]['is_start_disabled'])
        self.assertEqual([row['question_id'] for row in payload['scorebox']], [normal.id])
        tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', game.room_code, session.code)
        self.assertFalse(tutorial_state['current_unit_is_tutorial'])
        self.assertTrue(tutorial_state['tutorial_has_been_played'])

    def test_start_set_endpoint_warns_when_tutorial_acknowledgements_are_open(self):
        game = WerWeissMehrGame.objects.create(
            title='Set Start Tutorial',
            creator=self.user,
            status='waiting',
            tutorial_enabled=True,
            tutorial_title='Intro',
            tutorial_text='Bitte lesen.',
        )
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        game.question_order = [question.id]
        game.save(update_fields=['question_order'])
        session = HubSession.objects.create(code='WWMTUT', name='Set Tutorial')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )
        HubParticipant.objects.create(session=session, nickname='Lisa')
        HubParticipant.objects.create(session=session, nickname='Ben')
        self.assertTrue(start_session_check_in(session)['success'])
        self.assertTrue(participant_check_in(session, 'Lisa')['success'])
        self.assertTrue(participant_check_in(session, 'Ben')['success'])
        self.assertTrue(complete_session_check_in(session)['success'])
        WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Ben',
            hub_session_code=session.code,
        )
        game.start_quiz()
        activate_tutorial_runtime('wer_weiss_mehr', game.room_code, session.code, game, True)
        mark_tutorial_completed('wer_weiss_mehr', game.room_code, session.code, 'Lisa')

        response = self.client.post(
            f'/wer-weiss-mehr/start-set/{game.room_code}/',
            data=json.dumps({
                'hub_session': session.code,
                'question_id': question.id,
                'time_limit_seconds': 30,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['type'], 'tutorial_ack_warning')
        self.assertEqual(payload['message'], 'Nicht alle Teilnehmer haben die Erläuterung bestätigt')
        self.assertEqual(payload['completed'], 1)
        self.assertEqual(payload['total'], 2)
        runtime_session = game.session
        runtime_session.refresh_from_db()
        self.assertEqual(runtime_session.phase, WerWeissMehrSession.PHASE_IDLE)

    def test_end_round_endpoint_evaluates_submitted_pending_and_empty_answers(self):
        game = WerWeissMehrGame.objects.create(title='End Round', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMROUND', name='End Round')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )
        p1 = WerWeissMehrParticipant.objects.create(quiz=game, name='Lisa', hub_session_code=session.code)
        p2 = WerWeissMehrParticipant.objects.create(quiz=game, name='Max', hub_session_code=session.code)
        WerWeissMehrParticipant.objects.create(quiz=game, name='Tom', hub_session_code=session.code)
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)
        submit_answer(game, p1, 'Bayern')
        store_pending_input(game, p2, 'Saarland')

        response = self.client.post(
            reverse('admin_dashboard:end_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_REVIEW)
        self.assertEqual(payload['current_round'], 1)
        self.assertFalse(payload['can_start_next_round'])
        status_by_name = {item['participant_name']: item['final_status'] for item in payload['responses']}
        self.assertEqual(status_by_name['Lisa'], WerWeissMehrRoundResponse.STATUS_CORRECT)
        self.assertEqual(status_by_name['Max'], WerWeissMehrRoundResponse.STATUS_CORRECT)
        self.assertEqual(status_by_name['Tom'], WerWeissMehrRoundResponse.STATUS_WRONG)
        scores = {
            score['participant_name']: score
            for score in payload['scorebox'][0]['scores']
        }
        self.assertEqual(payload['scorebox'][0]['max_points'], 2)
        self.assertEqual(scores['Lisa']['points'], 1)
        self.assertEqual(scores['Lisa']['max_points'], 2)
        self.assertEqual(scores['Max']['points'], 1)
        self.assertEqual(scores['Tom']['points'], 0)

        runtime_session = game.session
        runtime_session.refresh_from_db()
        self.assertEqual(runtime_session.phase, WerWeissMehrSession.PHASE_REVIEW)
        self.assertIsNone(runtime_session.round_start_time)
        self.assertIsNone(runtime_session.round_end_time)

    def test_apply_correction_endpoint_maps_wrong_answer_to_target(self):
        game = WerWeissMehrGame.objects.create(title='Manual Correction', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        bayern = WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        thueringen = WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Thueringen')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMCORR', name='Correction')
        self._link_game_to_session(game, session)
        participant = WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)
        submit_answer(game, participant, 'Thuringn')
        end_current_round(game)

        round_response = WerWeissMehrRoundResponse.objects.get(
            quiz=game,
            participant=participant,
            question=question,
            round_number=1,
        )
        self.assertEqual(round_response.final_status, WerWeissMehrRoundResponse.STATUS_WRONG)

        response = self.client.post(
            reverse('admin_dashboard:apply_wer_weiss_mehr_correction', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'response_id': round_response.id,
                'target_answer_id': thueringen.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        response_by_name = {item['participant_name']: item for item in payload['responses']}
        lisa_response = response_by_name['Lisa']
        self.assertEqual(lisa_response['final_status'], WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED)
        self.assertTrue(lisa_response['is_correct'])
        self.assertEqual(lisa_response['matched_answer_id'], thueringen.id)
        self.assertNotIn('unclear', {item['final_status'] for item in payload['responses']})

        tile_by_id = {tile['id']: tile for tile in payload['question']['tiles']}
        self.assertFalse(tile_by_id[bayern.id]['revealed'])
        self.assertTrue(tile_by_id[thueringen.id]['revealed'])

        round_response.refresh_from_db()
        self.assertTrue(round_response.is_correct)
        self.assertTrue(round_response.is_manual_override)
        self.assertEqual(round_response.matched_answer_id, thueringen.id)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, 1)
        score = payload['scorebox'][0]['scores'][0]
        self.assertEqual(score['points'], 1)
        self.assertEqual(score['max_points'], 2)

        participant_payload = build_game_state(
            game,
            hub_session_code=session.code,
            participant_name='Lisa',
        )
        participant_tile_by_id = {tile['id']: tile for tile in participant_payload['question']['tiles']}
        self.assertEqual(participant_tile_by_id[thueringen.id]['text'], 'Thueringen')
        self.assertEqual(participant_payload['participant_state']['response']['final_status'], WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED)
        self.assertEqual(participant_payload['participant_state']['survived_rounds'], 1)
        self.assertEqual(participant_payload['scorebox'][0]['scores'][0]['points'], 1)
        self.assertEqual(participant_payload['scorebox'][0]['scores'][0]['max_points'], 2)

    def test_apply_correction_endpoint_allows_active_round_live_response(self):
        game = WerWeissMehrGame.objects.create(title='Active Correction', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        thueringen = WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Thueringen')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMLIVECORR', name='Live Correction')
        self._link_game_to_session(game, session)
        participant = WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)
        submit_answer(game, participant, 'Thuringn')
        round_response = WerWeissMehrRoundResponse.objects.get(
            quiz=game,
            participant=participant,
            question=question,
            round_number=1,
        )

        response = self.client.post(
            reverse('admin_dashboard:apply_wer_weiss_mehr_correction', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'response_id': round_response.id,
                'target_answer_id': thueringen.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_ROUND_ACTIVE)
        response_payload = payload['responses'][0]
        self.assertEqual(response_payload['final_status'], WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED)
        self.assertEqual(response_payload['auto_status'], WerWeissMehrRoundResponse.STATUS_WRONG)
        self.assertEqual(response_payload['matched_answer_id'], thueringen.id)
        self.assertEqual(response_payload['round_number'], 1)
        score = payload['scorebox'][0]['scores'][0]
        self.assertEqual(score['points'], 1)
        self.assertEqual(score['max_points'], 2)
        participant.refresh_from_db()
        self.assertEqual(participant.total_score, 1)

    def test_next_round_endpoint_starts_round_two_after_review(self):
        game = WerWeissMehrGame.objects.create(title='Next Round', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        bayern = WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Thueringen')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMNEXT', name='Next Round')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )
        lisa = WerWeissMehrParticipant.objects.create(quiz=game, name='Lisa', hub_session_code=session.code)
        max_player = WerWeissMehrParticipant.objects.create(quiz=game, name='Max', hub_session_code=session.code)
        game.start_quiz()
        self._enable_manual_question_flow(game, session.code)
        start_response = self.client.post(
            reverse('wer_weiss_mehr:start_game_set', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'question_id': question.id,
                'time_limit_seconds': 30,
            }),
            content_type='application/json',
        )
        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(self._open_presented_round(game, session.code).status_code, 200)
        submit_answer(game, lisa, 'Bayern')
        submit_answer(game, max_player, 'Falsch')
        end_response = self.client.post(
            reverse('admin_dashboard:end_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(end_response.status_code, 200)

        review_state = build_game_state(game, hub_session_code=session.code)
        self.assertEqual(review_state['phase'], WerWeissMehrSession.PHASE_REVIEW)
        self.assertTrue(review_state['can_start_next_round'])

        response = self.client.post(
            reverse('admin_dashboard:next_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(payload['question_phase'], GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE)
        self.assertEqual(payload['current_round'], 2)
        self.assertIsNone(payload['timer']['ends_at'])
        self.assertTrue(any(tile['id'] == bayern.id and tile['revealed'] for tile in payload['question']['tiles']))
        self.assertTrue(all(tile['presentation_index'] is None for tile in payload['question']['tiles']))
        self.assertFalse(any(item['answer_text'] == 'Bayern' for item in payload['responses']))
        participants = {item['name']: item for item in payload['participants']}
        self.assertFalse(participants['Lisa']['is_eliminated'])
        self.assertTrue(participants['Max']['is_eliminated'])

        lisa_state = build_game_state(game, hub_session_code=session.code, participant_name='Lisa')
        max_state = build_game_state(game, hub_session_code=session.code, participant_name='Max')
        self.assertFalse(lisa_state['participant_state']['can_answer'])
        self.assertFalse(max_state['participant_state']['can_answer'])

        open_response = self._open_presented_round(game, session.code)
        self.assertEqual(open_response.status_code, 200)
        lisa_state = build_game_state(game, hub_session_code=session.code, participant_name='Lisa')
        max_state = build_game_state(game, hub_session_code=session.code, participant_name='Max')
        self.assertTrue(lisa_state['participant_state']['can_answer'])
        self.assertFalse(max_state['participant_state']['can_answer'])

    def test_next_round_endpoint_rejects_when_no_next_round_possible(self):
        game = WerWeissMehrGame.objects.create(title='No Next Round', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundesland',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMNONEXT', name='No Next Round')
        self._link_game_to_session(game, session)
        participant = WerWeissMehrParticipant.objects.create(quiz=game, name='Lisa', hub_session_code=session.code)
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)
        submit_answer(game, participant, 'Bayern')
        end_current_round(game)

        response = self.client.post(
            reverse('admin_dashboard:next_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        game.session.refresh_from_db()
        self.assertEqual(game.session.phase, WerWeissMehrSession.PHASE_REVIEW)

    def test_finish_set_endpoint_finalizes_review_without_starting_next_round(self):
        game = WerWeissMehrGame.objects.create(title='Finish Review', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        bayern = WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Thueringen')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMFINISH', name='Finish Review')
        self._link_game_to_session(game, session)
        lisa = WerWeissMehrParticipant.objects.create(quiz=game, name='Lisa', hub_session_code=session.code)
        max_player = WerWeissMehrParticipant.objects.create(quiz=game, name='Max', hub_session_code=session.code)
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)
        submit_answer(game, lisa, 'Bayern')
        submit_answer(game, max_player, 'Falsch')
        end_current_round(game)

        review_state = build_game_state(game, hub_session_code=session.code)
        self.assertTrue(review_state['can_start_next_round'])

        response = self.client.post(
            reverse('admin_dashboard:finish_wer_weiss_mehr_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_SET_COMPLETED)
        self.assertEqual(payload['current_round'], 1)
        self.assertFalse(payload['can_start_next_round'])
        self.assertFalse(WerWeissMehrRound.objects.filter(
            quiz=game,
            question=question,
            round_number=2,
        ).exists())
        self.assertTrue(any(tile['id'] == bayern.id and tile['revealed'] for tile in payload['question']['tiles']))

        lisa.refresh_from_db()
        max_player.refresh_from_db()
        self.assertEqual(lisa.total_score, 1)
        self.assertEqual(max_player.total_score, 0)
        scores = {
            score['participant_name']: score['points']
            for score in payload['scorebox'][0]['scores']
        }
        self.assertEqual(scores['Lisa'], 1)
        self.assertEqual(scores['Max'], 0)

        participant_payload = build_game_state(game, hub_session_code=session.code, participant_name='Lisa')
        self.assertEqual(participant_payload['phase'], WerWeissMehrSession.PHASE_SET_COMPLETED)
        self.assertFalse(participant_payload['participant_state']['can_answer'])

    def test_finish_set_during_presentation_does_not_block_the_next_set(self):
        game = WerWeissMehrGame.objects.create(
            title='Finish Presentation',
            creator=self.user,
            status='waiting',
        )
        WerWeissMehrSession.objects.create(quiz=game)
        first_question = WerWeissMehrQuestion.objects.create(
            question_text='Erstes Set',
            round_time_limit=30,
            created_by=self.user,
        )
        second_question = WerWeissMehrQuestion.objects.create(
            question_text='Zweites Set',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=first_question, canonical_text='Eins')
        WerWeissMehrAnswerOption.objects.create(question=second_question, canonical_text='Zwei')
        first_question.recalculate_answer_sort_order()
        second_question.recalculate_answer_sort_order()
        game.selected_questions.add(first_question, second_question)
        session = HubSession.objects.create(code='WWMFINPROMPT', name='Finish Presentation')
        self._link_game_to_session(game, session)
        game.start_quiz()
        self._enable_manual_question_flow(game, session.code)

        start_response = self.client.post(
            reverse('wer_weiss_mehr:start_game_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code, 'question_id': first_question.id}),
            content_type='application/json',
        )
        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(
            start_response.json()['question_phase'],
            GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
        )

        finish_response = self.client.post(
            reverse('admin_dashboard:finish_wer_weiss_mehr_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(finish_response.status_code, 200)
        self.assertFalse(finish_response.json()['question_phase'])
        clear_response = self.client.post(
            reverse('admin_dashboard:clear_wer_weiss_mehr_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(clear_response.status_code, 200)

        next_response = self.client.post(
            reverse('wer_weiss_mehr:start_game_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code, 'question_id': second_question.id}),
            content_type='application/json',
        )
        self.assertEqual(next_response.status_code, 200, next_response.json())
        self.assertEqual(
            next_response.json()['question_phase'],
            GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
        )

    def test_clear_set_endpoint_returns_host_to_set_selection_without_resetting_scores(self):
        game = WerWeissMehrGame.objects.create(title='Clear Set', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        first_question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        second_question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Nachbarlaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=first_question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=second_question, canonical_text='Frankreich')
        first_question.recalculate_answer_sort_order()
        second_question.recalculate_answer_sort_order()
        game.selected_questions.add(first_question, second_question)
        game.question_order = [first_question.id, second_question.id]
        game.save(update_fields=['question_order'])
        session = HubSession.objects.create(code='WWMCLEAR', name='Clear Set')
        self._link_game_to_session(game, session)
        participant = WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        start_set(game, first_question.id, hub_session_code=session.code, time_limit_seconds=30)
        submit_answer(game, participant, 'Bayern')
        finish_set(game)

        response = self.client.post(
            reverse('admin_dashboard:clear_wer_weiss_mehr_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertIsNone(payload['question'])
        self.assertEqual(payload['current_round'], 0)
        self.assertEqual(len(payload['available_questions']), 2)
        question_by_id = {item['id']: item for item in payload['available_questions']}
        self.assertTrue(question_by_id[first_question.id]['is_completed'])
        self.assertEqual(question_by_id[first_question.id]['status'], 'completed')
        self.assertFalse(question_by_id[second_question.id]['is_completed'])
        score_by_question = {row['question_id']: row for row in payload['scorebox']}
        self.assertEqual(score_by_question[first_question.id]['scores'][0]['points'], 1)
        participant.refresh_from_db()
        game.refresh_from_db()
        game.session.refresh_from_db()
        self.assertEqual(participant.total_score, 1)
        self.assertIsNone(game.current_question)
        self.assertIn(first_question.id, game.session.completed_question_ids)

    def test_finish_set_endpoint_evaluates_active_round_before_completion(self):
        game = WerWeissMehrGame.objects.create(title='Finish Active', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMFINACT', name='Finish Active')
        self._link_game_to_session(game, session)
        lisa = WerWeissMehrParticipant.objects.create(quiz=game, name='Lisa', hub_session_code=session.code)
        max_player = WerWeissMehrParticipant.objects.create(quiz=game, name='Max', hub_session_code=session.code)
        tom = WerWeissMehrParticipant.objects.create(quiz=game, name='Tom', hub_session_code=session.code)
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)
        submit_answer(game, lisa, 'Bayern')
        store_pending_input(game, max_player, 'Saarland')

        response = self.client.post(
            reverse('admin_dashboard:finish_wer_weiss_mehr_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_SET_COMPLETED)
        self.assertEqual(payload['current_round'], 1)

        lisa.refresh_from_db()
        max_player.refresh_from_db()
        tom.refresh_from_db()
        self.assertEqual(lisa.total_score, 1)
        self.assertEqual(max_player.total_score, 1)
        self.assertEqual(tom.total_score, 0)
        self.assertEqual(
            WerWeissMehrRound.objects.get(quiz=game, question=question, round_number=1).status,
            WerWeissMehrRound.STATUS_COMPLETED,
        )
        self.assertFalse(payload['participant_state']['can_answer'] if payload.get('participant_state') else False)
        scores = {
            score['participant_name']: score['points']
            for score in payload['scorebox'][0]['scores']
        }
        self.assertEqual(scores['Lisa'], 1)
        self.assertEqual(scores['Max'], 1)
        self.assertEqual(scores['Tom'], 0)

    def test_host_grid_shows_answers_while_participant_payload_stays_masked(self):
        game = WerWeissMehrGame.objects.create(title='Masked Grid', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        bayern = WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMMASK', name='Mask')
        participant = WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)

        game.refresh_from_db()
        state = build_game_state(game, hub_session_code=session.code)
        self.assertEqual([tile['text'] for tile in state['question']['tiles']], ['Bayern', 'Saarland'])
        self.assertFalse(any(tile['revealed'] for tile in state['question']['tiles']))
        self.assertEqual(
            sorted(answer['text'] for answer in state['target_answers']),
            ['Bayern', 'Saarland'],
        )
        participant_state = build_game_state(game, hub_session_code=session.code, participant_name='Lisa')
        self.assertEqual([tile['text'] for tile in participant_state['question']['tiles']], ['', ''])
        self.assertFalse(any(tile['revealed'] for tile in participant_state['question']['tiles']))
        self.assertEqual(participant_state['target_answers'], [])

        submit_answer(game, participant, 'Bayern')
        end_current_round(game)
        game.refresh_from_db()
        state = build_game_state(game, hub_session_code=session.code)
        tile_by_id = {tile['id']: tile for tile in state['question']['tiles']}
        self.assertTrue(tile_by_id[bayern.id]['revealed'])
        self.assertEqual(tile_by_id[bayern.id]['text'], 'Bayern')
        self.assertEqual(
            [tile['text'] for tile in state['question']['tiles'] if tile['id'] != bayern.id],
            ['Saarland'],
        )
        participant_state = build_game_state(game, hub_session_code=session.code, participant_name='Lisa')
        participant_tile_by_id = {tile['id']: tile for tile in participant_state['question']['tiles']}
        self.assertEqual(participant_tile_by_id[bayern.id]['text'], 'Bayern')
        self.assertEqual(
            [tile['text'] for tile in participant_state['question']['tiles'] if tile['id'] != bayern.id],
            [''],
        )

    def test_end_endpoint_completes_game_and_disables_participant_answers(self):
        game = WerWeissMehrGame.objects.create(title='HTTP End', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Saarland')
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        game.question_order = [question.id]
        game.save(update_fields=['question_order'])
        session = HubSession.objects.create(code='WWMEND', name='End Game')
        HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )
        WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)

        response = self.client.post(
            f'/wer-weiss-mehr/end/{game.room_code}/',
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['game_status'], 'completed')
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_SET_COMPLETED)
        self.assertEqual(payload['question']['id'], question.id)

        game.refresh_from_db()
        runtime_session = game.session
        runtime_session.refresh_from_db()
        self.assertEqual(game.status, 'completed')
        self.assertIsNotNone(game.ended_at)
        self.assertFalse(is_game_routable_for_hub_auto_redirect(game))
        self.assertEqual(runtime_session.phase, WerWeissMehrSession.PHASE_SET_COMPLETED)
        self.assertIsNone(runtime_session.round_start_time)
        self.assertIsNone(runtime_session.round_end_time)

        participant_payload = build_game_state(
            game,
            hub_session_code=session.code,
            participant_name='Lisa',
        )
        self.assertEqual(participant_payload['game_status'], 'completed')
        self.assertFalse(participant_payload['participant_state']['can_answer'])

    def test_dashboard_active_games_has_wwm_end_url(self):
        game = WerWeissMehrGame.objects.create(title='Dashboard End URL', creator=self.user, status='active')
        game.started_at = game.created_at
        game.save(update_fields=['started_at'])
        WerWeissMehrSession.objects.create(quiz=game)

        response = self.client.get(reverse('admin_dashboard:sessions_overview'))

        self.assertEqual(response.status_code, 200)
        wwm_game = next(item for item in response.context['active_games'] if item['room_code'] == game.room_code)
        self.assertEqual(
            wwm_game['end_url'],
            reverse('admin_dashboard:end_wer_weiss_mehr_game_by_room_code', args=[game.room_code]),
        )

    def test_dashboard_end_endpoint_completes_wwm_game(self):
        game = WerWeissMehrGame.objects.create(title='Dashboard End', creator=self.user, status='active')
        WerWeissMehrSession.objects.create(quiz=game)

        response = self.client.post(
            reverse('admin_dashboard:end_wer_weiss_mehr_game_by_room_code', args=[game.room_code]),
            data=json.dumps({}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        game.refresh_from_db()
        self.assertEqual(game.status, 'completed')
        self.assertIsNotNone(game.ended_at)

    def test_end_endpoint_returns_json_for_auth_and_not_found_errors(self):
        game = WerWeissMehrGame.objects.create(title='HTTP End Errors', creator=self.user, status='active')
        WerWeissMehrSession.objects.create(quiz=game)

        self.client.logout()
        response = self.client.post(
            f'/wer-weiss-mehr/end/{game.room_code}/',
            data=json.dumps({}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertFalse(response.json()['success'])

        self.client.force_login(self.user)
        response = self.client.post(
            '/wer-weiss-mehr/end/9999/',
            data=json.dumps({}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertFalse(response.json()['success'])

    def test_participant_submit_endpoint_locks_answer_and_returns_participant_state(self):
        game = WerWeissMehrGame.objects.create(title='HTTP Submit', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMSUB', name='Submit')
        WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)

        response = self.client.post(
            f'/wer-weiss-mehr/submit/{game.room_code}/',
            data=json.dumps({
                'participant_name': 'Lisa',
                'hub_session': session.code,
                'answer_text': 'Bayern',
                **self._action_context(game, session.code, 'Lisa'),
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['game_status'], 'active')
        self.assertEqual(payload['phase'], WerWeissMehrSession.PHASE_ROUND_ACTIVE)
        self.assertFalse(payload['participant_state']['can_answer'])
        self.assertTrue(payload['participant_state']['has_submitted'])
        self.assertEqual(payload['participant_state']['submitted_answer'], 'Bayern')
        self.assertEqual(payload['participant_state']['response']['final_status'], 'submitted')

        game.refresh_from_db()
        host_state = build_game_state(game, hub_session_code=session.code)
        self.assertEqual(len(host_state['responses']), 1)
        self.assertEqual(host_state['responses'][0]['participant_name'], 'Lisa')
        self.assertEqual(host_state['responses'][0]['answer_text'], 'Bayern')
        self.assertEqual(host_state['responses'][0]['auto_status'], WerWeissMehrRoundResponse.STATUS_CORRECT)
        self.assertEqual(host_state['responses'][0]['final_status'], WerWeissMehrRoundResponse.STATUS_CORRECT)
        self.assertEqual(host_state['responses'][0]['round_number'], 1)
        self.assertIsNotNone(host_state['responses'][0]['submitted_at'])

    def test_participant_pending_endpoint_preserves_unlocked_input_for_round_end(self):
        game = WerWeissMehrGame.objects.create(title='HTTP Pending', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        WerWeissMehrAnswerOption.objects.create(question=question, canonical_text='Bayern')
        game.selected_questions.add(question)
        session = HubSession.objects.create(code='WWMPEND', name='Pending')
        participant = WerWeissMehrParticipant.objects.create(
            quiz=game,
            name='Lisa',
            hub_session_code=session.code,
        )
        game.start_quiz()
        start_set(game, question.id, hub_session_code=session.code, time_limit_seconds=30)

        response = self.client.post(
            f'/wer-weiss-mehr/pending/{game.room_code}/',
            data=json.dumps({
                'participant_name': 'Lisa',
                'hub_session': session.code,
                'answer_text': 'Bayern',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['pending_saved'])
        self.assertTrue(WerWeissMehrPendingInput.objects.filter(
            quiz=game,
            participant=participant,
            question=question,
            answer_text='Bayern',
        ).exists())

        end_current_round(game)
        round_response = WerWeissMehrRoundResponse.objects.get(
            quiz=game,
            participant=participant,
            question=question,
            round_number=1,
        )
        self.assertEqual(round_response.answer_text, 'Bayern')
        self.assertTrue(round_response.is_correct)

    def test_full_hub_session_flow_live_responses_manual_correction_and_overall_scoreboard(self):
        session = HubSession.objects.create(
            code='WWMFLOW',
            name='WWM Flow',
            overall_scoring_mode=HubSession.OVERALL_SCORING_RANKING,
        )
        for nickname in ['Anna', 'Ben', 'Carla']:
            HubParticipant.objects.create(session=session, nickname=nickname)
        start_session_check_in(session)
        for nickname in ['Anna', 'Ben', 'Carla']:
            participant_check_in(session, nickname)
        complete_session_check_in(session)
        session.refresh_from_db()
        self.assertEqual(session.locked_participant_count, 3)

        game = WerWeissMehrGame.objects.create(title='Bundeslaender Duel', creator=self.user, status='waiting')
        WerWeissMehrSession.objects.create(quiz=game)
        question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne Bundeslaender',
            round_time_limit=30,
            created_by=self.user,
        )
        answers = {
            canonical: WerWeissMehrAnswerOption.objects.create(question=question, canonical_text=canonical)
            for canonical in ['Bayern', 'Hamburg', 'Hessen', 'Saarland', 'Thueringen']
        }
        question.recalculate_answer_sort_order()
        game.selected_questions.add(question)
        game.question_order = [question.id]
        game.save(update_fields=['question_order'])
        step = HubGameStep.objects.create(
            session=session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=game.room_code,
            title=game.title,
        )

        response = self.client.post(
            reverse('wer_weiss_mehr:start_game', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        game.refresh_from_db()
        self.assertEqual(game.status, 'active')
        self.assertIsNotNone(game.started_at)
        self.assertEqual(HubGameParticipantSnapshot.objects.filter(game_step=step, included_in_scoring=True).count(), 3)

        for nickname in ['Anna', 'Ben', 'Carla']:
            response = self.client.post(
                reverse('wer_weiss_mehr:join'),
                data=json.dumps({
                    'participant_name': nickname,
                    'room_code': game.room_code,
                    'hub_session': session.code,
                }),
                content_type='application/json',
            )
            self.assertEqual(response.status_code, 200, response.json())
            self.assertTrue(response.json()['success'])
        participants = {
            participant.name: participant
            for participant in WerWeissMehrParticipant.objects.filter(quiz=game, hub_session_code=session.code)
        }

        response = self.client.post(
            reverse('wer_weiss_mehr:start_game_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code, 'question_id': question.id}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        set_payload = response.json()
        self.assertEqual(set_payload['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(set_payload['question_phase'], GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE)
        self.assertEqual(set_payload['current_round'], 1)
        self.assertIsNone(set_payload['answering_deadline_at'])
        self.assertEqual([tile['text'] for tile in set_payload['question']['tiles']], ['Bayern', 'Hamburg', 'Hessen', 'Saarland', 'Thueringen'])
        game.refresh_from_db()
        anna_state = build_game_state(game, hub_session_code=session.code, participant_name='Anna')
        self.assertEqual([tile['text'] for tile in anna_state['question']['tiles']], ['', '', '', '', ''])
        self.assertFalse(any(tile['revealed'] for tile in anna_state['question']['tiles']))
        self.assertFalse(anna_state['participant_state']['can_answer'])

        open_response = self._open_presented_round(game, session.code)
        self.assertEqual(open_response.status_code, 200)
        self.assertEqual(open_response.json()['question_phase'], GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN)

        submissions = {
            'Anna': 'Bayern',
            'Ben': 'Atlantis',
            'Carla': 'Thuringn',
        }
        for nickname, answer_text in submissions.items():
            response = self.client.post(
                reverse('wer_weiss_mehr:participant_submit_answer', args=[game.room_code]),
                data=json.dumps({
                    'participant_name': nickname,
                    'hub_session': session.code,
                    'answer_text': answer_text,
                    **self._action_context(game, session.code, nickname),
                }),
                content_type='application/json',
            )
            self.assertEqual(response.status_code, 200, response.json())
            self.assertTrue(response.json()['success'])

        game.refresh_from_db()
        live_state = build_game_state(game, hub_session_code=session.code)
        live_responses = {item['participant_name']: item for item in live_state['responses']}
        self.assertEqual(set(live_responses), {'Anna', 'Ben', 'Carla'})
        self.assertEqual(live_responses['Anna']['auto_status'], WerWeissMehrRoundResponse.STATUS_CORRECT)
        self.assertEqual(live_responses['Ben']['auto_status'], WerWeissMehrRoundResponse.STATUS_WRONG)
        self.assertEqual(live_responses['Carla']['auto_status'], WerWeissMehrRoundResponse.STATUS_WRONG)
        self.assertFalse(any(
            item['auto_status'] == 'unclear' or item['final_status'] == 'unclear'
            for item in live_state['responses']
        ))

        correction_response = self.client.post(
            reverse('admin_dashboard:apply_wer_weiss_mehr_correction', args=[game.room_code]),
            data=json.dumps({
                'hub_session': session.code,
                'response_id': live_responses['Carla']['id'],
                'target_answer_id': answers['Thueringen'].id,
            }),
            content_type='application/json',
        )
        self.assertEqual(correction_response.status_code, 200)
        correction_payload = correction_response.json()
        self.assertTrue(correction_payload['success'])
        corrected = next(item for item in correction_payload['responses'] if item['participant_name'] == 'Carla')
        carla_score = next(
            score for score in correction_payload['scorebox'][0]['scores']
            if score['participant_name'] == 'Carla'
        )
        self.assertEqual(corrected['final_status'], WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED)
        self.assertEqual(corrected['matched_answer_id'], answers['Thueringen'].id)
        self.assertEqual(carla_score['points'], 1)
        self.assertEqual(carla_score['max_points'], 5)
        game.refresh_from_db()
        carla_participant_state = build_game_state(game, hub_session_code=session.code, participant_name='Carla')
        hidden_texts = [
            tile['text']
            for tile in carla_participant_state['question']['tiles']
            if not tile['revealed']
        ]
        self.assertEqual(hidden_texts, ['', '', '', ''])
        self.assertEqual(
            [tile['text'] for tile in carla_participant_state['question']['tiles'] if tile['revealed']],
            ['Thueringen'],
        )

        response = self.client.post(
            reverse('admin_dashboard:end_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        review_payload = response.json()
        self.assertEqual(review_payload['phase'], WerWeissMehrSession.PHASE_REVIEW)
        states = {
            state.participant.name: state
            for state in WerWeissMehrParticipantState.objects.filter(quiz=game, question=question).select_related('participant')
        }
        self.assertFalse(states['Anna'].is_eliminated)
        self.assertTrue(states['Ben'].is_eliminated)
        self.assertFalse(states['Carla'].is_eliminated)
        score_by_name = {
            score['participant_name']: score
            for score in review_payload['scorebox'][0]['scores']
        }
        self.assertEqual(score_by_name['Anna']['points'], 1)
        self.assertEqual(score_by_name['Anna']['max_points'], 5)
        self.assertEqual(score_by_name['Ben']['points'], 0)
        self.assertEqual(score_by_name['Ben']['max_points'], 5)
        self.assertEqual(score_by_name['Carla']['points'], 1)
        self.assertEqual(score_by_name['Carla']['max_points'], 5)

        response = self.client.post(
            reverse('admin_dashboard:next_wer_weiss_mehr_round', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        round_two_payload = response.json()
        self.assertEqual(round_two_payload['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertEqual(round_two_payload['question_phase'], GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE)
        self.assertEqual(round_two_payload['current_round'], 2)
        self.assertIsNone(round_two_payload['timer']['ends_at'])
        self.assertTrue(any(
            tile['id'] == answers['Bayern'].id and tile['revealed']
            for tile in round_two_payload['question']['tiles']
        ))
        self.assertTrue(all(
            tile['presentation_index'] is None
            for tile in round_two_payload['question']['tiles']
        ))
        self.assertEqual(self._open_presented_round(game, session.code).status_code, 200)
        game.refresh_from_db()
        self.assertTrue(build_game_state(game, hub_session_code=session.code, participant_name='Anna')['participant_state']['can_answer'])
        self.assertFalse(build_game_state(game, hub_session_code=session.code, participant_name='Ben')['participant_state']['can_answer'])
        self.assertTrue(build_game_state(game, hub_session_code=session.code, participant_name='Carla')['participant_state']['can_answer'])

        response = self.client.post(
            reverse('wer_weiss_mehr:participant_submit_answer', args=[game.room_code]),
            data=json.dumps({
                'participant_name': 'Ben',
                'hub_session': session.code,
                'answer_text': 'Hamburg',
                **self._action_context(game, session.code, 'Ben'),
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('nicht mehr antworten', response.json()['error'])

        response = self.client.post(
            reverse('admin_dashboard:finish_wer_weiss_mehr_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        set_done_payload = response.json()
        self.assertEqual(set_done_payload['phase'], WerWeissMehrSession.PHASE_SET_COMPLETED)
        score_by_name = {
            score['participant_name']: score
            for score in set_done_payload['scorebox'][0]['scores']
        }
        self.assertEqual(score_by_name['Anna']['points'], 1)
        self.assertEqual(score_by_name['Carla']['points'], 1)
        self.assertEqual(score_by_name['Ben']['points'], 0)
        self.assertEqual(score_by_name['Anna']['max_points'], 5)

        response = self.client.post(
            reverse('admin_dashboard:clear_wer_weiss_mehr_set', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        clear_payload = response.json()
        self.assertEqual(clear_payload['phase'], WerWeissMehrSession.PHASE_IDLE)
        self.assertIsNone(clear_payload['question'])
        self.assertTrue(clear_payload['available_questions'][0]['is_completed'])
        cleared_scores = {
            score['participant_name']: score
            for score in clear_payload['scorebox'][0]['scores']
        }
        self.assertEqual(cleared_scores['Anna']['points'], 1)
        self.assertEqual(cleared_scores['Carla']['points'], 1)
        self.assertEqual(cleared_scores['Ben']['points'], 0)

        response = self.client.post(
            reverse('wer_weiss_mehr:end_game', args=[game.room_code]),
            data=json.dumps({'hub_session': session.code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        end_payload = response.json()
        self.assertEqual(end_payload['game_status'], 'completed')
        self.assertFalse(end_payload['participant_state']['can_answer'] if end_payload.get('participant_state') else False)
        game.refresh_from_db()
        self.assertEqual(game.status, 'completed')

        leaderboard = get_leaderboard_data(session)
        game_key = f'step:{step.id}'
        self.assertEqual(leaderboard['instances'][game_key]['score_range']['basis'], 'snapshot')
        self.assertEqual(leaderboard['instances'][game_key]['score_range']['max'], 3)
        overall_by_name = {item['name']: item for item in leaderboard['participants']}
        self.assertEqual(overall_by_name['Anna']['game_scores'][game_key], 1)
        self.assertEqual(overall_by_name['Carla']['game_scores'][game_key], 1)
        self.assertEqual(overall_by_name['Ben']['game_scores'][game_key], 0)
        self.assertEqual(overall_by_name['Anna']['game_base_scores'][game_key], 3)
        self.assertEqual(overall_by_name['Carla']['game_base_scores'][game_key], 3)
        self.assertEqual(overall_by_name['Ben']['game_base_scores'][game_key], 1)
        self.assertEqual(overall_by_name['Anna']['weighted_score'], 3)
        self.assertEqual(overall_by_name['Carla']['weighted_score'], 3)
        self.assertEqual(overall_by_name['Ben']['weighted_score'], 1)

        play_response = self.client.get(
            reverse('wer_weiss_mehr:play', args=[game.room_code, 'Anna']),
            {'hub_session': session.code},
        )
        self.assertEqual(play_response.status_code, 200)
        self.assertContains(play_response, 'id="lobbyActions"', html=False)
        self.assertContains(play_response, "endedStatuses.includes(state?.game_status)", html=False)


class WerWeissMehrQuestionPhaseBrowserTests(_BrowserTestBase):
    @staticmethod
    def _stop_live_connections(page):
        page.evaluate(
            """() => {
                if (typeof pollTimer !== 'undefined' && pollTimer) {
                    clearInterval(pollTimer);
                    pollTimer = null;
                }
                if (typeof liveResponsePollTimer !== 'undefined' && liveResponsePollTimer) {
                    clearInterval(liveResponsePollTimer);
                    liveResponsePollTimer = null;
                }
                if (typeof reconnectTimer !== 'undefined' && reconnectTimer) {
                    clearTimeout(reconnectTimer);
                    reconnectTimer = null;
                }
                if (typeof ws !== 'undefined' && ws) {
                    ws.onclose = null;
                    ws.onerror = null;
                    ws.close();
                    ws = null;
                }
                if (typeof hubWs !== 'undefined' && hubWs) {
                    hubWs.onclose = null;
                    hubWs.close();
                    hubWs = null;
                }
                if (typeof connectSocket === 'function') connectSocket = () => {};
                if (typeof connectHubSocket === 'function') connectHubSocket = () => {};
            }"""
        )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls._playwright, cls._browser = start_chromium_browser(headless=True)
            cls._playwright_error = None
        except Exception as exc:  # pragma: no cover - environment dependent
            cls._playwright = None
            cls._browser = None
            cls._playwright_error = exc

    @classmethod
    def tearDownClass(cls):
        if cls._browser:
            cls._browser.close()
            cls._playwright.stop()
        super().tearDownClass()

    def setUp(self):
        if not self._browser:
            self.skipTest(f'Playwright/Chromium nicht verfuegbar: {self._playwright_error}')

        self.admin = User.objects.create_superuser(
            username='wwm-browser-admin',
            password='testpass123',
            email='',
        )
        self.client.force_login(self.admin)
        self.hub_session = HubSession.objects.create(
            code='WWMBROWSER',
            name='WWM Browser',
            is_active=True,
            started_at=timezone.now(),
        )
        self.game = WerWeissMehrGame.objects.create(
            title='WWM Browser Flow',
            creator=self.admin,
            status='waiting',
        )
        WerWeissMehrSession.objects.create(quiz=self.game)
        self.question = WerWeissMehrQuestion.objects.create(
            question_text='Nenne fuenf Planeten',
            round_time_limit=30,
            created_by=self.admin,
        )
        for answer in ('Erde', 'Jupiter', 'Mars', 'Merkur', 'Venus'):
            WerWeissMehrAnswerOption.objects.create(
                question=self.question,
                canonical_text=answer,
            )
        self.question.recalculate_answer_sort_order()
        self.game.selected_questions.add(self.question)
        self.game.question_order = [self.question.id]
        self.game.save(update_fields=['question_order'])
        HubGameStep.objects.create(
            session=self.hub_session,
            order=0,
            game_key='wer_weiss_mehr',
            room_code=self.game.room_code,
            title=self.game.title,
        )
        for name in ('Anna', 'Ben'):
            WerWeissMehrParticipant.objects.create(
                quiz=self.game,
                name=name,
                hub_session_code=self.hub_session.code,
            )
        self.game.start_quiz(hub_session_code=self.hub_session.code)
        reset_question_flow(
            game_key='wer_weiss_mehr',
            room_code=self.game.room_code,
            session_code=self.hub_session.code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )

        self.admin_context = self._browser.new_context(viewport={'width': 1366, 'height': 768})
        install_browser_test_stubs(self.admin_context)
        session_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME]
        self.admin_context.add_cookies([{
            'name': settings.SESSION_COOKIE_NAME,
            'value': session_cookie.value,
            'url': self.live_server_url,
        }])
        self.admin_page = self.admin_context.new_page()

        self.participant_contexts = []
        self.participant_pages = []
        for name, viewport in (
            ('Anna', {'width': 390, 'height': 844}),
            ('Ben', {'width': 1366, 'height': 768}),
        ):
            context = self._browser.new_context(viewport=viewport)
            install_browser_test_stubs(context)
            context.add_init_script("localStorage.setItem('participant_interface_theme', 'vhs');")
            page = context.new_page()
            page.goto(
                f'{self.live_server_url}'
                f'{reverse("wer_weiss_mehr:play", args=[self.game.room_code, name])}'
                f'?hub_session={self.hub_session.code}'
            )
            page.wait_for_selector('#answerInput', state='attached')
            page.wait_for_function('latestState !== null')
            self._stop_live_connections(page)
            page.evaluate(
                """() => {
                    window.__wwmRevealOrder = [];
                    const seen = new Set();
                    const tiles = document.getElementById('tiles');
                    const scan = () => {
                        tiles.querySelectorAll('.tile:not(.wwm-tile-presentation-pending)').forEach(tile => {
                            if (tile.dataset.presentationIndex === '') return;
                            const position = Number(tile.dataset.tilePosition);
                            if (seen.has(position)) return;
                            seen.add(position);
                            window.__wwmRevealOrder.push({position, at: performance.now()});
                        });
                    };
                    new MutationObserver(scan).observe(tiles, {
                        childList: true,
                        subtree: true,
                        attributes: true,
                        attributeFilter: ['class'],
                    });
                    scan();
                }"""
            )
            self.participant_contexts.append(context)
            self.participant_pages.append(page)

    def tearDown(self):
        for page in [getattr(self, 'admin_page', None), *getattr(self, 'participant_pages', [])]:
            if not page:
                continue
            try:
                self._stop_live_connections(page)
            except Exception:
                pass
            try:
                page.close()
            except Exception:
                pass
        for context in [getattr(self, 'admin_context', None), *getattr(self, 'participant_contexts', [])]:
            if not context:
                continue
            try:
                context.close()
            except Exception:
                pass

    def test_two_participants_receive_ordered_fields_before_host_opens_answering(self):
        monitor_url = reverse(
            'admin_dashboard:wer_weiss_mehr_monitor',
            args=[self.game.room_code],
        )
        self.admin_page.goto(f'{self.live_server_url}{monitor_url}?hub_session={self.hub_session.code}')
        self.admin_page.wait_for_selector('.start-set-btn')
        self.admin_page.wait_for_function('state !== null')
        self._stop_live_connections(self.admin_page)
        self.assertEqual(self.admin_page.get_by_text('ANTWORTTAFEL ANZEIGEN').count(), 0)
        self.admin_page.locator('.start-set-btn').first.click()
        self.admin_page.wait_for_selector('#openRoundBtn')
        self.assertTrue(self.admin_page.locator('#openRoundBtn').is_disabled())
        runtime = GameRuntimeState.objects.get(
            game_key='wer_weiss_mehr',
            room_code=self.game.room_code,
            session__code=self.hub_session.code,
        )
        runtime.question_presented_at = timezone.now()
        runtime.save(update_fields=['question_presented_at', 'updated_at'])

        pending_counts = []
        for page in self.participant_pages:
            page.evaluate('fetchState()')
            page.wait_for_selector('.tile')
            pending_count = page.locator('.wwm-tile-presentation-pending').count()
            pending_counts.append(pending_count)
            self.assertTrue(page.locator('#answerArea').evaluate(
                "element => element.classList.contains('d-none')"
            ))
            page.wait_for_function('window.__wwmRevealOrder.length === 5', timeout=10_000)
            reveal_order = page.evaluate('window.__wwmRevealOrder.map(item => item.position)')
            self.assertEqual(reveal_order, [1, 2, 3, 4, 5])
            self.assertFalse(page.locator('#questionText').evaluate(
                "element => element.classList.contains('wwm-question-presentation-pending')"
            ))
            self.assertTrue(page.locator('#answerInput').is_disabled())
            self.assertTrue(page.locator('#timerBox').evaluate(
                "element => element.classList.contains('d-none')"
            ))
        self.assertGreater(max(pending_counts), 0)

        self.admin_page.wait_for_function(
            "!document.getElementById('openRoundBtn').disabled",
            timeout=10_000,
        )
        self.admin_page.locator('#openRoundBtn').click()
        self.admin_page.wait_for_function("state?.question_phase === 'answering_open'")

        for page in self.participant_pages:
            page.evaluate('fetchState()')
            page.wait_for_function(
                "!document.getElementById('answerInput').disabled",
                timeout=10_000,
            )
            self.assertEqual(page.locator('.wwm-tile-presentation-pending').count(), 0)
            self.assertFalse(page.locator('#timerBox').evaluate(
                "element => element.classList.contains('d-none')"
            ))

        typing_page = self.participant_pages[1]
        typing_page.reload()
        typing_page.wait_for_function(
            "latestState?.phase === 'round_active' && !document.getElementById('answerInput').disabled",
            timeout=10_000,
        )
        typing_input = typing_page.locator('#answerInput')
        typing_input.click()
        expected_text = ''
        for character in 'Mars':
            typing_page.keyboard.insert_text(character)
            expected_text += character
            typing_page.wait_for_timeout(350)
            typing_page.evaluate('fetchState()')
            typing_page.wait_for_timeout(150)
            self.assertTrue(typing_page.evaluate(
                "document.activeElement === document.getElementById('answerInput')"
            ))
            self.assertEqual(typing_input.input_value(), expected_text)

        self.participant_pages[0].locator('#answerInput').fill('Erde')
        self.participant_pages[0].locator('#submitBtn').click()
        self.participant_pages[0].wait_for_function(
            "document.getElementById('answerInput').disabled",
            timeout=10_000,
        )
        self.assertFalse(self.participant_pages[1].locator('#answerInput').is_disabled())

        for viewport in (
            {'width': 360, 'height': 800},
            {'width': 390, 'height': 844},
            {'width': 768, 'height': 1024},
            {'width': 1366, 'height': 768},
            {'width': 1920, 'height': 1080},
        ):
            self.participant_pages[1].set_viewport_size(viewport)
            self.assertTrue(self.participant_pages[1].evaluate(
                'document.documentElement.scrollWidth <= window.innerWidth + 1'
            ))
