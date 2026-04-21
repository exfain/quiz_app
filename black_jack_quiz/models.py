from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string
import math


class BlackJackQuiz(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200, default="Black Jack Quiz")
    internal_description = models.TextField(blank=True, default='')
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_blackjack_quizzes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('BlackJackQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_in_blackjack_quiz')
    current_question_number = models.IntegerField(default=0)  # Track which question number we're on
    question_start_time = models.DateTimeField(null=True, blank=True)
    max_participants = models.IntegerField(default=50)
    total_questions = models.IntegerField(default=5)  # Fixed at 5 for BlackJack
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('BlackJackQuestion', blank=True, related_name='quizzes')
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not BlackJackQuiz.objects.filter(room_code=code).exists():
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
    
    def is_quiz_complete(self):
        """Check if all 5 questions have been asked"""
        return self.current_question_number >= self.total_questions
    
    def __str__(self):
        return f"{self.title} ({self.room_code})"


class BlackJackBundle(SyncBase):
    """A reusable, named collection of questions that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('BlackJackQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='blackjack_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class BlackJackQuestion(SyncBase):
    """Questions for blackjack quiz games"""
    
    question_text = models.TextField(help_text="The question, e.g., 'How many legs does a spider have?'")
    correct_answer = models.IntegerField(help_text="The correct numerical answer (integer only)")
    time_limit = models.PositiveIntegerField(default=30, help_text="Time limit in seconds")
    explanation = models.TextField(blank=True, null=True, help_text="Optional explanation shown after answering")
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_blackjack_questions')
    
    class Meta:
        ordering = ['-created_at']
    
    def calculate_points(self, user_answer):
        """Calculate points based on BlackJack scoring - points = absolute difference from correct answer"""
        if user_answer is None:
            return 10  # High penalty for no answer
        
        try:
            user_answer = int(user_answer)
        except (ValueError, TypeError):
            return 10  # High penalty for invalid answer
        
        # Points = absolute difference from correct answer
        points = abs(user_answer - self.correct_answer)
        return points
    
    def __str__(self):
        return f"{self.question_text[:50]}..." if len(self.question_text) > 50 else self.question_text


class BlackJackParticipant(SyncBase):
    quiz = models.ForeignKey(BlackJackQuiz, on_delete=models.CASCADE, related_name='participants')
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    total_points = models.IntegerField(default=0)  # Total points across all questions
    questions_answered = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    is_busted = models.BooleanField(default=False)  # True if they went over 21
    final_score = models.IntegerField(default=0)  # Final score calculation
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)
    
    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['final_score', 'name']  # Lower score is better in BlackJack
    
    def calculate_score(self):
        """Recalculate total points and determine if busted"""
        total_points = sum(answer.points_earned for answer in self.blackjack_answers.all())
        self.total_points = total_points
        self.questions_answered = self.blackjack_answers.count()
        
        # Check if busted (over 21)
        if total_points > 21:
            self.is_busted = True
            self.final_score = 999  # High penalty score for busted players
        else:
            self.is_busted = False
            # Lower score is better - closest to 21 wins
            self.final_score = abs(21 - total_points)
        
        self.save()
        return self.total_points
    
    def get_rank(self):
        """Get participant's rank in the quiz (lower final_score is better)"""
        if self.is_busted:
            # Rank among busted players by who got closest to 21 before busting
            busted_ranks = BlackJackParticipant.objects.filter(
                quiz=self.quiz,
                is_busted=True,
                total_points__lt=self.total_points
            ).count()
            # Add all non-busted players + busted players with lower points
            non_busted_count = BlackJackParticipant.objects.filter(
                quiz=self.quiz,
                is_busted=False
            ).count()
            return non_busted_count + busted_ranks + 1
        else:
            # Rank among non-busted players by final_score (closer to 21 is better)
            better_scores = BlackJackParticipant.objects.filter(
                quiz=self.quiz,
                is_busted=False,
                final_score__lt=self.final_score
            ).count()
            return better_scores + 1
    
    def get_status(self):
        """Get participant status"""
        if self.is_busted:
            return 'busted'
        elif self.total_points == 21:
            return 'blackjack'
        else:
            return 'playing'
    
    def get_distance_from_21(self):
        """Get how far the participant is from 21"""
        if self.is_busted:
            return self.total_points - 21  # Positive number showing how much over
        else:
            return 21 - self.total_points  # Positive number showing how much under
    
    def __str__(self):
        return f"{self.name} in {self.quiz.room_code} ({self.total_points} pts)"


