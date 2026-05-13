from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string


class ClueRushGame(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    title = models.CharField(max_length=200, default="Clue Rush")
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_cluerush_games')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    max_participants = models.IntegerField(default=50)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    current_clue = models.ForeignKey('Clue', on_delete=models.SET_NULL, null=True, blank=True,related_name='current_in_games')
    clue_start_time = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('ClueQuestion', on_delete=models.SET_NULL, null=True, blank=True,related_name='current_in_games')
    question_start_time = models.DateTimeField(null=True, blank=True)

    selected_questions = models.ManyToManyField('ClueQuestion', blank=True, related_name='games')
    
    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not ClueRushGame.objects.filter(room_code=code).exists():
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



class ClueRushParticipant(SyncBase):
    quiz = models.ForeignKey(ClueRushGame,on_delete=models.CASCADE, related_name='participants')
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    # has_guessed = models.BooleanField(default=False)
    # guess_correct = models.BooleanField(default=False)

    total_score = models.IntegerField(default=0)
    questions_answered = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    # guess_clue_number = models.PositiveIntegerField(null=True, blank=True)

    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)

    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['-total_score', 'name']

    def calculate_score(self):
        self.total_score = sum(answer.points_earned for answer in self.clue_answers.filter(is_correct=True))
        self.questions_answered = self.clue_answers.count()
        self.save()

    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class ClueQuestion(SyncBase):
    # quiz = models.ForeignKey(ClueRushGame, on_delete=models.CASCADE, related_name='questions')
    question_text = models.TextField()
    points = models.IntegerField(default=10)
    time_limit = models.PositiveIntegerField(default=30, help_text="Time limit in seconds")
    answer = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_clue_questions')

    class Meta:
        ordering = ['-created_at']

    def is_correct_answer(self, answer):
        return answer.lower().strip() == self.answer.lower().strip()

    def __str__(self):
        return f"{self.question_text[:50]}..."

class Clue(SyncBase):
    clue_question = models.ForeignKey(
        ClueQuestion,
        on_delete=models.CASCADE,
        related_name='clues'
    )

    clue_text = models.TextField()
    order = models.PositiveIntegerField(help_text="Clue order (1 = first clue)")
    duration = models.PositiveIntegerField(
        default=10,
        help_text="Seconds this clue stays visible"
    )

    class Meta:
        ordering = ['order']
        unique_together = ['clue_question', 'order']

    def __str__(self):
        return f"Clue {self.order}: {self.clue_text[:40]}"


class ClueAnswer(SyncBase):
    quiz = models.ForeignKey(ClueRushGame, on_delete=models.CASCADE, related_name='clue_answers')
    participant = models.ForeignKey(ClueRushParticipant, on_delete=models.CASCADE, related_name='clue_answers')
    question = models.ForeignKey(ClueQuestion, on_delete=models.CASCADE, related_name='clue_answers')
    answer_text = models.CharField(max_length=200)
    is_correct = models.BooleanField(default=False)
    points_earned = models.IntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)

    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']

    def save(self, *args, **kwargs):
        if not self.pk:
            correct = self.answer_text.strip().lower() == self.question.answer.strip().lower()
            self.is_correct = correct

            total_clues = self.question.clues.count()
            current_clue_number = self.quiz.session.current_clue_number
            # Use the answer's own question reference as stable scoring source.
            # Avoid coupling to mutable quiz.current_question state.
            points = self.question.points
            if correct:
                self.points_earned = points + (total_clues - current_clue_number + 1)
            else:
                self.points_earned = 0

        super().save(*args, **kwargs)

        # Update participant's total score
        self.participant.calculate_score()

class ClueRushSession(SyncBase):
    quiz = models.OneToOneField(ClueRushGame, on_delete=models.CASCADE, related_name='session')
    total_questions_sent = models.PositiveIntegerField(default=0)
    current_question_number = models.PositiveIntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    current_clue_number = models.PositiveIntegerField(default=0)
    is_clue_active = models.BooleanField(default=False)
    clue_end_time = models.DateTimeField(null=True, blank=True)

    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    correct_responses_current_question = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def start_next_clue(self):
        # next_clue = self.quiz.clues.filter(order=self.current_clue_number + 1).first()
        next_clue = Clue.objects.filter(clue_question__game=self.quiz, order=self.current_clue_number + 1).first()
        if not next_clue:
            self.end_game()
            return

        self.current_clue_number = next_clue.order
        self.quiz.current_clue = next_clue
        self.quiz.clue_start_time = timezone.now()

        self.is_clue_active = True
        self.clue_end_time = timezone.now() + timezone.timedelta(seconds=next_clue.duration)

        self.quiz.save()
        self.save()

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
    

    def end_game(self):
        self.quiz.status = 'completed'
        self.quiz.ended_at = timezone.now()
        self.quiz.current_clue = None

        self.is_clue_active = False

        self.quiz.save()
        self.save()

    def __str__(self):
        return f"ClueRush Session ({self.quiz.room_code})"
