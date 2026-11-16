import math
import random
import string

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone
from games_hub.authoritative_state import (
    attach_snapshot_metadata,
    current_snapshot,
    finish_question_flow,
)
from games_hub.models import GameRuntimeState

from games_website.models import SyncBase


class WannWarDasGame(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    QUESTION_STATE_CHOICES = [
        ('ready', 'Ready'),
        ('active', 'Active'),
        ('revealed', 'Revealed'),
        ('ended', 'Ended'),
    ]

    title = models.CharField(max_length=200, default='Wann war das?')
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=6, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_wann_war_das_games')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    active_hub_session_code = models.CharField(max_length=16, blank=True, default='')
    selected_questions = models.ManyToManyField('WannWarDasQuestion', blank=True, related_name='games')
    tutorial_question = models.ForeignKey(
        'WannWarDasQuestion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tutorial_in_games',
    )
    current_question = models.ForeignKey(
        'WannWarDasQuestion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='current_in_games',
    )
    current_question_number = models.PositiveIntegerField(default=0)
    current_question_is_tutorial = models.BooleanField(default=False)
    question_state = models.CharField(max_length=20, choices=QUESTION_STATE_CHOICES, default='ready')
    question_started_at = models.DateTimeField(null=True, blank=True)
    question_ended_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    question_rounds = models.JSONField(default=list, blank=True)

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

        qs = HubGameStep.objects.select_related('session').filter(game_key='wann_war_das', room_code=self.room_code)
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

    def get_ordered_questions(self, session_code=None, include_tutorial=False):
        tutorial_ids = set()
        if not include_tutorial:
            try:
                from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids

                tutorial_ids = get_scorebox_excluded_tutorial_question_ids(
                    'wann_war_das',
                    self.room_code,
                    session_code,
                )
            except Exception:
                tutorial_ids = {self.tutorial_question_id} if self.tutorial_question_id else set()

        selected = list(self.selected_questions.filter(is_active=True))
        order_ids = []
        for raw_id in self.question_order or []:
            try:
                order_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        if order_ids:
            order_map = {question_id: index for index, question_id in enumerate(order_ids)}
            selected.sort(key=lambda question: order_map.get(question.id, len(order_ids)))
        return [question for question in selected if include_tutorial or question.id not in tutorial_ids]

    @transaction.atomic
    def start_quiz(self, session_code=None):
        self.refresh_from_db()
        session_code = session_code if session_code is not None else self.active_hub_session_code or ''
        if self.started_at and self.active_hub_session_code == session_code:
            if self.status == 'inactive':
                return self.set_paused(False)
            if self.status in {'active', 'completed'}:
                return False
        self.ensure_snapshot_participants(session_code or None)
        self.participants.filter(hub_session_code=session_code).update(total_score=0, is_active=True)
        self.status = 'active'
        self.started_at = timezone.now()
        self.ended_at = None
        self.active_hub_session_code = session_code
        self.current_question = None
        self.current_question_number = 0
        self.current_question_is_tutorial = False
        self.question_state = 'ready'
        self.question_started_at = None
        self.question_ended_at = None
        self.paused_at = None
        self.question_rounds = []
        self.tutorial_active = False
        self.save()
        return True

    @transaction.atomic
    def set_paused(self, paused, *, at=None):
        from games_hub.authoritative_state import get_runtime_state
        game = type(self).objects.select_for_update().get(pk=self.pk)
        if paused and game.status != 'active' or not paused and game.status != 'inactive':
            return False
        now = at or timezone.now()
        runtime = get_runtime_state('wann_war_das', game.room_code, game.active_hub_session_code)
        runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
        public = dict(runtime.public_snapshot or {})
        if paused:
            game.paused_at = now
            game.status = 'inactive'
            public['_paused_at'] = now.isoformat()
        else:
            delta = max(now - game.paused_at, timezone.timedelta(0)) if game.paused_at else timezone.timedelta(0)
            for field in ('question_started_at', 'question_ended_at'):
                if getattr(game, field):
                    setattr(game, field, getattr(game, field) + delta)
            for field in ('question_presented_at', 'content_revealed_at', 'starts_at', 'ends_at'):
                if getattr(runtime, field):
                    setattr(runtime, field, getattr(runtime, field) + delta)
            game.paused_at = None
            game.status = 'active'
            public.pop('_paused_at', None)
        runtime.public_snapshot = public
        runtime.state_revision += 1
        runtime.context_revision = runtime.state_revision
        runtime.save()
        game.save(update_fields=['status', 'paused_at', 'question_started_at', 'question_ended_at', 'updated_at'])
        self.refresh_from_db()
        return True

    def get_current_round_number(self):
        return len(self.question_rounds or [])

    def get_question_progress(self):
        played = [entry for entry in self.question_rounds or [] if not entry.get('is_tutorial')]
        selected = self.get_ordered_questions(self.active_hub_session_code)
        seen = {entry.get('question_id') for entry in played}
        return {'kind': 'question', 'current': len(played) if self.current_question_id and not self.current_question_is_tutorial else None,
                'total': len(played) + sum(question.id not in seen for question in selected)}

    def prepare_question(self, question, session_code=None, is_tutorial_round=False):
        if self.status != 'active':
            return False
        if (
            session_code
            and self.active_hub_session_code
            and session_code != self.active_hub_session_code
        ):
            return False
        self.current_question = question
        self.question_rounds = list(self.question_rounds or []) + [{
            'question_id': question.id, 'is_tutorial': bool(is_tutorial_round),
            'revealed': False, 'max_points': question.max_points,
        }]
        self.current_question_is_tutorial = bool(is_tutorial_round)
        if not is_tutorial_round:
            self.current_question_number = (self.current_question_number or 0) + 1
        self.question_state = 'ready'
        self.question_started_at = None
        self.question_ended_at = None
        self.save(update_fields=[
            'current_question',
            'current_question_number',
            'current_question_is_tutorial',
            'question_rounds',
            'question_state',
            'question_started_at',
            'question_ended_at',
            'updated_at',
        ])
        return True

    def open_answering(self, question, *, started_at):
        if (
            self.status != 'active'
            or self.current_question_id != question.id
            or self.question_state not in {'ready', 'active'}
        ):
            return False
        if self.question_state == 'active' and self.question_started_at:
            return True
        self.question_state = 'active'
        self.question_started_at = started_at
        self.question_ended_at = None
        self.save(update_fields=[
            'question_state',
            'question_started_at',
            'question_ended_at',
            'updated_at',
        ])
        return True

    def start_question(self, question, session_code=None, is_tutorial_round=False):
        """Compatibility path for legacy callers that still start immediately."""
        if not self.prepare_question(question, session_code, is_tutorial_round):
            return False
        return self.open_answering(question, started_at=timezone.now())

    @transaction.atomic
    def reveal_current_question(self, session_code=None, *, at=None):
        game = type(self).objects.select_for_update().select_related('current_question').get(pk=self.pk)
        if not game.current_question_id or game.question_state == 'revealed':
            return False
        now = at or game.paused_at or timezone.now()
        if game.question_started_at:
            deadline = game.question_started_at + timezone.timedelta(seconds=game.current_question.get_effective_time_limit())
            effective_end = min(now, deadline)
            for pending in game.pending_inputs.filter(round_number=game.get_current_round_number(),
                    question=game.current_question, hub_session_code=game.active_hub_session_code).select_related('participant'):
                game.submit_answer(pending.participant, pending.raw_answer, submitted_at=effective_end, finalizing=True)
        game.question_state = 'revealed'
        game.question_ended_at = now
        rounds = list(game.question_rounds or [])
        if rounds:
            rounds[-1] = {**rounds[-1], 'revealed': True}
        game.question_rounds = rounds
        game.save(update_fields=['question_state', 'question_ended_at', 'question_rounds', 'updated_at'])
        phase = current_snapshot('wann_war_das', game.room_code, session_code or game.active_hub_session_code)
        if phase.get('question_flow_mode') == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE and phase.get('question_phase') == GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN:
            finish_question_flow(game_key='wann_war_das', room_code=game.room_code,
                session_code=session_code or game.active_hub_session_code, question_id=game.current_question_id)
        self.refresh_from_db()
        return True

    @transaction.atomic
    def end_quiz(self, status='completed'):
        self.refresh_from_db()
        if status == 'inactive':
            return self.set_paused(True)
        if self.status == status and self.ended_at:
            return False
        if self.current_question_id:
            self.reveal_current_question(self.active_hub_session_code)
        self.status = status
        self.ended_at = timezone.now()
        self.paused_at = None
        # Keep the revealed question until the participant acknowledges it.
        self.save(update_fields=['status', 'ended_at', 'paused_at', 'updated_at'])
        return True

    @transaction.atomic
    def store_pending_answer(self, participant, raw_answer, question_id, round_number):
        game = type(self).objects.select_for_update().select_related('current_question').get(pk=self.pk)
        if not isinstance(raw_answer, str) or len(raw_answer) > 120 or not participant:
            return False
        if (game.status != 'active' or game.paused_at or game.question_state != 'active'
                or str(game.current_question_id) != str(question_id)
                or str(game.get_current_round_number()) != str(round_number)
                or not game.is_official_participant(participant)):
            return False
        if not game.question_started_at or timezone.now() >= game.question_started_at + timezone.timedelta(seconds=game.current_question.get_effective_time_limit()):
            return False
        if game.answers.filter(participant=participant, round_number=game.get_current_round_number()).exists():
            return False
        WannWarDasPendingInput.objects.update_or_create(quiz=game, participant=participant,
            hub_session_code=participant.hub_session_code, round_number=game.get_current_round_number(),
            defaults={'question_id': game.current_question_id, 'raw_answer': raw_answer.strip()})
        return True

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

    @transaction.atomic
    def submit_answer(self, participant, raw_answer, submitted_at=None, *, finalizing=False):
        submitted_at = submitted_at or timezone.now()
        if participant is None:
            return None, 'Teilnehmer ist nicht spielberechtigt.'
        game = (
            type(self).objects.select_for_update()
            .select_related('current_question')
            .get(pk=self.pk)
        )
        participant = WannWarDasParticipant.objects.select_for_update().get(
            pk=participant.pk,
            quiz=game,
        )
        if game.status not in ({'active', 'inactive'} if finalizing else {'active'}) or game.question_state != 'active' or not game.current_question_id:
            return None, 'Die Frage ist nicht aktiv.'
        if not game.is_official_participant(participant) and not finalizing:
            return None, 'Teilnehmer ist nicht spielberechtigt.'
        if game.question_started_at:
            if submitted_at < game.question_started_at:
                return None, 'Die Frage ist noch nicht freigegeben.'
            elapsed = max(0.0, (submitted_at - game.question_started_at).total_seconds())
            if not finalizing and elapsed >= game.current_question.get_effective_time_limit():
                return None, 'Die Frage ist bereits beendet.'
        if game.question_ended_at and submitted_at >= game.question_ended_at:
            return None, 'Die Frage ist bereits beendet.'
        existing = WannWarDasAnswer.objects.filter(
            quiz=game,
            participant=participant,
            question=game.current_question,
            round_number=game.get_current_round_number(),
            hub_session_code=participant.hub_session_code,
        ).first()
        if existing:
            return existing, 'Antwort wurde bereits abgegeben.'

        evaluation = game.current_question.evaluate_answer(
            raw_answer,
            submitted_at=submitted_at,
            started_at=game.question_started_at,
        )
        if game.current_question_is_tutorial:
            evaluation['points_earned'] = 0

        answer, created = WannWarDasAnswer.objects.get_or_create(
            quiz=game,
            participant=participant,
            question=game.current_question,
            round_number=game.get_current_round_number(),
            hub_session_code=participant.hub_session_code,
            defaults={
                'raw_answer': '' if raw_answer is None else str(raw_answer).strip(),
                'user_answer': evaluation['user_answer'],
                'is_numeric': evaluation['is_numeric'],
                'absolute_deviation': evaluation['absolute_deviation'],
                'tolerance_at_submit': evaluation['current_tolerance'],
                'step_index': evaluation['step_index'],
                'points_earned': evaluation['points_earned'],
                'is_correct': evaluation['is_correct'],
                'time_taken': evaluation['elapsed_time'],
                'is_tutorial': game.current_question_is_tutorial,
                'submitted_at': submitted_at,
            },
        )
        if not created:
            return answer, 'Antwort wurde bereits abgegeben.'
        game.pending_inputs.filter(participant=participant, round_number=game.get_current_round_number()).delete()
        participant.calculate_score()
        return answer, None

    def serialize_state(self, session_code=None, participant_name=None, *, for_host=False):
        if self.pk:
            self.refresh_from_db()
        session_code = session_code or self.active_hub_session_code or ''
        self.ensure_snapshot_participants(session_code or None)
        mismatch = bool(session_code and self.active_hub_session_code and session_code != self.active_hub_session_code)
        question = None if mismatch else self.current_question
        revealed = bool(question and self.question_state == 'revealed')
        round_number = self.get_current_round_number() if not mismatch else 0
        now = self.paused_at or timezone.now()
        timer = question.get_timer_state(self.question_started_at, now) if question and self.question_started_at and self.question_state == 'active' else None
        current_answers = list(self.answers.filter(question=question, round_number=round_number,
            hub_session_code=session_code).select_related('participant', 'question')) if question else []
        own_answer = next((answer for answer in current_answers if answer.participant.name == participant_name), None)
        pending = self.pending_inputs.filter(participant__name=participant_name, round_number=round_number,
            hub_session_code=session_code).first() if question and participant_name and not own_answer else None
        scorebox = self.build_scorebox(session_code, for_host=for_host)
        hidden_points = {answer.participant_id: answer.points_earned for answer in current_answers} if not revealed and not for_host else {}
        participants = [{'id': p.id, 'name': p.name,
            'score': p.total_score - hidden_points.get(p.id, 0), 'is_active': p.is_active,
            'official': self.is_official_participant(p)} for p in self.get_ordered_participants(session_code or None)]
        own = next((p for p in participants if p['name'] == participant_name), None)
        answers = current_answers if for_host or revealed else [answer for answer in current_answers if answer.participant.name == participant_name]
        question_payload = question.to_dict(reveal=revealed or for_host) if question else None
        state = {
            'game': {'id': self.id, 'title': self.title, 'room_code': self.room_code, 'status': 'waiting' if mismatch else self.status},
            'question': question_payload, 'question_state': 'ready' if mismatch else self.question_state,
            'question_number': 0 if mismatch else self.current_question_number,
            'progress': self.get_question_progress() if not mismatch else {'kind': 'question', 'current': None, 'total': len(self.get_ordered_questions(session_code))},
            'current_round_id': round_number, 'is_tutorial_round': self.current_question_is_tutorial,
            'started_at': self.question_started_at.isoformat() if question and self.question_started_at else None,
            'ends_at': (self.question_started_at + timezone.timedelta(seconds=question.get_effective_time_limit())).isoformat()
                if question and self.question_started_at and self.question_state == 'active' else None,
            'ended_at': self.question_ended_at.isoformat() if question and self.question_ended_at else None,
            'timer': timer, 'paused': bool(self.paused_at) if not mismatch else False,
            'participants': participants, 'answers': [a.to_dict(reveal=revealed or for_host) for a in answers],
            'response_count': len(current_answers), 'scorebox': scorebox, 'participant': own,
            'own_answer': own_answer.to_dict(reveal=revealed or for_host) if own_answer else None,
            'pending_answer': pending.raw_answer if pending else None,
            'can_answer': bool(own and own['official'] and not mismatch and self.status == 'active' and not self.paused_at
                and self.question_state == 'active' and question and not own_answer and timer and timer['remaining_seconds'] > 0),
            'server_now': timezone.now().isoformat(),
        }
        state['_revision_state'] = {'game': state['game'],
            'question': question.to_dict(reveal=revealed) if question else None,
            'question_state': state['question_state'], 'question_number': state['question_number'],
            'current_round_id': round_number, 'response_count': len(current_answers),
            'scorebox': self.build_scorebox(session_code, for_host=False)}
        return attach_snapshot_metadata(state, game_key='wann_war_das', room_code=self.room_code, session_code=session_code)

    def build_scorebox(self, session_code=None, *, for_host=False):
        history = [(i, entry) for i, entry in enumerate(self.question_rounds or [], 1) if not entry.get('is_tutorial')]
        seen = {entry.get('question_id') for _, entry in history}
        rounds = [{'question_id': entry.get('question_id'), 'round_number': physical,
            'max_points': entry.get('max_points', 0), 'revealed': entry.get('revealed', False)} for physical, entry in history]
        rounds.extend({'question_id': q.id, 'round_number': None, 'max_points': q.max_points, 'revealed': False}
            for q in self.get_ordered_questions(session_code) if q.id not in seen)
        all_answers = list(self.answers.filter(hub_session_code=session_code or '', is_tutorial=False))
        rows = []
        for participant in self.get_ordered_participants(session_code or None):
            entries = []
            total = 0
            for index, entry in enumerate(rounds, 1):
                answer = next((a for a in all_answers if a.participant_id == participant.id
                    and a.question_id == entry['question_id'] and a.round_number == entry['round_number']), None)
                visible = bool(for_host or entry['revealed'])
                if answer and visible:
                    total += answer.points_earned
                entries.append({'question_id': entry['question_id'], 'round_number': entry['round_number'],
                    'number': index, 'points': answer.points_earned if answer and visible else None,
                    'correct': answer.is_correct if answer and visible else None,
                    'status': 'played' if entry['revealed'] else 'current' if entry['round_number'] == self.get_current_round_number() and self.current_question_id else 'upcoming'})
            rows.append({'participant_id': participant.id, 'participant_name': participant.name, 'total_score': total, 'entries': entries})
        return {'questions': [{'id': entry['question_id'], 'number': i, 'round_number': entry['round_number'], 'max_points': entry['max_points']} for i, entry in enumerate(rounds, 1)], 'rows': rows}



class WannWarDasQuestion(SyncBase):
    question_text = models.TextField()
    correct_answer = models.FloatField()
    unit = models.CharField(max_length=40, blank=True, default='Jahr')
    start_tolerance = models.FloatField(default=0, validators=[MinValueValidator(0)])
    tolerance_increment = models.FloatField(default=1)
    seconds_per_step = models.PositiveIntegerField(default=5)
    max_tolerance = models.FloatField(default=10, validators=[MinValueValidator(0)])
    max_points = models.PositiveIntegerField(default=10)
    min_points = models.PositiveIntegerField(default=1)
    time_limit = models.PositiveIntegerField(null=True, blank=True)
    explanation = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_wann_war_das_questions')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.question_text[:80]

    def clean(self):
        errors = {}
        if self.start_tolerance < 0:
            errors['start_tolerance'] = 'Starttoleranz darf nicht negativ sein.'
        if self.tolerance_increment <= 0:
            errors['tolerance_increment'] = 'Toleranzsteigerung muss groesser als 0 sein.'
        if self.seconds_per_step <= 0:
            errors['seconds_per_step'] = 'Schrittzeit muss groesser als 0 sein.'
        if self.max_tolerance < self.start_tolerance:
            errors['max_tolerance'] = 'Maximale Toleranz muss >= Starttoleranz sein.'
        if self.max_points < self.min_points:
            errors['max_points'] = 'Maximalpunkte muessen >= Minimalpunkte sein.'
        if self.min_points < 0:
            errors['min_points'] = 'Minimalpunkte duerfen nicht negativ sein.'
        if self.time_limit is not None and self.time_limit <= 0:
            errors['time_limit'] = 'Fragedauer muss groesser als 0 sein.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def max_steps(self):
        if self.max_tolerance <= self.start_tolerance:
            return 0
        return int(math.ceil((self.max_tolerance - self.start_tolerance) / self.tolerance_increment))

    def step_index_for_elapsed(self, elapsed_time):
        try:
            elapsed = max(0.0, float(elapsed_time or 0))
        except (TypeError, ValueError):
            elapsed = 0.0
        return int(math.floor(elapsed / max(1, self.seconds_per_step)))

    def tolerance_for_step(self, step_index):
        return min(
            float(self.start_tolerance) + max(0, int(step_index)) * float(self.tolerance_increment),
            float(self.max_tolerance),
        )

    def points_for_step(self, step_index):
        max_steps = self.max_steps()
        current_step = min(max(0, int(step_index)), max_steps)
        if max_steps <= 0:
            return int(self.max_points)
        drop = math.ceil((int(self.max_points) - int(self.min_points)) * (current_step / max_steps))
        return max(int(self.min_points), int(self.max_points) - drop)

    def get_timer_state(self, started_at, now=None):
        now = now or timezone.now()
        elapsed = max(0.0, (now - started_at).total_seconds()) if started_at else 0.0
        step_index = self.step_index_for_elapsed(elapsed)
        next_step_at = (step_index + 1) * self.seconds_per_step
        time_until_next_step = max(0, int(math.ceil(next_step_at - elapsed)))
        time_limit = self.get_effective_time_limit()
        remaining = max(0, int(math.ceil(time_limit - elapsed))) if time_limit else None
        return {
            'elapsed_seconds': elapsed,
            'step_index': step_index,
            'current_tolerance': self.tolerance_for_step(step_index),
            'current_points': self.points_for_step(step_index),
            'time_until_next_step': time_until_next_step,
            'time_limit': time_limit,
            'remaining_seconds': remaining,
        }

    def get_effective_time_limit(self):
        if self.time_limit:
            return int(self.time_limit)
        return (self.max_steps() + 1) * int(self.seconds_per_step)

    def evaluate_answer(self, raw_answer, submitted_at, started_at):
        raw = '' if raw_answer is None else str(raw_answer).strip()
        elapsed = max(0.0, (submitted_at - started_at).total_seconds()) if started_at else 0.0
        step_index = self.step_index_for_elapsed(elapsed)
        current_tolerance = self.tolerance_for_step(step_index)
        try:
            user_answer = float(raw)
            is_numeric = math.isfinite(user_answer)
            if not is_numeric:
                user_answer = None
        except (TypeError, ValueError):
            user_answer = None
            is_numeric = False
        deviation = abs(user_answer - float(self.correct_answer)) if user_answer is not None else None
        is_correct = bool(is_numeric and deviation is not None and deviation <= current_tolerance)
        return {
            'user_answer': user_answer,
            'is_numeric': is_numeric,
            'absolute_deviation': deviation,
            'step_index': step_index,
            'current_tolerance': current_tolerance,
            'is_correct': is_correct,
            'points_earned': self.points_for_step(step_index) if is_correct else 0,
            'elapsed_time': elapsed,
        }

    def format_number(self, value):
        if value is None:
            return ''
        value = float(value)
        if value.is_integer():
            return str(int(value))
        return f'{value:.2f}'.rstrip('0').rstrip('.')

    def formatted_correct_answer(self):
        unit = (self.unit or '').strip()
        value = self.format_number(self.correct_answer)
        return f'{value} {unit}'.strip()

    def to_dict(self, reveal=True):
        result = {
            'id': self.id,
            'question_text': self.question_text,
            'correct_answer': self.correct_answer,
            'formatted_correct_answer': self.formatted_correct_answer(),
            'unit': self.unit,
            'start_tolerance': self.start_tolerance,
            'tolerance_increment': self.tolerance_increment,
            'seconds_per_step': self.seconds_per_step,
            'max_tolerance': self.max_tolerance,
            'max_points': self.max_points,
            'min_points': self.min_points,
            'time_limit': self.get_effective_time_limit(),
            'explanation': self.explanation,
        }
        if not reveal:
            for field in ('correct_answer', 'formatted_correct_answer', 'explanation'):
                result.pop(field, None)
        return result


class WannWarDasParticipant(SyncBase):
    quiz = models.ForeignKey(WannWarDasGame, related_name='participants', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    hub_session_code = models.CharField(max_length=16, blank=True, default='')
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(default=timezone.now)
    last_activity = models.DateTimeField(default=timezone.now)
    total_score = models.IntegerField(default=0)

    class Meta:
        unique_together = ('quiz', 'name', 'hub_session_code')
        ordering = ['-total_score', 'name']

    def __str__(self):
        return f'{self.name} - {self.quiz.title}'

    def calculate_score(self):
        total = sum(
            answer.points_earned
            for answer in self.answers.filter(is_tutorial=False)
        )
        self.total_score = int(total)
        self.save(update_fields=['total_score', 'updated_at'])
        return self.total_score


class WannWarDasAnswer(SyncBase):
    quiz = models.ForeignKey(WannWarDasGame, related_name='answers', on_delete=models.CASCADE)
    participant = models.ForeignKey(WannWarDasParticipant, related_name='answers', on_delete=models.CASCADE)
    question = models.ForeignKey(WannWarDasQuestion, related_name='answers', on_delete=models.CASCADE)
    hub_session_code = models.CharField(max_length=16, blank=True, default='')
    round_number = models.PositiveIntegerField(default=0)
    raw_answer = models.CharField(max_length=120, blank=True, default='')
    user_answer = models.FloatField(null=True, blank=True)
    is_numeric = models.BooleanField(default=False)
    absolute_deviation = models.FloatField(null=True, blank=True)
    tolerance_at_submit = models.FloatField(default=0)
    step_index = models.PositiveIntegerField(default=0)
    points_earned = models.IntegerField(default=0)
    is_correct = models.BooleanField(default=False)
    time_taken = models.FloatField(default=0)
    is_tutorial = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('quiz', 'participant', 'question', 'round_number', 'hub_session_code')
        ordering = ['submitted_at', 'id']
        indexes = [
            models.Index(fields=['quiz', 'hub_session_code']),
            models.Index(fields=['question', 'hub_session_code']),
        ]

    def __str__(self):
        return f'{self.participant.name}: {self.raw_answer} ({self.points_earned})'

    def to_dict(self, reveal=False):
        return {
            'id': self.id,
            'participant_id': self.participant_id,
            'participant_name': self.participant.name,
            'question_id': self.question_id,
            'raw_answer': self.raw_answer,
            'user_answer': self.user_answer,
            'formatted_answer': self.question.format_number(self.user_answer) if self.user_answer is not None else self.raw_answer,
            'is_numeric': self.is_numeric,
            'absolute_deviation': self.absolute_deviation if reveal else None,
            'tolerance_at_submit': self.tolerance_at_submit,
            'step_index': self.step_index,
            'points_earned': self.points_earned if reveal else None,
            'is_correct': self.is_correct if reveal else None,
            'revealed': reveal,
            'time_taken': self.time_taken,
            'submitted_at': self.submitted_at.isoformat() if self.submitted_at else None,
            'is_tutorial': self.is_tutorial,
        }



class WannWarDasPendingInput(SyncBase):
    quiz = models.ForeignKey(WannWarDasGame, related_name='pending_inputs', on_delete=models.CASCADE)
    participant = models.ForeignKey(WannWarDasParticipant, related_name='pending_inputs', on_delete=models.CASCADE)
    question = models.ForeignKey(WannWarDasQuestion, on_delete=models.CASCADE)
    hub_session_code = models.CharField(max_length=16, blank=True, default='')
    round_number = models.PositiveIntegerField(default=0)
    raw_answer = models.CharField(max_length=120, blank=True, default='')

    class Meta:
        unique_together = ('quiz', 'participant', 'round_number', 'hub_session_code')
