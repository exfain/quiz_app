from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.utils import timezone
import random
import string

class SortingLadderGame(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    title = models.CharField(max_length=200, default="Sorting Ladder")
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_sorting_games')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    max_participants = models.IntegerField(default=50)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # The topic currently being played (e.g., "Countries by Size")
    current_question = models.ForeignKey('SortingQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='active_in_games')
    selected_questions = models.ManyToManyField('SortingQuestion', blank=True, related_name='games')
    tutorial_question = models.ForeignKey('SortingQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='tutorial_in_games')
    
    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not SortingLadderGame.objects.filter(room_code=code).exists():
                return code

    def start_quiz(self):
        self.status = 'active'
        self.started_at = timezone.now()
        self.save()
    
    def end_quiz(self):
        self.status = 'completed'
        self.ended_at = timezone.now()
        self.save()

    def __str__(self):
        return f"{self.title} ({self.room_code})"


class SortingLadderParticipant(SyncBase):
    quiz = models.ForeignKey(SortingLadderGame, on_delete=models.CASCADE, related_name='participants')
    name = models.CharField(max_length=100)
    joined_at = models.DateTimeField(auto_now_add=True)
    
    # Active status controls connection, eliminated controls game logic
    is_active = models.BooleanField(default=True) 
    is_eliminated = models.BooleanField(default=False)

    total_score = models.IntegerField(default=0)
    rounds_survived = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)

    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['-rounds_survived', 'name']

    def eliminate(self):
        self.is_eliminated = True
        self.save()

    def calculate_total_score(self):
        """Recalculate this participant's total_score for this quiz.

        Scoring rule per question:
            total_for_question = correct_rounds_for_question * question.points

        where correct_rounds_for_question is the number of RoundSubmission rows
        for this (quiz, participant, question) with is_correct=True. The
        per-question scores are then summed across all questions in this quiz.
        """
        from collections import Counter

        # Import here to avoid circular imports at module load time if this
        # method is called during migrations.
        from .models import RoundSubmission, SortingQuestion  # type: ignore
        from games_hub.unit_tutorial_runtime import get_unit_tutorial_state

        # All correct submissions for this participant in this quiz
        qs = RoundSubmission.objects.filter(quiz=self.quiz, participant=self, is_correct=True)
        tutorial_state = get_unit_tutorial_state(
            'sorting_ladder',
            self.quiz.room_code,
            self.hub_session_code,
        )
        tutorial_question_id = tutorial_state.get('tutorial_question_id') if tutorial_state.get('requested') else None
        if tutorial_question_id:
            qs = qs.exclude(question_id=tutorial_question_id)
        qs = qs.values_list('question_id', flat=True)

        counts = Counter(qs)
        if not counts:
            self.total_score = 0
            self.save(update_fields=['total_score'])
            return self.total_score

        question_ids = list(counts.keys())
        points_map = {
            q.id: q.points
            for q in SortingQuestion.objects.filter(id__in=question_ids)
        }

        total = 0
        for qid, rounds_won in counts.items():
            points = points_map.get(qid, 0)
            total += rounds_won * points

        self.total_score = total
        self.save(update_fields=['total_score'])
        return self.total_score

    def __str__(self):
        status = "Eliminated" if self.is_eliminated else "Alive"
        return f"{self.name} ({status}) in {self.quiz.room_code}"


class SortingBundle(SyncBase):
    """A reusable, named collection of topics that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('SortingQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sorting_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class SortingQuestion(SyncBase):
    """
    Represents the category or question, e.g., 'Sort Countries from Smallest to Largest'.
    """
    question_text = models.TextField()
    description = models.TextField(blank=True, help_text="Instructions for the players")
    upper_label = models.CharField(max_length=50, default="Ascending", help_text="Label representing the highest extreme (e.g., Largest, Oldest, Highest)")
    lower_label = models.CharField(max_length=50, default="Descending", help_text="Label representing the lowest extreme (e.g., Smallest, Youngest, Lowest)")
    points = models.IntegerField(default=10)
    round_time_limit = models.PositiveIntegerField(default=30, help_text="Per-round time limit in seconds")
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_sorting_topics')
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    starting_item = models.ForeignKey('SortingItem', on_delete=models.SET_NULL, null=True, blank=True, related_name='starting_in_topics')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.question_text


class SortingItem(SyncBase):
    """
    The individual items to be sorted, e.g., 'Germany', 'Egypt'.
    """
    topic = models.ForeignKey(SortingQuestion, on_delete=models.CASCADE, related_name='elements')
    text = models.CharField(max_length=200)
    
    # The correct mathematical/logical order (e.g., 1=Smallest, 100=Largest)
    correct_rank = models.DecimalField(max_digits=10, decimal_places=2, help_text="Lower numbers come first")
    
    # Optional image for the element
    image_url = models.URLField(blank=True, null=True)

    class Meta:
        ordering = ['correct_rank']
        unique_together = ['topic', 'correct_rank']

    def __str__(self):
        return f"{self.text} (Rank: {self.correct_rank})"


class RoundSubmission(SyncBase):
    """
    Records a player's move for a specific round.
    """
    quiz = models.ForeignKey(SortingLadderGame, on_delete=models.CASCADE, related_name='submissions')
    participant = models.ForeignKey(SortingLadderParticipant, on_delete=models.CASCADE, related_name='submissions')
    question = models.ForeignKey(SortingQuestion, on_delete=models.CASCADE, related_name='submissions')
    all_elements = models.JSONField(help_text="List of all elements submitted in that round", default=list)
    is_correct = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-submitted_at']

    def save(self, *args, **kwargs):
        """On first save, compute is_correct from the submitted ordering.

        Expects all_elements to be a list/array of SortingItem IDs in the
        order chosen by the player for that round. A submission is marked
        correct if the corresponding items are in non-decreasing
        correct_rank order.
        """
        # Only attempt to compute correctness on initial insert
        if self.pk is None and self.all_elements:
            try:
                element_ids = [int(x) for x in self.all_elements]
            except (TypeError, ValueError):
                element_ids = []

            if element_ids:
                from .models import SortingItem  # type: ignore

                items = list(SortingItem.objects.filter(id__in=element_ids))
                if len(items) == len(element_ids):
                    rank_map = {item.id: item.correct_rank for item in items}
                    is_correct = True
                    for idx in range(len(element_ids) - 1):
                        if rank_map[element_ids[idx]] > rank_map[element_ids[idx + 1]]:
                            is_correct = False
                            break
                    self.is_correct = is_correct

        return super().save(*args, **kwargs)



        
class SortingLadderSession(SyncBase):
    """
    Manages the live state of the quiz rounds.
    """
    quiz = models.OneToOneField(SortingLadderGame, on_delete=models.CASCADE, related_name='session')
    
    current_round = models.PositiveIntegerField(default=0)
    
    # State flags
    is_round_active = models.BooleanField(default=False)
    round_start_time = models.DateTimeField(null=True, blank=True)
    round_end_time = models.DateTimeField(null=True, blank=True)
    time_limit_seconds = models.IntegerField(default=30)

    # The Logic:
    # 1. placed_elements: These are the reference points currently visible (The "Ladder").
    # 2. active_element: This is the ONE item players must drag into the correct gap.
    # 3. shuffled_item_ids: Comma-separated IDs representing the shared shuffled
    #    order of SortingItem records for the current question. All participants
    #    see/operate over this same order.
    placed_elements = models.ManyToManyField(SortingItem, related_name='sessions_as_placed', blank=True)
    active_element = models.ForeignKey(SortingItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='sessions_as_active')
    shuffled_item_ids = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def start_next_round(self, next_element):
        """
        Advances the quiz. The previous active element becomes 'placed', 
        and a new element becomes 'active'.
        """
        if self.active_element:
            self.placed_elements.add(self.active_element)
        
        self.active_element = next_element
        self.current_round += 1
        self.is_round_active = True
        self.round_start_time = timezone.now()
        self.round_end_time = timezone.now() + timezone.timedelta(seconds=self.time_limit_seconds)
        
        self.save()
        self.quiz.save()

    def end_round(self):
        self.is_round_active = False
        self.save()

    def get_remaining_survivors(self):
        return self.quiz.participants.filter(is_eliminated=False, is_active=True).count()

    def __str__(self):
        return f"Session for {self.quiz.room_code} - Round {self.current_round}"
