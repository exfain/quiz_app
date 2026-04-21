from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string

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
    
    def start_quiz(self):
        self.status = 'active'
        self.started_at = timezone.now()
        self.save()
    
    def end_quiz(self, status='completed'):
        self.status = status
        self.ended_at = timezone.now()
        self.save()
    
    def __str__(self):
        return f"{self.title} ({self.room_code})"


class QuizQuestion(SyncBase):
    QUESTION_TYPES = [
        ('multiple_choice', 'Multiple Choice'),
        ('true_false', 'True/False'),
        ('short_answer', 'Short Answer'),
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
    
    def is_correct_answer(self, answer):
        """Check if the provided answer is correct"""
        if self.question_type in ['multiple_choice', 'true_false']:
            return answer.upper().strip() == self.correct_answer.upper().strip()
        else:  # short_answer
            return answer.lower().strip() == self.correct_answer.lower().strip()
    
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
        self.total_score = sum(answer.points_earned for answer in correct_answers)
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
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-check if answer is correct and assign points
        if not self.pk:  # Only on creation
            self.is_correct = self.question.is_correct_answer(self.answer_text)
            if self.is_correct:
                self.points_earned = self.question.points
            else:
                self.points_earned = 0
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()
    
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
    
    def send_question(self, question):
        """Send a question to all participants"""
        self.quiz.current_question = question
        self.quiz.question_start_time = timezone.now()
        self.current_question_number += 1
        self.total_questions_sent += 1
        self.is_question_active = True
        self.question_end_time = timezone.now() + timezone.timedelta(seconds=question.time_limit)
        self.total_responses_current_question = 0
        self.correct_responses_current_question = 0
        
        self.quiz.save()
        self.save()
    
    def end_current_question(self):
        """End the current active question"""
        self.is_question_active = False
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        
        self.quiz.save()
        self.save()
    
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