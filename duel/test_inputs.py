"""Real planned Hub starts and authenticated device-input actions."""
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.utils import timezone
from where_is_this.models import WhereQuiz, WhereQuestion
from black_jack_quiz.models import BlackJackQuiz, BlackJackQuestion
from . import test_oral as reference
from .models import DuelInputRound, DuelScoreEntry, DuelUnitOutcome, DuelSubmission
from .deadlines import reconcile_due_rounds
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase, Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from io import StringIO
from uuid import uuid4
from games_hub.models import HubParticipant, HubGameStep
from games_hub.lobby_join import issue_rejoin_token
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from .commands import ActorContext, ActionConflict
from .controllers import inputs
from .models import DuelActionReceipt, ReceiptActorType, ReceiptStatus
from . import test_submissions_concurrency as concurrency
from .test_deadline_worker import worker_environment
import subprocess
from io import BytesIO
from PIL import Image, PngImagePlugin
from django.core.files.uploadedfile import SimpleUploadedFile


def make_material(case, key):
    if not hasattr(case, 'user'): case.user = User.objects.create_superuser('input-host', '', 'pw')
    if key == 'where':
        case.game = WhereQuiz.objects.create(creator=case.user, title='Where Duel', scoring_mode='rank')
        case.questions = [WhereQuestion.objects.create(created_by=case.user, question_text=f'Wo liegt Berlin? {region}',
            correct_latitude=52.5, correct_longitude=13.4, map_type=region, time_limit=120) for region in ('world_mercator', 'europe', 'germany')]
        case.question = case.questions[0]
    else:
        case.game = BlackJackQuiz.objects.create(creator=case.user, title='Black Jack Duel', total_questions=2)
        case.questions = [BlackJackQuestion.objects.create(created_by=case.user, question_text=f'Zahl {i+1}?', correct_answer=100, time_limit=120) for i in range(2)]
        case.question = case.questions[0]
        case.game.question_order = [[q.pk for q in case.questions]]; case.game.save()
    case.game.selected_questions.add(*case.questions)


class InputFixture(reference.OralFixture):
    key = 'where'

    def setUp(self):
        with patch('duel.test_oral.make_material', make_material): super().setUp()

    def prepare(self):
        selection = self.question.pk if self.key == 'where' else 1
        response = self.post({'action': 'prepare', 'question_id': selection, 'previous_unit_id': self.unit.pk if self.unit else None}, match=True)
        self.assertEqual(response.status_code, 200, response.content); self.unit = self.instance.units.latest('number')

    def command(self, action, **kw):
        index = DuelInputRound.objects.get(unit=self.unit).question_index
        return self.post({'action': action, 'question_index': index, **kw})

    def start(self):
        self.prepare(); response = self.command('start'); self.assertEqual(response.status_code, 200, response.content)
        return DuelInputRound.objects.get(unit=self.unit)

    def answer(self, side, value, action='commit', key=None, index=None):
        if index is None: index = DuelInputRound.objects.get(unit=self.unit).question_index
        payload = value if self.key == 'where' else {'value': value}
        return self.post({'action': action, 'question_index': index, 'payload': payload}, side, key)

    def reveal(self):
        for action in (('reveal',) if self.key == 'where' else ('show_answers', 'show_solution')):
            response = self.command(action); self.assertEqual(response.status_code, 200, response.content)
        response = self.command('evaluate'); self.assertEqual(response.status_code, 200, response.content)
        return response.json()['snapshot']


