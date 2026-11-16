"""Shared round identity with estimation-specific point limits and history."""
from games_hub.quiz_rounds import current_round, ordered_rounds, progress as round_progress
from games_hub.quiz_pause import session_code_for


def scope_code(quiz, session_code=None):
    code = session_code if session_code is not None else session_code_for(quiz, 'estimation')
    if code is None:
        # Legacy imported games may predate HubGameStep records. Never mix pools.
        codes = list(quiz.participants.order_by().values_list('hub_session_code', flat=True).distinct()[:2])
        if len(codes) == 1:
            code = codes[0]
    return code


def participant_count(quiz, session_code=None):
    code = scope_code(quiz, session_code)
    return quiz.participants.filter(hub_session_code=code).count()


def max_points(quiz, question, session_code=None, round_number=None):
    session = getattr(quiz, 'session', None)
    if round_number is None and question.pk == quiz.current_question_id:
        round_number = current_round(quiz)
    if (quiz.get_effective_scoring_mode() == 'rank' and quiz.status == 'active'
            and question.pk == quiz.current_question_id and round_number == current_round(quiz)):
        return participant_count(quiz, session_code)
    limits = session.round_max_points if session else {}
    if str(round_number) in (limits or {}):
        return int(limits[str(round_number)])
    if quiz.get_effective_scoring_mode() == 'rank' and round_number == 0:
        scored = quiz.estimation_answers.filter(question=question, round_number=0,
            participant__hub_session_code=session_code).order_by('-points_earned').first()
        if scored:
            return scored.points_earned
    return question.get_max_points_for_mode(quiz.get_effective_scoring_mode(), participant_count(quiz, session_code))


def question_progress(quiz, session_code=None, question_id=None):
    return round_progress(quiz, 'estimation', session_code, question_id=question_id)


def metadata(quiz, session_code=None):
    from games_hub.unit_tutorial_runtime import is_current_unit_tutorial_question
    tutorial = is_current_unit_tutorial_question('estimation', quiz.room_code, session_code, quiz.current_question_id)
    progress = question_progress(quiz, session_code, quiz.current_question_id)
    if not progress['current'] and not tutorial and quiz.current_question_id and not getattr(getattr(quiz, 'session', None), 'question_rounds', None):
        progress['current'] = max(1, getattr(getattr(quiz, 'session', None), 'current_question_number', 0))
    return {'round_number': current_round(quiz), 'question_number': progress['current'],
        'total_questions': progress['total'], 'is_tutorial_round': tutorial,
        'paused': quiz.status == 'inactive',
        'max_points': 0 if tutorial else (max_points(quiz, quiz.current_question, session_code) if quiz.current_question else 0),
        'scoreboard': scoreboard(quiz, session_code)}


def scoreboard(quiz, session_code=None):
    active = current_round(quiz)
    return [{'id': row['question'].pk if row['question'] else None, 'number': row['number'],
        'round_number': row['round_number'],
        'max_points': max_points(quiz, row['question'], session_code, row['round_number']) if row['question'] else 0,
        'status': 'current' if quiz.current_question_id and row['round_number'] == active else 'played' if row['round_number'] else 'upcoming'}
        for row in ordered_rounds(quiz, 'estimation', session_code)]


def participant_board(quiz, participant, session_code=None):
    rows = scoreboard(quiz, session_code)
    answers = list(participant.estimation_answers.filter(quiz=quiz))
    history = []
    first_rounds = {}
    for row in rows:
        first_rounds.setdefault(row['id'], row['round_number'])
        if row['status'] != 'played':
            continue
        answer = next((answer for answer in answers if answer.question_id == row['id'] and (
            answer.round_number == row['round_number'] or
            (answer.round_number == 0 and row['round_number'] == first_rounds[row['id']]))), None)
        row['earned_points'] = answer.points_earned if answer else 0
        history.append({'question_id': row['id'], 'question_number': row['number'],
            'round_number': row['round_number'], 'points': row['earned_points'], 'max_points': row['max_points']})
    progress = question_progress(quiz, session_code, quiz.current_question_id)
    return rows, history, progress['current'] or sum(row['status'] == 'played' for row in rows)
