from django.db import models
from games_website.models import SyncBase
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.utils import timezone
import random
import string
import math


class EstimationQuiz(SyncBase):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200, default="Estimation Quiz")
    internal_description = models.TextField(blank=True, default='')
    tutorial_enabled = models.BooleanField(default=False)
    tutorial_title = models.CharField(max_length=200, blank=True, default='')
    tutorial_text = models.TextField(blank=True, default='')
    tutorial_active = models.BooleanField(default=False)
    question_order = models.JSONField(default=list, blank=True)
    room_code = models.CharField(max_length=4, unique=True, blank=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_estimation_quizzes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    current_question = models.ForeignKey('EstimationQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_in_estimation_quiz')
    question_start_time = models.DateTimeField(null=True, blank=True)
    max_participants = models.IntegerField(default=50)
    # Optional predefined set of questions for this quiz session
    selected_questions = models.ManyToManyField('EstimationQuestion', blank=True, related_name='quizzes')
    tutorial_question = models.ForeignKey('EstimationQuestion', on_delete=models.SET_NULL, null=True, blank=True, related_name='tutorial_in_quizzes')
    # "tolerance" is kept as a legacy alias for the zone scoring mode.
    SCORING_CHOICES = [
        ('zones', 'Zone Mode'),
        ('rank', 'Ranking Mode'),
        ('tolerance', 'Zone Mode (legacy)'),
    ]
    scoring_mode = models.CharField(max_length=16, choices=SCORING_CHOICES, default='tolerance')
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        if not self.room_code:
            self.room_code = self.generate_unique_room_code()
        super().save(*args, **kwargs)
    
    def generate_unique_room_code(self):
        while True:
            code = ''.join(random.choices(string.digits, k=4))
            if not EstimationQuiz.objects.filter(room_code=code).exists():
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

    def get_effective_scoring_mode(self):
        return 'rank' if self.scoring_mode == 'rank' else 'zones'
    
    def __str__(self):
        return f"{self.title} ({self.room_code})"


class EstimationBundle(SyncBase):
    """A reusable, named collection of questions that can be used as a template when creating sessions."""
    name = models.CharField(max_length=200)
    questions = models.ManyToManyField('EstimationQuestion', blank=True, related_name='bundles')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='estimation_bundles')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class EstimationQuestion(SyncBase):
    """Questions for estimation games"""
    
    UNIT_CHOICES = [
        ('number', 'Number (no unit)'),
        ('meters', 'Meters'),
        ('kilometers', 'Kilometers'),
        ('feet', 'Feet'),
        ('inches', 'Inches'),
        ('centimeters', 'Centimeters'),
        ('years', 'Years'),
        ('months', 'Months'),
        ('days', 'Days'),
        ('hours', 'Hours'),
        ('minutes', 'Minutes'),
        ('seconds', 'Seconds'),
        ('kilograms', 'Kilograms'),
        ('pounds', 'Pounds'),
        ('grams', 'Grams'),
        ('tons', 'Tons'),
        ('liters', 'Liters'),
        ('gallons', 'Gallons'),
        ('milliliters', 'Milliliters'),
        ('degrees', 'Degrees'),
        ('fahrenheit', 'Fahrenheit'),
        ('celsius', 'Celsius'),
        ('dollars', 'Dollars ($)'),
        ('euros', 'Euros (€)'),
        ('percent', 'Percent (%)'),
        ('people', 'People'),
        ('calories', 'Calories'),
        ('watts', 'Watts'),
        ('miles', 'Miles'),
        ('mph', 'Miles per hour'),
        ('kmh', 'Kilometers per hour'),
    ]
    LEGACY_UNIT_ALIASES = {
        'm': 'meters',
        'km': 'kilometers',
        'cm': 'centimeters',
        'kg': 'kilograms',
        '%': 'percent',
        'EUR': 'euros',
        'year': 'years',
        'other': 'number',
    }
    UNIT_PRESENTATION = {
        'number': ('Zahl (keine Einheit)', ''),
        'meters': ('Meter', 'm'),
        'kilometers': ('Kilometer', 'km'),
        'feet': ('Fu\u00df', 'ft'),
        'inches': ('Zoll', 'in'),
        'centimeters': ('Zentimeter', 'cm'),
        'year': ('Jahr', 'Jahr'),
        'years': ('Jahre', 'Jahre'),
        'months': ('Monate', 'Monate'),
        'days': ('Tage', 'Tage'),
        'hours': ('Stunden', 'Stunden'),
        'minutes': ('Minuten', 'Minuten'),
        'seconds': ('Sekunden', 'Sekunden'),
        'kilograms': ('Kilogramm', 'kg'),
        'pounds': ('Pfund', 'lbs'),
        'grams': ('Gramm', 'g'),
        'tons': ('Tonnen', 'Tonnen'),
        'liters': ('Liter', 'L'),
        'gallons': ('Gallonen', 'gal'),
        'milliliters': ('Milliliter', 'mL'),
        'degrees': ('Grad', '\u00b0'),
        'fahrenheit': ('Fahrenheit', '\u00b0F'),
        'celsius': ('Celsius', '\u00b0C'),
        'dollars': ('Dollar ($)', '$'),
        'euros': ('Euro (\u20ac)', '\u20ac'),
        'percent': ('Prozent (%)', '%'),
        'people': ('Personen', 'Personen'),
        'calories': ('Kalorien', 'cal'),
        'watts': ('Watt', 'W'),
        'miles': ('Meilen', 'mi'),
        'mph': ('Meilen pro Stunde', 'mph'),
        'kmh': ('Kilometer pro Stunde', 'km/h'),
    }
    
    question_text = models.TextField(help_text="The question, e.g., 'How tall is Mount Everest?'")
    correct_answer = models.FloatField(help_text="The correct numerical answer")
    unit = models.CharField(max_length=20, choices=UNIT_CHOICES, default='number', help_text="Unit of measurement")
    max_points = models.PositiveIntegerField(default=100, help_text="Manual maximum points value for zone scoring")
    use_manual_points = models.BooleanField(
        default=False,
        help_text="Use max_points as the inner-zone maximum instead of zone_count",
    )
    tolerance_percentage = models.FloatField(default=10.0, help_text="Zone width as percentage deviation")
    zone_count = models.PositiveIntegerField(
        default=5,
        validators=[MinValueValidator(1)],
        help_text="Number of scoring zones; also the maximum score in zone mode",
    )
    hint_text = models.TextField(blank=True, null=True, help_text="Optional hint shown during question")
    explanation = models.TextField(blank=True, null=True, help_text="Optional explanation shown after answering")
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_estimation_questions')
    
    class Meta:
        ordering = ['-created_at']

    @classmethod
    def normalize_unit_value(cls, unit):
        normalized = str(unit or 'number').strip()
        if not normalized:
            return 'number'
        normalized = cls.LEGACY_UNIT_ALIASES.get(normalized, normalized)
        valid_units = {choice[0] for choice in cls.UNIT_CHOICES}
        return normalized if normalized in valid_units else 'number'

    @classmethod
    def get_unit_presentation(cls, unit):
        raw_unit = str(unit or 'number').strip() or 'number'
        presentation = cls.UNIT_PRESENTATION.get(raw_unit)
        if presentation:
            return presentation
        return cls.UNIT_PRESENTATION[cls.normalize_unit_value(raw_unit)]

    @classmethod
    def get_unit_choices_for_display(cls):
        return [
            (value, cls.get_unit_presentation(value)[0])
            for value, _label in cls.UNIT_CHOICES
        ]

    def get_unit_display(self):
        """Return the localized long label without changing the stored value."""
        return self.get_unit_presentation(self.unit)[0]
    
    def get_unit_display_text(self):
        """Get the display text for the unit"""
        return self.get_unit_presentation(self.unit)[1]
    
    def get_zone_count(self):
        try:
            return max(1, int(self.zone_count or 0))
        except (TypeError, ValueError):
            return 1

    def get_zone_width_percentage(self):
        try:
            return float(self.tolerance_percentage or 0)
        except (TypeError, ValueError):
            return 0

    def get_max_points_for_mode(self, scoring_mode='zones', participant_count=None):
        if scoring_mode == 'rank':
            return max(0, int(participant_count or 0))
        return self.get_zone_max_points()

    def get_zone_max_points(self):
        if self.use_manual_points:
            try:
                return max(1, int(self.max_points or 0))
            except (TypeError, ValueError):
                return 1
        return self.get_zone_count()

    def get_points_for_zone(self, zone_number):
        try:
            zone_number = max(1, int(zone_number))
        except (TypeError, ValueError):
            zone_number = 1
        return max(0, self.get_zone_max_points() - zone_number + 1)

    def get_zone_reveal_data(self):
        zone_width = self.get_zone_width_percentage()
        zone_count = self.get_zone_count()
        max_points = self.get_zone_max_points()

        if self.correct_answer == 0:
            return {
                'special_case': 'exact_zero',
                'zone_width_percentage': zone_width,
                'zone_count': zone_count,
                'outside_points': 0,
                'zones': [
                    {
                        'zone_number': 1,
                        'min_percentage': 0.0,
                        'max_percentage': 0.0,
                        'absolute_min_value': 0,
                        'absolute_max_value': 0,
                        'absolute_range_display': '0-0',
                        'points': max_points,
                    }
                ],
            }

        if zone_width <= 0 or zone_count < 1:
            return None

        zones = []
        for zone_number in range(1, zone_count + 1):
            min_percentage = 0.0 if zone_number == 1 else zone_width * (zone_number - 1)
            max_percentage = zone_width * zone_number
            zone_radius = abs(float(self.correct_answer)) * (max_percentage / 100.0)
            lower_bound = self.correct_answer - zone_radius
            upper_bound = self.correct_answer + zone_radius
            absolute_min_value = int(round(min(lower_bound, upper_bound)))
            absolute_max_value = int(round(max(lower_bound, upper_bound)))
            zones.append({
                'zone_number': zone_number,
                'min_percentage': round(min_percentage, 4),
                'max_percentage': round(max_percentage, 4),
                'absolute_min_value': absolute_min_value,
                'absolute_max_value': absolute_max_value,
                'absolute_range_display': f'{absolute_min_value}-{absolute_max_value}',
                'points': self.get_points_for_zone(zone_number),
            })

        return {
            'special_case': None,
            'zone_width_percentage': zone_width,
            'zone_count': zone_count,
            'outside_points': 0,
            'zones': zones,
        }

    def calculate_score(self, user_answer):
        """Zone scoring based on percentage deviation from the correct answer."""
        if user_answer is None:
            return 0
        
        try:
            user_answer = float(user_answer)
        except (ValueError, TypeError):
            return 0
        
        zone_count = self.get_zone_count()
        if self.correct_answer == 0:
            return self.get_zone_max_points() if user_answer == 0 else 0
        
        # Calculate percentage difference
        percentage_diff = abs((user_answer - self.correct_answer) / abs(self.correct_answer)) * 100

        zone_width = self.get_zone_width_percentage()
        if zone_width <= 0:
            return 0
        if percentage_diff > zone_width * zone_count:
            return 0

        zone_number = max(1, math.ceil(percentage_diff / zone_width))
        return self.get_points_for_zone(zone_number)
    
    def get_accuracy_percentage(self, user_answer):
        """Get accuracy percentage for display purposes"""
        if user_answer is None:
            return 0
        
        try:
            user_answer = float(user_answer)
        except (ValueError, TypeError):
            return 0
        
        if self.correct_answer == 0:
            return 100 if user_answer == 0 else 0
        
        percentage_diff = abs((user_answer - self.correct_answer) / self.correct_answer) * 100
        
        # Convert to accuracy percentage
        accuracy = max(0, 100 - percentage_diff)
        return min(100, accuracy)  # Cap at 100%
    
    def format_number(self, number):
        """Format number for display"""
        if number == int(number):
            return str(int(number))
        return f"{number:.2f}".rstrip('0').rstrip('.')
    
    def get_formatted_correct_answer(self):
        """Get formatted correct answer with unit"""
        formatted_number = self.format_number(self.correct_answer)
        unit_text = self.get_unit_display_text()
        
        if self.unit in ['dollars', 'euros']:
            return f"{unit_text}{formatted_number}"
        elif unit_text:
            return f"{formatted_number} {unit_text}"
        else:
            return formatted_number
    
    def __str__(self):
        return f"{self.question_text[:50]}..." if len(self.question_text) > 50 else self.question_text


