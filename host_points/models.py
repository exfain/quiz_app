import random
import string

from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from games_website.models import SyncBase


class HostPointsGame(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    title = models.CharField(max_length=200, default='Host-Punktevergabe')
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    room_code = models.CharField(max_length=6, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_host_points_games')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    active_hub_session_code = models.CharField(max_length=16, blank=True, default='')
    current_round_number = models.PositiveIntegerField(default=0)

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

    def _get_relevant_step(self, session_code=None):
        from games_hub.models import HubGameStep

        qs = HubGameStep.objects.select_related('session').filter(game_key='host_points', room_code=self.room_code)
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
        should_be_active = bool(
            self.status == 'active'
            and (
                not self.active_hub_session_code
                or self.active_hub_session_code == step.session.code
            )
        )
        participants = []
        for snapshot in snapshots:
            if not snapshot.active_player or not snapshot.included_in_scoring:
                continue
            participant, _ = self.participants.get_or_create(
                name=snapshot.participant.nickname,
                hub_session_code=step.session.code,
                defaults={'is_active': should_be_active},
            )
            if should_be_active and not participant.is_active:
                participant.is_active = True
                participant.save(update_fields=['is_active', 'updated_at'])
            participants.append(participant)
        return participants

    def is_official_participant(self, participant):
        if not participant or participant.quiz_id != self.id:
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

    def get_ordered_participants(self, session_code=None):
        qs = self.participants.all()
        if session_code:
            qs = qs.filter(hub_session_code=session_code)
        return qs.order_by('name')

    def start_quiz(self, session_code=None):
        session_code = session_code or self.active_hub_session_code or ''
        official_participants = self.ensure_snapshot_participants(session_code or None)
        if session_code:
            official_ids = [participant.id for participant in official_participants]
            self.participants.exclude(hub_session_code=session_code).update(is_active=False)
            self.participants.filter(hub_session_code=session_code).exclude(id__in=official_ids).update(is_active=False)
            self.participants.filter(id__in=official_ids).update(total_score=0, is_active=True)
            self.adjustments.filter(hub_session_code=session_code).delete()
        self.status = 'active'
        self.started_at = timezone.now()
        self.ended_at = None
        self.active_hub_session_code = session_code
        self.current_round_number = 1
        self.tutorial_active = False
        self.save(update_fields=[
            'status',
            'started_at',
            'ended_at',
            'active_hub_session_code',
            'current_round_number',
            'tutorial_active',
            'updated_at',
        ])

    def end_quiz(self, status='completed'):
        if self.status == status and self.ended_at:
            return False

        self.status = status
        self.ended_at = timezone.now()
        self.save(update_fields=['status', 'ended_at', 'updated_at'])
        return True

    @transaction.atomic
    def adjust_score(self, participant_id, delta):
        game = HostPointsGame.objects.select_for_update().get(pk=self.pk)
        if game.status != 'active':
            return False, 'Das Spiel ist nicht aktiv.'
        try:
            safe_delta = int(delta)
        except (TypeError, ValueError):
            return False, 'Ungültige Punktezahl.'
        if safe_delta == 0:
            return False, 'Die Punkteänderung darf nicht 0 sein.'

        participant = HostPointsParticipant.objects.select_for_update().filter(
            pk=participant_id,
            quiz=game,
            hub_session_code=game.active_hub_session_code,
        ).first()
        if not game.is_official_participant(participant):
            return False, 'Teilnehmer ist nicht spielberechtigt.'

        participant.total_score += safe_delta
        participant.save(update_fields=['total_score', 'updated_at'])
        HostPointsAdjustment.objects.create(
            quiz=game,
            participant=participant,
            hub_session_code=participant.hub_session_code,
            round_number=game.current_round_number or 1,
            points_delta=safe_delta,
        )
        return True, participant

    @transaction.atomic
    def next_round(self):
        game = HostPointsGame.objects.select_for_update().get(pk=self.pk)
        if game.status != 'active':
            return False
        game.current_round_number = max(game.current_round_number or 0, 1) + 1
        game.save(update_fields=['current_round_number', 'updated_at'])
        return True

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
        round_number = 0 if session_mismatch else self.current_round_number

        participants = []
        for participant in self.get_ordered_participants(session_code or None):
            if not self.is_official_participant(participant):
                continue
            participants.append({
                'id': participant.id,
                'name': participant.name,
                'score': participant.total_score,
                'is_active': participant.is_active,
                'official': self.is_official_participant(participant),
            })
        own = next((p for p in participants if p['name'] == participant_name), None)
        round_scores = []
        if own and not session_mismatch:
            score_totals = {
                row['round_number']: row['points']
                for row in self.adjustments.filter(
                    hub_session_code=session_code,
                    participant_id=own['id'],
                )
                .values('round_number')
                .annotate(points=Sum('points_delta'))
            }
            visible_round_count = max(
                round_number or 0,
                max(score_totals, default=0),
            )
            round_scores = [
                {
                    'number': number,
                    'points': score_totals[number] if number in score_totals else None,
                }
                for number in range(1, visible_round_count + 1)
            ]

        return {
            'game': {
                'id': self.id,
                'title': self.title,
                'room_code': self.room_code,
                'status': game_status,
            },
            'round': {
                'number': round_number or 0,
            },
            'participants': participants,
            'participant': own,
            'round_scores': round_scores,
            'scorebox': {
                'rows': participants,
            },
        }


class HostPointsParticipant(SyncBase):
    quiz = models.ForeignKey(HostPointsGame, related_name='participants', on_delete=models.CASCADE)
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


class HostPointsAdjustment(SyncBase):
    quiz = models.ForeignKey(HostPointsGame, related_name='adjustments', on_delete=models.CASCADE)
    participant = models.ForeignKey(HostPointsParticipant, related_name='adjustments', on_delete=models.CASCADE)
    hub_session_code = models.CharField(max_length=16, blank=True, default='')
    round_number = models.PositiveIntegerField(default=1)
    points_delta = models.IntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['quiz', 'hub_session_code']),
            models.Index(fields=['participant', 'round_number']),
        ]

    def __str__(self):
        return f'{self.participant.name}: {self.points_delta:+d}'
