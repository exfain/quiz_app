from django.db import models, transaction
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string


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
    tutorial_question = models.ForeignKey('WhoThatQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='tutorial_in_quizzes')
    
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
    
    def start_quiz(self, session_code=None):
        if self.status == 'inactive' and self.started_at:
            from games_hub.quiz_pause import resume
            resume(self, 'who_that', session_code)
            self.refresh_from_db()
            return
        if self.status == 'active' and self.started_at:
            return
        if self.status == 'waiting' or not self.started_at:
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
        from games_hub.authoritative_state import reset_question_flow
        from games_hub.models import GameRuntimeState
        from games_hub.quiz_pause import session_code_for
        reset_question_flow(game_key='who_that', room_code=self.room_code,
            session_code=session_code or session_code_for(self, 'who_that'),
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE)
    
    def end_quiz(self, status='completed'):
        from .runtime import end_game
        end_game(self.pk, status=status)
        self.refresh_from_db()
        return
    
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

    def check_answer(self, user_answer, *, threshold=100):
        """Match names and explicit aliases at the requested text threshold."""
        from games_hub.text_matching import matches
        return any(matches(user_answer, answer, threshold=threshold)
                   for answer in [self.correct_answer, *(self.alternative_answers or [])])

    def get_match_quality(self, user_answer):
        from games_hub.text_matching import similarity
        return max((similarity(user_answer, answer) for answer in [self.correct_answer, *(self.alternative_answers or [])]), default=0) / 100

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
    score_adjustment = models.IntegerField(default=0)
    questions_answered = models.IntegerField(default=0)
    correct_answers = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)
    
    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['-total_score', 'name']
    
    @transaction.atomic
    def calculate_score(self):
        """Recalculate total score based on answers"""
        self.score_adjustment = type(self).objects.select_for_update().get(pk=self.pk).score_adjustment
        answers = self.who_that_answers.all()
        self.total_score = sum(answer.points_earned for answer in answers) + self.score_adjustment
        self.questions_answered = answers.count()
        self.correct_answers = answers.filter(is_correct=True).count()
        self.save(update_fields=['total_score', 'questions_answered', 'correct_answers', 'updated_at'])
        return self.total_score
    
    def get_rank(self):
        """Get participant's rank in the quiz"""
        higher_scores = WhoThatParticipant.objects.filter(
            quiz=self.quiz,
            hub_session_code=self.hub_session_code,
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
    round_number = models.PositiveIntegerField(default=0)
    is_tutorial = models.BooleanField(default=False)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question', 'round_number']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-calculate correctness and points on creation
        if not self.pk:
            from games_hub.quiz_rounds import current_round
            self.round_number = self.round_number or current_round(self.quiz)
            from games_hub.text_matching import threshold_for
            self.is_correct = self.question.check_answer(self.user_answer, threshold=threshold_for(self.participant))
        self.points_earned = int(self.is_correct and not self.is_tutorial)
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
    question_rounds = models.JSONField(default=list, blank=True)
    
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
        self.question_rounds = []
        self.total_responses_current_question = 0
        self.correct_responses_current_question = 0
        self.average_response_time_current_question = 0
        self.save(update_fields=[
            'current_question_number',
            'total_questions_sent',
            'is_question_active',
            'question_end_time',
            'pending_answers',
            'question_rounds',
            'total_responses_current_question',
            'correct_responses_current_question',
            'average_response_time_current_question',
            'updated_at',
        ])
    
    def prepare_question(self, question):
        """Persist a question without opening its answer window."""
        from games_hub.quiz_rounds import record_round
        record_round(self, question)
        self.quiz.current_question = question
        self.quiz.question_start_time = None
        self.current_question_number += 1
        self.total_questions_sent += 1
        self.is_question_active = False
        self.question_end_time = None
        self.total_responses_current_question = 0
        self.correct_responses_current_question = 0
        self.average_response_time_current_question = 0
        self.pending_answers = {}
        
        self.quiz.save(update_fields=['current_question', 'question_start_time'])
        self.save()

    def open_answering(self, question, *, started_at, answer_duration_seconds):
        """Open the prepared question using the authoritative phase timestamp."""
        if self.quiz.current_question_id != question.id:
            raise ValueError('Cannot open answering for a different question.')
        duration = float(answer_duration_seconds)
        self.quiz.question_start_time = started_at
        self.is_question_active = True
        self.question_end_time = started_at + timezone.timedelta(seconds=duration)
        self.quiz.save(update_fields=['question_start_time'])
        self.save(update_fields=[
            'is_question_active',
            'question_end_time',
            'updated_at',
        ])
    
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
