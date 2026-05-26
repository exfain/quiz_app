import json
import random
import string

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from games_website.models import SyncBase

class Quiz(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200, default="Quick Quiz")
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_quizzes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('QuizQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_in_quiz')
    question_start_time = models.DateTimeField(null=True, blank=True)
    max_participants = models.IntegerField(default=50)
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('QuizQuestion', blank=True, related_name='quizzes')
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not Quiz.objects.filter(room_code=code).exists():
                return code
    
    def get_participant_count(self, session_code=None):
        if session_code:
            return self.participants.filter(hub_session_code=session_code).count()
        return self.participants.count()
    
    def get_active_participants(self, session_code=None):
        if session_code:
            return self.participants.filter(is_active=True, hub_session_code=session_code)
        return self.participants.filter(is_active=True)
    
    def _session_participants(self, session_code=None):
        participants = self.participants.all()
        if session_code is not None:
            participants = participants.filter(hub_session_code=session_code)
        return participants

    def reset_runtime_state(self, session_code=None, clear_answers=True, reset_scores=True):
        """Clear per-run state without touching reusable quiz/question configuration."""
        self.started_at = None
        self.ended_at = None
        self.current_question = None
        self.question_start_time = None
        self.tutorial_active = False
        self.save(update_fields=[
            'started_at',
            'ended_at',
            'current_question',
            'question_start_time',
            'tutorial_active',
            'updated_at',
        ])

        session, _ = QuizSession.objects.get_or_create(quiz=self)
        session.reset_runtime_state()

        if clear_answers:
            answers = self.quiz_answers.all()
            if session_code is not None:
                answers = answers.filter(participant__hub_session_code=session_code)
            answers.delete()

        if reset_scores:
            self._session_participants(session_code).update(
                total_score=0,
                questions_answered=0,
                tutorial_completed=False,
            )

    def ensure_clean_prestart_state(self, session_code=None):
        session_dirty = False
        try:
            session_dirty = bool(
                self.session.is_question_active
                or self.session.question_end_time
                or self.session.current_question_number
                or self.session.total_questions_sent
            )
        except QuizSession.DoesNotExist:
            session_dirty = False

        stale_answers = False
        stale_scores = False
        if self.status == 'waiting':
            answers = self.quiz_answers.all()
            if session_code is not None:
                answers = answers.filter(participant__hub_session_code=session_code)
            stale_answers = answers.exists()
            stale_scores = self._session_participants(session_code).filter(
                models.Q(total_score__gt=0)
                | models.Q(total_score__lt=0)
                | models.Q(questions_answered__gt=0)
                | models.Q(tutorial_completed=True)
            ).exists()

        if self.status == 'waiting' and (
            self.started_at
            or self.ended_at
            or self.current_question_id
            or self.question_start_time
            or self.tutorial_active
            or session_dirty
            or stale_answers
            or stale_scores
        ):
            self.reset_runtime_state(session_code=session_code)

    def start_quiz(self, session_code=None):
        self.reset_runtime_state(session_code=session_code)
        self.status = 'active'
        self.started_at = timezone.now()
        self.ended_at = None
        self.save(update_fields=['status', 'started_at', 'ended_at', 'updated_at'])
    
    def end_quiz(self, status='completed'):
        if hasattr(self, 'session'):
            self.session.end_current_question()
        self.current_question = None
        self.question_start_time = None
        self.status = status
        self.ended_at = timezone.now()
        self.save(update_fields=['status', 'ended_at', 'current_question', 'question_start_time', 'updated_at'])
    
    def __str__(self):
        return f"{self.title} ({self.room_code})"


