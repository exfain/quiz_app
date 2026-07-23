import random
import string

from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

from games_website.models import SyncBase


class BuzzerGame(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    ROUND_STATE_CHOICES = [
        ('ready', 'Ready'),
        ('open', 'Buzzer open'),
        ('locked', 'Buzz locked'),
        ('answered', 'Answered'),
        ('ended', 'Ended'),
    ]

    title = models.CharField(max_length=200, default='Buzzer')
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    room_code = models.CharField(max_length=6, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_buzzer_games')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    points_per_correct = models.PositiveIntegerField(default=1)
    planned_rounds = models.PositiveIntegerField(null=True, blank=True)
    active_hub_session_code = models.CharField(max_length=16, blank=True, default='')
    current_round_number = models.PositiveIntegerField(default=0)
    round_state = models.CharField(max_length=20, choices=ROUND_STATE_CHOICES, default='ready')
    buzzer_open = models.BooleanField(default=False)
    current_round = models.ForeignKey(
        'BuzzerRound',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    current_buzz_participant = models.ForeignKey(
        'BuzzerParticipant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} ({self.room_code})'

    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)

    @classmethod
    def generate_unique_room_code(cls):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not cls.objects.filter(room_code=code).exists():
                return code

    def get_participant_count(self, session_code=None):
        qs = self.participants.all()
        if session_code:
            qs = qs.filter(hub_session_code=session_code)
        return qs.count()

    def get_active_participants(self, session_code=None):
        qs = self.participants.filter(is_active=True)
        if session_code:
            qs = qs.filter(hub_session_code=session_code)
        return qs

    def get_ordered_participants(self, session_code=None):
        qs = self.participants.all()
        if session_code:
            qs = qs.filter(hub_session_code=session_code)
        return qs.order_by('name')

    def _get_relevant_step(self, session_code=None):
        from games_hub.models import HubGameStep

        qs = HubGameStep.objects.select_related('session').filter(game_key='buzzer', room_code=self.room_code)
        if session_code:
            return qs.filter(session__code=session_code).first()
        if self.active_hub_session_code:
            return qs.filter(session__code=self.active_hub_session_code).first()
        active = qs.filter(session__is_active=True, session__ended_at__isnull=True).order_by('-id').first()
        return active or qs.order_by('-id').first()

    def ensure_snapshot_participants(self, session_code=None):
        step = self._get_relevant_step(session_code)
        if not step:
            return []

        from games_hub.models import HubGameParticipantSnapshot

        snapshots = HubGameParticipantSnapshot.create_for_step(step)
        participants = []
        should_be_active = bool(
            self.status == 'active'
            and (
                not self.active_hub_session_code
                or self.active_hub_session_code == step.session.code
            )
        )
        for snapshot in snapshots:
            if not snapshot.active_player or not snapshot.included_in_scoring:
                continue
            participant, _ = self.participants.get_or_create(
                name=snapshot.participant.nickname,
                hub_session_code=step.session.code,
                defaults={'is_active': should_be_active},
            )
            participants.append(participant)
        return participants

    def is_official_participant(self, participant):
        if not participant or participant.quiz_id != self.id or not participant.is_active:
            return False
        if not participant.hub_session_code:
            return True

        step = self._get_relevant_step(participant.hub_session_code)
        if not step:
            return False
        return step.participant_snapshots.filter(
            participant__nickname=participant.name,
            active_player=True,
            included_in_scoring=True,
        ).exists()

    def start_quiz(self, session_code=None):
        session_code = session_code or self.active_hub_session_code or ''
        self.ensure_snapshot_participants(session_code or None)
        if session_code:
            self.participants.exclude(hub_session_code=session_code).update(is_active=False)
            self.participants.filter(hub_session_code=session_code).update(total_score=0, is_active=True)
        self.status = 'active'
        self.started_at = timezone.now()
        self.ended_at = None
        self.active_hub_session_code = session_code
        self.current_round = None
        self.current_round_number = 0
        self.current_buzz_participant = None
        self.buzzer_open = False
        self.round_state = 'ready'
        self.tutorial_active = False
        self.save(update_fields=[
            'status',
            'started_at',
            'ended_at',
            'active_hub_session_code',
            'current_round',
            'current_round_number',
            'current_buzz_participant',
            'buzzer_open',
            'round_state',
            'tutorial_active',
            'updated_at',
        ])

    def end_quiz(self, status='completed'):
        if self.status == status and self.ended_at:
            return False

        now = timezone.now()
        if self.current_round and self.current_round.status not in {'ended', 'answered'}:
            self.current_round.status = 'ended'
            self.current_round.buzzer_open = False
            self.current_round.ended_at = now
            self.current_round.save(update_fields=['status', 'buzzer_open', 'ended_at', 'updated_at'])
        self.status = status
        self.ended_at = now
        self.buzzer_open = False
        self.round_state = 'ended'
        self.current_buzz_participant = None
        self.save(update_fields=[
            'status',
            'ended_at',
            'buzzer_open',
            'round_state',
            'current_buzz_participant',
            'updated_at',
        ])
        return True

    def start_round(self, session_code=None):
        if self.pk:
            self.refresh_from_db()
        session_code = session_code or self.active_hub_session_code or ''
        if self.status != 'active':
            return None
        if (
            session_code
            and self.active_hub_session_code
            and session_code != self.active_hub_session_code
        ):
            return None
        if self.current_round_id and self.round_state not in {'answered', 'ended'}:
            return None

        last_number = self.rounds.filter(hub_session_code=session_code).aggregate(Max('round_number'))['round_number__max'] or 0
        next_number = last_number + 1
        round_obj = BuzzerRound.objects.create(
            quiz=self,
            hub_session_code=session_code,
            round_number=next_number,
            status='ready',
            started_at=timezone.now(),
        )
        self.current_round = round_obj
        self.current_round_number = next_number
        self.current_buzz_participant = None
        self.buzzer_open = False
        self.round_state = 'ready'
        self.save(update_fields=[
            'current_round',
            'current_round_number',
            'current_buzz_participant',
            'buzzer_open',
            'round_state',
            'updated_at',
        ])
        return round_obj

    @transaction.atomic
    def open_buzzer(self):
        game = BuzzerGame.objects.select_for_update().get(pk=self.pk)
        round_obj = game.current_round
        if not round_obj or game.status != 'active':
            return False
        round_obj = BuzzerRound.objects.select_for_update().get(pk=round_obj.pk)
        if round_obj.status != 'ready' or round_obj.current_buzz_participant_id:
            return False
        round_obj.status = 'open'
        round_obj.buzzer_open = True
        round_obj.current_buzz_participant = None
        round_obj.opened_at = timezone.now()
        round_obj.save(update_fields=[
            'status',
            'buzzer_open',
            'current_buzz_participant',
            'opened_at',
            'updated_at',
        ])
        game.buzzer_open = True
        game.current_buzz_participant = None
        game.round_state = 'open'
        game.save(update_fields=['buzzer_open', 'current_buzz_participant', 'round_state', 'updated_at'])
        return True

    @transaction.atomic
    def accept_buzz(self, participant):
        game = BuzzerGame.objects.select_for_update().get(pk=self.pk)
        if not game.current_round_id:
            return False, 'Keine aktive Runde.'
        round_obj = BuzzerRound.objects.select_for_update().get(pk=game.current_round_id)
        participant = BuzzerParticipant.objects.select_for_update().filter(pk=getattr(participant, 'pk', None)).first()

        if game.status != 'active':
            return False, 'Der Buzzer ist nicht freigegeben.'
        if round_obj.current_buzz_participant_id or game.current_buzz_participant_id:
            return False, 'Ein anderer Teilnehmer war schneller.'
        if round_obj.status != 'open' or not round_obj.buzzer_open:
            return False, 'Der Buzzer ist nicht freigegeben.'
        if not game.is_official_participant(participant):
            return False, 'Teilnehmer ist nicht buzzberechtigt.'
        if (
            participant.hub_session_code != round_obj.hub_session_code
            or (
                game.active_hub_session_code
                and participant.hub_session_code != game.active_hub_session_code
            )
        ):
            return False, 'Teilnehmer ist nicht buzzberechtigt.'
        if BuzzerBuzz.objects.filter(round=round_obj, participant=participant, status='wrong').exists():
            return False, 'Du bist fuer diese Runde gesperrt.'

        sequence = (round_obj.buzzes.aggregate(Max('sequence'))['sequence__max'] or 0) + 1
        buzz = BuzzerBuzz.objects.create(
            quiz=game,
            round=round_obj,
            participant=participant,
            hub_session_code=participant.hub_session_code,
            sequence=sequence,
            status='accepted',
            is_first=True,
        )
        now = timezone.now()
        round_obj.status = 'locked'
        round_obj.buzzer_open = False
        round_obj.current_buzz_participant = participant
        round_obj.locked_at = now
        round_obj.save(update_fields=[
            'status',
            'buzzer_open',
            'current_buzz_participant',
            'locked_at',
            'updated_at',
        ])
        game.buzzer_open = False
        game.round_state = 'locked'
        game.current_buzz_participant = participant
        game.save(update_fields=['buzzer_open', 'round_state', 'current_buzz_participant', 'updated_at'])
        return True, buzz

    @transaction.atomic
    def mark_current_correct(self):
        game = BuzzerGame.objects.select_for_update().get(pk=self.pk)
        if game.status != 'active':
            return False
        if not game.current_round_id or not game.current_buzz_participant_id:
            return False
        round_obj = BuzzerRound.objects.select_for_update().get(pk=game.current_round_id)
        participant = BuzzerParticipant.objects.select_for_update().get(pk=game.current_buzz_participant_id)
        if round_obj.status not in {'locked', 'open'}:
            return False

        participant.total_score += game.points_per_correct
        participant.save(update_fields=['total_score', 'updated_at'])
        round_obj.status = 'answered'
        round_obj.buzzer_open = False
        round_obj.correct_participant = participant
        round_obj.ended_at = timezone.now()
        round_obj.save(update_fields=[
            'status',
            'buzzer_open',
            'correct_participant',
            'ended_at',
            'updated_at',
        ])
        round_obj.buzzes.filter(participant=participant, status='accepted').update(status='correct')
        game.buzzer_open = False
        game.round_state = 'answered'
        game.save(update_fields=['buzzer_open', 'round_state', 'updated_at'])
        return True

    @transaction.atomic
    def mark_current_wrong(self):
        game = BuzzerGame.objects.select_for_update().get(pk=self.pk)
        if game.status != 'active':
            return False
        if not game.current_round_id or not game.current_buzz_participant_id:
            return False
        round_obj = BuzzerRound.objects.select_for_update().get(pk=game.current_round_id)
        participant = BuzzerParticipant.objects.select_for_update().get(pk=game.current_buzz_participant_id)
        round_obj.buzzes.filter(participant=participant, status='accepted').update(status='wrong')

        still_available = game.available_participants(round_obj).exclude(pk=participant.pk).exists()
        round_obj.current_buzz_participant = None
        round_obj.buzzer_open = still_available
        round_obj.status = 'open' if still_available else 'locked'
        round_obj.save(update_fields=['current_buzz_participant', 'buzzer_open', 'status', 'updated_at'])
        game.current_buzz_participant = None
        game.buzzer_open = still_available
        game.round_state = 'open' if still_available else 'locked'
        game.save(update_fields=['current_buzz_participant', 'buzzer_open', 'round_state', 'updated_at'])
        return True

    @transaction.atomic
    def end_current_round(self):
        game = BuzzerGame.objects.select_for_update().get(pk=self.pk)
        if game.status != 'active':
            return False
        if not game.current_round_id:
            return False
        round_obj = BuzzerRound.objects.select_for_update().get(pk=game.current_round_id)
        round_obj.status = 'ended'
        round_obj.buzzer_open = False
        round_obj.ended_at = timezone.now()
        round_obj.save(update_fields=['status', 'buzzer_open', 'ended_at', 'updated_at'])
        game.buzzer_open = False
        game.round_state = 'ended'
        game.current_buzz_participant = None
        game.save(update_fields=['buzzer_open', 'round_state', 'current_buzz_participant', 'updated_at'])
        return True

    def available_participants(self, round_obj=None):
        round_obj = round_obj or self.current_round
        qs = self.participants.filter(is_active=True)
        session_code = self.active_hub_session_code or (round_obj.hub_session_code if round_obj else '')
        if session_code:
            qs = qs.filter(hub_session_code=session_code)
            step = self._get_relevant_step(session_code)
            if step:
                official_names = step.participant_snapshots.filter(
                    active_player=True,
                    included_in_scoring=True,
                ).values_list('participant__nickname', flat=True)
                qs = qs.filter(name__in=official_names)
        if round_obj:
            blocked_ids = round_obj.buzzes.filter(status='wrong').values_list('participant_id', flat=True)
            qs = qs.exclude(id__in=blocked_ids)
        return qs

    def serialize_state(self, session_code=None, participant_name=None):
        if self.pk:
            self.refresh_from_db()
        session_code = session_code or self.active_hub_session_code or ''
        self.ensure_snapshot_participants(session_code or None)
        session_mismatch = bool(
            session_code
            and self.active_hub_session_code
            and session_code != self.active_hub_session_code
        )
        game_status = 'waiting' if session_mismatch else self.status
        round_obj = self.current_round
        round_number = self.current_round_number
        round_state = self.round_state
        buzzer_open = self.buzzer_open
        current_buzz_participant = self.current_buzz_participant
        if session_mismatch or (session_code and round_obj and round_obj.hub_session_code != session_code):
            round_obj = None
            round_number = 0
            round_state = 'ready'
            buzzer_open = False
            current_buzz_participant = None
        blocked_names = []
        buzzes = []
        if round_obj:
            blocked_names = list(
                round_obj.buzzes.filter(status='wrong').values_list('participant__name', flat=True).distinct()
            )
            buzzes = [
                {
                    'participant_name': buzz.participant.name,
                    'status': buzz.status,
                    'sequence': buzz.sequence,
                    'buzzed_at': buzz.buzzed_at.isoformat(),
                }
                for buzz in round_obj.buzzes.select_related('participant').order_by('sequence', 'buzzed_at')
            ]

        participants = []
        for participant in self.get_ordered_participants(session_code or None):
            is_blocked = participant.name in blocked_names
            participants.append({
                'id': participant.id,
                'name': participant.name,
                'score': participant.total_score,
                'is_active': participant.is_active,
                'blocked': is_blocked,
                'has_answer_right': bool(current_buzz_participant and current_buzz_participant.id == participant.id),
                'official': self.is_official_participant(participant),
            })

        current_name = current_buzz_participant.name if current_buzz_participant else None
        own = next((p for p in participants if p['name'] == participant_name), None)
        round_open_for_participant = bool(
            own
            and own['official']
            and not own['blocked']
            and game_status == 'active'
            and buzzer_open
            and not current_buzz_participant
            and round_obj
            and round_obj.status == 'open'
        )

        return {
            'game': {
                'id': self.id,
                'title': self.title,
                'room_code': self.room_code,
                'status': game_status,
                'points_per_correct': self.points_per_correct,
                'planned_rounds': self.planned_rounds,
            },
            'round': {
                'number': round_number,
                'status': round_state,
                'buzzer_open': buzzer_open,
                'current_buzz_participant': current_name,
                'blocked_participants': blocked_names,
                'available_count': self.available_participants(round_obj).count() if round_obj else 0,
                'buzzes': buzzes,
            },
            'participants': participants,
            'participant': own,
            'can_buzz': round_open_for_participant,
        }


class BuzzerParticipant(SyncBase):
    quiz = models.ForeignKey(BuzzerGame, related_name='participants', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    hub_session_code = models.CharField(max_length=16, blank=True, default='')
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(default=timezone.now)
    last_activity = models.DateTimeField(default=timezone.now)
    total_score = models.IntegerField(default=0)

    class Meta:
        unique_together = ('quiz', 'name', 'hub_session_code')
        ordering = ['name']

    def __str__(self):
        return f'{self.name} - {self.quiz.title}'


class BuzzerRound(SyncBase):
    STATUS_CHOICES = BuzzerGame.ROUND_STATE_CHOICES

    quiz = models.ForeignKey(BuzzerGame, related_name='rounds', on_delete=models.CASCADE)
    hub_session_code = models.CharField(max_length=16, blank=True, default='')
    round_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ready')
    buzzer_open = models.BooleanField(default=False)
    current_buzz_participant = models.ForeignKey(
        BuzzerParticipant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_buzzer_rounds',
    )
    correct_participant = models.ForeignKey(
        BuzzerParticipant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='correct_buzzer_rounds',
    )
    started_at = models.DateTimeField(default=timezone.now)
    opened_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('quiz', 'hub_session_code', 'round_number')
        ordering = ['round_number']

    def __str__(self):
        return f'{self.quiz.title} Runde {self.round_number}'


class BuzzerBuzz(SyncBase):
    STATUS_CHOICES = [
        ('accepted', 'Accepted'),
        ('correct', 'Correct'),
        ('wrong', 'Wrong'),
    ]

    quiz = models.ForeignKey(BuzzerGame, related_name='buzzes', on_delete=models.CASCADE)
    round = models.ForeignKey(BuzzerRound, related_name='buzzes', on_delete=models.CASCADE)
    participant = models.ForeignKey(BuzzerParticipant, related_name='buzzes', on_delete=models.CASCADE)
    hub_session_code = models.CharField(max_length=16, blank=True, default='')
    sequence = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='accepted')
    is_first = models.BooleanField(default=False)
    buzzed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['round', 'sequence', 'buzzed_at']
        indexes = [
            models.Index(fields=['quiz', 'hub_session_code']),
            models.Index(fields=['round', 'status']),
        ]

    def __str__(self):
        return f'{self.participant.name} buzzed in round {self.round.round_number}'
