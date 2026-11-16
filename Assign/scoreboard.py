from .models import AssignAnswer
from games_hub.set_progress import ordered_sets
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids, get_unit_tutorial_state


def _get_run_tutorial_question_id(quiz, session_code=None):
    state = get_unit_tutorial_state('assign', quiz.room_code, session_code)
    return state.get('tutorial_question_id') if state.get('requested') else quiz.tutorial_question_id


def get_ordered_quiz_questions(quiz, session_code=None):
    return [row['question'] for row in ordered_sets(quiz, 'assign', session_code)]


def build_participant_progress_history(quiz, participant, session_code=None):
    rows = ordered_sets(quiz, 'assign', session_code if session_code is not None else participant.hub_session_code)
    answers = list(AssignAnswer.objects.filter(quiz=quiz, participant=participant, is_tutorial=False).select_related('question').order_by('submitted_at', 'id'))
    first = {}
    history = []
    for row in rows:
        question = row['question']
        first.setdefault(question.pk, row['set_number'])
        answer = next((answer for answer in answers if answer.question_id == question.pk and (
            answer.set_number == row['set_number'] or
            (answer.set_number == 0 and row['set_number'] == first[question.pk]))), None)
        if answer is None or not row['played']:
            continue
        history.append({'question_id': question.pk, 'question_number': row['number'],
                        **({'set_number': row['set_number']} if row['set_number'] else {}), 'survived_rounds': answer.get_correct_matches_count(),
                        'max_rounds': len(question.correct_matches or {})})
    return history


def build_question_scoreboard(quiz, participant=None, session_code=None):
    rows = ordered_sets(quiz, 'assign', session_code)
    history = build_participant_progress_history(quiz, participant, session_code) if participant else []
    progress = {entry['question_number']: entry for entry in history}
    active = quiz.get_current_set_number()
    current = next((row['number'] for row in rows if row['question'].pk == quiz.current_question_id and row['set_number'] == active), None)
    if current is None:
        current = next((row['number'] for row in rows if row['question'].pk == quiz.current_question_id), None)
    if current is None:
        current = next((row['number'] for row in rows if row['number'] not in progress), None)
    result = []
    for row in rows:
        entry = progress.get(row['number'])
        status = 'played' if entry else 'current' if row['number'] == current else 'upcoming'
        result.append({'id': row['question'].pk, 'number': row['number'], **({'set_number': row['set_number']} if row['set_number'] else {}),
                       'earned_points': entry['survived_rounds'] if entry else None,
                       'max_points': entry['max_rounds'] if entry else len(row['question'].correct_matches or {}) if row['question'].pk == quiz.current_question_id else None,
                       'status': status})
    return result
