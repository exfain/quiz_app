from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string
import json
import math


class WhereQuiz(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200, default="Where is this?")
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_where_quizzes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('WhereQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_in_where_quiz')
    question_start_time = models.DateTimeField(null=True, blank=True)
    max_participants = models.IntegerField(default=50)
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('WhereQuestion', blank=True, related_name='quizzes')
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not WhereQuiz.objects.filter(room_code=code).exists():
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


class WhereBundle(SyncBase):
    """A reusable, named collection of questions that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('WhereQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='where_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class WhereQuestion(SyncBase):
    """Location-based questions for 'Where is this?' game"""
    
    question_text = models.TextField(help_text="The question, e.g., 'Where is the Eiffel Tower?'")
    image = models.ImageField(upload_to='where_questions/', blank=True, null=True, help_text="Optional image of the location")
    
    # Correct location coordinates
    correct_latitude = models.FloatField(help_text="Latitude of the correct location")
    correct_longitude = models.FloatField(help_text="Longitude of the correct location")
    
    # Scoring settings
    points = models.PositiveIntegerField(default=100, help_text="Maximum points for exact answer")
    time_limit = models.PositiveIntegerField(default=60, help_text="Time limit in seconds")
    
    # Distance thresholds for scoring (in kilometers)
    perfect_distance = models.FloatField(default=10, help_text="Distance in km for full points")
    good_distance = models.FloatField(default=100, help_text="Distance in km for 75% points")
    fair_distance = models.FloatField(default=500, help_text="Distance in km for 50% points")
    poor_distance = models.FloatField(default=2000, help_text="Distance in km for 25% points")
    
    hint_text = models.TextField(blank=True, help_text="Optional hint text", null=True)
    explanation = models.TextField(blank=True, null=True, help_text="Optional explanation shown after answering")
    
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_where_questions')
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def calculate_distance(self, lat1, lon1):
        """Calculate distance between two points using Haversine formula"""
        lat2, lon2 = self.correct_latitude, self.correct_longitude
        
        # Convert latitude and longitude from degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        
        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        # Radius of earth in kilometers
        r = 6371
        
        return c * r
    
    def calculate_score(self, user_latitude, user_longitude):
        """Calculate score based on distance accuracy"""
        distance = self.calculate_distance(user_latitude, user_longitude)
        
        if distance <= self.perfect_distance:
            return self.points  # 100%
        elif distance <= self.good_distance:
            return int(self.points * 0.75)  # 75%
        elif distance <= self.fair_distance:
            return int(self.points * 0.5)   # 50%
        elif distance <= self.poor_distance:
            return int(self.points * 0.25)  # 25%
        else:
            return 0  # 0%
    
    def get_accuracy_percentage(self, user_latitude, user_longitude):
        """Get accuracy percentage based on distance"""
        distance = self.calculate_distance(user_latitude, user_longitude)
        
        if distance <= self.perfect_distance:
            return 100
        elif distance <= self.good_distance:
            return 75
        elif distance <= self.fair_distance:
            return 50
        elif distance <= self.poor_distance:
            return 25
        else:
            return 0
    
    def __str__(self):
        return f"{self.question_text[:50]}..." if len(self.question_text) > 50 else self.question_text


class WhereParticipant(SyncBase):
    quiz = models.ForeignKey(WhereQuiz, on_delete=models.CASCADE, related_name='participants')
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
        total_score = sum(answer.points_earned for answer in self.where_answers.all())
        self.total_score = total_score
        self.questions_answered = self.where_answers.count()
        self.save()
        return self.total_score
    
    def get_rank(self):
        """Get participant's rank in the quiz"""
        higher_scores = WhereParticipant.objects.filter(
            quiz=self.quiz,
            total_score__gt=self.total_score
        ).count()
        return higher_scores + 1
    
    def get_average_accuracy(self):
        """Get average accuracy percentage"""
        answers = self.where_answers.all()
        if not answers:
            return 0
        
        total_accuracy = sum(answer.accuracy_percentage for answer in answers)
        return round(total_accuracy / answers.count(), 1)
    
    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class WhereAnswer(SyncBase):
    quiz = models.ForeignKey(WhereQuiz, on_delete=models.CASCADE, related_name='where_answers')
    participant = models.ForeignKey(WhereParticipant, on_delete=models.CASCADE, related_name='where_answers')
    question = models.ForeignKey(WhereQuestion, on_delete=models.CASCADE, related_name='where_answers')
    
    # User's answer coordinates
    user_latitude = models.FloatField(help_text="User's selected latitude")
    user_longitude = models.FloatField(help_text="User's selected longitude")
    
    # Calculated results
    distance_km = models.FloatField(help_text="Distance from correct answer in km")
    points_earned = models.IntegerField(default=0)
    accuracy_percentage = models.IntegerField(default=0, help_text="Accuracy percentage (0-100)")
    
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-calculate distance, points, and accuracy on creation
        if not self.pk:
            self.distance_km = self.question.calculate_distance(self.user_latitude, self.user_longitude)
            self.points_earned = self.question.calculate_score(self.user_latitude, self.user_longitude)
            self.accuracy_percentage = self.question.get_accuracy_percentage(self.user_latitude, self.user_longitude)
        
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()
    
    def get_accuracy_category(self):
        """Get accuracy category for display purposes"""
        if self.accuracy_percentage == 100:
            return 'perfect'
        elif self.accuracy_percentage >= 75:
            return 'excellent'
        elif self.accuracy_percentage >= 50:
            return 'good'
        elif self.accuracy_percentage >= 25:
            return 'fair'
        else:
            return 'poor'
    
    def get_formatted_distance(self):
        """Get human-readable distance string"""
        if self.distance_km < 1:
            return f"{int(self.distance_km * 1000)}m"
        elif self.distance_km < 100:
            return f"{self.distance_km:.1f}km"
        else:
            return f"{int(self.distance_km)}km"
    
    def __str__(self):
        return f"{self.participant.name}: {self.get_formatted_distance()} ({self.accuracy_percentage}% accuracy)"


