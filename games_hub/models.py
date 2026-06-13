from django.db import models, transaction
from games_website.models import SyncBase
from django.utils import timezone


class HubSession(SyncBase):
    CHECK_IN_NOT_STARTED = 'not_started'
    CHECK_IN_OPEN = 'open'
    CHECK_IN_COMPLETED = 'completed'
    CHECK_IN_STATUS_CHOICES = [
        (CHECK_IN_NOT_STARTED, 'Nicht gestartet'),
        (CHECK_IN_OPEN, 'Offen'),
        (CHECK_IN_COMPLETED, 'Abgeschlossen'),
    ]

    VOTING_OFF = 'off'
    VOTING_NORMAL = 'normal'
    VOTING_LOSER = 'loser'
    VOTING_WINNER = 'winner'
    VOTING_MODE_CHOICES = [
        (VOTING_OFF, 'Aus'),
        (VOTING_NORMAL, 'Normales Voting'),
        (VOTING_LOSER, 'Verlierer-Voting'),
        (VOTING_WINNER, 'Sieger-Voting'),
    ]

    OVERALL_SCORING_SIMPLE = 'simple'
    OVERALL_SCORING_RANKING = 'ranking'
    OVERALL_SCORING_CHOICES = [
        (OVERALL_SCORING_SIMPLE, 'Einfach'),
        (OVERALL_SCORING_RANKING, 'Ranking'),
    ]
    OVERALL_WEIGHTING_NONE = 'none'
    OVERALL_WEIGHTING_LINEAR_CAP = 'linear_cap'
    OVERALL_WEIGHTING_CHOICES = [
        (OVERALL_WEIGHTING_NONE, 'Keine Gewichtung'),
        (OVERALL_WEIGHTING_LINEAR_CAP, 'Lineare Gewichtung mit Cap'),
    ]

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    current_step_index = models.IntegerField(default=0)
    games_weight = models.FloatField(default=2.0)
    scoreboard_visible = models.BooleanField(default=False)
    overall_scoring_mode = models.CharField(
        max_length=20,
        choices=OVERALL_SCORING_CHOICES,
        default=OVERALL_SCORING_SIMPLE,
    )
    overall_weighting_mode = models.CharField(
        max_length=20,
        choices=OVERALL_WEIGHTING_CHOICES,
        default=OVERALL_WEIGHTING_NONE,
    )
    weighting_step = models.FloatField(default=0.15)
    weighting_cap = models.FloatField(default=2.0)
    check_in_status = models.CharField(
        max_length=20,
        choices=CHECK_IN_STATUS_CHOICES,
        default=CHECK_IN_NOT_STARTED,
    )
    check_in_started_at = models.DateTimeField(null=True, blank=True)
    check_in_completed_at = models.DateTimeField(null=True, blank=True)
    locked_participant_count = models.PositiveIntegerField(null=True, blank=True)
    voting_mode = models.CharField(
        max_length=20,
        choices=VOTING_MODE_CHOICES,
        default=VOTING_OFF,
    )
    voting_open = models.BooleanField(default=False)
    current_voting_round = models.PositiveIntegerField(default=0)
    voting_started_at = models.DateTimeField(null=True, blank=True)
    voting_closed_at = models.DateTimeField(null=True, blank=True)

    @staticmethod
    def _get_game_model_map():
        from QuizGame.models import Quiz as QuizGameModel
        from Assign.models import AssignQuiz
        from Estimation.models import EstimationQuiz
        from where_is_this.models import WhereQuiz
        from who_is_lying.models import WhoQuiz
        from who_is_that.models import WhoThatQuiz
        from black_jack_quiz.models import BlackJackQuiz
        from clue_rush.models import ClueRushGame
        from sorting_ladder.models import SortingLadderGame
        from wer_weiss_mehr.models import WerWeissMehrGame

        return {
            'quiz': QuizGameModel,
            'assign': AssignQuiz,
            'estimation': EstimationQuiz,
            'where': WhereQuiz,
            'who': WhoQuiz,
            'who_that': WhoThatQuiz,
            'blackjack': BlackJackQuiz,
            'clue_rush': ClueRushGame,
            'sorting_ladder': SortingLadderGame,
            'wer_weiss_mehr': WerWeissMehrGame,
        }

    @classmethod
    def pause_active_games_for_session_codes(cls, session_codes):
        session_codes = [code for code in session_codes if code]
        if not session_codes:
            return

        room_codes_by_game = {}
        for step in HubGameStep.objects.filter(session__code__in=session_codes).exclude(room_code=''):
            room_codes_by_game.setdefault(step.game_key, set()).add(step.room_code)

        for game_key, room_codes in room_codes_by_game.items():
            model = cls._get_game_model_map().get(game_key)
            if not model or not room_codes:
                continue
            model.objects.filter(
                room_code__in=room_codes,
                status='active',
            ).update(status='inactive')

    @classmethod
    def activate_exclusive(cls, session_code: str):
        """Activate exactly one not-ended session and mark all other active sessions as inactive."""
        with transaction.atomic():
            sessions_to_pause = list(
                cls.objects.select_for_update().filter(
                    is_active=True,
                    ended_at__isnull=True
                ).exclude(code=session_code).values_list('code', flat=True)
            )
            cls.objects.filter(
                is_active=True,
                ended_at__isnull=True
            ).exclude(code=session_code).update(is_active=False)
            cls.pause_active_games_for_session_codes(sessions_to_pause)

            session = cls.objects.select_for_update().get(code=session_code)
            if session.ended_at:
                return session
            if not session.started_at:
                session.started_at = timezone.now()
            session.is_active = True
            session.save(update_fields=['started_at', 'is_active'])
            return session

    def get_game_weight(self, game_number):
        if self.overall_weighting_mode != self.OVERALL_WEIGHTING_LINEAR_CAP:
            return 1.0
        number = max(int(game_number or 1), 1)
        weight = 1 + (float(self.weighting_step) * (number - 1))
        return min(weight, float(self.weighting_cap))

    def has_started_game(self):
        model_map = self._get_game_model_map()
        for step in self.steps.exclude(room_code='').order_by('order'):
            model = model_map.get(step.game_key)
            if not model:
                continue
            game = model.objects.filter(room_code=step.room_code).first()
            if not game:
                continue
            if getattr(game, 'status', 'waiting') != 'waiting':
                return True
        return False

    @property
    def scoring_settings_locked(self):
        return self.has_started_game()

    @property
    def check_in_locked(self):
        return self.has_started_game()

    @property
    def check_in_completed(self):
        return self.check_in_status == self.CHECK_IN_COMPLETED

    def get_official_participants(self):
        if self.check_in_completed:
            return self.participants.filter(
                scoring_eligible=True,
                check_in_excluded_by_host=False,
            )
        return self.participants.all()

    def get_locked_participant_count(self):
        if self.locked_participant_count is not None:
            return self.locked_participant_count
        return self.get_official_participants().count()

    def __str__(self):
        return f"HubSession {self.code}"


