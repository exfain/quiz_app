import secrets
import string
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F
from django.utils import timezone
from wer_weiss_mehr.models import normalize_answer_text


def room_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


def validate_solutions(value):
    if not isinstance(value, list) or not value:
        raise ValidationError('Mindestens eine Lösung ist erforderlich.')
    seen = set()
    for option in value:
        if (not isinstance(option, dict) or set(option) != {'text', 'aliases'}
                or not isinstance(option['text'], str) or not option['text'].strip()
                or len(option['text']) > 255 or not isinstance(option['aliases'], list)
                or any(not isinstance(a, str) or not a.strip() or len(a) > 255 for a in option['aliases'])):
            raise ValidationError('Lösungen und alternative Schreibweisen müssen nicht leere Texte sein (maximal 255 Zeichen).')
        key = normalize_answer_text(option['text'])
        if not key:
            raise ValidationError('Eine Lösung darf nach der Textnormalisierung nicht leer sein.')
        if key in seen:
            raise ValidationError('Jede Lösung darf nur einmal hinterlegt werden.')
        seen.add(key)


class TargetGame(models.Model):
    title = models.CharField(max_length=200)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    room_code = models.CharField(max_length=16, unique=True, default=room_code)
    scoring_mode = models.CharField(max_length=10, choices=[('simple', 'Einfach'), ('ranking', 'Ranking')])
    answer_seconds = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=12, default='waiting', choices=[(s, s) for s in ('waiting', 'active', 'inactive', 'completed')])
    active_hub_session_code = models.CharField(max_length=16, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(answer_seconds__gt=0), name='target_positive_duration'),
            models.CheckConstraint(condition=models.Q(scoring_mode__in=['simple', 'ranking']), name='target_valid_scoring')]

    @property
    def participants(self):
        return Enrollment.objects.filter(run__game=self).annotate(hub_session_code=F('run__step__session__code'))

    def end_quiz(self):
        from .service import end_from_hub
        end_from_hub(self)


class Task(models.Model):
    game = models.ForeignKey(TargetGame, on_delete=models.CASCADE, related_name='tasks')
    question_text = models.TextField()
    solutions = models.JSONField(validators=[validate_solutions])
    is_active = models.BooleanField(default=True)


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    step = models.OneToOneField('games_hub.HubGameStep', on_delete=models.DO_NOTHING, related_name='target_run')
    game = models.ForeignKey(TargetGame, on_delete=models.PROTECT, related_name='runs')
    scoring_mode = models.CharField(max_length=10)
    answer_seconds = models.PositiveIntegerField()
    text_match_threshold = models.PositiveSmallIntegerField()
    revision = models.PositiveBigIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)


class Enrollment(models.Model):
    run = models.ForeignKey(Run, on_delete=models.PROTECT, related_name='teams')
    participant = models.ForeignKey('games_hub.HubParticipant', on_delete=models.DO_NOTHING)
    total_score = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'participant'], name='target_one_enrollment')]


class Round(models.Model):
    run = models.ForeignKey(Run, on_delete=models.PROTECT, related_name='rounds')
    number = models.PositiveIntegerField()
    task = models.ForeignKey(Task, on_delete=models.PROTECT)
    material = models.JSONField()
    phase = models.CharField(max_length=12, default='nomination')
    started_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'number'], name='target_round_number'),
            models.CheckConstraint(condition=models.Q(number__gt=0), name='target_positive_round'),
            models.CheckConstraint(condition=models.Q(phase__in=['nomination', 'answering', 'review', 'published']), name='target_valid_phase'),
            models.CheckConstraint(condition=(models.Q(phase='nomination', started_at__isnull=True, deadline__isnull=True, published_at__isnull=True)
                | models.Q(phase__in=['answering', 'review'], started_at__isnull=False, deadline__isnull=False, deadline__gt=F('started_at'), published_at__isnull=True)
                | models.Q(phase='published', started_at__isnull=False, deadline__isnull=False, deadline__gt=F('started_at'), published_at__isnull=False)), name='target_phase_clock')]


class TeamRound(models.Model):
    round = models.ForeignKey(Round, on_delete=models.PROTECT, related_name='teams')
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT)
    ready_at = models.DateTimeField(null=True, blank=True)
    target = models.PositiveIntegerField(null=True, blank=True)
    points = models.PositiveIntegerField(default=0)
    achieved = models.BooleanField(default=False)
    correct_count = models.PositiveIntegerField(default=0)
    rank = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['round', 'enrollment'], name='target_one_team_round'),
            models.CheckConstraint(condition=models.Q(target__isnull=True) | models.Q(target__gt=0, ready_at__isnull=False), name='target_valid_goal')]


class Answer(models.Model):
    team = models.ForeignKey(TeamRound, on_delete=models.PROTECT, related_name='answers')
    text = models.CharField(max_length=500)
    matched_index = models.PositiveIntegerField(null=True, blank=True)
    override_index = models.PositiveIntegerField(null=True, blank=True)
    needs_review = models.BooleanField(default=False)
    corrected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    corrected_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(default=timezone.now)


class Receipt(models.Model):
    run = models.ForeignKey(Run, on_delete=models.PROTECT)
    actor_key = models.CharField(max_length=100)
    action_id = models.CharField(max_length=128)
    fingerprint = models.CharField(max_length=64)
    result = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'actor_key', 'action_id'], name='target_idempotent_action')]