class WhereSession(SyncBase):
    """Tracks the current state of a live 'Where is this?' quiz session"""
    quiz = models.OneToOneField(WhereQuiz, on_delete=models.CASCADE, related_name='session')
    current_question_number = models.IntegerField(default=0)
    total_questions_sent = models.IntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    
    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    average_distance_current_question = models.FloatField(default=0)
    average_accuracy_current_question = models.FloatField(default=0)
    
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
        self.average_distance_current_question = 0
        self.average_accuracy_current_question = 0
        
        self.quiz.save()
        self.save()
    
    def end_current_question(self):
        """End the current active question"""
        self.is_question_active = False
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        
        self.quiz.save()
        self.save()
    
    def record_answer(self, distance_km, accuracy_percentage):
        """Record statistics for an answer"""
        self.total_responses_current_question += 1
        
        # Calculate new averages
        current_total_distance = (self.average_distance_current_question * 
                                 (self.total_responses_current_question - 1))
        self.average_distance_current_question = (
            (current_total_distance + distance_km) / self.total_responses_current_question
        )
        
        current_total_accuracy = (self.average_accuracy_current_question * 
                                 (self.total_responses_current_question - 1))
        self.average_accuracy_current_question = (
            (current_total_accuracy + accuracy_percentage) / self.total_responses_current_question
        )
        
        self.save()
    
    def get_current_question_stats(self):
        """Get statistics for current question"""
        return {
            'total_responses': self.total_responses_current_question,
            'average_distance': round(self.average_distance_current_question, 1),
            'average_accuracy': round(self.average_accuracy_current_question, 1),
            'participation_rate': (
                (self.total_responses_current_question / 
                 max(1, self.quiz.get_active_participants().count())) * 100
            )
        }
    
    def __str__(self):
        return f"Session for {self.quiz.room_code}"
