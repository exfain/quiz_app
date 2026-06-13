from .models import AssignAnswer
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids, get_unit_tutorial_state


def _get_run_tutorial_question_id(quiz, session_code=None):
    state = get_unit_tutorial_state('assign', quiz.room_code, session_code)
    if not state.get('requested'):
        return getattr(quiz, 'tutorial_question_id', None)
    return state.get('tutorial_question_id')


def get_ordered_quiz_questions(quiz, session_code=None):
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('assign', quiz.room_code, session_code)
    configured_questions = []
    configured_by_id = {}
    if quiz.selected_questions.exists():
        selected = list(quiz.selected_questions.all())
        order = [int(question_id) for question_id in (quiz.question_order or [])]
        if order:
            order_map = {question_id: index for index, question_id in enumerate(order)}
            selected.sort(key=lambda question: order_map.get(question.id, len(order)))
        configured_questions = selected
        configured_by_id = {question.id: question for question in selected}

    questions = []
    seen_ids = set()

    played_answers = (
        AssignAnswer.objects
        .filter(quiz=quiz)
        .select_related('question')
        .order_by('submitted_at', 'id')
    )
    if session_code:
        played_answers = played_answers.filter(participant__hub_session_code=session_code)
    for answer in played_answers:
        if answer.question_id in tutorial_question_ids:
            continue
        if answer.question_id in seen_ids:
            continue
        questions.append(answer.question)
        seen_ids.add(answer.question_id)

    if quiz.current_question_id and quiz.current_question_id not in seen_ids and quiz.current_question_id not in tutorial_question_ids:
        current_question = configured_by_id.get(quiz.current_question_id, quiz.current_question)
        questions.append(current_question)
        seen_ids.add(quiz.current_question_id)

    for question in configured_questions:
        if question.id in tutorial_question_ids:
            continue
        if question.id in seen_ids:
            continue
        questions.append(question)
        seen_ids.add(question.id)

    return questions


def build_participant_progress_history(quiz, participant, session_code=None):
    scoped_session_code = session_code if session_code is not None else participant.hub_session_code
    ordered_questions = get_ordered_quiz_questions(quiz, scoped_session_code)
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids('assign', quiz.room_code, scoped_session_code)
    question_number_by_id = {
        question.id: index
        for index, question in enumerate(ordered_questions, start=1)
    }
    answers = (
        AssignAnswer.objects
        .filter(quiz=quiz, participant=participant)
        .select_related('question')
        .order_by('submitted_at', 'id')
    )
    history = []
    seen_question_ids = set()
    for answer in answers:
        if answer.question_id in tutorial_question_ids:
            continue
        if answer.question_id in seen_question_ids:
            continue
        question_number = question_number_by_id.get(answer.question_id)
        if question_number is None:
            continue
        seen_question_ids.add(answer.question_id)
        history.append({
            'question_id': answer.question_id,
            'question_number': question_number,
            'survived_rounds': answer.get_correct_matches_count(),
            'max_rounds': len(answer.question.correct_matches or {}),
        })
    history.sort(key=lambda entry: entry['question_number'])
    return history


def build_question_scoreboard(quiz, participant=None, session_code=None):
    ordered_questions = get_ordered_quiz_questions(quiz, session_code)
    progress_history = (
        build_participant_progress_history(quiz, participant, session_code)
        if participant is not None
        else []
    )
    progress_by_number = {
        entry['question_number']: entry
        for entry in progress_history
    }

    current_question_id = quiz.current_question_id if quiz.current_question_id else None
    current_question_number = next(
        (index for index, question in enumerate(ordered_questions, start=1) if question.id == current_question_id),
        None,
    )
    if current_question_number is None:
        current_question_number = next(
            (
                index for index, _question in enumerate(ordered_questions, start=1)
                if index not in progress_by_number
            ),
            None,
        )

    question_scoreboard = []
    for index, question in enumerate(ordered_questions, start=1):
        history_entry = progress_by_number.get(index)
        if history_entry:
            status = 'played'
            earned_points = history_entry['survived_rounds']
            max_points = history_entry['max_rounds']
        elif current_question_id and current_question_number == index:
            status = 'current'
            earned_points = None
            max_points = len(question.correct_matches or {})
        elif current_question_number == index:
            status = 'current'
            earned_points = None
            max_points = None
        else:
            status = 'upcoming'
            earned_points = None
            max_points = None

        question_scoreboard.append({
            'id': question.id,
            'number': index,
            'earned_points': earned_points,
            'max_points': max_points,
            'status': status,
        })

    return question_scoreboard
