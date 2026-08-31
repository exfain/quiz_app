from datetime import timedelta


QUIZ_ANSWER_REVEAL_STAGGER_MS = 300
QUIZ_ANSWER_REVEAL_ANIMATION_MS = 220
QUIZ_ANSWER_REVEAL_PAUSE_MS = 180
QUIZ_AUTOMATIC_ANSWER_OPEN_TYPES = frozenset({'multiple_choice', 'true_false'})


def quiz_answers_open_automatically(question_type):
    return question_type in QUIZ_AUTOMATIC_ANSWER_OPEN_TYPES


def multiple_choice_answering_starts_at(revealed_at, option_count):
    if not revealed_at:
        return None
    final_option_offset = max(0, int(option_count or 0) - 1)
    delay_ms = (
        final_option_offset * QUIZ_ANSWER_REVEAL_STAGGER_MS
        + QUIZ_ANSWER_REVEAL_ANIMATION_MS
        + QUIZ_ANSWER_REVEAL_PAUSE_MS
    )
    return revealed_at + timedelta(milliseconds=delay_ms)