class WhereDuelTests(InputFixture):
    def test_optional_question_image_is_frozen_sanitized_and_role_checked(self):
        image = BytesIO(); metadata = PngImagePlugin.PngInfo(); metadata.add_text('Location', 'SECRET-LOCATION')
        Image.new('RGB', (64, 64), 'green').save(image, format='PNG', pnginfo=metadata)
        self.question.image = SimpleUploadedFile('SECRET-LOCATION.png', image.getvalue(), content_type='image/png'); self.question.save()
        self.prepare(); url = reverse('duel:input_image', args=[self.hub.code, self.instance.pk, self.unit.pk, 'spectator'])
        self.assertEqual(self.spectator.get(url).status_code, 404)
        self.assertEqual(self.command('start').status_code, 200)
        response = self.spectator.get(url); self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertNotIn('Location', Image.open(BytesIO(response.content)).info)
        self.assertNotIn('SECRET-LOCATION', str(self.snapshot()))
        host_url = reverse('duel:input_image', args=[self.hub.code, self.instance.pk, self.unit.pk, 'host'])
        self.assertEqual(self.players[0].get(host_url).status_code, 403)

    def test_all_map_material_stays_frozen_and_repeated_questions_are_new_units(self):
        self.start(); state = DuelInputRound.objects.get(unit=self.unit)
        frozen = state.material.copy()
        self.question.question_text = 'Changed'; self.question.correct_longitude = 1; self.question.save()
        self.assertEqual(self.snapshot()['question'], frozen['questions'][0]['text'])
        self.assertEqual(DuelInputRound.objects.get(pk=state.pk).material, frozen)
        self.answer(0, {'latitude': 52.5, 'longitude': 13.4}); self.answer(1, {'latitude': 50, 'longitude': 10})
        self.reveal(); self.command('complete'); first = self.unit.pk; self.start()
        self.assertNotEqual(self.unit.pk, first); self.assertEqual(self.snapshot()['submitted'], {'a': False, 'b': False})

    def test_permissions_and_transport_cannot_choose_reveal_or_foreign_identity(self):
        self.start()
        third = HubParticipant.objects.create(session=self.hub, nickname='Third')
        client = Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(third))
        body = {'action': 'commit', 'question_index': 0, 'payload': {'latitude': 50, 'longitude': 10}, 'client_action_id': 'forged'}
        self.assertEqual(client.post(self.url('participant'), body, content_type='application/json').status_code, 403)
        self.assertEqual(self.players[0].post(self.url(), body, content_type='application/json').status_code, 403)
        self.assertEqual(self.spectator.post(self.url('spectator'), body, content_type='application/json').status_code, 403)
        self.assertEqual(self.players[0].get(self.url('participant')+'?reveal=true').status_code, 400)
        self.assertEqual(self.post({'action': 'reveal', 'question_index': 0}, 0).status_code, 403)
        foreign = self.unit.__class__.objects.create(game_instance=self.instance, number=2)
        url = reverse('duel:where_participant', args=[self.hub.code, self.instance.pk, foreign.pk])
        self.assertEqual(self.players[0].get(url).status_code, 409)  # No bound question, never another unit's data.
        self.assertFalse(DuelSubmission.objects.exists())

    def test_draft_commit_wrong_coordinates_and_snapshot_is_read_only(self):
        self.question = self.questions[2]; self.start()
        for payload in ({'latitude': 0, 'longitude': 0}, {'latitude': 90, 'longitude': 13}, {'latitude': True, 'longitude': 13}, {'latitude': 52}, {'latitude': 52, 'longitude': 13, 'target': 7}):
            self.assertIn(self.answer(0, payload).status_code, (400, 409))
        self.assertFalse(DuelSubmission.objects.exists())
        with CaptureQueriesContext(connection) as queries:
            for role in ('host', 'participant', 'spectator'): self.snapshot(role)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))

    def test_reveal_idempotence_and_result_rollback(self):
        self.start(); self.answer(0, {'latitude': 52.5, 'longitude': 13.4}); self.answer(1, {'latitude': 50, 'longitude': 10})
        self.assertEqual(self.post({'action': 'reveal', 'question_index': 0}, key='same').status_code, 200)
        self.assertTrue(self.post({'action': 'reveal', 'question_index': 0}, key='same').json()['action']['replayed'])
        self.assertEqual(self.post({'action': 'evaluate', 'question_index': 0}, key='same').status_code, 409)
        self.instance.refresh_from_db(); revision = self.instance.state_revision
        with patch('duel.controllers.inputs.complete_unit_in_action', side_effect=IntegrityError('injected')):
            with self.assertRaises(IntegrityError):
                inputs.advance(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=ActorContext(ReceiptActorType.HOST, self.user.pk),
                    client_action_id='fail', question_index=0, action='evaluate')
        self.assertFalse(DuelScoreEntry.objects.exists()); self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertFalse(DuelActionReceipt.objects.filter(client_action_id='fail').exists())
        self.instance.refresh_from_db(); self.assertEqual(self.instance.state_revision, revision)
        self.assertEqual(self.snapshot()['phase'], 'show_solution')

    def test_closer_wins_only_after_manual_reveal_and_result(self):
        self.start()
        self.assertEqual(self.answer(0, {'latitude': 52.5, 'longitude': 13.4}).status_code, 200)
        self.assertEqual(self.snapshot()['phase'], 'answering')
        self.assertEqual(self.answer(1, {'latitude': 50, 'longitude': 10}).status_code, 200)
        public = self.snapshot(); self.assertEqual(public['phase'], 'awaiting_reveal')
        self.assertNotIn('answers', public); self.assertNotIn('target', public)
        self.assertFalse(DuelScoreEntry.objects.exists())
        result = self.reveal(); self.assertEqual(result['points'], {'a': 1, 'b': 0})
        self.assertEqual(result['distances']['a'], 0)
        self.assertEqual(self.unit.outcome.winner_id, self.parties[0].pk)
        self.assertEqual(self.command('complete').status_code, 200)

    def test_draft_moves_commit_locks_retry_and_rejoin(self):
        self.start(); first = {'latitude': 50, 'longitude': 10}; last = {'latitude': 52.5, 'longitude': 13.4}
        self.assertEqual(self.answer(0, first, 'draft').status_code, 200)
        self.assertEqual(self.answer(0, last, 'draft').status_code, 200)
        self.assertEqual(self.snapshot('participant')['draft'], last)
        self.assertNotIn('draft', self.snapshot())
        self.assertEqual(self.answer(0, last, key='marker').status_code, 200)
        self.assertTrue(self.answer(0, last, key='marker').json()['action']['replayed'])
        self.assertEqual(self.answer(0, first, key='marker').status_code, 409)
        self.assertEqual(self.answer(0, first).status_code, 409)
        self.assertEqual(self.answer(0, first, 'draft').status_code, 409)
        self.assertEqual(self.snapshot('participant')['own_answer'], last)
        self.assertFalse(self.snapshot('participant')['can_answer'])

    def test_equal_distance_gives_both_a_point_without_a_round_winner(self):
        self.start()
        for side in (0, 1): self.assertEqual(self.answer(side, {'latitude': 52.5, 'longitude': 13.4}).status_code, 200)
        result = self.reveal(); self.assertEqual(result['result_reason'], 'equal_distances')
        self.assertEqual(result['points'], {'a':1, 'b':1}); self.assertIsNone(self.unit.outcome.winner_id)
        self.assertEqual(self.command('complete').status_code, 200)

    def test_deadline_without_clients_and_missing_answer_has_no_winner(self):
        state = self.start()
        with patch('django.utils.timezone.now', return_value=state.deadline):
            self.assertEqual(self.answer(0, {'latitude': 52.5, 'longitude': 13.4}).status_code, 409)
            self.assertEqual(reconcile_due_rounds(), 1); self.assertEqual(reconcile_due_rounds(), 0)
        result = self.reveal(); self.assertEqual(result['result_reason'], 'no_answers')
        self.assertEqual(result['points'], {'a':0,'b':0}); self.assertEqual(result['round_outcome'], 'no_winner')


