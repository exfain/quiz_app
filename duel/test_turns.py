"""Real Hub/HTTP identities, durable turns, scoring and SQLite competition."""
import json
from io import StringIO
from unittest.mock import patch
from uuid import uuid4
from django.apps import apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from games_hub.models import HubParticipant
from games_hub.lobby_join import issue_rejoin_token
from games_website.services import sync_all_models_to_supabase, restore_all_models_from_supabase
from Assign.models import AssignQuiz, AssignQuestion
from sorting_ladder.models import SortingLadderGame, SortingQuestion, SortingItem
from wer_weiss_mehr.models import WerWeissMehrGame, WerWeissMehrQuestion, WerWeissMehrAnswerOption
from . import test_oral as reference
from . import test_submissions_concurrency as concurrency
from .commands import ActorContext, ActionConflict, CommandRejected
from .controllers import turns
from .turn_material import board, exhausted
from .models import (DuelTurnRound, DuelDecision, DuelScoreEntry, DuelUnitOutcome,
    DuelActionReceipt, ReceiptActorType, ReceiptStatus)


def make_material(case, key):
    if not hasattr(case, 'user'): case.user = User.objects.create_superuser('turn-host', '', 'pw')
    if key == 'assign':
        case.game = AssignQuiz.objects.create(creator=case.user, title='Assign Duel')
        case.question = AssignQuestion.objects.create(created_by=case.user, question_text='Ordne zu',
            left_items=['A', 'B', 'C', 'D'], right_items=['Alpha', 'Beta', 'Gamma', 'Delta'],
            correct_matches={'0': 0, '1': 1, '2': 2, '3': 3})
    elif key == 'sorting_ladder':
        case.game = SortingLadderGame.objects.create(creator=case.user, title='Sorting Duel')
        case.question = SortingQuestion.objects.create(created_by=case.user, question_text='Klein nach groß', description='Aufsteigend')
        elements = [SortingItem.objects.create(topic=case.question, text=n, correct_rank=i) for i, n in enumerate(['Alpha', 'Beta', 'Gamma', 'Delta'])]
        case.question.starting_item = elements[1]; case.question.save()
    else:
        case.game = WerWeissMehrGame.objects.create(creator=case.user, title='Wer weiß mehr Duel')
        case.question = WerWeissMehrQuestion.objects.create(created_by=case.user, question_text='Nenne die Begriffe')
        for text in ['Secret Delta', 'Secret Alpha', 'Secret Gamma', 'Secret Beta']:
            WerWeissMehrAnswerOption.objects.create(question=case.question, canonical_text=text)
    case.game.selected_questions.add(case.question)


def correct_selection(key, state):
    progress = board(key, state.material, state.actions)
    if key == 'assign':
        left = next(i for i in state.material['solution'] if i not in progress)
        return {'kind': 'pair', 'left_id': int(left), 'right_id': state.material['solution'][left]}
    if key == 'sorting_ladder':
        solution = state.material['solution']; item = next(i for i in solution if i not in progress)
        position = sum(solution.index(p) < solution.index(item) for p in progress)
        return {'kind': 'place', 'item_id': item, 'position': position}
    return {'kind': 'answer', 'answer_id': next(a['id'] for a in state.material['answers'] if a['id'] not in progress)}


class TurnFixture(reference.OralFixture):
    key = 'assign'

    def setUp(self):
        with patch('duel.test_oral.make_material', make_material): super().setUp()

    def start(self, side=0):
        self.prepare()
        response = self.post({'action': 'start', 'starting_participant_id': self.parties[side].pk})
        self.assertEqual(response.status_code, 200, response.content)
        return DuelTurnRound.objects.get(unit=self.unit)

    def record(self, selection=None, expected=None, key=None):
        state = DuelTurnRound.objects.get(unit=self.unit)
        return self.post({'action': 'record', 'expected_turn': expected or len(state.actions)+1,
            'selection': selection if selection is not None else correct_selection(self.key, state)}, key=key)

    def finish_error(self):
        self.assertEqual(self.record({'kind': 'wrong'}).status_code, 200)
        self.assertEqual(self.post({'action': 'show_result'}).status_code, 200)