class EstimationParticipant(SyncBase):
    quiz = models.ForeignKey(EstimationQuiz, on_delete=models.CASCADE, related_name='participants')
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
        total_score = sum(answer.points_earned for answer in self.estimation_answers.all())
        self.total_score = total_score
        self.questions_answered = self.estimation_answers.count()
        self.save()
        return self.total_score
    
    def get_rank(self):
        """Get participant's rank in the quiz"""
        higher_scores = EstimationParticipant.objects.filter(
            quiz=self.quiz,
            total_score__gt=self.total_score
        ).count()
        return higher_scores + 1
    
    def get_average_accuracy(self):
        """Get average accuracy percentage across all answers"""
        answers = self.estimation_answers.all()
        if not answers:
            return 0
        
        total_accuracy = 0
        for answer in answers:
            total_accuracy += answer.question.get_accuracy_percentage(answer.user_answer)
        
        return round(total_accuracy / answers.count(), 1)
    
    def __str__(self):
        return f"{self.name} in {self.quiz.room_code}"


class EstimationAnswer(SyncBase):
    quiz = models.ForeignKey(EstimationQuiz, on_delete=models.CASCADE, related_name='estimation_answers')
    participant = models.ForeignKey(EstimationParticipant, on_delete=models.CASCADE, related_name='estimation_answers')
    question = models.ForeignKey(EstimationQuestion, on_delete=models.CASCADE, related_name='estimation_answers')
    
    user_answer = models.FloatField(help_text="The participant's numerical answer")
    points_earned = models.IntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    time_taken = models.FloatField(help_text="Time taken to answer in seconds", null=True, blank=True)
    
    class Meta:
        unique_together = ['quiz', 'participant', 'question']
        ordering = ['-submitted_at']
    
    def save(self, *args, **kwargs):
        # Auto-calculate points on creation
        if not self.pk:
            # Zone mode is computed immediately; ranking is resolved when the round ends.
            if self.quiz.get_effective_scoring_mode() == 'zones':
                self.points_earned = self.question.calculate_score(self.user_answer)
        super().save(*args, **kwargs)
        
        # Update participant's total score
        self.participant.calculate_score()
    
    def get_accuracy_percentage(self):
        """Get accuracy percentage for this answer"""
        return self.question.get_accuracy_percentage(self.user_answer)
    
    def get_percentage_difference(self):
        """Get the percentage difference from correct answer"""
        if self.question.correct_answer == 0:
            return 0 if self.user_answer == 0 else float('inf')
        
        return abs((self.user_answer - self.question.correct_answer) / self.question.correct_answer) * 100
    
    def get_formatted_user_answer(self):
        """Get formatted user answer with unit"""
        return f"{self.question.format_number(self.user_answer)} {self.question.get_unit_display_text()}".strip()
    
    def get_difference_indicator(self):
        """Get indicator showing if answer was high, low, or exact"""
        if self.user_answer == self.question.correct_answer:
            return 'exact'
        elif self.user_answer > self.question.correct_answer:
            return 'high'
        else:
            return 'low'
    
    def __str__(self):
        return f"{self.participant.name}: {self.get_formatted_user_answer()} ({self.points_earned} pts)"


class EstimationSession(SyncBase):
    """Tracks the current state of a live estimation quiz session"""
    quiz = models.OneToOneField(EstimationQuiz, on_delete=models.CASCADE, related_name='session')
    current_question_number = models.IntegerField(default=0)
    total_questions_sent = models.IntegerField(default=0)
    is_question_active = models.BooleanField(default=False)
    question_end_time = models.DateTimeField(null=True, blank=True)
    pending_answers = models.JSONField(default=dict, blank=True)
    
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
        self.question_end_time = timezone.now() + timezone.timedelta(seconds=90)  # Default 90 seconds
        self.pending_answers = {}
        self.total_responses_current_question = 0
        self.average_score_current_question = 0
        self.average_accuracy_current_question = 0
        
        self.quiz.save()
        self.save()
    
    def end_current_question(self):
        """End the current active question"""
        self.is_question_active = False
        self.pending_answers = {}
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
