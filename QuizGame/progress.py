from games_hub.quiz_rounds import progress
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids, get_unit_tutorial_state


def public_question_number(session, room_code, hub_session_code=None):
    if session is None:
        return 0
    if session.question_rounds:
        excluded = set(get_scorebox_excluded_tutorial_question_ids('quiz', room_code, hub_session_code))
        return sum(question_id not in excluded for question_id in session.question_rounds)
    tutorial = get_unit_tutorial_state('quiz', room_code, hub_session_code)
    tutorial_was_started = tutorial.get('requested') and (
        tutorial.get('current_unit_is_tutorial') or tutorial.get('tutorial_has_been_played')
    )
    return max(0, session.current_question_number - int(bool(tutorial_was_started)))


def question_progress(quiz, hub_session_code=None, *, question_id=None, prepared=False, session=None):
    return progress(quiz, 'quiz', hub_session_code, question_id=question_id, prepared=prepared)