class QuizQuestion(SyncBase):
    QUESTION_TYPES = [
        ('multiple_choice', 'Multiple Choice'),
        ('true_false', 'True/False'),
        ('short_answer', 'Short Answer'),
        ('double_answer', 'Double Answer'),
    ]
    
    question_text = models.TextField()
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPES, default='multiple_choice')
    points = models.PositiveIntegerField(default=10)
    time_limit = models.PositiveIntegerField(default=30, help_text="Time limit in seconds")
    
    # Multiple choice options
    option_a = models.CharField(max_length=200, blank=True)
    option_b = models.CharField(max_length=200, blank=True)
    option_c = models.CharField(max_length=200, blank=True)
    option_d = models.CharField(max_length=200, blank=True)
    
    correct_answer = models.CharField(max_length=500, help_text="For multiple choice: A, B, C, or D. For true/false: True or False. For short answer: the correct answer text.")
    correct_answer_2 = models.CharField(max_length=500, blank=True, default='', help_text="For double answer: the correct answer text for field 2.")
    correct_answer_3 = models.CharField(max_length=500, blank=True, default='', help_text="For short answer: the correct answer text for field 3.")
    correct_answer_4 = models.CharField(max_length=500, blank=True, default='', help_text="For short answer: the correct answer text for field 4.")
    double_answer_label_1 = models.CharField(max_length=120, blank=True, default='', help_text="For double answer: label for field 1.")
    double_answer_label_2 = models.CharField(max_length=120, blank=True, default='', help_text="For double answer: label for field 2.")
    answer_label_1 = models.CharField(max_length=120, blank=True, default='', help_text="For short answer: label for field 1.")
    answer_label_2 = models.CharField(max_length=120, blank=True, default='', help_text="For short answer: label for field 2.")
    answer_label_3 = models.CharField(max_length=120, blank=True, default='', help_text="For short answer: label for field 3.")
    answer_label_4 = models.CharField(max_length=120, blank=True, default='', help_text="For short answer: label for field 4.")
    explanation = models.TextField(blank=True, help_text="Optional explanation for the answer")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_quiz_questions')
    
    class Meta:
        ordering = ['-created_at']
    
    def get_options(self):
        """Return non-empty options as a list"""
        options = []
        if self.option_a: options.append(('A', self.option_a))
        if self.option_b: options.append(('B', self.option_b))
        if self.option_c: options.append(('C', self.option_c))
        if self.option_d: options.append(('D', self.option_d))
        return options

    def get_effective_question_type(self):
        if self.question_type == 'double_answer':
            return 'short_answer'
        return self.question_type

    @staticmethod
    def normalize_text_answer(value):
        return (value or '').strip().lower()

    def get_short_answer_field_count(self):
        answers = [
            (self.correct_answer or '').strip(),
            (self.correct_answer_2 or '').strip(),
            (self.correct_answer_3 or '').strip(),
            (self.correct_answer_4 or '').strip(),
        ]
        for index in range(len(answers), 1, -1):
            if answers[index - 1]:
                return index
        return 1

    def get_short_answer_fields(self):
        field_count = self.get_short_answer_field_count()
        labels = [
            self.answer_label_1 or self.double_answer_label_1 or '',
            self.answer_label_2 or self.double_answer_label_2 or '',
            self.answer_label_3 or '',
            self.answer_label_4 or '',
        ]
        answers = [
            self.correct_answer or '',
            self.correct_answer_2 or '',
            self.correct_answer_3 or '',
            self.correct_answer_4 or '',
        ]
        fields = []
        for index in range(field_count):
            label = labels[index] if field_count > 1 else ''
            fields.append({
                'index': index + 1,
                'key': f'answer_{index + 1}',
                'label': label or (f'Answer {index + 1}' if field_count > 1 else ''),
                'correct_answer': answers[index],
            })
        return fields

    def get_public_short_answer_fields(self):
        return [
            {
                'index': field['index'],
                'key': field['key'],
                'label': field['label'],
            }
            for field in self.get_short_answer_fields()
        ]

    def parse_short_answer_submission(self, answer):
        field_count = self.get_short_answer_field_count()
        if field_count <= 1:
            if isinstance(answer, dict):
                return [(answer.get('answer_1') or '').strip()]
            if isinstance(answer, str):
                try:
                    parsed = json.loads(answer)
                except (TypeError, ValueError):
                    return [answer.strip()]
                if isinstance(parsed, dict):
                    return [str(parsed.get('answer_1') or '').strip()]
            return [str(answer or '').strip()]

        parsed = answer
        if isinstance(answer, str):
            try:
                parsed = json.loads(answer)
            except (TypeError, ValueError):
                parsed = {}

        values = []
        for index in range(field_count):
            key = f'answer_{index + 1}'
            value = ''
            if isinstance(parsed, dict):
                value = parsed.get(key) or ''
            elif isinstance(parsed, list) and index < len(parsed):
                value = parsed[index]
            values.append(str(value or '').strip())
        return values

    def serialize_short_answer_submission(self, answer):
        values = self.parse_short_answer_submission(answer)
        if self.get_short_answer_field_count() <= 1:
            return values[0]
        return json.dumps(
            {f'answer_{index + 1}': values[index] for index in range(len(values))},
            ensure_ascii=False,
        )

    def format_short_answer_submission(self, answer):
        values = self.parse_short_answer_submission(answer)
        fields = self.get_short_answer_fields()
        if len(fields) <= 1:
            return values[0]
        return ' | '.join(
            f"{fields[index]['label']}: {values[index]}"
            for index in range(len(fields))
        )

    def get_formatted_correct_answer(self):
        if self.question_type == 'multiple_choice':
            mapping = {
                'A': self.option_a or '',
                'B': self.option_b or '',
                'C': self.option_c or '',
                'D': self.option_d or '',
            }
            key = (self.correct_answer or '').strip().upper()
            option_text = mapping.get(key, '')
            return f"{key}{'. ' + option_text if option_text else ''}".strip()
        if self.question_type == 'true_false':
            value = self.normalize_text_answer(self.correct_answer)
            return 'True' if value in ['true', 't', '1', 'yes'] else 'False'
        return self.format_short_answer_submission(
            {
                field['key']: field['correct_answer']
                for field in self.get_short_answer_fields()
            }
        )

    def is_correct_answer(self, answer):
        """Check if the provided answer is correct"""
        if self.question_type in ['multiple_choice', 'true_false']:
            return answer.upper().strip() == self.correct_answer.upper().strip()
        return all(field['is_correct'] for field in self.evaluate_short_answer_fields(answer))

    def evaluate_short_answer_fields(self, answer):
        submitted = self.parse_short_answer_submission(answer)
        expected = [
            self.normalize_text_answer(field['correct_answer'])
            for field in self.get_short_answer_fields()
        ]
        results = []
        for index, field in enumerate(self.get_short_answer_fields()):
            submitted_value = submitted[index] if index < len(submitted) else ''
            auto_correct = self.normalize_text_answer(submitted_value) == expected[index]
            results.append({
                'index': field['index'],
                'key': field['key'],
                'label': field['label'],
                'answer': submitted_value,
                'correct_answer': field['correct_answer'],
                'auto_correct': auto_correct,
                'is_correct': auto_correct,
                'manual_override': False,
            })
        return results
    
    def __str__(self):
        return f"{self.question_text[:50]}..." if len(self.question_text) > 50 else self.question_text


