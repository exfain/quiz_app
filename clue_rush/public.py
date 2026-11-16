"""Participant data remains ungraded until the common reveal."""
from .models import ClueAnswer
from .progress import current_round


def visible_score(participant):
    quiz = participant.quiz
    hidden = 0
    if quiz.current_question_id:
        hidden = sum(ClueAnswer.objects.filter(quiz=quiz, participant=participant,
            question_id=quiz.current_question_id, round_number=current_round(quiz)).values_list('points_earned', flat=True))
    return participant.total_score - hidden


def ungraded_answer(answer):
    result = dict(answer or {})
    for field in ('is_correct', 'points_earned', 'is_close', 'total_score', 'can_mark_correct', 'is_manual_override'):
        result.pop(field, None)
    return result
