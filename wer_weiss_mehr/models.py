import random
import re
import string
import unicodedata

from django.contrib.auth.models import User
from django.db import models, transaction
from django.utils import timezone

from games_website.models import SyncBase


def normalize_answer_text(value):
    """Controlled normalization for exact free-text matching and aliases."""
    text = str(value or '').strip().lower()
    text = (
        text
        .replace('ä', 'ae')
        .replace('ö', 'oe')
        .replace('ü', 'ue')
        .replace('ß', 'ss')
    )
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[-–—_/]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class WerWeissMehrGame(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    title = models.CharField(max_length=200, default='Wer weiß mehr')
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_wer_weiss_mehr_games')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    max_participants = models.IntegerField(default=50)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey(
        'WerWeissMehrQuestion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_in_games',
    )
    selected_questions = models.ManyToManyField('WerWeissMehrQuestion', blank=True, related_name='games')
    tutorial_question = models.ForeignKey('WerWeissMehrQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='tutorial_in_games')

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)

    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not WerWeissMehrGame.objects.filter(room_code=code).exists():
                return code

    @transaction.atomic
    def start_quiz(self, hub_session_code=None):
        if self.status != 'active' or self.started_at is None:
            self.status = 'active'
            self.started_at = timezone.now()
            self.ended_at = None
            self.current_question = None
            self.save(update_fields=['status', 'started_at', 'ended_at', 'current_question'])
            session, _ = WerWeissMehrSession.objects.get_or_create(quiz=self)
            session.phase = WerWeissMehrSession.PHASE_IDLE
            session.current_round = 0
            session.round_start_time = None
            session.round_end_time = None
            session.completed_question_ids = []
            session.save(update_fields=[
                'phase',
                'current_round',
                'round_start_time',
                'round_end_time',
                'completed_question_ids',
            ])
            session.revealed_answers.clear()

            participants = self.participants.all()
            if hub_session_code is not None:
                participants = participants.filter(hub_session_code=hub_session_code)
            participant_ids = list(participants.values_list('id', flat=True))
            WerWeissMehrParticipantState.objects.filter(
                quiz=self,
                participant_id__in=participant_ids,
            ).delete()
            WerWeissMehrRoundResponse.objects.filter(
                quiz=self,
                participant_id__in=participant_ids,
            ).delete()
            WerWeissMehrPendingInput.objects.filter(
                quiz=self,
                participant_id__in=participant_ids,
            ).delete()
            participants.update(total_score=0)
            WerWeissMehrRound.objects.filter(quiz=self).delete()

    def set_inactive(self):
        self.status = 'inactive'
        self.save(update_fields=['status'])

    def end_quiz(self, status='completed'):
        self.status = status
        self.ended_at = timezone.now()
        self.tutorial_active = False
        self.save(update_fields=['status', 'ended_at', 'tutorial_active'])
        session = getattr(self, 'session', None)
        if session:
            session.phase = WerWeissMehrSession.PHASE_SET_COMPLETED
            session.round_start_time = None
            session.round_end_time = None
            session.save(update_fields=['phase', 'round_start_time', 'round_end_time'])

    def __str__(self):
        return f"{self.title} ({self.room_code})"


class WerWeissMehrQuestion(SyncBase):
    question_text = models.TextField()
    round_time_limit = models.PositiveIntegerField(default=30)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_wer_weiss_mehr_questions')
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.question_text

    def recalculate_answer_sort_order(self):
        answers = sorted(
            self.answers.all(),
            key=lambda answer: (normalize_answer_text(answer.canonical_text), answer.canonical_text.lower(), answer.id),
        )
        for index, answer in enumerate(answers, start=1):
            if answer.sort_order != index:
                answer.sort_order = index
                answer.save(update_fields=['sort_order'])

    def find_matching_answer(self, answer_text):
        normalized = normalize_answer_text(answer_text)
        if not normalized:
            return None

        answers = list(self.answers.all())
        for answer in answers:
            if normalized == answer.normalized_text:
                return answer
            aliases = answer.aliases if isinstance(answer.aliases, list) else []
            if normalized in {normalize_answer_text(alias) for alias in aliases}:
                return answer
        return None