class BlackJackDuelTests(InputFixture):
    key = 'blackjack'

    def question_result(self, a, b, reverse=False):
        for side in ((1, 0) if reverse else (0, 1)):
            response = self.answer(side, [a, b][side]); self.assertEqual(response.status_code, 200, response.content)
            self.assertFalse(DuelScoreEntry.objects.exists())
        return self.reveal()

    def test_whole_set_one_round_and_question_scoped_answers(self):
        self.start(); state = self.question_result(110, 105)
        self.assertEqual(state['phase'], 'question_result'); self.assertEqual(state['stars'], {'a': 10, 'b': 5})
        self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertEqual(self.command('next').status_code, 200); self.assertEqual(self.command('start').status_code, 200)
        result = self.question_result(111, 110, reverse=True)
        self.assertEqual(result['stars'], {'a': 21, 'b': 15}); self.assertEqual(result['points'], {'a': 1, 'b': 0})
        self.assertEqual(self.instance.units.count(), 1); self.assertEqual(DuelSubmission.objects.filter(status='committed').count(), 4)

    def test_exact_twenty_one_beats_bust_after_both_answers(self):
        self.start()
        self.assertEqual(self.answer(1, 122).status_code, 200)
        self.assertEqual(self.snapshot()['stars'], {'a': 0, 'b': 0})
        self.assertEqual(self.answer(0, 121).status_code, 200)
        result = self.reveal(); self.assertEqual(result['stars'], {'a': 21, 'b': 22})
        self.assertEqual(result['points'], {'a': 1, 'b': 0}); self.assertTrue(result['bust']['b'])
        self.assertEqual(self.command('next').status_code, 409)

    def test_both_bust_end_without_points_or_winner(self):
        self.start(); result = self.question_result(122, 123)
        self.assertEqual(result['result_reason'], 'both_bust'); self.assertEqual(result['points'], {'a':0,'b':0}); self.assertEqual(self.command('complete').status_code,200)

    def test_stars_do_not_become_duel_points_and_bust_is_batched_in_reverse_order(self):
        self.start(); result = self.question_result(123, 102, reverse=True)
        self.assertEqual(result['stars'], {'a': 23, 'b': 2}); self.assertEqual(result['points'], {'a': 0, 'b': 1})
        self.assertEqual(result['match']['party_b']['score'], 1); self.assertEqual(result['match']['party_b']['round_wins'], 1)

    def test_set_tie_completes_without_a_winner(self):
        self.start(); self.question_result(101, 101)
        self.command('next'); self.command('start'); result = self.question_result(102, 102)
        self.assertEqual(result['result_reason'], 'equal_stars'); self.assertEqual(result['points'], {'a':0,'b':0}); self.assertEqual(self.command('complete').status_code,200)

    def test_missing_response_does_not_invent_ten_stars_or_a_winner(self):
        state = self.start(); self.answer(0, 100)
        with patch('django.utils.timezone.now', return_value=state.deadline): self.assertEqual(reconcile_due_rounds(), 1)
        result = self.reveal(); self.assertEqual(result['resolution_reason'], 'missing_answers')
        self.assertEqual(result['question_stars'], {'a': 0, 'b': None}); self.assertFalse(DuelScoreEntry.objects.exists())

    def test_old_question_actions_cannot_affect_next_question(self):
        self.start(); self.assertEqual(self.answer(0, 110, key='old').status_code, 200); self.answer(1, 105); self.reveal()
        self.command('next'); self.command('start')
        self.assertTrue(self.answer(0, 110, key='old', index=0).json()['action']['replayed'])
        self.assertEqual(self.answer(0, 110, index=0).status_code, 409)
        self.assertFalse(self.snapshot('participant')['own_answer']); self.assertEqual(self.snapshot()['phase'], 'answering')
        self.assertEqual(DuelSubmission.objects.filter(input_key='question:1').count(), 0)

    def test_integer_validation_and_sequence_spans_all_questions(self):
        self.start()
        for value in (True, 'NaN', '1.5', 2**31, None, [], {}): self.assertEqual(self.answer(0, value).status_code, 400)
        self.question_result(110, 105); self.command('next'); self.command('start')
        self.answer(0, 111); self.answer(1, 117)
        self.assertEqual(list(DuelSubmission.objects.filter(status='committed').order_by('acceptance_sequence').values_list('acceptance_sequence', flat=True)), [1, 2, 3, 4])

    def test_database_scoped_unique_constraint_preserves_old_empty_scope(self):
        self.start(); self.answer(0, 110)
        original = DuelSubmission.objects.get(status='committed')
        original.pk = uuid4(); original.acceptance_sequence = 5
        with self.assertRaises(IntegrityError), transaction.atomic(): original.save(force_insert=True)

    def test_lossy_schema_rollback_is_explicitly_refused(self):
        self.start(); self.answer(0, 110)
        executor = MigrationExecutor(connection); latest = executor.loader.graph.leaf_nodes()
        before = [n for n in latest if n[0] != 'duel']+[('duel', '0013_alternating_turn_rounds')]
        try:
            with self.assertRaisesMessage(RuntimeError, 'Archive scoped Black Jack inputs'):
                executor.migrate(before)
            self.assertEqual(DuelSubmission.objects.get().input_key, 'question:0')
            self.assertIn('duel_duelinputround', connection.introspection.table_names())
        finally:
            # Later additive migrations may already have been reversed before
            # 0014 refuses its lossy rollback. Restore them for subsequent tests.
            MigrationExecutor(connection).migrate(latest)


    def test_two_answers_first_then_solution(self):
        self.start(); self.answer(0, 100); self.answer(1, 101)
        self.assertNotIn('answers', self.snapshot()); self.assertNotIn('target', self.snapshot())
        self.assertEqual(self.command('show_solution').status_code, 409)
        self.assertEqual(self.command('show_answers').status_code, 200)
        self.assertEqual(self.snapshot()['answers'], {'a': {'value': 100}, 'b': {'value': 101}})
        self.assertNotIn('target', self.snapshot())
        self.assertEqual(self.command('show_solution').status_code, 200)
        self.assertEqual(self.snapshot()['target'], 100); self.assertFalse(DuelScoreEntry.objects.exists())