class HubParticipant(SyncBase):
    session = models.ForeignKey(HubSession, related_name='participants', on_delete=models.CASCADE)
    nickname = models.CharField(max_length=50)
    joined_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    last_seen = models.DateTimeField(default=timezone.now)
    score_adjustment = models.IntegerField(default=0)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    scoring_eligible = models.BooleanField(default=False)
    check_in_excluded_by_host = models.BooleanField(default=False)
    left_permanently_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('session', 'nickname')

    def __str__(self):
        return f"{self.nickname} ({self.session.code})"


class HubGameParticipantSnapshot(SyncBase):
    REASON_ACTIVE = 'active'
    REASON_LEFT_PERMANENTLY = 'left_permanently'
    REASON_LATE_NOT_YET_ELIGIBLE = 'late_not_yet_eligible'
    REASON_EXCLUDED = 'excluded'
    REASON_CHOICES = [
        (REASON_ACTIVE, 'Aktiv'),
        (REASON_LEFT_PERMANENTLY, 'Dauerhaft verlassen'),
        (REASON_LATE_NOT_YET_ELIGIBLE, 'Late Join noch nicht zugelassen'),
        (REASON_EXCLUDED, 'Ausgeschlossen'),
    ]

    session = models.ForeignKey(HubSession, related_name='participant_snapshots', on_delete=models.CASCADE)
    game_step = models.ForeignKey('HubGameStep', related_name='participant_snapshots', on_delete=models.CASCADE)
    participant = models.ForeignKey(HubParticipant, related_name='game_snapshots', on_delete=models.CASCADE)
    included_in_scoring = models.BooleanField(default=True)
    active_player = models.BooleanField(default=True)
    auto_zero = models.BooleanField(default=False)
    reason = models.CharField(max_length=40, choices=REASON_CHOICES, default=REASON_ACTIVE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('game_step', 'participant')
        ordering = ['game_step__order', 'participant__joined_at', 'participant__nickname']

    def __str__(self):
        return f"{self.session.code} step {self.game_step_id}: {self.participant.nickname}"

    @classmethod
    @transaction.atomic
    def create_for_step(cls, step):
        locked_step = HubGameStep.objects.select_related('session').select_for_update().get(pk=step.pk)
        if cls.objects.filter(game_step=locked_step).exists():
            return list(cls.objects.filter(game_step=locked_step).select_related('participant'))

        participants = locked_step.session.get_official_participants().select_for_update().order_by('joined_at', 'nickname')
        rows = []
        for participant in participants:
            if participant.check_in_excluded_by_host:
                continue
            left_permanently = participant.left_permanently_at is not None
            rows.append(cls(
                session=locked_step.session,
                game_step=locked_step,
                participant=participant,
                included_in_scoring=True,
                active_player=not left_permanently,
                auto_zero=left_permanently,
                reason=cls.REASON_LEFT_PERMANENTLY if left_permanently else cls.REASON_ACTIVE,
            ))
        if rows:
            cls.objects.bulk_create(rows)
        return list(cls.objects.filter(game_step=locked_step).select_related('participant'))


class HubGameTutorialRuntime(SyncBase):
    session = models.ForeignKey(HubSession, related_name='game_tutorial_runtimes', on_delete=models.CASCADE)
    game_step = models.OneToOneField('HubGameStep', related_name='tutorial_runtime', on_delete=models.CASCADE)
    active = models.BooleanField(default=False)
    title = models.CharField(max_length=200, blank=True, default='')
    text = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['game_step__order']

    def __str__(self):
        return f"Tutorial {self.session.code} step {self.game_step_id}"


class HubGameTutorialAcknowledgement(SyncBase):
    runtime = models.ForeignKey(
        HubGameTutorialRuntime,
        related_name='acknowledgements',
        on_delete=models.CASCADE,
    )
    snapshot = models.ForeignKey(
        HubGameParticipantSnapshot,
        related_name='tutorial_acknowledgements',
        on_delete=models.CASCADE,
    )
    acknowledged_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('runtime', 'snapshot')
        ordering = ['snapshot__participant__joined_at', 'snapshot__participant__nickname']

    def __str__(self):
        return f"{self.runtime_id}: {self.snapshot.participant.nickname}"


class HubGameUnitTutorialRuntime(SyncBase):
    session = models.ForeignKey(HubSession, related_name='game_unit_tutorial_runtimes', on_delete=models.CASCADE)
    game_step = models.OneToOneField('HubGameStep', related_name='unit_tutorial_runtime', on_delete=models.CASCADE)
    requested = models.BooleanField(default=False)
    tutorial_question_id = models.PositiveIntegerField(null=True, blank=True)
    tutorial_has_been_played = models.BooleanField(default=False)
    current_unit_is_tutorial = models.BooleanField(default=False)
    requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['game_step__order']

    def __str__(self):
        return f"Unit tutorial {self.session.code} step {self.game_step_id}"


class HubGameStep(SyncBase):
    GAME_CHOICES = [
        ('quiz', 'QuizGame'),
        ('assign', 'Assign'),
        ('estimation', 'Estimation'),
        ('where', 'Where Is This'),
        ('who', 'Who Is Lying'),
        ('who_that', 'Who Is That'),
        ('blackjack', 'Black Jack Quiz'),
        ('sorting_ladder', 'Sorting Ladder'),
        ('clue_rush', 'Clue Rush'),
        ('wer_weiss_mehr', 'Wer weiß mehr?'),
    ]
    session = models.ForeignKey(HubSession, related_name='steps', on_delete=models.CASCADE)
    order = models.PositiveIntegerField()
    game_key = models.CharField(max_length=20, choices=GAME_CHOICES)
    room_code = models.CharField(max_length=16, blank=True)
    title = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['order']
        unique_together = ('session', 'order')

    def __str__(self):
        return f"{self.order}: {self.get_game_key_display()} ({self.session.code})"


class GameVote(models.Model):
    session = models.ForeignKey(HubSession, related_name='votes', on_delete=models.CASCADE)
    voting_round = models.PositiveIntegerField(default=0)
    participant_nickname = models.CharField(max_length=50)
    step = models.ForeignKey(HubGameStep, related_name='votes', on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('session', 'voting_round', 'participant_nickname')

    def __str__(self):
        return f"{self.participant_nickname} → {self.step} ({self.session.code})"
