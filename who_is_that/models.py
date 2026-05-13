from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string
import difflib
import re


class WhoThatQuiz(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200, default="Who is That?")
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_who_that_quizzes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('WhoThatQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_in_who_that_quiz')
    question_start_time = models.DateTimeField(null=True, blank=True)
    max_participants = models.IntegerField(default=50)
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('WhoThatQuestion', blank=True, related_name='quizzes')
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)

    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not WhoThatQuiz.objects.filter(room_code=code).exists():
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
        if self.status == 'waiting':
            self.current_question = None
            self.question_start_time = None
            if hasattr(self, 'session'):
                self.session.reset_for_new_run()
        self.status = 'active'
        self.tutorial_active = False
        self.started_at = timezone.now()
        self.ended_at = None
        self.save(update_fields=[
            'status',
            'tutorial_active',
            'started_at',
            'ended_at',
            'current_question',
            'question_start_time',
        ])
    
    def end_quiz(self, status='completed'):
        self.status = status
        self.ended_at = timezone.now()
        self.save()
    
    def __str__(self):
        return f"{self.title} ({self.room_code})"


class WhoThatBundle(SyncBase):
    """A reusable, named collection of questions that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('WhoThatQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='who_that_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class WhoThatQuestion(SyncBase):
    """Questions for Who is That game"""
    
    question_text = models.CharField(max_length=200, default="Who is this person?", help_text="Question text, e.g., 'Who is this actor?'")
    image = models.ImageField(upload_to='who_that_images/', help_text="Image of the person to be identified")
    correct_answer = models.CharField(max_length=200, help_text="The correct name/answer")
    alternative_answers = models.JSONField(default=list, blank=True, help_text="List of alternative correct answers")
    points = models.PositiveIntegerField(default=1, help_text="Points awarded for correct answer")
    time_limit = models.PositiveIntegerField(default=30, help_text="Time limit in seconds")
    hint_text = models.TextField(blank=True, null=True, help_text="Optional hint shown during question")
    explanation = models.TextField(blank=True, null=True, help_text="Optional explanation shown after answering")
    category = models.CharField(max_length=100, blank=True, null=True, help_text="Category like 'Actor', 'Politician', etc.")
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_who_that_questions')
    
    class Meta:
        ordering = ['-created_at']

    def _is_meaningful_partial_match(self, user_answer, correct_answer):
        normalized_user = ' '.join((user_answer or '').strip().lower().split())
        normalized_correct = ' '.join((correct_answer or '').strip().lower().split())

        if len(normalized_user) < 3 or len(normalized_correct) < 3:
            return False

        user_pattern = rf'(^|\W){re.escape(normalized_user)}($|\W)'
        correct_pattern = rf'(^|\W){re.escape(normalized_correct)}($|\W)'

        if re.search(user_pattern, normalized_correct):
            return True
        if re.search(correct_pattern, normalized_user):
            return True
        return False
    
    def check_answer(self, user_answer):
        """Check if the user's answer is correct"""
        if not user_answer:
            return False
        
        user_answer = user_answer.strip().lower()
        correct_answer = self.correct_answer.strip().lower()
        
        # Exact match
        if user_answer == correct_answer:
            return True
        
        # Check alternative answers
        for alt_answer in self.alternative_answers:
            if user_answer == alt_answer.strip().lower():
                return True
        
        # Partial match using sequence matching (70% similarity)
        similarity = difflib.SequenceMatcher(None, user_answer, correct_answer).ratio()
        if similarity >= 0.7:
            return True
        
        # Allow only meaningful whole-token or whole-phrase partials.
        if self._is_meaningful_partial_match(user_answer, correct_answer):
            return True
        
        return False
    
    def get_match_quality(self, user_answer):
        """Get the quality of the match for scoring purposes"""
        if not user_answer:
            return 0
        
        user_answer = user_answer.strip().lower()
        correct_answer = self.correct_answer.strip().lower()
        
        # Exact match
        if user_answer == correct_answer:
            return 1.0
        
        # Check alternative answers
        for alt_answer in self.alternative_answers:
            if user_answer == alt_answer.strip().lower():
                return 1.0
        
        # Calculate similarity
        similarity = difflib.SequenceMatcher(None, user_answer, correct_answer).ratio()
        return similarity
    
    def calculate_score(self, user_answer):
        """Calculate score based on answer quality"""
        return 1 if self.check_answer(user_answer) else 0
    
    def __str__(self):
        return f"{self.question_text} - {self.correct_answer}"