class InputConcurrencyTests(InputFixture):
    key = 'blackjack'
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def options(self, side, id):
        return dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=self.actor(side), client_action_id=id, question_index=0)

    def test_parallel_independent_answers_close_once_and_revision_is_not_lost(self):
        self.start(); self.instance.refresh_from_db(); revision = self.instance.state_revision
        results = self.parallel([lambda side=side: inputs.submit(**self.options(side, f'party-{side}'), payload={'value': 121+side}) for side in (0, 1)])
        self.assertTrue(all(r.status == ReceiptStatus.APPLIED for r in results))
        self.assertEqual(sorted(r.state_revision for r in results), [revision+1, revision+2])
        self.assertEqual(self.snapshot()['phase'], 'awaiting_reveal'); self.assertEqual(DuelSubmission.objects.count(), 2)
        self.assertEqual(self.reveal()['points'], {'a': 1, 'b': 0})

    def test_parallel_identical_conflicting_and_duplicate_result(self):
        self.start(); options = self.options(0, 'same')
        results = self.parallel([lambda: inputs.submit(**options, payload={'value': 121})]*2)
        self.assertEqual(sorted(r.replayed for r in results), [False, True])
        with self.assertRaises(ActionConflict): inputs.submit(**options, payload={'value': 120})
        self.answer(1, 122); self.command('show_answers'); self.command('show_solution')
        args = dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=ActorContext(ReceiptActorType.HOST, self.user.pk), question_index=0, action='evaluate')
        results = self.parallel([lambda i=i: inputs.advance(**args, client_action_id=f'eval-{i}') for i in (0, 1)])
        self.assertEqual(sum(r.status == ReceiptStatus.APPLIED for r in results), 1)
        self.assertEqual(DuelScoreEntry.objects.count(), 2); self.assertEqual(DuelUnitOutcome.objects.count(), 1)

    def test_second_submission_at_deadline_and_worker_cannot_reverse_phase(self):
        state = self.start(); self.answer(0, 100)
        with patch('django.utils.timezone.now', return_value=state.deadline):
            results = self.parallel([lambda: inputs.submit(**self.options(1, 'late'), payload={'value': 101}),
                lambda: inputs.reconcile_deadline(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id='due', question_index=0)])
        self.assertEqual(results[0].status, ReceiptStatus.REJECTED); self.assertEqual(results[1].status, ReceiptStatus.APPLIED)
        self.assertEqual(self.snapshot()['phase'], 'awaiting_reveal'); self.assertEqual(DuelSubmission.objects.count(), 1)


