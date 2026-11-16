"""Round-aware Clue Rush progress and participant history."""
from games_hub.quiz_rounds import current_round, ordered_rounds, progress
from games_hub.quiz_pause import session_code_for
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids, get_unit_tutorial_state


def record_round(session, question):
    history = list(session.question_rounds or [])
    if not history and session.current_question_number:
        code = session_code_for(session.quiz, 'clue_rush')
        known = session.quiz.clue_answers.filter(round_number=0, participant__hub_session_code=code).order_by('submitted_at', 'id').values_list('question_id', flat=True)
        history = list(dict.fromkeys(known))[:session.current_question_number]
        tutorial = get_unit_tutorial_state('clue_rush', session.quiz.room_code, code)
        tutorial_id = tutorial.get('tutorial_question_id')
        if tutorial.get('tutorial_has_been_played') and tutorial_id and tutorial_id not in history:
            history.insert(0, tutorial_id)
            history = history[:session.current_question_number]
        history.extend([None] * max(0, session.current_question_number - len(history)))
    history.append(question.pk)
    session.question_rounds = history


def question_progress(quiz, code=None, question_id=None):
    result = progress(quiz, 'clue_rush', code, question_id=question_id)
    result['kind'] = 'question'
    return result


def score_history(quiz, participant):
    excluded = set(get_scorebox_excluded_tutorial_question_ids('clue_rush', quiz.room_code, participant.hub_session_code))
    answers = participant.clue_answers.filter(quiz=quiz, is_tutorial=False).exclude(question_id__in=excluded).select_related('question').order_by('submitted_at', 'id')
    rows = ordered_rounds(quiz, 'clue_rush', participant.hub_session_code)
    history = []
    for answer in answers:
        if quiz.current_question_id == answer.question_id and answer.round_number == current_round(quiz):
            continue
        number = next((row['number'] for row in rows if row['round_number'] == answer.round_number), len(history) + 1)
        history.append({'question_number': number,
                        'correct_answer': answer.question.answer,
                        'achieved_points': answer.points_earned, 'max_points': answer.total_clues_at_submission or answer.question.clues.count()})
    return history