class QuizParticipant(SyncBase):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='participants')
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    total_score = models.IntegerField(default=0)
    questions_answered = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)
    tutorial_completed = models.BooleanField(default=False)

    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['-total_score', 'name']
    
    def calculate_score(self):
        """Recalculate total score based on answers"""
        correct_answers = self.quiz_answers.filter(is_correct=True)
        self.total_score = correct_answers.count()
        self.questions_answered = self.quiz_answers.count()
        self.save()
        return self.total_score
    
    def get_rank(self):
        """Get participant's rank in the quiz"""
        higher_scores = QuizParticipant.objects.filter(
            quiz=self.quiz,
            total_score__gt=self.total_score
        ).count()
        return higher_scores + 1
    
    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class QuizAnswer(SyncBase):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='quiz_answers')
    participant = models.ForeignKey(QuizParticipant, on_delete=models.CASCADE, related_name='quiz_answers')
    question = models.ForeignKey(QuizQuestion, on_delete=models.CASCADE, related_name='quiz_answers')
    answer_text = models.TextField()
    is_correct = models.BooleanField(default=False)
    points_earned = models.IntegerField(default=0)
    field_results = models.JSONField(default=list, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-check if answer is correct and assign points
        if not self.pk:  # Only on creation
            if self.question.get_effective_question_type() == 'short_answer':
                self.answer_text = self.question.serialize_short_answer_submission(self.answer_text)
                self.field_results = self.question.evaluate_short_answer_fields(self.answer_text)
                self.is_correct = all(field['is_correct'] for field in self.field_results)
            else:
                self.is_correct = self.question.is_correct_answer(self.answer_text)
                self.field_results = []
            self.points_earned = 1 if self.is_correct else 0
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()

    def get_short_answer_field_results(self):
        if self.question.get_effective_question_type() != 'short_answer':
            return []

        base_results = self.question.evaluate_short_answer_fields(self.answer_text)
        stored_by_key = {
            str(field.get('key')): field
            for field in (self.field_results or [])
            if isinstance(field, dict) and field.get('key')
        }
        normalized = []
        for base in base_results:
            stored = stored_by_key.get(base['key']) or {}
            manual_override = bool(stored.get('manual_override', False))
            if manual_override:
                base['is_correct'] = bool(stored.get('is_correct', base['is_correct']))
                base['manual_override'] = True
            normalized.append(base)
        return normalized

    def set_short_answer_field_correctness(self, field_key, is_correct=True):
        if self.question.get_effective_question_type() != 'short_answer':
            raise ValueError('Only short-answer responses support field correction.')

        results = self.get_short_answer_field_results()
        target = None
        for field in results:
            if field['key'] == field_key:
                target = field
                break
        if not target:
            raise ValueError('Unknown answer field.')

        target['is_correct'] = bool(is_correct)
        target['manual_override'] = target['is_correct'] != bool(target.get('auto_correct'))
        self.field_results = results
        self.is_correct = all(field['is_correct'] for field in results)
        self.points_earned = 1 if self.is_correct else 0
        self.save(update_fields=['field_results', 'is_correct', 'points_earned', 'updated_at'])

    def promote_short_answer_to_correct(self):
        if self.question.get_effective_question_type() != 'short_answer':
            raise ValueError('Only short-answer responses can be promoted manually.')

        results = self.get_short_answer_field_results()
        for field in results:
            field['is_correct'] = True
            field['manual_override'] = not bool(field.get('auto_correct'))
        self.field_results = results
        self.is_correct = True
        self.points_earned = 1
        self.save(update_fields=['field_results', 'is_correct', 'points_earned', 'updated_at'])
    
    def __str__(self):
        return f"{self.participant.name}: {self.answer_text[:30]}"


class QuizBundle(SyncBase):
    """A reusable, named collection of questions that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('QuizQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='quiz_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class QuizSession(SyncBase):
    """Tracks the current state of a live quiz session"""
    quiz = models.OneToOneField(Quiz, on_delete=models.CASCADE, related_name='session')
    current_question_number = models.IntegerField(default=0)
    total_questions_sent = models.IntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    
    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    correct_responses_current_question = models.IntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def reset_runtime_state(self):
        self.current_question_number = 0
        self.total_questions_sent = 0
        self.is_question_active = False
        self.question_end_time = None
        self.total_responses_current_question = 0
        self.correct_responses_current_question = 0
        self.save(update_fields=[
            'current_question_number',
            'total_questions_sent',
            'is_question_active',
            'question_end_time',
            'total_responses_current_question',
            'correct_responses_current_question',
            'updated_at',
        ])

    def send_question(self, question, time_limit=None):
        """Send a question to all participants"""
        now = timezone.now()
        effective_time_limit = int(time_limit or question.time_limit)
        self.quiz.current_question = question
        self.quiz.question_start_time = now
        self.current_question_number += 1
        self.total_questions_sent += 1
        self.is_question_active = True
        self.question_end_time = now + timezone.timedelta(seconds=effective_time_limit)
        self.total_responses_current_question = 0
        self.correct_responses_current_question = 0
        
        self.quiz.save(update_fields=['current_question', 'question_start_time', 'updated_at'])
        self.save(update_fields=[
            'current_question_number',
            'total_questions_sent',
            'is_question_active',
            'question_end_time',
            'total_responses_current_question',
            'correct_responses_current_question',
            'updated_at',
        ])
    
    def end_current_question(self):
        """End the current active question"""
        self.is_question_active = False
        self.question_end_time = None
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        
        self.quiz.save(update_fields=['current_question', 'question_start_time', 'updated_at'])
        self.save(update_fields=['is_question_active', 'question_end_time', 'updated_at'])
    
    def record_answer(self, is_correct):
        """Record statistics for an answer"""
        self.total_responses_current_question += 1
        if is_correct:
            self.correct_responses_current_question += 1
        self.save()
    
    def get_current_question_stats(self):
        """Get statistics for current question"""
        return {
            'total_responses': self.total_responses_current_question,
            'correct_responses': self.correct_responses_current_question,
            'accuracy_percentage': (self.correct_responses_current_question / max(1, self.total_responses_current_question)) * 100
        }
    
    def __str__(self):
        return f"Session for {self.quiz.room_code}"