class InputHubTests(InputFixture):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def test_real_hub_repeated_and_parallel_starts_keep_one_match(self):
        clients = [Client(), Client()]
        for client in clients: client.force_login(self.user)
        from .hub import start_slot
        self.instance.refresh_from_db(); revision = self.instance.state_revision
        results = self.parallel([lambda: start_slot(user=self.user, session_code=self.hub.code, step_id=self.step.pk)]*2)
        self.assertEqual(results[0]['instance_id'], results[1]['instance_id'])
        self.assertEqual(self.hub_start().status_code, 200)
        self.assertEqual(self.hub.duel_instances.count(), 1); self.assertFalse(self.instance.units.exists())
        self.instance.refresh_from_db(); self.assertEqual(self.instance.state_revision, revision)

    def test_two_person_multiplayer_still_uses_original_source_start(self):
        # Remove the fixture-only running duel before testing the normal path.
        self.instance.action_receipts.all().delete(); self.instance.delete()
        self.step.game_format = 'standard'; self.step.intro_started_at = self.step.intro_ends_at = None
        self.step.save(update_fields=['game_format', 'intro_started_at', 'intro_ends_at'])
        self.hub.session_type = 'multiplayer'; self.hub.started_at = timezone.now()
        self.hub.save(update_fields=['session_type', 'started_at'])
        response = self.hub_start(); self.assertEqual(response.status_code, 200, response.content)
        self.game.refresh_from_db(); self.assertEqual(self.game.status, 'active')
        self.assertEqual(self.hub.duel_instances.count(), 0)

    def test_preparation_error_rolls_back_unit_and_can_be_retried(self):
        self.game.selected_questions.clear()
        response = self.post({'action': 'prepare', 'question_id': self.question.pk, 'previous_unit_id': None}, match=True)
        self.assertEqual(response.status_code, 409); self.assertFalse(self.instance.units.exists())
        self.game.selected_questions.add(*self.questions); self.prepare(); self.assertIsNotNone(self.unit.pk)

    def test_fresh_worker_process_recovers_saved_deadline_without_clients(self):
        self.prepare(); clock = timezone.now()-timedelta(minutes=3)
        HubGameStep.objects.filter(pk=self.step.pk).update(intro_ends_at=clock-timedelta(seconds=1))
        with patch('django.utils.timezone.now', return_value=clock):
            response = self.command('start'); self.assertEqual(response.status_code, 200, response.content)
        with worker_environment() as (command, options):
            first = subprocess.run(command+['--once'], capture_output=True, text=True, timeout=35, **options)
            self.assertEqual(first.returncode, 0, first.stderr); self.assertEqual(first.stdout.strip(), '1')
            before = DuelInputRound.objects.values().get(unit=self.unit)
            second = subprocess.run(command+['--once'], capture_output=True, text=True, timeout=35, **options)
            self.assertEqual(second.returncode, 0, second.stderr); self.assertEqual(second.stdout.strip(), '0')
            self.assertEqual(DuelInputRound.objects.values().get(unit=self.unit), before)
        self.assertEqual(self.snapshot()['phase'], 'awaiting_reveal')
        self.assertNotIn('target', self.snapshot()); self.assertFalse(DuelScoreEntry.objects.exists())


