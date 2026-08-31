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
    SCORING_MODE_CHOICES = [
        ('simple', 'Einfach'),
        ('rank', 'Ranking'),
    ]
    
    title = models.CharField(max_length=200, default="Black Jack Quiz")
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
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
    total_questions = models.IntegerField(default=5)
    scoring_mode = models.CharField(max_length=10, choices=SCORING_MODE_CHOICES, default='simple')
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('BlackJackQuestion', blank=True, related_name='quizzes')
    tutorial_set_number = models.PositiveIntegerField(null=True, blank=True)
    
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

    def get_current_run_answers(self):
        if self.status == 'waiting':
            return self.blackjack_answers.none()
        answers = self.blackjack_answers.all()
        if self.started_at:
            answers = answers.filter(submitted_at__gte=self.started_at)
        return answers

    def get_hub_session_for_runtime(self, hub_session_code=None):
        from games_hub.models import HubGameStep, HubSession

        if hub_session_code:
            return HubSession.objects.filter(code=hub_session_code).first()

        steps = HubGameStep.objects.select_related('session').filter(
            game_key='blackjack',
            room_code=self.room_code,
        )
        active_step = steps.filter(
            session__is_active=True,
            session__ended_at__isnull=True,
        ).order_by('-id').first()
        step = active_step or steps.order_by('-id').first()
        return step.session if step else None

    def has_runtime_state(self):
        try:
            session = self.session
        except BlackJackSession.DoesNotExist:
            session = None

        return bool(
            self.status != 'waiting'
            or self.started_at is not None
            or self.ended_at is not None
            or self.current_question_id
            or self.current_question_number > 0
            or self.question_start_time is not None
            or self.tutorial_active
            or (session and (
                session.current_question_number > 0
                or session.total_questions_sent > 0
                or session.completed_sets_count > 0
                or session.is_question_active
                or session.question_end_time is not None
                or bool(session.asked_question_ids or [])
                or bool(session.finalized_set_numbers or [])
            ))
        )

    def is_runtime_stale_for_hub_session(self, hub_session_code=None):
        hub_session = self.get_hub_session_for_runtime(hub_session_code)
        if not hub_session or not self.has_runtime_state():
            return False

        if hub_session.started_at is None:
            return True

        if self.started_at is None:
            return True

        return self.started_at < hub_session.started_at

    def reset_runtime_state_for_new_hub_session(self):
        try:
            session = self.session
        except BlackJackSession.DoesNotExist:
            session = None

        self.status = 'waiting'
        self.tutorial_active = False
        self.started_at = None
        self.ended_at = None
        self.current_question = None
        self.current_question_number = 0
        self.question_start_time = None
        self.save(update_fields=[
            'status',
            'tutorial_active',
            'started_at',
            'ended_at',
            'current_question',
            'current_question_number',
            'question_start_time',
        ])

        if session:
            session.reset_for_new_run()

    def ensure_runtime_scoped_to_hub_session(self, hub_session_code=None):
        if self.is_runtime_stale_for_hub_session(hub_session_code):
            self.reset_runtime_state_for_new_hub_session()
            return True
        return False

    def get_active_participants(self, session_code=None):
        if session_code:
            return self.participants.filter(is_active=True, hub_session_code=session_code)
        return self.participants.filter(is_active=True)

    def start_quiz(self):
        try:
            session = self.session
        except BlackJackSession.DoesNotExist:
            session = None

        has_active_question_state = bool(
            self.current_question_id
            or self.question_start_time is not None
            or (session and session.is_question_active)
        )
        should_reset_runtime_state = (
            self.status in {'waiting', 'completed', 'cancelled'}
            or (
                self.status in {'active', 'inactive'}
                and not has_active_question_state
                and self.current_question_number == 0
            )
        )

        if should_reset_runtime_state:
            self.blackjack_answers.all().delete()
            self.participants.update(
                total_points=0,
                overall_points=0,
                questions_answered=0,
                is_busted=False,
                final_score=0,
            )
            self.current_question = None
            self.current_question_number = 0
            self.question_start_time = None
            self.ended_at = None
            self.save(update_fields=[
                'current_question',
                'current_question_number',
                'question_start_time',
                'ended_at',
            ])
            try:
                self.session.reset_for_new_run()
            except BlackJackSession.DoesNotExist:
                pass
        self.status = 'active'
        self.started_at = timezone.now()
        self.save()
    
    def end_quiz(self, status='completed'):
        self.status = status
        self.ended_at = timezone.now()
        self.save()
    
    def get_questions_per_set(self):
        return max(1, int(self.total_questions or 1))

    @staticmethod
    def _normalize_question_id(raw_id):
        try:
            question_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        return question_id if question_id > 0 else None

    @classmethod
    def build_explicit_question_sets(cls, question_ids=None, default_set_size=1, set_sizes=None):
        normalized_ids = []
        seen_ids = set()
        for raw_id in question_ids or []:
            question_id = cls._normalize_question_id(raw_id)
            if question_id is None or question_id in seen_ids:
                continue
            normalized_ids.append(question_id)
            seen_ids.add(question_id)

        if not normalized_ids:
            return []

        normalized_sizes = []
        if isinstance(set_sizes, str):
            raw_sizes = [part.strip() for part in set_sizes.split(',')]
        elif isinstance(set_sizes, (list, tuple)):
            raw_sizes = list(set_sizes)
        else:
            raw_sizes = []

        for raw_size in raw_sizes:
            if raw_size in (None, ''):
                continue
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                raise ValueError('Set sizes must be whole numbers.')
            if size <= 0:
                raise ValueError('Set sizes must be greater than zero.')
            normalized_sizes.append(size)

        explicit_sets = []
        cursor = 0

        if normalized_sizes:
            for size in normalized_sizes:
                if cursor >= len(normalized_ids):
                    break
                question_set = normalized_ids[cursor:cursor + size]
                if question_set:
                    explicit_sets.append(question_set)
                cursor += size
            if cursor != len(normalized_ids):
                raise ValueError('Set sizes must cover all selected questions exactly.')
            return explicit_sets

        chunk_size = max(1, int(default_set_size or 1))
        while cursor < len(normalized_ids):
            explicit_sets.append(normalized_ids[cursor:cursor + chunk_size])
            cursor += chunk_size
        return explicit_sets

    def _get_legacy_configured_question_ids(self, active_only=True):
        configured_ids = []
        seen_ids = set()

        ordered_ids = []
        for raw_id in self.question_order or []:
            question_id = self._normalize_question_id(raw_id)
            if question_id is not None:
                ordered_ids.append(question_id)

        if ordered_ids:
            ordered_questions = BlackJackQuestion.objects.filter(id__in=ordered_ids)
            if active_only:
                ordered_questions = ordered_questions.filter(is_active=True)
            valid_ordered_ids = set(ordered_questions.values_list('id', flat=True))

            for question_id in ordered_ids:
                if question_id in valid_ordered_ids and question_id not in seen_ids:
                    configured_ids.append(question_id)
                    seen_ids.add(question_id)

        selected_questions = self.selected_questions.all()
        if active_only:
            selected_questions = selected_questions.filter(is_active=True)

        for question_id in selected_questions.values_list('id', flat=True):
            if question_id not in seen_ids:
                configured_ids.append(question_id)
                seen_ids.add(question_id)

        return configured_ids

    def get_explicit_question_sets(self, active_only=True):
        explicit_sets = []
        seen_ids = set()
        raw_order = self.question_order or []

        if isinstance(raw_order, list) and any(isinstance(item, (list, tuple)) for item in raw_order):
            for raw_set in raw_order:
                raw_candidates = raw_set if isinstance(raw_set, (list, tuple)) else [raw_set]
                normalized_set = []
                for raw_id in raw_candidates:
                    question_id = self._normalize_question_id(raw_id)
                    if question_id is None or question_id in seen_ids:
                        continue
                    normalized_set.append(question_id)
                    seen_ids.add(question_id)
                if normalized_set:
                    explicit_sets.append(normalized_set)

        if not explicit_sets:
            legacy_ids = self._get_legacy_configured_question_ids(active_only=False)
            if legacy_ids:
                explicit_sets = self.build_explicit_question_sets(
                    legacy_ids,
                    default_set_size=self.get_questions_per_set(),
                )

        if not explicit_sets:
            return []

        if not active_only:
            return explicit_sets

        allowed_ids = set(
            BlackJackQuestion.objects.filter(
                id__in=[question_id for question_set in explicit_sets for question_id in question_set],
                is_active=True,
            ).values_list('id', flat=True)
        )

        filtered_sets = []
        for question_set in explicit_sets:
            active_question_set = [question_id for question_id in question_set if question_id in allowed_ids]
            if active_question_set:
                filtered_sets.append(active_question_set)
        return filtered_sets

    def get_configured_question_ids(self, active_only=True):
        explicit_sets = self.get_explicit_question_sets(active_only=active_only)
        if explicit_sets:
            return [question_id for question_set in explicit_sets for question_id in question_set]
        return self._get_legacy_configured_question_ids(active_only=active_only)

    def has_configured_question_pool(self):
        return bool(self.question_order) or self.selected_questions.exists()

    def is_configured_question(self, question_id, active_only=True):
        try:
            normalized_question_id = int(question_id)
        except (TypeError, ValueError):
            return False
        return normalized_question_id in self.get_configured_question_ids(active_only=active_only)

    def get_total_game_questions(self):
        configured_count = len(self.get_configured_question_ids())
        if configured_count > 0:
            return configured_count
        return self.get_questions_per_set()

    def get_total_sets(self):
        explicit_sets = self.get_explicit_question_sets()
        if explicit_sets:
            return len(explicit_sets)
        return max(1, math.ceil(self.get_total_game_questions() / self.get_questions_per_set()))

    def get_set_number_for_question_id(self, question_id, active_only=False):
        normalized_question_id = self._normalize_question_id(question_id)
        if normalized_question_id is None:
            return 1

        explicit_sets = self.get_explicit_question_sets(active_only=active_only)
        if explicit_sets:
            for index, question_set in enumerate(explicit_sets, start=1):
                if normalized_question_id in question_set:
                    return index
            return min(len(explicit_sets), 1)

        configured_ids = self.get_configured_question_ids(active_only=active_only)
        if normalized_question_id in configured_ids:
            question_index = configured_ids.index(normalized_question_id)
            return (question_index // self.get_questions_per_set()) + 1
        return 1

    def get_question_number_in_set_for_question_id(self, question_id, active_only=False):
        normalized_question_id = self._normalize_question_id(question_id)
        if normalized_question_id is None:
            return 0

        explicit_sets = self.get_explicit_question_sets(active_only=active_only)
        if explicit_sets:
            for question_set in explicit_sets:
                if normalized_question_id in question_set:
                    return question_set.index(normalized_question_id) + 1
            return 0

        configured_ids = self.get_configured_question_ids(active_only=active_only)
        if normalized_question_id in configured_ids:
            question_index = configured_ids.index(normalized_question_id)
            return (question_index % self.get_questions_per_set()) + 1
        return 0

    def get_set_number_for_question(self, question_number):
        if question_number <= 0:
            return 1
        explicit_sets = self.get_explicit_question_sets()
        if explicit_sets:
            remaining_questions = int(question_number)
            for index, question_set in enumerate(explicit_sets, start=1):
                if remaining_questions <= len(question_set):
                    return index
                remaining_questions -= len(question_set)
            return len(explicit_sets)
        return ((question_number - 1) // self.get_questions_per_set()) + 1

    def get_current_set_number(self):
        if self.current_question_id:
            return self.get_set_number_for_question_id(self.current_question_id, active_only=False)
        try:
            session = self.session
        except Exception:
            session = None
        if session:
            return session.get_normalized_selected_set_number(active_only=True)
        return self.get_set_number_for_question(self.current_question_number)

    def get_question_number_in_set(self, question_number=None, question_id=None):
        if question_id is not None:
            return self.get_question_number_in_set_for_question_id(question_id, active_only=False)
        question_number = self.current_question_number if question_number is None else question_number
        if question_number is None and self.current_question_id:
            return self.get_question_number_in_set_for_question_id(self.current_question_id, active_only=False)
        if question_number is None:
            question_number = self.current_question_number
        if question_number == self.current_question_number and self.current_question_id:
            return self.get_question_number_in_set_for_question_id(self.current_question_id, active_only=False)
        if question_number <= 0:
            return 0
        explicit_sets = self.get_explicit_question_sets()
        if explicit_sets:
            remaining_questions = int(question_number)
            for question_set in explicit_sets:
                if remaining_questions <= len(question_set):
                    return remaining_questions
                remaining_questions -= len(question_set)
            return len(explicit_sets[-1])
        return ((question_number - 1) % self.get_questions_per_set()) + 1

    def get_current_question_position_in_set(self):
        if not self.current_question_id:
            return 0
        try:
            session = self.session
        except Exception:
            session = None
        if session:
            return session.get_played_question_position_in_set(
                question_id=self.current_question_id,
                active_only=False,
            )
        return self.get_question_number_in_set(question_id=self.current_question_id)

    def get_set_question_count(self, set_number=None, question_number=None, question_id=None):
        explicit_sets = self.get_explicit_question_sets()
        if explicit_sets:
            if question_id is not None:
                set_number = self.get_set_number_for_question_id(question_id, active_only=False)
            if question_number is not None and question_number > 0:
                set_number = self.get_set_number_for_question(question_number)
            if set_number is None or set_number <= 0:
                set_number = 1
            if set_number > len(explicit_sets):
                set_number = len(explicit_sets)
            return len(explicit_sets[set_number - 1])
        return self.get_questions_per_set()

    def get_set_question_ids(self, set_number, active_only=True):
        explicit_sets = self.get_explicit_question_sets(active_only=active_only)
        if not explicit_sets or set_number <= 0 or set_number > len(explicit_sets):
            return []
        return list(explicit_sets[set_number - 1])

    def get_tutorial_set_question_ids(self, active_only=True):
        if not self.tutorial_set_number:
            return []
        return self.get_set_question_ids(self.tutorial_set_number, active_only=active_only)

    def get_tutorial_question_id_for_runtime(self, active_only=True):
        tutorial_question_ids = self.get_tutorial_set_question_ids(active_only=active_only)
        return tutorial_question_ids[0] if tutorial_question_ids else None

    def get_remaining_question_ids_for_next_turn(self, active_only=True, set_number=None):
        try:
            session = self.session
        except Exception:
            session = None
        if session and self.has_configured_question_pool():
            return session.get_remaining_question_ids_for_set(set_number=set_number, active_only=active_only)

        configured_question_ids = self.get_configured_question_ids(active_only=active_only)
        if not configured_question_ids:
            return []

        next_question_number = self.current_question_number + 1
        if next_question_number > len(configured_question_ids):
            return []

        next_question_in_set = self.get_question_number_in_set(next_question_number)
        current_set_size = self.get_set_question_count(question_number=next_question_number)
        remaining_in_set = current_set_size - next_question_in_set + 1
        start_index = self.current_question_number
        end_index = start_index + max(remaining_in_set, 0)
        return configured_question_ids[start_index:end_index]

    def get_next_question_send_error(self, question_id=None, selected_set_number=None):
        try:
            session = self.session
        except Exception:
            session = None

        if self.current_question_id or (session and session.is_question_active):
            return 'A question is already active for this quiz.'

        if self.current_question_number >= self.get_total_game_questions():
            return 'All questions in this quiz have already been asked.'

        if self.has_configured_question_pool() and session:
            if selected_set_number is None and question_id is not None:
                selected_set_number = session.get_normalized_selected_set_number(active_only=True)
            remaining_question_ids = self.get_remaining_question_ids_for_next_turn(
                set_number=selected_set_number,
                active_only=True,
            )
            if not remaining_question_ids:
                if self.is_quiz_complete():
                    return 'All questions in this quiz have already been asked.'
                return 'All questions in the selected set have already been asked.'
            if question_id is not None:
                normalized_question_id = self._normalize_question_id(question_id)
                if normalized_question_id is None or normalized_question_id not in remaining_question_ids:
                    return 'This question is not part of the current active set for this quiz.'

        return None

    def is_set_complete(self, question_number=None, set_number=None, question_id=None):
        try:
            session = self.session
        except Exception:
            session = None
        if session and self.has_configured_question_pool():
            target_set_number = set_number
            if target_set_number is None and question_id is not None:
                target_set_number = self.get_set_number_for_question_id(question_id, active_only=False)
            if target_set_number is None and self.current_question_id:
                target_set_number = self.get_set_number_for_question_id(self.current_question_id, active_only=False)
            return session.is_set_complete(set_number=target_set_number, active_only=True)

        question_number = self.current_question_number if question_number is None else question_number
        if question_number <= 0:
            return False
        if question_number >= self.get_total_game_questions():
            return True
        return self.get_question_number_in_set(question_number) >= self.get_set_question_count(
            question_number=question_number
        )

    def is_quiz_complete(self):
        """Check if all configured questions have been asked."""
        try:
            session = self.session
        except Exception:
            session = None
        if session and self.has_configured_question_pool():
            return session.are_all_configured_questions_sent(active_only=True)
        return self.current_question_number >= self.get_total_game_questions()

    def uses_ranking_mode(self):
        return self.scoring_mode == 'rank'

    def can_change_scoring_mode(self):
        if self.current_question_number > 0:
            return False
        if self.blackjack_answers.exists():
            return False
        try:
            session = self.session
        except Exception:
            session = None
        if session and session.total_questions_sent > 0:
            return False
        return True

    def get_ordered_participants(self, active_only=False, session_code=None):
        qs = self.participants.all()
        if session_code is not None:
            qs = qs.filter(hub_session_code=session_code)
        if active_only:
            qs = qs.filter(is_active=True)
        if self.is_quiz_complete():
            return list(qs.order_by('-overall_points', 'name'))
        if self.uses_ranking_mode():
            return list(qs.filter(is_busted=False).order_by('final_score', 'name')) + list(
                qs.filter(is_busted=True).order_by('final_score', 'name')
            )
        return list(qs.order_by('-total_points', 'name'))

    def get_set_ranking_points(self, participants=None):
        ranked_participants = list(participants if participants is not None else self.participants.all())
        if not ranked_participants:
            return {}

        ranked_participants.sort(
            key=lambda participant: (participant.is_busted, participant.final_score, participant.name)
        )

        max_points = len(ranked_participants)
        ranking_points = {}
        previous_rank_key = None
        current_rank = 0

        for position, participant in enumerate(ranked_participants, start=1):
            rank_key = (participant.is_busted, participant.final_score)
            if previous_rank_key is None or rank_key != previous_rank_key:
                current_rank = position
                previous_rank_key = rank_key
            current_points = max(max_points - (current_rank - 1), 0)
            ranking_points[participant.id] = current_points

        return ranking_points
    
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
    total_points = models.IntegerField(default=0)  # Current set stars / effective set score
    overall_points = models.IntegerField(default=0)  # Total points accumulated across completed sets
    questions_answered = models.IntegerField(default=0)
    last_activity = models.DateTimeField(auto_now=True)
    is_busted = models.BooleanField(default=False)  # True if they went over 21
    final_score = models.IntegerField(default=0)  # Final score calculation
    hub_session_code = models.CharField(max_length=16, null=True, blank=True, db_index=True)
    
    class Meta:
        unique_together = ['quiz', 'name', 'hub_session_code']
        ordering = ['final_score', 'name']  # Lower score is better in BlackJack
    
    def calculate_score(self):
        """Recalculate the current set stars and determine if the player busted in this set."""
        from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids

        tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids(
            'blackjack',
            self.quiz.room_code,
            self.hub_session_code,
        )
        scored_answers = self.blackjack_answers.select_related('question')
        if tutorial_question_ids:
            scored_answers = scored_answers.exclude(question_id__in=tutorial_question_ids)
        latest_answer = scored_answers.order_by('-submitted_at', '-id').first()

        if not latest_answer:
            self.questions_answered = 0
            self.total_points = 0
            self.is_busted = False
            self.final_score = 0
            self.save(update_fields=['questions_answered', 'total_points', 'is_busted', 'final_score'])
            return self.total_points

        current_set_number = self.quiz.get_set_number_for_question_id(latest_answer.question_id, active_only=False)
        current_set_question_ids = self.quiz.get_set_question_ids(current_set_number, active_only=False)
        if current_set_question_ids:
            current_set_answers = scored_answers.filter(
                question_id__in=current_set_question_ids,
            )
        else:
            question_number = latest_answer.question_number or self.quiz.current_question_number
            current_set_number = self.quiz.get_set_number_for_question(question_number)
            questions_per_set = self.quiz.get_questions_per_set()
            first_question_number = ((current_set_number - 1) * questions_per_set) + 1
            last_question_number = current_set_number * questions_per_set
            current_set_answers = scored_answers.filter(
                question_number__gte=first_question_number,
                question_number__lte=last_question_number,
            )
        raw_total_points = sum(answer.points_earned for answer in current_set_answers)
        self.questions_answered = current_set_answers.count()
        
        # Check if busted (over 21)
        if raw_total_points > 21:
            self.is_busted = True
            self.total_points = 0
            # Keep busted participants sortable by how little they exceeded 21.
            self.final_score = raw_total_points
        else:
            self.is_busted = False
            self.total_points = raw_total_points
            # Lower score is better - closest to 21 wins
            self.final_score = abs(21 - raw_total_points)
        
        self.save(update_fields=['questions_answered', 'total_points', 'is_busted', 'final_score'])
        return self.total_points
    
    def get_rank(self):
        """Get participant's rank in the quiz."""
        if self.quiz.is_quiz_complete():
            better_scores = BlackJackParticipant.objects.filter(
                quiz=self.quiz,
                overall_points__gt=self.overall_points,
            ).values('overall_points').distinct().count()
            return better_scores + 1

        if not self.quiz.uses_ranking_mode():
            better_scores = BlackJackParticipant.objects.filter(
                quiz=self.quiz,
                total_points__gt=self.total_points,
            ).values('total_points').distinct().count()
            return better_scores + 1

        if self.is_busted:
            # Rank among busted players by who got closest to 21 before busting
            busted_ranks = BlackJackParticipant.objects.filter(
                quiz=self.quiz,
                is_busted=True,
                final_score__lt=self.final_score
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
            return self.final_score - 21  # Positive number showing how much over
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
    question_number = models.IntegerField(default=1)  # Which question number this was in the set
    
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
    completed_sets_count = models.IntegerField(default=0)
    selected_set_number = models.IntegerField(default=1)
    asked_question_ids = models.JSONField(default=list, blank=True)
    finalized_set_numbers = models.JSONField(default=list, blank=True)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    
    # Session statistics
    total_responses_current_question = models.IntegerField(default=0)
    average_points_current_question = models.FloatField(default=0)
    busted_participants_count = models.IntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def _normalize_int_list(self, values):
        normalized_values = []
        seen_values = set()
        for raw_value in values or []:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value <= 0 or value in seen_values:
                continue
            normalized_values.append(value)
            seen_values.add(value)
        return normalized_values

    def is_waiting_fresh_start_state(self):
        return (
            (
                self.quiz.status == 'waiting'
                and not self.quiz.current_question_id
                and not self.is_question_active
            )
            or (
                self.quiz.status == 'active'
                and not self.quiz.current_question_id
                and not self.is_question_active
                and self.quiz.current_question_number == 0
                and self.quiz.question_start_time is None
                and not self.quiz.get_current_run_answers().exists()
            )
        )

    def has_saved_progress_state(self):
        return bool(
            self.current_question_number > 0
            or self.total_questions_sent > 0
            or self.completed_sets_count > 0
            or (self.asked_question_ids or [])
            or (self.finalized_set_numbers or [])
        )

    def reset_for_new_run(self):
        self.current_question_number = 0
        self.total_questions_sent = 0
        self.completed_sets_count = 0
        self.selected_set_number = 1
        self.asked_question_ids = []
        self.finalized_set_numbers = []
        self.is_question_active = False
        self.question_end_time = None
        self.total_responses_current_question = 0
        self.average_points_current_question = 0
        self.busted_participants_count = 0
        self.save(update_fields=[
            'current_question_number',
            'total_questions_sent',
            'completed_sets_count',
            'selected_set_number',
            'asked_question_ids',
            'finalized_set_numbers',
            'is_question_active',
            'question_end_time',
            'total_responses_current_question',
            'average_points_current_question',
            'busted_participants_count',
        ])

    def get_asked_question_ids(self):
        if self.is_waiting_fresh_start_state() or (
            not self.quiz.current_question_id
            and not self.is_question_active
            and self.current_question_number == 0
            and self.total_questions_sent == 0
            and self.completed_sets_count == 0
            and not (self.asked_question_ids or [])
            and not (self.finalized_set_numbers or [])
        ):
            return []
        normalized_ids = self._normalize_int_list(self.asked_question_ids)
        if normalized_ids:
            return normalized_ids

        fallback_ids = []
        answer_ids = list(
            self.quiz.get_current_run_answers().order_by('question_number', 'submitted_at')
            .values_list('question_id', flat=True)
            .distinct()
        )
        for question_id in answer_ids:
            if question_id not in fallback_ids:
                fallback_ids.append(question_id)

        if self.quiz.current_question_id and self.quiz.current_question_id not in fallback_ids:
            fallback_ids.append(self.quiz.current_question_id)

        if not fallback_ids and self.total_questions_sent > 0 and self.quiz.has_configured_question_pool():
            fallback_ids = self.quiz.get_configured_question_ids(active_only=False)[:self.total_questions_sent]

        return fallback_ids

    def get_finalized_set_numbers(self):
        if self.is_waiting_fresh_start_state():
            return []
        return self._normalize_int_list(self.finalized_set_numbers)

    def normalize_set_number(self, set_number=None, active_only=True):
        total_sets = self.quiz.get_total_sets()
        if total_sets <= 0:
            return 1
        try:
            normalized_set_number = int(set_number) if set_number is not None else int(self.selected_set_number or 1)
        except (TypeError, ValueError):
            normalized_set_number = 1
        if normalized_set_number < 1:
            return 1
        if normalized_set_number > total_sets:
            return total_sets
        return normalized_set_number

    def get_normalized_selected_set_number(self, active_only=True):
        if self.is_waiting_fresh_start_state() and self.has_saved_progress_state():
            return 1
        if self.quiz.current_question_id:
            return self.quiz.get_set_number_for_question_id(self.quiz.current_question_id, active_only=False)
        if (
            self.selected_set_number == 1
            and self.current_question_number > 0
            and not (self.asked_question_ids or [])
            and not (self.finalized_set_numbers or [])
        ):
            next_question_number = self.current_question_number if self.quiz.current_question else min(
                self.current_question_number + 1,
                max(1, self.quiz.get_total_game_questions()),
            )
            return self.quiz.get_set_number_for_question(next_question_number)
        return self.normalize_set_number(self.selected_set_number, active_only=active_only)

    def set_selected_set_number(self, set_number):
        self.selected_set_number = self.normalize_set_number(set_number, active_only=True)

    def get_sent_question_ids_for_set(self, set_number=None, active_only=True):
        normalized_set_number = self.normalize_set_number(set_number, active_only=active_only)
        set_question_ids = self.quiz.get_set_question_ids(normalized_set_number, active_only=active_only)
        set_question_id_lookup = set(set_question_ids)
        return [
            question_id
            for question_id in self.get_asked_question_ids()
            if question_id in set_question_id_lookup
        ]

    def get_played_question_position_in_set(self, question_id=None, set_number=None, active_only=False):
        normalized_question_id = self.quiz._normalize_question_id(question_id or self.quiz.current_question_id)
        if normalized_question_id is None:
            return 0

        normalized_set_number = self.normalize_set_number(
            set_number or self.quiz.get_set_number_for_question_id(normalized_question_id, active_only=False),
            active_only=active_only,
        )
        sent_question_ids = self.get_sent_question_ids_for_set(normalized_set_number, active_only=active_only)
        if normalized_question_id in sent_question_ids:
            return sent_question_ids.index(normalized_question_id) + 1

        if self.quiz.current_question_id == normalized_question_id and self.is_question_active:
            return len(sent_question_ids) + 1

        return self.quiz.get_question_number_in_set(question_id=normalized_question_id)

    def get_remaining_question_ids_for_set(self, set_number=None, active_only=True):
        normalized_set_number = self.normalize_set_number(set_number, active_only=active_only)
        set_question_ids = self.quiz.get_set_question_ids(normalized_set_number, active_only=active_only)
        asked_ids = set(self.get_asked_question_ids())
        return [question_id for question_id in set_question_ids if question_id not in asked_ids]

    def is_set_complete(self, set_number=None, active_only=True):
        normalized_set_number = self.normalize_set_number(set_number, active_only=active_only)
        set_question_ids = self.quiz.get_set_question_ids(normalized_set_number, active_only=active_only)
        if not set_question_ids:
            return False
        return len(self.get_remaining_question_ids_for_set(normalized_set_number, active_only=active_only)) == 0

    def is_set_finalized(self, set_number):
        return self.normalize_set_number(set_number, active_only=True) in self.get_finalized_set_numbers()

    def mark_question_sent(self, question_id):
        asked_ids = self.get_asked_question_ids()
        try:
            normalized_question_id = int(question_id)
        except (TypeError, ValueError):
            normalized_question_id = None
        if normalized_question_id and normalized_question_id not in asked_ids:
            asked_ids.append(normalized_question_id)
        self.asked_question_ids = asked_ids

    def mark_set_finalized(self, set_number):
        finalized_sets = self.get_finalized_set_numbers()
        normalized_set_number = self.normalize_set_number(set_number, active_only=True)
        if normalized_set_number not in finalized_sets:
            finalized_sets.append(normalized_set_number)
        self.finalized_set_numbers = finalized_sets
        self.completed_sets_count = len(finalized_sets)

    def are_all_configured_questions_sent(self, active_only=True):
        configured_question_ids = self.quiz.get_configured_question_ids(active_only=active_only)
        if not configured_question_ids:
            return self.current_question_number >= self.quiz.get_total_game_questions()
        asked_ids = set(self.get_asked_question_ids())
        return all(question_id in asked_ids for question_id in configured_question_ids)

    @staticmethod
    def _effective_time_limit(question, time_limit=None):
        try:
            effective_time_limit = int(time_limit) if time_limit is not None else question.time_limit
            if effective_time_limit <= 0:
                effective_time_limit = question.time_limit
        except (TypeError, ValueError):
            effective_time_limit = question.time_limit
        return effective_time_limit

    def prepare_question(self, question, *, track_progress=True):
        """Prepare a question without opening the participant answer window."""
        if track_progress:
            set_number = self.quiz.get_set_number_for_question_id(question.id, active_only=False)
            self.set_selected_set_number(set_number)
            self.mark_question_sent(question.id)
        self.quiz.current_question = question
        self.quiz.question_start_time = None
        if track_progress:
            self.current_question_number += 1
            self.quiz.current_question_number = self.current_question_number
            self.total_questions_sent += 1
        self.is_question_active = False
        self.question_end_time = None
        self.total_responses_current_question = 0
        self.average_points_current_question = 0

        self.quiz.save()
        self.save()

    def open_answering(self, question, *, started_at, answer_duration_seconds):
        """Open the prepared question using authoritative phase timestamps."""
        if self.quiz.current_question_id != question.id:
            raise ValueError('The prepared Black Jack question is no longer current.')
        self.quiz.question_start_time = started_at
        self.is_question_active = True
        self.question_end_time = started_at + timezone.timedelta(
            seconds=answer_duration_seconds,
        )
        self.quiz.save(update_fields=['question_start_time', 'updated_at'])
        self.save(update_fields=[
            'is_question_active',
            'question_end_time',
            'updated_at',
        ])

    def send_question(self, question, time_limit=None):
        """Compatibility path for legacy callers that still start immediately."""
        effective_time_limit = self._effective_time_limit(question, time_limit)
        self.prepare_question(question)
        self.open_answering(
            question,
            started_at=timezone.now(),
            answer_duration_seconds=effective_time_limit,
        )
    
    def finalize_completed_set(self):
        current_set_number = self.quiz.get_current_set_number()
        if not self.quiz.is_set_complete(set_number=current_set_number):
            return {'set_complete': False, 'set_number': current_set_number, 'participants': []}
        if self.is_set_finalized(current_set_number):
            return {'set_complete': False, 'set_number': current_set_number, 'participants': []}

        participants = list(self.quiz.participants.all())
        ranking_points = self.quiz.get_set_ranking_points(participants) if self.quiz.uses_ranking_mode() else {}
        quiz_complete = self.quiz.is_quiz_complete()
        participant_updates = []

        for participant in participants:
            set_stars = participant.total_points
            awarded_points = ranking_points.get(participant.id, 0) if self.quiz.uses_ranking_mode() else set_stars
            participant.overall_points += awarded_points
            participant.questions_answered = 0
            participant.is_busted = False
            participant.final_score = 0
            participant.total_points = participant.overall_points if quiz_complete else 0
            participant.save(update_fields=[
                'overall_points',
                'questions_answered',
                'is_busted',
                'final_score',
                'total_points',
            ])
            participant_updates.append({
                'id': participant.id,
                'name': participant.name,
                'set_stars': set_stars,
                'awarded_points': awarded_points,
                'overall_points': participant.overall_points,
                'current_points': participant.total_points,
            })

        self.mark_set_finalized(current_set_number)
        return {
            'set_complete': True,
            'set_number': current_set_number,
            'participants': participant_updates,
        }

    def get_current_runtime_participants(self):
        participants = self.quiz.participants.filter(is_active=True)
        hub_session = self.quiz.get_hub_session_for_runtime()
        if hub_session:
            participants = participants.filter(hub_session_code=hub_session.code)
        return participants

    def mark_unanswered_current_question_participants_busted(self):
        """Eliminate active set participants who submitted no answer for the current question."""
        if not self.quiz.current_question_id:
            return []

        eligible_participants = self.get_current_runtime_participants().filter(is_busted=False)
        answered_participant_ids = self.quiz.get_current_run_answers().filter(
            question_id=self.quiz.current_question_id,
            participant__in=eligible_participants,
        ).values_list('participant_id', flat=True).distinct()
        unanswered_participants = list(
            eligible_participants.exclude(id__in=answered_participant_ids)
        )
        if not unanswered_participants:
            return []

        unanswered_ids = [participant.id for participant in unanswered_participants]
        self.quiz.participants.filter(id__in=unanswered_ids).update(
            is_busted=True,
            total_points=0,
            final_score=22,
        )
        return [
            {
                'id': participant.id,
                'name': participant.name,
                'hub_session_code': participant.hub_session_code or '',
                'reason': 'no_answer',
                'total_points': 0,
                'overall_points': participant.overall_points,
                'is_busted': True,
            }
            for participant in unanswered_participants
        ]

    def end_current_question(self):
        """End the current active question"""
        no_answer_bust_participants = self.mark_unanswered_current_question_participants_busted()
        set_result = self.finalize_completed_set()
        self.is_question_active = False
        self.question_end_time = None
        self.quiz.current_question = None
        self.quiz.question_start_time = None
        
        if self.quiz.is_quiz_complete():
            self.quiz.status = 'completed'
            self.quiz.ended_at = timezone.now()

        self.quiz.save()
        self.save()
        return {
            **set_result,
            'no_answer_bust_participants': no_answer_bust_participants,
            'quiz_complete': self.quiz.is_quiz_complete(),
        }
    
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
        return f"BlackJack Session for {self.quiz.room_code} (Q{self.current_question_number}/{self.quiz.total_questions})"
