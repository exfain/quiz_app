from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string
import json


class WhoQuiz(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200, default="Who is Lying?")
    internal_description = models.TextField(blank=True, default='')
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_who_quizzes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('WhoQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_in_who_quiz')
    question_start_time = models.DateTimeField(null=True, blank=True)
    max_participants = models.IntegerField(default=50)
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('WhoQuestion', blank=True, related_name='quizzes')
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not WhoQuiz.objects.filter(room_code=code).exists():
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


class WhoBundle(SyncBase):
    """A reusable, named collection of questions that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('WhoQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='who_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class WhoQuestion(SyncBase):
    """Who is lying questions with statement and people to evaluate"""
    
    statement = models.TextField(help_text="The statement that may be true or false for different people")
    points = models.PositiveIntegerField(default=10, help_text="Points per correct identification")
    time_limit = models.PositiveIntegerField(default=60, help_text="Time limit in seconds")
    
    # JSON fields to store people and their truth values
    people = models.JSONField(help_text="List of people with their names and whether they are lying")
    # Example: [{"name": "Robin Williams", "is_lying": false}, {"name": "Cristiano Ronaldo", "is_lying": true}]
    
    explanation = models.TextField(blank=True, help_text="Optional explanation shown after answering")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_who_questions')
    
    class Meta:
        ordering = ['-created_at']
    
    def get_total_possible_points(self):
        """Return total possible points for this question"""
        return len(self.people) * self.points
    
    def get_liars(self):
        """Return list of people who are lying"""
        return [person for person in self.people if person.get('is_lying', False)]
    
    def get_truth_tellers(self):
        """Return list of people who are telling the truth"""
        return [person for person in self.people if not person.get('is_lying', False)]
    
    def calculate_score(self, selected_liars):
        """Calculate score based on user's selected liars"""
        if not selected_liars or not self.people:
            return 0
        
        correct_count = 0
        total_people = len(self.people)
        
        for i, person in enumerate(self.people):
            is_actually_lying = person.get('is_lying', False)
            is_selected_as_liar = i in selected_liars
            
            # Score for correct identification (both liars and truth-tellers)
            if is_actually_lying == is_selected_as_liar:
                correct_count += 1
        
        # Calculate points based on accuracy
        accuracy = correct_count / total_people
        return int(accuracy * self.get_total_possible_points())
    
    def get_randomized_people(self, room_code=None):
        """Return people shuffled for gameplay"""
        import random
        
        # Create a deterministic seed based on question ID and room code
        seed_string = f"{self.id}_{room_code or 'default'}"
        seed = abs(hash(seed_string)) % (2**32)
        
        # Save the current random state
        random_state = random.getstate()
        
        try:
            # Set our deterministic seed
            random.seed(seed)
            
            # Create a copy of people with original indices
            people_with_indices = [(i, person) for i, person in enumerate(self.people)]
            
            # Shuffle the people deterministically
            shuffled_people = people_with_indices.copy()
            random.shuffle(shuffled_people)
            
            # Create mapping from shuffled position to original index
            position_to_original = {}
            shuffled_people_formatted = []
            
            for new_pos, (original_idx, person) in enumerate(shuffled_people):
                position_to_original[new_pos] = original_idx
                shuffled_people_formatted.append({
                    'id': new_pos, 
                    'name': person['name'],
                    'original_index': original_idx
                })
            
        finally:
            # Always restore the previous random state
            random.setstate(random_state)
        
        return {
            'people': shuffled_people_formatted,
            'position_to_original': position_to_original  # Maps shuffled position to original index
        }
    
    def __str__(self):
        return f"{self.statement[:50]}..." if len(self.statement) > 50 else self.statement


