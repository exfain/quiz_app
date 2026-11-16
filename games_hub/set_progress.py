"""Played-set identity and progress for Assign and Sorting Ladder."""
from .quiz_pause import session_code_for
from .unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids


def scope_code(quiz, game_key, code=None):
    return code or session_code_for(quiz, game_key) or None


def ordered_sets(quiz, game_key, code=None):
    code = scope_code(quiz, game_key, code)
    excluded = set(get_scorebox_excluded_tutorial_question_ids(game_key, quiz.room_code, code))
    selected = list(quiz.selected_questions.all())
    order = {str(pk): number for number, pk in enumerate(quiz.question_order or [])}
    selected.sort(key=lambda question: order.get(str(question.pk), len(order)))
    if game_key == 'assign':
        played = [(row.question, row.set_number) for row in quiz.set_runtimes.filter(hub_session_code=code or '').select_related('question').order_by('set_number')]
        legacy = quiz.assign_answers.filter(set_number=0, participant__hub_session_code=code).select_related('question').order_by('submitted_at', 'id')
    else:
        session = getattr(quiz, 'session', None)
        history = list(session.question_rounds or []) if session else []
        by_id = quiz.selected_questions.model.objects.in_bulk([pk for pk in history if pk])
        played = [(by_id[pk], number) for number, pk in enumerate(history, 1) if pk in by_id]
        legacy = quiz.submissions.filter(set_number=0, participant__hub_session_code=code).select_related('question').order_by('submitted_at', 'id')
    known = {question.pk for question, _ in played}
    before = []
    for answer in legacy:
        if answer.question_id not in known:
            before.append((answer.question, 0))
            known.add(answer.question_id)
    played = before + played
    if quiz.current_question_id and quiz.current_question_id not in known:
        played.append((quiz.current_question, quiz.get_current_set_number() or 0))
    rows = []
    for question, number in played:
        if question.pk not in excluded:
            rows.append({'question': question, 'set_number': number, 'number': len(rows) + 1, 'played': True})
    seen = {row['question'].pk for row in rows}
    for question in selected:
        if question.pk not in excluded and question.pk not in seen:
            rows.append({'question': question, 'set_number': None, 'number': len(rows) + 1, 'played': False})
    return rows


def set_progress(quiz, game_key, code=None):
    rows = ordered_sets(quiz, game_key, code)
    current = quiz.get_current_set_number()
    active = next((row['number'] for row in rows if row['played'] and row['set_number'] == current and row['question'].pk == quiz.current_question_id), None)
    if active is None and quiz.current_question_id:
        active = next((row['number'] for row in rows if row['question'].pk == quiz.current_question_id), None)
    if active is None:
        active = sum(row['played'] for row in rows) or None
    return {'kind': 'set', 'current': active, 'total': len(rows) or None}