class WerWeissMehrAnswerOption(SyncBase):
    question = models.ForeignKey(WerWeissMehrQuestion, on_delete=models.CASCADE, related_name='answers')
    canonical_text = models.CharField(max_length=255)
    normalized_text = models.CharField(max_length=255, db_index=True, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'canonical_text', 'id']
        unique_together = ['question', 'normalized_text']

    def save(self, *args, **kwargs):
        self.normalized_text = normalize_answer_text(self.canonical_text)
        if not isinstance(self.aliases, list):
            self.aliases = []
        super().save(*args, **kwargs)

    def __str__(self):
        return self.canonical_text


class WerWeissMehrParticipant(SyncBase):
    quiz = models.ForeignKey(WerWeissMehrGame, on_delete=models.CASCADE, related_name='participants')
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    total_score = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)

    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['-total_score', 'name']

    def recalculate_total_score(self):
        from games_hub.unit_tutorial_runtime import get_unit_tutorial_state

        tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', self.quiz.room_code, self.hub_session_code)
        tutorial_question_id = tutorial_state.get('tutorial_question_id') if tutorial_state.get('requested') else None
        states = WerWeissMehrParticipantState.objects.filter(quiz=self.quiz, participant=self)
        if tutorial_question_id:
            states = states.exclude(question_id=tutorial_question_id)
        total = (
            states
            .aggregate(total=models.Sum('survived_rounds'))
            .get('total') or 0
        )
        self.total_score = total
        self.save(update_fields=['total_score'])
        return total

    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class WerWeissMehrSession(SyncBase):
    PHASE_IDLE = 'idle'
    PHASE_ROUND_ACTIVE = 'round_active'
    PHASE_REVIEW = 'review'
    PHASE_SET_COMPLETED = 'set_completed'
    PHASE_CHOICES = [
        (PHASE_IDLE, 'Idle'),
        (PHASE_ROUND_ACTIVE, 'Round active'),
        (PHASE_REVIEW, 'Review'),
        (PHASE_SET_COMPLETED, 'Set completed'),
    ]

    quiz = models.OneToOneField(WerWeissMehrGame, on_delete=models.CASCADE, related_name='session')
    current_round = models.PositiveIntegerField(default=0)
    phase = models.CharField(max_length=24, choices=PHASE_CHOICES, default=PHASE_IDLE)
    round_start_time = models.DateTimeField(null=True, blank=True)
    round_end_time = models.DateTimeField(null=True, blank=True)
    time_limit_seconds = models.PositiveIntegerField(default=30)
    revealed_answers = models.ManyToManyField(WerWeissMehrAnswerOption, blank=True, related_name='revealed_in_sessions')
    completed_question_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['quiz_id']

    @transaction.atomic
    def start_set(self, question, participants_qs=None, time_limit_seconds=None):
        prepared_round = self.prepare_set(
            question,
            participants_qs=participants_qs,
            time_limit_seconds=time_limit_seconds,
        )
        if not prepared_round:
            return None
        return self.open_prepared_round()

    @transaction.atomic
    def prepare_set(self, question, participants_qs=None, time_limit_seconds=None):
        self.quiz.current_question = question
        self.quiz.save(update_fields=['current_question'])

        WerWeissMehrParticipantState.objects.filter(quiz=self.quiz, question=question).delete()
        WerWeissMehrRoundResponse.objects.filter(quiz=self.quiz, question=question).delete()
        WerWeissMehrPendingInput.objects.filter(quiz=self.quiz, question=question).delete()
        WerWeissMehrRound.objects.filter(quiz=self.quiz, question=question).delete()

        self.revealed_answers.clear()
        self.current_round = 0
        self.phase = self.PHASE_IDLE
        self.time_limit_seconds = int(time_limit_seconds or question.round_time_limit or 30)
        self.round_start_time = None
        self.round_end_time = None
        self.save(update_fields=['current_round', 'phase', 'time_limit_seconds', 'round_start_time', 'round_end_time'])

        participants = participants_qs if participants_qs is not None else self.quiz.participants.filter(is_active=True)
        states = [
            WerWeissMehrParticipantState(
                quiz=self.quiz,
                participant=participant,
                question=question,
            )
            for participant in participants
        ]
        if states:
            WerWeissMehrParticipantState.objects.bulk_create(states, ignore_conflicts=True)

        return self.prepare_next_round()

    @transaction.atomic
    def start_next_round(self):
        prepared_round = self.prepare_next_round()
        if not prepared_round:
            return None
        return self.open_prepared_round()

    @transaction.atomic
    def prepare_next_round(self):
        question = self.quiz.current_question
        if not question:
            self.phase = self.PHASE_IDLE
            self.current_round = 0
            self.round_start_time = None
            self.round_end_time = None
            self.save(update_fields=['phase', 'current_round', 'round_start_time', 'round_end_time'])
            return None

        if self.get_active_state_count() <= 0 or self.get_hidden_answer_count() <= 0:
            self.phase = self.PHASE_SET_COMPLETED
            self.round_start_time = None
            self.round_end_time = None
            self.save(update_fields=['phase', 'round_start_time', 'round_end_time'])
            self._mark_current_question_completed()
            return None

        self.current_round += 1
        self.phase = self.PHASE_IDLE
        self.round_start_time = None
        self.round_end_time = None
        self.save(update_fields=['current_round', 'phase', 'round_start_time', 'round_end_time'])
        return self.current_round

    @transaction.atomic
    def open_prepared_round(self, started_at=None):
        question = self.quiz.current_question
        if not question or self.current_round <= 0:
            return None
        if self.phase == self.PHASE_ROUND_ACTIVE:
            return WerWeissMehrRound.objects.filter(
                quiz=self.quiz,
                question=question,
                round_number=self.current_round,
            ).first()
        if self.phase != self.PHASE_IDLE:
            return None
        if self.get_active_state_count() <= 0 or self.get_hidden_answer_count() <= 0:
            self.phase = self.PHASE_SET_COMPLETED
            self.round_start_time = None
            self.round_end_time = None
            self.save(update_fields=['phase', 'round_start_time', 'round_end_time'])
            self._mark_current_question_completed()
            return None

        now = started_at or timezone.now()
        self.phase = self.PHASE_ROUND_ACTIVE
        self.round_start_time = now
        self.round_end_time = now + timezone.timedelta(seconds=self.time_limit_seconds)
        self.save(update_fields=['phase', 'round_start_time', 'round_end_time'])

        revealed_ids = list(self.revealed_answers.filter(question=question).values_list('id', flat=True))
        round_state, _ = WerWeissMehrRound.objects.update_or_create(
            quiz=self.quiz,
            question=question,
            round_number=self.current_round,
            defaults={
                'status': WerWeissMehrRound.STATUS_ACTIVE,
                'started_at': now,
                'ended_at': None,
                'revealed_answer_ids_at_start': revealed_ids,
            },
        )
        return round_state

    @transaction.atomic
    def end_current_round(self):
        question = self.quiz.current_question
        if not question or self.phase != self.PHASE_ROUND_ACTIVE:
            return None

        round_state = WerWeissMehrRound.objects.filter(
            quiz=self.quiz,
            question=question,
            round_number=self.current_round,
        ).first()
        if not round_state:
            return None

        active_states = WerWeissMehrParticipantState.objects.select_related('participant').filter(
            quiz=self.quiz,
            question=question,
            is_eliminated=False,
            participant__is_active=True,
        )
        for state in active_states:
            response, _ = WerWeissMehrRoundResponse.objects.get_or_create(
                quiz=self.quiz,
                participant=state.participant,
                question=question,
                round_number=self.current_round,
                defaults={'answer_text': ''},
            )
            if not response.answer_text:
                pending = WerWeissMehrPendingInput.objects.filter(
                    quiz=self.quiz,
                    participant=state.participant,
                    question=question,
                    round_number=self.current_round,
                ).first()
                if pending:
                    response.answer_text = pending.answer_text
            response.evaluate(revealed_before_round_ids=round_state.revealed_answer_ids_at_start)

        round_state.status = WerWeissMehrRound.STATUS_REVIEW
        round_state.ended_at = timezone.now()
        round_state.save(update_fields=['status', 'ended_at'])
        self.phase = self.PHASE_REVIEW
        self.round_start_time = None
        self.round_end_time = None
        self.save(update_fields=['phase', 'round_start_time', 'round_end_time'])
        self.sync_review_scores()
        return round_state

    @transaction.atomic
    def sync_review_scores(self):
        question = self.quiz.current_question
        if not question or self.phase != self.PHASE_REVIEW:
            return None

        round_state = WerWeissMehrRound.objects.filter(
            quiz=self.quiz,
            question=question,
            round_number=self.current_round,
        ).first()
        if not round_state or round_state.status == WerWeissMehrRound.STATUS_COMPLETED:
            return round_state

        correct_answer_ids = set()
        response_map = {
            response.participant_id: response
            for response in WerWeissMehrRoundResponse.objects.filter(
                quiz=self.quiz,
                question=question,
                round_number=self.current_round,
            ).select_related('matched_answer')
        }

        states = WerWeissMehrParticipantState.objects.select_related('participant').filter(
            quiz=self.quiz,
            question=question,
        )
        for state in states:
            response = response_map.get(state.participant_id)
            if response and response.is_correct:
                state.is_eliminated = False
                state.survived_rounds = max(state.survived_rounds, self.current_round)
                state.last_status = response.final_status
                state.save(update_fields=['is_eliminated', 'survived_rounds', 'last_status'])
                if response.matched_answer_id:
                    correct_answer_ids.add(response.matched_answer_id)
            elif response:
                state.is_eliminated = True
                state.last_status = response.final_status
                state.save(update_fields=['is_eliminated', 'last_status'])
            elif not state.is_eliminated:
                state.is_eliminated = True
                state.last_status = WerWeissMehrRoundResponse.STATUS_WRONG
                state.save(update_fields=['is_eliminated', 'last_status'])
            else:
                continue
            state.participant.recalculate_total_score()

        if correct_answer_ids:
            self.revealed_answers.add(*WerWeissMehrAnswerOption.objects.filter(id__in=correct_answer_ids))
        return round_state

    @transaction.atomic
    def finalize_review(self, advance=True, open_round=True):
        question = self.quiz.current_question
        if not question or self.phase != self.PHASE_REVIEW:
            return None

        round_state = self.sync_review_scores()
        if not round_state or round_state.status == WerWeissMehrRound.STATUS_COMPLETED:
            return round_state

        round_state.status = WerWeissMehrRound.STATUS_COMPLETED
        round_state.save(update_fields=['status'])

        if not advance or self.get_active_state_count() <= 0 or self.get_hidden_answer_count() <= 0:
            self.phase = self.PHASE_SET_COMPLETED
            self.round_start_time = None
            self.round_end_time = None
            self.save(update_fields=['phase', 'round_start_time', 'round_end_time'])
            self._mark_current_question_completed()
            return round_state
        if open_round:
            return self.start_next_round()
        return self.prepare_next_round()

    @transaction.atomic
    def finish_current_set(self):
        self.phase = self.PHASE_SET_COMPLETED
        self.round_start_time = None
        self.round_end_time = None
        self.save(update_fields=['phase', 'round_start_time', 'round_end_time'])
        self._mark_current_question_completed()

    @transaction.atomic
    def clear_current_set(self):
        self.quiz.current_question = None
        self.quiz.save(update_fields=['current_question'])
        self.current_round = 0
        self.phase = self.PHASE_IDLE
        self.round_start_time = None
        self.round_end_time = None
        self.revealed_answers.clear()
        self.save(update_fields=['current_round', 'phase', 'round_start_time', 'round_end_time'])

    def get_active_state_count(self):
        question = self.quiz.current_question
        if not question:
            return 0
        return WerWeissMehrParticipantState.objects.filter(
            quiz=self.quiz,
            question=question,
            is_eliminated=False,
            participant__is_active=True,
        ).count()

    def get_hidden_answer_count(self):
        question = self.quiz.current_question
        if not question:
            return 0
        revealed_ids = self.revealed_answers.filter(question=question).values_list('id', flat=True)
        return question.answers.exclude(id__in=revealed_ids).count()

    def _mark_current_question_completed(self):
        question = self.quiz.current_question
        if not question:
            return
        completed = list(self.completed_question_ids or [])
        if question.id not in completed:
            completed.append(question.id)
            self.completed_question_ids = completed
            self.save(update_fields=['completed_question_ids'])

    def __str__(self):
        return f"Wer weiß mehr session for {self.quiz.room_code}"


