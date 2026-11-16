"""Shared round identity; game adapters retain their own scoring rules."""
from .unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids


def current_round(quiz):
    session = getattr(quiz, 'session', None)
    return session.current_question_number if session and session.question_rounds else 0


def record_round(session, question):
    history = list((session.question_rounds or [])[:session.current_question_number])
    if not history and session.current_question_number:
        # Retain the legacy ordering of saved answers; keep unanswered slots unknown.
        from .quiz_pause import session_code_for
        from .unit_tutorial_runtime import get_unit_tutorial_state
        key = {'QuizGame': 'quiz', 'who_is_lying': 'who', 'who_is_that': 'who_that', 'Estimation': 'estimation'}[session.quiz._meta.app_label]
        code = session_code_for(session.quiz, key)
        if key == 'estimation' and code is None:
            codes = list(session.quiz.participants.order_by().values_list('hub_session_code', flat=True).distinct()[:2])
            if len(codes) == 1:
                code = codes[0]
        relation = {'quiz': 'quiz_answers', 'who': 'who_answers', 'who_that': 'who_that_answers', 'estimation': 'estimation_answers'}[key]
        known = getattr(session.quiz, relation).filter(round_number=0, participant__hub_session_code=code).order_by('submitted_at', 'id').values_list('question_id', flat=True)
        history = list(dict.fromkeys(known))[:session.current_question_number]
        tutorial = get_unit_tutorial_state(key, session.quiz.room_code, code)
        tutorial_id = tutorial.get('tutorial_question_id')
        if tutorial.get('tutorial_has_been_played') and tutorial_id and tutorial_id not in history:
            history.insert(0, tutorial_id)
            history = history[:session.current_question_number]
    history.extend([None] * max(0, session.current_question_number - len(history)))
    history.append(question.id)
    session.question_rounds = history


def ordered_rounds(quiz, game_key, session_code=None):
    session = getattr(quiz, 'session', None)
    excluded = set(get_scorebox_excluded_tutorial_question_ids(game_key, quiz.room_code, session_code))
    selected = list(quiz.selected_questions.all())
    order = {str(value): index for index, value in enumerate(quiz.question_order or [])}
    selected.sort(key=lambda question: order.get(str(question.id), len(order)))
    history = list(session.question_rounds or []) if session else []
    question_model = quiz.selected_questions.model
    by_id = {question.id: question for question in question_model.objects.filter(pk__in=[pk for pk in history if pk])}
    rows = []
    seen = set()
    for physical_number, question_id in enumerate(history, 1):
        if question_id in excluded:
            continue
        rows.append({'question': by_id.get(question_id), 'round_number': physical_number, 'number': len(rows) + 1})
        seen.add(question_id)
    unknown = history.count(None)
    for question in selected:
        if question.id in excluded or question.id in seen:
            continue
        if unknown:
            unknown -= 1
            continue
        rows.append({'question': question, 'round_number': None, 'number': len(rows) + 1})
    return rows


def participant_history(quiz, participant, game_key, session_code=None):
    relation = {'quiz': 'quiz_answers', 'who': 'who_answers', 'who_that': 'who_that_answers', 'estimation': 'estimation_answers'}[game_key]
    answers = list(getattr(participant, relation).filter(quiz=quiz).select_related('question'))
    result = []
    rows = ordered_rounds(quiz, game_key, session_code)
    legacy_positions = {}
    for row in rows:
        if row['question']:
            legacy_positions.setdefault(row['question'].id, row['round_number'])
    for row in rows:
        question = row['question']
        if not question or row['round_number'] is None:
            continue
        if quiz.current_question_id and row['round_number'] == current_round(quiz):
            continue
        answer = next((answer for answer in answers if answer.question_id == question.id
                       and (answer.round_number == row['round_number'] or (
                           answer.round_number == 0 and row['round_number'] == legacy_positions.get(question.id)))), None)
        result.append({'question_id': question.id, 'question_number': row['number'],
                       'round_number': row['round_number'], 'is_correct': getattr(answer, 'is_correct', False),
                       'points': answer.points_earned if answer else 0, 'points_earned': answer.points_earned if answer else 0,
                       'max_points': question.get_effective_max_points() if game_key == 'quiz' else 1 if game_key == 'who_that' else question.get_total_possible_points()})
    return result


def scoreboard(quiz, game_key, session_code=None):
    result = []
    active = current_round(quiz)
    for row in ordered_rounds(quiz, game_key, session_code):
        question = row['question']
        result.append({'id': question.id if question else None, 'number': row['number'],
                       'round_number': row['round_number'],
                       'max_points': (question.get_effective_max_points() if game_key == 'quiz' else 1 if game_key == 'who_that' else question.get_total_possible_points()) if question else 0,
                       'status': 'current' if quiz.current_question_id and row['round_number'] == active else 'played' if row['round_number'] else 'upcoming'})
    return result


def answer_number(answer, game_key):
    return next((row['number'] for row in ordered_rounds(answer.quiz, game_key, answer.participant.hub_session_code)
                 if row['round_number'] == answer.round_number), None)


def progress(quiz, game_key, session_code=None, *, question_id=None, prepared=False):
    session = getattr(quiz, 'session', None)
    excluded = set(get_scorebox_excluded_tutorial_question_ids(game_key, quiz.room_code, session_code))
    rows = ordered_rounds(quiz, game_key, session_code)
    history = list(session.question_rounds or []) if session else []
    if history:
        number = sum(question_id not in excluded for question_id in history)
        total = len(rows)
    else:
        from .unit_tutorial_runtime import get_unit_tutorial_state
        tutorial = get_unit_tutorial_state(game_key, quiz.room_code, session_code)
        tutorial_started = tutorial.get('requested') and (tutorial.get('current_unit_is_tutorial') or tutorial.get('tutorial_has_been_played'))
        number = max(0, (session.current_question_number if session else 0) - int(bool(tutorial_started)))
        total = len(rows)
    if prepared and question_id is not None:
        number += 1
        if history and question_id in history:
            total += 1
    return {'kind': 'question' if game_key in {'quiz', 'who_that', 'estimation'} else 'set',
            'current': number if question_id is not None and question_id not in excluded and number > 0 else None,
            'total': total or None}
