from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class RoundSubmissionConstraintMigrationTests(TransactionTestCase):
    migrate_from = ('sorting_ladder', '0013_persist_pending_round_selection')
    migrate_to = ('sorting_ladder', '0014_roundsubmission_round_number')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        User = old_apps.get_model('auth', 'User')
        Game = old_apps.get_model('sorting_ladder', 'SortingLadderGame')
        Participant = old_apps.get_model('sorting_ladder', 'SortingLadderParticipant')
        Question = old_apps.get_model('sorting_ladder', 'SortingQuestion')
        Submission = old_apps.get_model('sorting_ladder', 'RoundSubmission')

        user = User.objects.create(username='sorting-migration-owner')
        game = Game.objects.create(
            title='Migration game',
            room_code='MIGR',
            creator_id=user.pk,
        )
        question = Question.objects.create(
            question_text='Migration question',
            created_by_id=user.pk,
        )
        participant = Participant.objects.create(quiz_id=game.pk, name='Alice')
        self.first_submission_id = Submission.objects.create(
            quiz_id=game.pk,
            participant_id=participant.pk,
            question_id=question.pk,
            all_elements=[11, 12],
            is_correct=True,
        ).pk
        Submission.objects.create(
            quiz_id=game.pk,
            participant_id=participant.pk,
            question_id=question.pk,
            all_elements=[21, 22],
            is_correct=False,
        )
        Submission.objects.create(
            quiz_id=game.pk,
            participant_id=participant.pk,
            question_id=question.pk,
            all_elements=[31, 32, 33],
            is_correct=True,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_duplicate_inferred_round_keeps_earliest_final_submission(self):
        Submission = self.apps.get_model('sorting_ladder', 'RoundSubmission')
        submissions = list(Submission.objects.order_by('round_number', 'id'))

        self.assertEqual(len(submissions), 2)
        self.assertEqual([submission.round_number for submission in submissions], [1, 2])
        self.assertEqual(submissions[0].pk, self.first_submission_id)
        self.assertEqual(submissions[0].all_elements, [11, 12])


class SortingLadderScoreMigrationTests(TransactionTestCase):
    migrate_from = ('sorting_ladder', '0014_roundsubmission_round_number')
    migrate_to = ('sorting_ladder', '0015_recalculate_one_point_round_scores')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        User = old_apps.get_model('auth', 'User')
        Game = old_apps.get_model('sorting_ladder', 'SortingLadderGame')
        Participant = old_apps.get_model('sorting_ladder', 'SortingLadderParticipant')

        user = User.objects.create(username='sorting-score-migration-owner')
        game = Game.objects.create(
            title='Legacy ten-point score',
            room_code='SMIG',
            creator_id=user.pk,
        )
        self.participant_id = Participant.objects.create(
            quiz_id=game.pk,
            name='Alice',
            rounds_survived=3,
            total_score=30,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_score_is_repaired_from_survived_rounds(self):
        Participant = self.apps.get_model('sorting_ladder', 'SortingLadderParticipant')
        participant = Participant.objects.get(pk=self.participant_id)

        self.assertEqual(participant.rounds_survived, 3)
        self.assertEqual(participant.total_score, 3)
