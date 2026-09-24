from datetime import timedelta


QUIZ_ANSWER_REVEAL_STAGGER_MS = 300
QUIZ_ANSWER_REVEAL_ANIMATION_MS = 220
QUIZ_ANSWER_REVEAL_PAUSE_MS = 180
QUIZ_AUTOMATIC_ANSWER_OPEN_TYPES = frozenset({'multiple_choice', 'true_false'})


def quiz_answers_open_automatically(question_type):
    return question_type in QUIZ_AUTOMATIC_ANSWER_OPEN_TYPES


def quiz_answer_reveal_stagger_ms(question_type):
    if question_type != 'multiple_choice':
        return QUIZ_ANSWER_REVEAL_STAGGER_MS

    from admin_dashboard.models import DashboardSettings

    return DashboardSettings.quiz_mc_answer_reveal_stagger()


def multiple_choice_answering_starts_at(
    revealed_at,
    option_count,
    stagger_ms=QUIZ_ANSWER_REVEAL_STAGGER_MS,
):
    if not revealed_at:
        return None
    final_option_offset = max(0, int(option_count or 0) - 1)
    delay_ms = (
        final_option_offset * max(0, int(stagger_ms or 0))
        + QUIZ_ANSWER_REVEAL_ANIMATION_MS
        + QUIZ_ANSWER_REVEAL_PAUSE_MS
    )
    return revealed_at + timedelta(milliseconds=delay_ms)


def question_typewriter_duration_ms(text, milliseconds_per_character):
    """Return the deterministic Unicode code-point reveal duration."""
    return len(str(text or '')) * max(1, int(milliseconds_per_character or 1))


def question_typewriter_timing(text):
    """Return the shared dashboard-controlled timing for a public prompt."""
    from admin_dashboard.models import DashboardSettings

    milliseconds_per_character = DashboardSettings.question_reveal_speed()
    return {
        'question_presentation_duration_ms': question_typewriter_duration_ms(
            text,
            milliseconds_per_character,
        ),
        'question_reveal_ms_per_character': milliseconds_per_character,
    }