class TurnRules:
    def test_turns_alternate_and_error_awards_only_opponent_after_reveal(self):
        state = self.start()
        for side in (0, 1):
            self.assertEqual(self.snapshot()['current_participant_id'], self.parties[side].pk)
            response = self.record(); self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.snapshot()['current_participant_id'], self.parties[0].pk)
        self.assertFalse(DuelScoreEntry.objects.exists()); self.assertFalse(DuelUnitOutcome.objects.exists())
        self.assertEqual(self.record({'kind': 'wrong'}).status_code, 200)
        self.assertEqual(self.snapshot()['phase'], 'awaiting_result'); self.assertNotIn('solution', self.snapshot())
        self.assertEqual(self.record().status_code, 409)
        self.assertEqual(self.post({'action': 'show_result'}).status_code, 200)
        self.assertEqual(self.snapshot()['points'], {'a': 0, 'b': 1})
        self.assertEqual(self.unit.outcome.winner_id, self.parties[1].pk)
        self.assertEqual(DuelDecision.objects.count(), 3)
        self.assertEqual(self.post({'action': 'complete'}).status_code, 200)
        self.unit.refresh_from_db(); self.assertEqual(self.unit.status, 'completed')

    def test_retries_and_stale_turn_cannot_advance_another_party(self):
        self.start(); selection = correct_selection(self.key, DuelTurnRound.objects.get(unit=self.unit))
        self.instance.refresh_from_db(); before = self.instance.state_revision
        first = self.record(selection, 1, 'same'); self.assertEqual(first.status_code, 200)
        self.assertTrue(self.record(selection, 1, 'same').json()['action']['replayed'])
        self.assertEqual(self.record({'kind': 'wrong'}, 1, 'same').status_code, 409)
        self.assertEqual(self.record(selection, 1, 'new').json()['action']['code'], 'stale_turn')
        self.instance.refresh_from_db(); self.assertEqual(self.instance.state_revision, before+1)
        self.assertEqual(DuelDecision.objects.count(), 1)

    def test_completed_task_has_no_winner_zero_points_and_can_finish(self):
        state = self.start()
        while not exhausted(self.key, state.material, state.actions):
            self.assertEqual(self.record().status_code, 200); state.refresh_from_db()
        data = self.snapshot(); self.assertEqual(data['phase'], 'awaiting_result')
        self.assertNotIn('solution', data)
        self.assertEqual(self.post({'action': 'show_result'}).status_code, 200)
        self.assertEqual(self.snapshot()['points'], {'a':0, 'b':0})
        self.assertEqual(self.snapshot()['result_reason'], 'task_fully_solved')
        self.assertEqual(self.post({'action': 'complete'}).status_code, 200)
        self.assertEqual(DuelScoreEntry.objects.count(), 2); self.assertEqual(DuelUnitOutcome.objects.get(unit=self.unit).outcome, 'no_winner')

    def test_participants_spectator_and_third_party_cannot_record_or_start(self):
        self.start()
        self.assertEqual(self.post({'action': 'record', 'expected_turn': 1, 'selection': {'kind': 'wrong'}}, side=0).status_code, 403)
        self.assertEqual(self.spectator.post(self.url('spectator'), data=json.dumps({'action': 'show_result'}), content_type='application/json').status_code, 403)
        other = HubParticipant.objects.create(session=self.hub, nickname='Other')
        client = Client(HTTP_AUTHORIZATION='Bearer '+issue_rejoin_token(other))
        self.assertEqual(client.get(self.url('participant', match=True)).status_code, 403)
        self.assertEqual(self.post({'action': 'record', 'expected_turn': 1, 'selection': {'kind': 'wrong'}, 'participant_id': self.parties[1].pk}).status_code, 400)
        self.assertEqual(DuelDecision.objects.count(), 0)

    def test_snapshot_read_is_write_free_and_source_edits_do_not_change_task(self):
        state = self.start()
        self.question.question_text = 'Changed source'; self.question.save()
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries: after = self.snapshot()
        self.assertEqual(before, after)
        self.assertTrue(all(not q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertNotEqual(before['question'], 'Changed source'); self.assertNotIn('solution', before)

    def test_result_retry_is_atomic_and_does_not_double_book(self):
        self.start(); self.record({'kind': 'wrong'})
        response = self.post({'action': 'show_result'}, key='release'); self.assertEqual(response.status_code, 200)
        self.instance.refresh_from_db(); revision = self.instance.state_revision
        self.assertTrue(self.post({'action': 'show_result'}, key='release').json()['action']['replayed'])
        self.assertEqual(self.post({'action': 'show_result'}).status_code, 409)
        self.instance.refresh_from_db(); self.assertEqual(self.instance.state_revision, revision)
        self.assertEqual(DuelScoreEntry.objects.count(), 2); self.assertEqual(DuelUnitOutcome.objects.count(), 1)

    test_multiplayer_session_with_two_people_keeps_original_start = reference.ClueDuelTests.test_multiplayer_session_with_two_people_keeps_original_start


class AssignTurnTests(TurnRules, TurnFixture):
    def test_existing_string_match_indices_are_normalized_without_source_changes(self):
        original = {str(i): str(i) for i in range(4)}
        self.question.correct_matches = original; self.question.save()
        state = self.start()
        right_id = next(r['id'] for r in state.material['right'] if r['text'] == 'Alpha')
        self.assertEqual(self.record({'kind': 'pair', 'left_id': 0, 'right_id': right_id}).status_code, 200)
        self.assertEqual(self.snapshot()['confirmed'], {'0': right_id})
        self.question.refresh_from_db(); self.assertEqual(self.question.correct_matches, original)

    def test_source_pair_is_correct_even_after_right_side_shuffle(self):
        state = self.start()
        right_id = next(r['id'] for r in state.material['right'] if r['text'] == 'Alpha')
        self.assertEqual(self.record({'kind': 'pair', 'left_id': 0, 'right_id': right_id}).status_code, 200)
        self.assertEqual(self.snapshot()['confirmed'], {'0': right_id})
        state.refresh_from_db(); self.assertTrue(state.actions[0]['correct'])

    def test_start_requires_explicit_valid_party_and_cannot_be_restarted(self):
        self.prepare()
        self.assertEqual(self.post({'action': 'start', 'starting_participant_id': 0}).status_code, 409)
        response = self.post({'action': 'start', 'starting_participant_id': self.parties[1].pk}, key='start')
        self.assertEqual(response.status_code, 200)
        state = DuelTurnRound.objects.get(unit=self.unit); started = state.started_at
        self.assertTrue(self.post({'action': 'start', 'starting_participant_id': self.parties[1].pk}, key='start').json()['action']['replayed'])
        self.assertEqual(self.post({'action': 'start', 'starting_participant_id': self.parties[0].pk}).status_code, 409)
        state.refresh_from_db(); self.assertEqual(state.started_at, started)
        self.assertEqual(self.snapshot()['current_participant_id'], self.parties[1].pk)

    def test_frozen_material_and_existing_turn_history_cannot_be_changed(self):
        state = self.start(); self.record(); state.refresh_from_db()
        state.material['question'] = 'Altered'
        with self.assertRaises(ValidationError): state.full_clean()
        state.refresh_from_db(); state.actions[0]['party_id'] = self.parties[1].pk
        with self.assertRaises(ValidationError): state.full_clean()
        state.refresh_from_db(); state.starting_participant = self.parties[1]
        with self.assertRaises(ValidationError): state.full_clean()

    def test_foreign_question_is_rejected_without_leaving_a_prepared_unit(self):
        self.game.selected_questions.clear()
        response = self.post({'action': 'prepare', 'question_id': self.question.pk, 'previous_unit_id': None}, match=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['action']['code'], 'question_not_in_quiz')
        self.assertFalse(self.instance.units.exists())

    def test_wrong_pair_really_ends_round(self):
        state = self.start(); selection = correct_selection(self.key, state)
        selection['right_id'] = (selection['right_id']+1) % len(state.material['right'])
        self.assertEqual(self.record(selection).status_code, 200)
        self.assertEqual(self.snapshot()['phase'], 'awaiting_result'); self.assertEqual(self.snapshot()['confirmed'], {})

    def test_repeated_pair_is_rejected_not_scored_as_a_new_turn(self):
        state = self.start(); selection = correct_selection(self.key, state)
        self.record(selection); self.assertEqual(self.record(selection).status_code, 409)
        self.assertEqual(self.snapshot()['turn'], 2)

    def test_exception_and_database_failure_roll_back_decisions_and_result(self):
        self.start()
        with patch.object(DuelTurnRound, 'save', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError): turns.record_turn(instance_id=self.instance.pk, unit_id=self.unit.pk,
                actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id='fail', expected_turn=1, selection={'kind': 'wrong'})
        self.assertFalse(DuelDecision.objects.exists()); self.assertFalse(DuelActionReceipt.objects.filter(client_action_id='fail').exists())
        self.record({'kind': 'wrong'}); self.instance.refresh_from_db(); revision = self.instance.state_revision
        with patch.object(DuelUnitOutcome, 'save', side_effect=IntegrityError('failure')):
            with self.assertRaises(IntegrityError): turns.advance(instance_id=self.instance.pk, unit_id=self.unit.pk,
                actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id='fail-result', action='show_result')
        self.assertFalse(DuelScoreEntry.objects.exists()); self.assertFalse(DuelUnitOutcome.objects.exists())
        self.instance.refresh_from_db(); self.assertEqual(self.instance.state_revision, revision)

    def test_old_unit_and_new_instance_are_isolated(self):
        self.start(); old = self.unit; self.finish_error(); self.post({'action': 'complete'})
        self.start()
        with self.assertRaises(CommandRejected):
            turns.record_turn(instance_id=self.instance.pk, unit_id=old.pk,
                actor=ActorContext(ReceiptActorType.HOST, self.user.pk), client_action_id='old', expected_turn=1, selection={'kind': 'wrong'})
        self.assertEqual(DuelTurnRound.objects.get(unit=self.unit).actions, [])

    def test_database_guards_unit_identity_phase_and_start_context(self):
        self.prepare(); state = DuelTurnRound.objects.get(unit=self.unit)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DuelTurnRound.objects.create(unit=self.unit, source_question_id=self.question.pk, material=state.material)
        for update in ({'phase': 'invalid'}, {'phase': 'playing'}, {'source_question_id': 0}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                DuelTurnRound.objects.filter(pk=state.pk).update(**update)


class SortingTurnTests(TurnRules, TurnFixture):
    key = 'sorting_ladder'

    def test_named_elements_are_checked_against_actual_source_ranks(self):
        state = self.start(); ids = {item['text']: item['id'] for item in state.material['items']}
        self.assertEqual(self.snapshot()['confirmed'], [ids['Beta']])
        self.assertEqual(self.record({'kind': 'place', 'item_id': ids['Alpha'], 'position': 0}).status_code, 200)
        self.assertEqual(self.snapshot()['confirmed'], [ids['Alpha'], ids['Beta']])
        self.assertEqual(self.record({'kind': 'place', 'item_id': ids['Delta'], 'position': 0}).status_code, 200)
        self.assertEqual(self.snapshot()['phase'], 'awaiting_result')
        self.assertEqual(self.snapshot()['confirmed'], [ids['Alpha'], ids['Beta']])

    def test_incorrect_insertion_does_not_publish_the_complete_order(self):
        state = self.start(); valid = correct_selection(self.key, state)
        valid['position'] = 1-valid['position']
        self.assertEqual(self.record(valid).status_code, 200)
        data = self.snapshot(); self.assertEqual(data['confirmed'], state.material['initial']); self.assertNotIn('solution', data)
        self.assertEqual(data['phase'], 'awaiting_result')
        self.post({'action': 'show_result'}); self.assertEqual(self.snapshot()['solution'], state.material['solution'])


class KnowledgeTurnTests(TurnRules, TurnFixture):
    key = 'wer_weiss_mehr'

    def test_frozen_cards_follow_existing_alphabetical_convention(self):
        self.start()
        self.assertEqual([c['text'] for c in self.snapshot('host')['cards']],
            ['Secret Alpha', 'Secret Beta', 'Secret Delta', 'Secret Gamma'])

    def test_only_revealed_cards_are_public_and_cannot_be_counted_twice(self):
        state = self.start(); public = self.snapshot(); private = self.snapshot('host')
        self.assertTrue(all(c['text'] is None for c in public['cards']))
        self.assertTrue(all(c['text'] for c in private['cards']))
        selection = correct_selection(self.key, state); self.record(selection)
        cards = self.snapshot()['cards']; self.assertEqual(sum(c['text'] is not None for c in cards), 1)
        self.assertEqual(self.record(selection).status_code, 409)
        self.assertEqual(DuelDecision.objects.count(), 1)


class TurnConcurrencyTests(TurnFixture):
    parallel = concurrency.SubmissionConcurrencyTests.parallel

    def test_different_action_ids_for_same_turn_apply_only_once(self):
        state = self.start(); self.instance.refresh_from_db(); revision = self.instance.state_revision
        actor = ActorContext(ReceiptActorType.HOST, self.user.pk)
        results = self.parallel([lambda i=i: turns.record_turn(instance_id=self.instance.pk, unit_id=self.unit.pk,
            actor=actor, client_action_id=f'parallel-{i}', expected_turn=1, selection=correct_selection(self.key, state)) for i in range(2)])
        self.assertEqual(sum(r.status == ReceiptStatus.APPLIED for r in results), 1)
        self.assertEqual(DuelDecision.objects.count(), 1); self.instance.refresh_from_db(); self.assertEqual(self.instance.state_revision, revision+1)

    def test_conflicting_retries_and_parallel_results_never_double_book(self):
        state = self.start(); actor = ActorContext(ReceiptActorType.HOST, self.user.pk)
        args = dict(instance_id=self.instance.pk, unit_id=self.unit.pk, actor=actor, client_action_id='same', expected_turn=1)
        results = self.parallel([lambda: turns.record_turn(**args, selection=correct_selection(self.key, state)),
            lambda: turns.record_turn(**args, selection={'kind': 'wrong'})])
        self.assertEqual(sum(isinstance(r, ActionConflict) for r in results), 1)
        if DuelTurnRound.objects.get(unit=self.unit).phase == 'playing': self.record({'kind': 'wrong'})
        results = self.parallel([lambda i=i: turns.advance(instance_id=self.instance.pk, unit_id=self.unit.pk,
            actor=actor, client_action_id=f'result-{i}', action='show_result') for i in range(2)])
        self.assertEqual(sum(r.status == ReceiptStatus.APPLIED for r in results), 1)
        self.assertEqual(DuelScoreEntry.objects.count(), 2); self.assertEqual(DuelUnitOutcome.objects.count(), 1)


class TurnMigrationTests(TransactionTestCase):
    def test_additive_round_trip_only_adds_turn_table(self):
        executor = MigrationExecutor(connection); latest = executor.loader.graph.leaf_nodes()
        other = [n for n in latest if n[0] != 'duel']
        before = other+[('duel', '0012_oral_buzzer_rounds')]; after = other+[('duel', '0013_alternating_turn_rounds')]
        migration = executor.loader.get_migration('duel', '0013_alternating_turn_rounds')
        self.assertEqual(migration.dependencies, [('duel', '0012_oral_buzzer_rounds')]); self.assertEqual(len(migration.operations), 1)
        executor.migrate(before)
        try:
            tables = set(connection.introspection.table_names()); MigrationExecutor(connection).migrate(after)
            self.assertEqual(set(connection.introspection.table_names())-tables, {'duel_duelturnround'})
            MigrationExecutor(connection).migrate(before); self.assertEqual(set(connection.introspection.table_names()), tables)
        finally: MigrationExecutor(connection).migrate(latest)


class TurnSupabaseTests(SimpleTestCase):
    def test_turn_round_is_excluded_before_sync_and_restore_model_discovery(self):
        config = apps.get_app_config('duel'); self.assertIn(DuelTurnRound, config.get_models())
        for service in (sync_all_models_to_supabase, restore_all_models_from_supabase):
            with patch('games_website.services.apps.get_app_configs', return_value=[config]), patch.object(config, 'get_models', side_effect=AssertionError('must not inspect duel')):
                self.assertEqual(service(stdout=StringIO(), stderr=StringIO()), (0, []))