class WhoParticipant(SyncBase):
    quiz = models.ForeignKey(WhoQuiz, on_delete=models.CASCADE, related_name='participants')
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
        total_score = sum(answer.points_earned for answer in self.who_answers.all())
        self.total_score = total_score
        self.questions_answered = self.who_answers.count()
        self.save()
        return self.total_score
    
    def get_rank(self):
        """Get participant's rank in the quiz"""
        higher_scores = WhoParticipant.objects.filter(
            quiz=self.quiz,
            total_score__gt=self.total_score
        ).count()
        return higher_scores + 1
    
    def get_average_accuracy(self):
        """Get average accuracy across all answers"""
        answers = self.who_answers.all()
        if not answers:
            return 0
        
        total_accuracy = sum(answer.get_accuracy_percentage() for answer in answers)
        return round(total_accuracy / len(answers), 1)
    
    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class WhoAnswer(SyncBase):
    quiz = models.ForeignKey(WhoQuiz, on_delete=models.CASCADE, related_name='who_answers')
    participant = models.ForeignKey(WhoParticipant, on_delete=models.CASCADE, related_name='who_answers')
    question = models.ForeignKey(WhoQuestion, on_delete=models.CASCADE, related_name='who_answers')
    
    # Store the user's selected liars as list of indices (in original question order)
    selected_liars = models.JSONField(help_text="List of indices of people selected as liars")
    points_earned = models.IntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-calculate points on creation
        if not self.pk:
            self.points_earned = self.question.calculate_score(self.selected_liars or [])
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()
    
    def get_correct_identifications_count(self):
        """Get number of people correctly identified (both liars and truth-tellers)"""
        if not self.selected_liars or not self.question.people:
            return 0
        
        correct_count = 0
        for i, person in enumerate(self.question.people):
            is_actually_lying = person.get('is_lying', False)
            is_selected_as_liar = i in (self.selected_liars or [])
            
            if is_actually_lying == is_selected_as_liar:
                correct_count += 1
        
        return correct_count
    
    def get_total_people_count(self):
        """Get total number of people in the question"""
        return len(self.question.people) if self.question.people else 0
    
    def get_accuracy_percentage(self):
        """Get accuracy percentage for this answer"""
        total_people = self.get_total_people_count()
        if total_people == 0:
            return 0
        correct = self.get_correct_identifications_count()
        return round((correct / total_people) * 100, 1)
    
    def get_selected_liars_names(self):
        """Get names of people selected as liars"""
        if not self.selected_liars or not self.question.people:
            return []
        
        selected_names = []
        for index in self.selected_liars:
            if 0 <= index < len(self.question.people):
                selected_names.append(self.question.people[index]['name'])
        return selected_names
    
    def get_actual_liars_names(self):
        """Get names of people who are actually lying"""
        if not self.question.people:
            return []
        
        return [person['name'] for person in self.question.people if person.get('is_lying', False)]
    
    def get_detailed_analysis(self):
        """Get detailed analysis of the answer"""
        if not self.question.people:
            return {}
        
        analysis = {
            'correct_liars': [],      # Correctly identified as liars
            'missed_liars': [],       # Actually liars but not selected
            'false_accusations': [],  # Selected as liars but actually truth-tellers
            'correct_truth_tellers': [] # Correctly identified as truth-tellers
        }
        
        for i, person in enumerate(self.question.people):
            is_actually_lying = person.get('is_lying', False)
            is_selected_as_liar = i in (self.selected_liars or [])
            person_name = person['name']
            
            if is_actually_lying and is_selected_as_liar:
                analysis['correct_liars'].append(person_name)
            elif is_actually_lying and not is_selected_as_liar:
                analysis['missed_liars'].append(person_name)
            elif not is_actually_lying and is_selected_as_liar:
                analysis['false_accusations'].append(person_name)
            else:  # not is_actually_lying and not is_selected_as_liar
                analysis['correct_truth_tellers'].append(person_name)
        
        return analysis
    
    def __str__(self):
        return f"{self.participant.name}: {self.get_correct_identifications_count()}/{self.get_total_people_count()} correct"


class WhoSession(SyncBase):
    """Tracks the current state of a live who is lying quiz session"""
    quiz = models.OneToOneField(WhoQuiz, on_delete=models.CASCADE, related_name='session')
    current_question_number = models.IntegerField(default=0)
    total_questions_sent = models.IntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    
    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    average_score_current_question = models.FloatField(default=0)
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
        self.average_score_current_question = 0
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
    
    def record_answer(self, points_earned, accuracy_percentage):
        """Record statistics for an answer"""
        self.total_responses_current_question += 1
        
        # Calculate new average score
        current_total_score = (self.average_score_current_question * 
                              (self.total_responses_current_question - 1))
        self.average_score_current_question = (
            (current_total_score + points_earned) / self.total_responses_current_question
        )
        
        # Calculate new average accuracy
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
            'average_score': round(self.average_score_current_question, 1),
            'average_accuracy': round(self.average_accuracy_current_question, 1),
            'participation_rate': (
                (self.total_responses_current_question / 
                 max(1, self.quiz.get_active_participants().count())) * 100
            )
        }
    
    def __str__(self):
        return f"Session for {self.quiz.room_code}"