from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string
import json


class AssignQuiz(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200, default="Drag & Drop Quiz")
    internal_description = models.TextField(blank=True, default='')
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_assign_quizzes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('AssignQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_in_assign_quiz')
    question_start_time = models.DateTimeField(null=True, blank=True)
    max_participants = models.IntegerField(default=50)
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('AssignQuestion', blank=True, related_name='quizzes')
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not AssignQuiz.objects.filter(room_code=code).exists():
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


class AssignBundle(SyncBase):
    """A reusable, named collection of questions that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('AssignQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assign_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class AssignQuestion(SyncBase):
    """Drag and drop questions with items to match"""
    
    question_text = models.TextField(help_text="The question or instruction, e.g., 'Match countries with their capitals'")
    points = models.PositiveIntegerField(default=10, help_text="Points per correct match")
    time_limit = models.PositiveIntegerField(default=60, help_text="Time limit in seconds")
    
    # JSON fields to store drag-drop items
    left_items = models.JSONField(help_text="List of items on the left side to drag from")
    right_items = models.JSONField(help_text="List of items on the right side to drop to") 
    correct_matches = models.JSONField(help_text="Dictionary mapping left item indices to right item indices")
    
    explanation = models.TextField(blank=True, help_text="Optional explanation shown after answering")
    
    is_active = models.BooleanField(default=True, help_text="Whether this question is active and can be used in quizzes")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_assign_questions')
    
    class Meta:
        ordering = ['-created_at']
    
    def get_total_possible_points(self):
        """Return total possible points for this question"""
        return len(self.correct_matches) * self.points
    
    def calculate_score(self, user_matches):
        """Calculate score based on user's matches"""
        if not user_matches or not self.correct_matches:
            return 0
        
        correct_count = 0
        for left_idx, right_idx in user_matches.items():
            if str(left_idx) in self.correct_matches:
                if self.correct_matches[str(left_idx)] == int(right_idx):
                    correct_count += 1
        
        return correct_count * self.points
    
    def get_formatted_items(self):
        """Return formatted items for display"""
        return {
            'left_items': [{'id': i, 'text': item} for i, item in enumerate(self.left_items)],
            'right_items': [{'id': i, 'text': item} for i, item in enumerate(self.right_items)],
            'correct_matches': self.correct_matches
        }
    
    def get_randomized_items(self, room_code=None):
        """Return items with right items shuffled for gameplay"""
        import random
        import hashlib

        # Deterministic seed via MD5 (unaffected by PYTHONHASHSEED, thread-safe)
        seed_string = f"{self.id}_{room_code or 'default'}"
        seed_bytes = hashlib.md5(seed_string.encode()).digest()
        seed = int.from_bytes(seed_bytes[:4], 'big')

        # Use a local Random instance — never touches the global random state,
        # so concurrent threads cannot corrupt each other's shuffle.
        rng = random.Random(seed)

        right_items_with_indices = [(i, item) for i, item in enumerate(self.right_items)]
        shuffled_right_items = right_items_with_indices.copy()
        rng.shuffle(shuffled_right_items)

        position_to_original = {}
        shuffled_right_items_formatted = []
        for new_pos, (original_idx, text) in enumerate(shuffled_right_items):
            position_to_original[new_pos] = original_idx
            shuffled_right_items_formatted.append({'id': new_pos, 'text': text})

        return {
            'left_items': [{'id': i, 'text': item} for i, item in enumerate(self.left_items)],
            'right_items': shuffled_right_items_formatted,
            'position_to_original': position_to_original  # Maps shuffled position to original index
        }
    
    def __str__(self):
        return f"{self.question_text[:50]}..." if len(self.question_text) > 50 else self.question_text


class AssignParticipant(SyncBase):
    quiz = models.ForeignKey(AssignQuiz, on_delete=models.CASCADE, related_name='participants')
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    total_score = models.IntegerField(default=0)
    questions_answered = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)
    
    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['-total_score', 'name']
    
    def calculate_score(self):
        """Recalculate total score based on answers"""
        total_score = sum(answer.points_earned for answer in self.assign_answers.all())
        self.total_score = total_score
        self.questions_answered = self.assign_answers.count()
        self.save()
        return self.total_score
    
    def get_rank(self):
        """Get participant's rank in the quiz"""
        higher_scores = AssignParticipant.objects.filter(
            quiz=self.quiz,
            total_score__gt=self.total_score
        ).count()
        return higher_scores + 1
    
    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class AssignAnswer(SyncBase):
    quiz = models.ForeignKey(AssignQuiz, on_delete=models.CASCADE, related_name='assign_answers')
    participant = models.ForeignKey(AssignParticipant, on_delete=models.CASCADE, related_name='assign_answers')
    question = models.ForeignKey(AssignQuestion, on_delete=models.CASCADE, related_name='assign_answers')
    
    # Store the user's drag-drop matches as JSON
    user_matches = models.JSONField(help_text="Dictionary mapping left item indices to right item indices")
    points_earned = models.IntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-calculate points on creation
        if not self.pk:
            self.points_earned = self.question.calculate_score(self.user_matches)
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()
    
    def get_correct_matches_count(self):
        """Get number of correct matches"""
        if not self.user_matches or not self.question.correct_matches:
            return 0
        
        correct_count = 0
        for left_idx, right_idx in self.user_matches.items():
            if str(left_idx) in self.question.correct_matches:
                if self.question.correct_matches[str(left_idx)] == int(right_idx):
                    correct_count += 1
        return correct_count
    
    def get_total_matches_count(self):
        """Get total number of matches made"""
        return len(self.user_matches) if self.user_matches else 0
    
    def get_accuracy_percentage(self):
        """Get accuracy percentage for this answer"""
        total_possible = len(self.question.correct_matches)
        if total_possible == 0:
            return 0
        correct = self.get_correct_matches_count()
        return round((correct / total_possible) * 100, 1)
    
    def __str__(self):
        return f"{self.participant.name}: {self.get_correct_matches_count()}/{len(self.question.correct_matches)} correct"


class AssignSession(SyncBase):
    """Tracks the current state of a live assign quiz session"""
    quiz = models.OneToOneField(AssignQuiz, on_delete=models.CASCADE, related_name='session')
    current_question_number = models.IntegerField(default=0)
    total_questions_sent = models.IntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    
    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    average_score_current_question = models.FloatField(default=0)
    # Rundenbasierter Modus: aktueller Runden-Index (0-basiert)
    current_round_index = models.IntegerField(default=0)
    
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
        self.average_score_current_question = 0
        
        self.quiz.save()
        self.save()
    
    def end_current_question(self):
        """End the current active question"""
        self.is_question_active = False
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        
        self.quiz.save()
        self.save()
    
    def record_answer(self, points_earned):
        """Record statistics for an answer"""
        self.total_responses_current_question += 1
        
        # Calculate new average score
        current_total = (self.average_score_current_question * 
                        (self.total_responses_current_question - 1))
        self.average_score_current_question = (
            (current_total + points_earned) / self.total_responses_current_question
        )
        
        self.save()
    
    def get_current_question_stats(self):
        """Get statistics for current question"""
        return {
            'total_responses': self.total_responses_current_question,
            'average_score': round(self.average_score_current_question, 1),
            'participation_rate': (
                (self.total_responses_current_question / 
                 max(1, self.quiz.get_active_participants().count())) * 100
            )
        }
    
    def __str__(self):
        return f"Session for {self.quiz.room_code}"