class BlackJackAnswer(SyncBase):
    quiz = models.ForeignKey(BlackJackQuiz, on_delete=models.CASCADE, related_name='blackjack_answers')
    participant = models.ForeignKey(BlackJackParticipant, on_delete=models.CASCADE, related_name='blackjack_answers')
    question = models.ForeignKey(BlackJackQuestion, on_delete=models.CASCADE, related_name='blackjack_answers')
    
    user_answer = models.IntegerField(help_text="The participant's numerical answer")
    points_earned = models.IntegerField(default=0)  # Points for this specific question
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    question_number = models.IntegerField(default=1)  # Which question number this was (1-5)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-calculate points on creation
        if not self.pk:
            self.points_earned = self.question.calculate_points(self.user_answer)
            # Set question number based on current quiz state
            self.question_number = self.quiz.current_question_number
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()
    
    def get_difference(self):
        """Get the difference from the correct answer"""
        return abs(self.user_answer - self.question.correct_answer)
    
    def get_difference_direction(self):
        """Get if the answer was high, low, or exact"""
        if self.user_answer == self.question.correct_answer:
            return 'exact'
        elif self.user_answer > self.question.correct_answer:
            return 'high'
        else:
            return 'low'
    
    def __str__(self):
        return f"{self.participant.name}: {self.user_answer} (Q{self.question_number}, {self.points_earned} pts)"


class BlackJackSession(SyncBase):
    """Tracks the current state of a live blackjack quiz session"""
    quiz = models.OneToOneField(BlackJackQuiz, on_delete=models.CASCADE, related_name='session')
    current_question_number = models.IntegerField(default=0)
    total_questions_sent = models.IntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    
    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    average_points_current_question = models.FloatField(default=0)
    busted_participants_count = models.IntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def send_question(self, question):
        """Send a question to all participants"""
        self.quiz.current_question = question
        self.quiz.question_start_time = timezone.now()
        self.current_question_number += 1
        self.quiz.current_question_number = self.current_question_number
        self.total_questions_sent += 1
        self.is_question_active = True
        self.question_end_time = timezone.now() + timezone.timedelta(seconds=question.time_limit)
        self.total_responses_current_question = 0
        self.average_points_current_question = 0
        
        self.quiz.save()
        self.save()
    
    def end_current_question(self):
        """End the current active question"""
        self.is_question_active = False
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        
        # Check if quiz should end (after 5 questions)
        if self.current_question_number >= 5:
            self.quiz.end_quiz('completed')
        
        self.quiz.save()
        self.save()
    
    def record_answer(self, points_earned):
        """Record statistics for an answer"""
        self.total_responses_current_question += 1
        
        # Calculate new average points
        current_total_points = (self.average_points_current_question * 
                               (self.total_responses_current_question - 1))
        self.average_points_current_question = (
            (current_total_points + points_earned) / self.total_responses_current_question
        )
        
        # Update busted count
        self.busted_participants_count = self.quiz.participants.filter(is_busted=True).count()
        
        self.save()
    
    def get_current_question_stats(self):
        """Get statistics for current question"""
        active_participants = self.quiz.get_active_participants().count()
        return {
            'question_number': self.current_question_number,
            'total_responses': self.total_responses_current_question,
            'average_points': round(self.average_points_current_question, 1),
            'participation_rate': (
                (self.total_responses_current_question / 
                 max(1, active_participants)) * 100
            ),
            'busted_count': self.busted_participants_count,
        }
    
    def __str__(self):
        return f"BlackJack Session for {self.quiz.room_code} (Q{self.current_question_number}/5)"