class BlackJackHubTests(InputHubTests):
    key = 'blackjack'

    def test_preparation_error_rolls_back_unit_and_can_be_retried(self):
        self.questions[0].is_active = False; self.questions[0].save()
        # Explicit configuration is filtered by the source helper; a genuinely
        # empty configured set must be rejected, rather than inventing a pool.
        for q in self.questions: q.is_active = False; q.save()
        response = self.post({'action': 'prepare', 'question_id': 1, 'previous_unit_id': None}, match=True)
        self.assertEqual(response.status_code, 409); self.assertFalse(self.instance.units.exists())
        for q in self.questions: q.is_active = True; q.save()
        self.prepare(); self.assertIsNotNone(self.unit.pk)


class InputMigrationTests(TransactionTestCase):
    def test_roundtrip_adds_only_duel_table_and_keeps_legacy_input_keys(self):
        executor = MigrationExecutor(connection); latest = executor.loader.graph.leaf_nodes(); others = [n for n in latest if n[0] != 'duel']
        before = others+[('duel', '0013_alternating_turn_rounds')]; after = others+[('duel', '0014_device_input_rounds')]
        executor.migrate(before)
        try:
            models = executor.loader.project_state(before).apps
            hub = models.get_model('games_hub', 'HubSession').objects.create(code='INPUT-MIGRATION')
            step = models.get_model('games_hub', 'HubGameStep').objects.create(session=hub, order=0, game_key='estimation')
            parties = [models.get_model('games_hub', 'HubParticipant').objects.create(session=hub, nickname=n) for n in ('A', 'B')]
            instance = models.get_model('duel', 'DuelGameInstance').objects.create(hub_session=hub, hub_game_step=step, participant_a=parties[0], participant_b=parties[1], game_key='estimation')
            unit = models.get_model('duel', 'DuelUnit').objects.create(game_instance=instance, number=1)
            old = models.get_model('duel', 'DuelSubmission').objects.create(unit=unit, participant=parties[0], submission_kind='answer', payload={'value': 42},
                status='committed', committed_at=timezone.now(), acceptance_sequence=1)
            tables = set(connection.introspection.table_names()); MigrationExecutor(connection).migrate(after)
            self.assertEqual(set(connection.introspection.table_names())-tables, {'duel_duelinputround'})
            row = DuelSubmission.objects.get(pk=old.pk); self.assertEqual(row.input_key, ''); self.assertEqual(row.payload, {'value': 42})
            duplicate = DuelSubmission(unit_id=unit.pk, participant_id=parties[0].pk, submission_kind='answer', status='committed', committed_at=timezone.now(), acceptance_sequence=2)
            with self.assertRaises(IntegrityError), transaction.atomic(): duplicate.save()
            MigrationExecutor(connection).migrate(before); self.assertEqual(set(connection.introspection.table_names()), tables)
            self.assertEqual(models.get_model('duel', 'DuelSubmission').objects.get(pk=old.pk).payload, {'value': 42})
        finally: MigrationExecutor(connection).migrate(latest)


class InputSupabaseTests(SimpleTestCase):
    def test_new_round_and_scoped_inputs_remain_excluded_before_discovery(self):
        config = apps.get_app_config('duel'); self.assertIn(DuelInputRound, config.get_models())
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with patch('games_website.services.apps.get_app_configs', return_value=[config]), patch.object(config, 'get_models', side_effect=AssertionError('must not inspect duel')):
                self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))
