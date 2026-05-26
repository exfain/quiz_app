from django.db import models, transaction
from games_website.models import SyncBase
from django.utils import timezone


class HubSession(SyncBase):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    current_step_index = models.IntegerField(default=0)
    games_weight = models.FloatField(default=2.0)
    scoreboard_visible = models.BooleanField(default=False)

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

    def __str__(self):
        return f"HubSession {self.code}"


class HubParticipant(SyncBase):
    session = models.ForeignKey(HubSession, related_name='participants', on_delete=models.CASCADE)
    nickname = models.CharField(max_length=50)
    joined_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    last_seen = models.DateTimeField(default=timezone.now)
    score_adjustment = models.IntegerField(default=0)

    class Meta:
        unique_together = ('session', 'nickname')

    def __str__(self):
        return f"{self.nickname} ({self.session.code})"


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
    participant_nickname = models.CharField(max_length=50)
    step = models.ForeignKey(HubGameStep, related_name='votes', on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('session', 'participant_nickname')

    def __str__(self):
        return f"{self.participant_nickname} → {self.step} ({self.session.code})"