class WerWeissMehrRound(SyncBase):
    STATUS_ACTIVE = 'active'
    STATUS_REVIEW = 'review'
    STATUS_COMPLETED = 'completed'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_REVIEW, 'Review'),
        (STATUS_COMPLETED, 'Completed'),
    ]

    quiz = models.ForeignKey(WerWeissMehrGame, on_delete=models.CASCADE, related_name='rounds')
    question = models.ForeignKey(WerWeissMehrQuestion, on_delete=models.CASCADE, related_name='rounds')
    round_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    revealed_answer_ids_at_start = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = ['quiz', 'question', 'round_number']
        ordering = ['round_number']

    def __str__(self):
        return f"{self.quiz.room_code} Q{self.question_id} R{self.round_number}"


class WerWeissMehrParticipantState(SyncBase):
    quiz = models.ForeignKey(WerWeissMehrGame, on_delete=models.CASCADE, related_name='participant_states')
    participant = models.ForeignKey(WerWeissMehrParticipant, on_delete=models.CASCADE, related_name='set_states')
    question = models.ForeignKey(WerWeissMehrQuestion, on_delete=models.CASCADE, related_name='participant_states')
    is_eliminated = models.BooleanField(default=False)
    survived_rounds = models.PositiveIntegerField(default=0)
    last_status = models.CharField(max_length=32, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['participant__name']

    def __str__(self):
        return f"{self.participant.name}: {self.survived_rounds} rounds"


class WerWeissMehrRoundResponse(SyncBase):
    STATUS_CORRECT = 'correct'
    STATUS_WRONG = 'wrong'
    STATUS_MANUAL_CORRECTED = 'manual_corrected'
    STATUS_CHOICES = [
        (STATUS_CORRECT, 'Correct'),
        (STATUS_WRONG, 'Wrong'),
        (STATUS_MANUAL_CORRECTED, 'Manual corrected'),
    ]

    quiz = models.ForeignKey(WerWeissMehrGame, on_delete=models.CASCADE, related_name='responses')
    participant = models.ForeignKey(WerWeissMehrParticipant, on_delete=models.CASCADE, related_name='responses')
    question = models.ForeignKey(WerWeissMehrQuestion, on_delete=models.CASCADE, related_name='responses')
    round_number = models.PositiveIntegerField()
    answer_text = models.TextField(blank=True, default='')
    normalized_answer = models.CharField(max_length=255, blank=True, default='')
    matched_answer = models.ForeignKey(WerWeissMehrAnswerOption, on_delete=models.SET_NULL, null=True, blank=True, related_name='responses')
    auto_status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_WRONG)
    final_status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_WRONG)
    is_correct = models.BooleanField(default=False)
    is_manual_override = models.BooleanField(default=False)
    time_taken = models.FloatField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ['quiz', 'participant', 'question', 'round_number']
        ordering = ['participant__name', 'round_number']

    def evaluate(self, revealed_before_round_ids=None):
        revealed_before_round_ids = {int(item) for item in (revealed_before_round_ids or [])}
        self.normalized_answer = normalize_answer_text(self.answer_text)
        matched_answer = self.question.find_matching_answer(self.answer_text)

        status = self.STATUS_CORRECT if matched_answer and matched_answer.id not in revealed_before_round_ids else self.STATUS_WRONG

        self.auto_status = status
        update_fields = [
            'answer_text',
            'normalized_answer',
            'auto_status',
            'evaluated_at',
        ]
        if not self.is_manual_override:
            self.matched_answer = matched_answer
            self.final_status = status
            self.is_correct = status == self.STATUS_CORRECT
            update_fields.extend(['matched_answer', 'final_status', 'is_correct'])
        self.evaluated_at = timezone.now()
        self.save(update_fields=update_fields)
        return status

    def apply_manual_correction(self, target_answer):
        self.matched_answer = target_answer
        self.final_status = self.STATUS_MANUAL_CORRECTED
        self.is_correct = True
        self.is_manual_override = True
        self.evaluated_at = timezone.now()
        self.save(update_fields=[
            'matched_answer',
            'final_status',
            'is_correct',
            'is_manual_override',
            'evaluated_at',
        ])

    def __str__(self):
        return f"{self.participant.name}: {self.answer_text}"


class WerWeissMehrPendingInput(SyncBase):
    quiz = models.ForeignKey(WerWeissMehrGame, on_delete=models.CASCADE, related_name='pending_inputs')
    participant = models.ForeignKey(WerWeissMehrParticipant, on_delete=models.CASCADE, related_name='pending_inputs')
    question = models.ForeignKey(WerWeissMehrQuestion, on_delete=models.CASCADE, related_name='pending_inputs')
    round_number = models.PositiveIntegerField()
    answer_text = models.TextField(blank=True, default='')
    normalized_answer = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        unique_together = ['quiz', 'participant', 'question', 'round_number']

    def save(self, *args, **kwargs):
        self.normalized_answer = normalize_answer_text(self.answer_text)
        super().save(*args, **kwargs)