class WhoThatParticipant(SyncBase):
    quiz = models.ForeignKey(WhoThatQuiz, on_delete=models.CASCADE, related_name='participants')
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    total_score = models.IntegerField(default=0)
    questions_answered = models.IntegerField(default=0)
    correct_answers = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)
    
    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['-total_score', 'name']
    
    def calculate_score(self):
        """Recalculate total score based on answers"""
        answers = self.who_that_answers.all()
        self.total_score = sum(1 for answer in answers if answer.is_correct)
        self.questions_answered = answers.count()
        self.correct_answers = answers.filter(is_correct=True).count()
        self.save()
        return self.total_score
    
    def get_rank(self):
        """Get participant's rank in the quiz"""
        higher_scores = WhoThatParticipant.objects.filter(
            quiz=self.quiz,
            total_score__gt=self.total_score
        ).count()
        return higher_scores + 1
    
    def get_average_accuracy(self):
        """Get average accuracy percentage across all answers"""
        if self.questions_answered == 0:
            return 0
        return round((self.correct_answers / self.questions_answered) * 100, 1)
    
    def get_accuracy_percentage(self):
        """Get accuracy percentage for display"""
        return self.get_average_accuracy()
    
    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class WhoThatAnswer(SyncBase):
    quiz = models.ForeignKey(WhoThatQuiz, on_delete=models.CASCADE, related_name='who_that_answers')
    participant = models.ForeignKey(WhoThatParticipant, on_delete=models.CASCADE, related_name='who_that_answers')
    question = models.ForeignKey(WhoThatQuestion, on_delete=models.CASCADE, related_name='who_that_answers')
    
    user_answer = models.CharField(max_length=200, help_text="The participant's answer")
    is_correct = models.BooleanField(default=False)
    points_earned = models.IntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-calculate correctness and points on creation
        if not self.pk:
            self.is_correct = self.question.check_answer(self.user_answer)
        self.points_earned = 1 if self.is_correct else 0
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()
    
    def get_accuracy_percentage(self):
        """Get accuracy percentage (100% if correct, 0% if incorrect, or match quality * 100)"""
        if self.is_correct:
            match_quality = self.question.get_match_quality(self.user_answer)
            return round(match_quality * 100, 1)
        return 0
    
    def get_match_quality(self):
        """Get match quality description"""
        if not self.is_correct:
            return "Incorrect"
        
        match_quality = self.question.get_match_quality(self.user_answer)
        if match_quality >= 0.9:
            return "Perfect Match"
        elif match_quality >= 0.7:
            return "Good Match"
        else:
            return "Partial Match"
    
    def __str__(self):
        return f"{self.participant.name}: {self.user_answer} ({'✓' if self.is_correct else '✗'})"


class WhoThatSession(SyncBase):
    """Tracks the current state of a live Who is That quiz session"""
    quiz = models.OneToOneField(WhoThatQuiz, on_delete=models.CASCADE, related_name='session')
    current_question_number = models.IntegerField(default=0)
    total_questions_sent = models.IntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    pending_answers = models.JSONField(default=dict, blank=True)
    
    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    correct_responses_current_question = models.IntegerField(default=0)
    average_response_time_current_question = models.FloatField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def reset_for_new_run(self):
        """Clear stale runtime progress before a fresh waiting->active start."""
        self.current_question_number = 0
        self.total_questions_sent = 0
        self.is_question_active = False
        self.question_end_time = None
        self.pending_answers = {}
        self.total_responses_current_question = 0
        self.correct_responses_current_question = 0
        self.average_response_time_current_question = 0
        self.save(update_fields=[
            'current_question_number',
            'total_questions_sent',
            'is_question_active',
            'question_end_time',
            'pending_answers',
            'total_responses_current_question',
            'correct_responses_current_question',
            'average_response_time_current_question',
            'updated_at',
        ])
    
    def send_question(self, question, time_limit_seconds=None):
        """Send a question to all participants"""
        effective_time_limit = time_limit_seconds if time_limit_seconds is not None else question.time_limit
        now = timezone.now()
        self.quiz.current_question = question
        self.quiz.question_start_time = now
        self.current_question_number += 1
        self.total_questions_sent += 1
        self.is_question_active = True
        self.question_end_time = now + timezone.timedelta(seconds=effective_time_limit)
        self.total_responses_current_question = 0
        self.correct_responses_current_question = 0
        self.average_response_time_current_question = 0
        self.pending_answers = {}
        
        self.quiz.save(update_fields=['current_question', 'question_start_time'])
        self.save()
    
    def end_current_question(self):
        """End the current active question"""
        self.is_question_active = False
        self.question_end_time = None
        self.pending_answers = {}
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        
        self.quiz.save(update_fields=['current_question', 'question_start_time'])
        self.save()
    
    def record_answer(self, is_correct, response_time=None):
        """Record statistics for an answer"""
        self.total_responses_current_question += 1
        if is_correct:
            self.correct_responses_current_question += 1
        
        if response_time:
            # Calculate new average response time
            total_time = self.average_response_time_current_question * (self.total_responses_current_question - 1)
            self.average_response_time_current_question = (total_time + response_time) / self.total_responses_current_question
        
        self.save()
    
    def get_current_question_stats(self):
        """Get statistics for current question"""
        accuracy_rate = 0
        if self.total_responses_current_question > 0:
            accuracy_rate = (self.correct_responses_current_question / self.total_responses_current_question) * 100
        
        participation_rate = 0
        if self.quiz.get_active_participants().count() > 0:
            participation_rate = (self.total_responses_current_question / self.quiz.get_active_participants().count()) * 100
        
        return {
            'total_responses': self.total_responses_current_question,
            'correct_responses': self.correct_responses_current_question,
            'accuracy_rate': round(accuracy_rate, 1),
            'participation_rate': round(participation_rate, 1),
            'average_response_time': round(self.average_response_time_current_question, 1),
        }
    
    def __str__(self):
        return f"Session for {self.quiz.room_code}"
