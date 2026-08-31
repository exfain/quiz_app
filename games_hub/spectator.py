import math

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from Assign.models import AssignQuiz
from Assign.runtime import (
    ASSIGN_REVEAL_ANIMATION_MS,
    ASSIGN_REVEAL_STAGGER_MS,
    current_set_runtime,
)
from Estimation.models import EstimationQuiz
from QuizGame.models import Quiz as QuizGameModel
from QuizGame.presentation import (
    QUIZ_ANSWER_REVEAL_ANIMATION_MS,
    QUIZ_ANSWER_REVEAL_PAUSE_MS,
    QUIZ_ANSWER_REVEAL_STAGGER_MS,
)
from black_jack_quiz.models import BlackJackQuiz
from buzzer.models import BuzzerGame
from host_points.models import HostPointsGame
from wann_war_das.models import WannWarDasGame
from clue_rush.models import ClueRushGame
from clue_rush.runtime import reconcile_clue_schedule
from games_hub.authoritative_state import current_snapshot, question_content_is_visible
from games_hub.game_intro import serialize_game_intro
from games_hub.models import GameRuntimeState, HubSession
from games_hub.unit_tutorial_runtime import is_current_unit_tutorial_question
from sorting_ladder.models import SortingLadderGame, SortingLadderSession
from sorting_ladder.runtime import (
    SORTING_LADDER_REVEAL_ANIMATION_MS,
    SORTING_LADDER_REVEAL_STAGGER_MS,
    sorting_ladder_reveal_counts,
)
from wer_weiss_mehr.models import WerWeissMehrGame
from wer_weiss_mehr.services import build_game_state as build_wer_weiss_mehr_state
from where_is_this.models import WhereQuiz
from who_is_lying.models import WhoQuiz, get_question_timer_state
from who_is_that.models import WhoThatQuiz


GAME_MODELS = {
    'quiz': QuizGameModel,
    'assign': AssignQuiz,
    'estimation': EstimationQuiz,
    'where': WhereQuiz,
    'who': WhoQuiz,
    'who_that': WhoThatQuiz,
    'blackjack': BlackJackQuiz,
    'clue_rush': ClueRushGame,
    'sorting_ladder': SortingLadderGame,
    'wer_weiss_mehr': WerWeissMehrGame,
    'buzzer': BuzzerGame,
    'host_points': HostPointsGame,
    'wann_war_das': WannWarDasGame,
}

GAME_LABELS = {
    'quiz': 'Quick Quiz',
    'assign': 'Assign',
    'estimation': 'Estimation',
    'where': 'Where is this',
    'who': 'Who is lying',
    'who_that': 'Who is that',
    'blackjack': 'Black Jack Quiz',
    'clue_rush': 'Clue Rush',
    'sorting_ladder': 'Sorting Ladder',
    'buzzer': 'Buzzer',
    'host_points': 'Host-Punktevergabe',
    'wann_war_das': 'Wann war das?',
    'wer_weiss_mehr': 'Wer weiß mehr?',
}

_IN_PROGRESS_QUESTION_PHASES = {
    GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE,
    GameRuntimeState.QUESTION_PHASE_CONTENT_VISIBLE,
    GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN,
}
_REVEAL_ONLY_QUESTION_FIELDS = {
    'correct_answer',
    'correct_location',
    'correct_pairs',
    'explanation',
    'final_order',
    'liars',
    'target',
    'truth_tellers',
    'zones',
}


def build_spectator_state(session):
    """Build a read-only, session-scoped state for the hub spectator view."""
    if isinstance(session, str):
        session = HubSession.objects.get(code=session)

    server_now = timezone.now()
    state = {
        'success': True,
        'server_now': server_now.isoformat(),
        'session': {
            'code': session.code,
            'name': session.name or session.code,
            'is_active': session.is_active,
            'started': session.started_at is not None,
            'ended': session.ended_at is not None,
        },
        'phase': 'ended' if session.ended_at else 'waiting',
        'message': 'Session beendet.' if session.ended_at else 'Warte auf das naechste Spiel.',
        'game': None,
    }

    step, game = _resolve_display_game(session)
    if not step or not game:
        return state
    if step.game_key == 'clue_rush':
        reconcile_clue_schedule(step.room_code)
        game = _get_step_game(step)
        if not game:
            return state

    runtime = current_snapshot(step.game_key, step.room_code, session.code)
    intro = serialize_game_intro(step, server_now=server_now)
    game_state = {
        'game_key': step.game_key,
        'label': GAME_LABELS.get(step.game_key, step.get_game_key_display()),
        'room_code': step.room_code,
        'title': getattr(game, 'title', '') or step.title or GAME_LABELS.get(step.game_key, step.game_key),
        'status': getattr(game, 'status', None),
        'step_order': step.order,
        'step_title': step.title,
        'game_number': step.order + 1,
        'game_instance_id': intro['game_instance_id'],
        'state_revision': max(
            int(runtime.get('state_revision') or 0),
            int(intro.get('state_revision') or 0),
        ),
        'intro': intro,
    }

    serializer = {
        'quiz': _serialize_quick_quiz,
        'estimation': _serialize_estimation,
        'where': _serialize_where,
        'who': _serialize_who,
        'who_that': _serialize_who_that,
        'blackjack': _serialize_blackjack,
        'clue_rush': _serialize_clue_rush,
        'assign': _serialize_assign,
        'sorting_ladder': _serialize_sorting_ladder,
        'wer_weiss_mehr': _serialize_wer_weiss_mehr,
        'buzzer': _serialize_buzzer,
        'host_points': _serialize_host_points,
        'wann_war_das': _serialize_wann_war_das,
    }.get(step.game_key)

    payload = _public_spectator_payload(serializer(game, session) if serializer else {})
    game_state.update(payload)
    question = game_state.get('question')
    if question:
        question['is_tutorial_round'] = is_current_unit_tutorial_question(
            step.game_key,
            step.room_code,
            session.code,
            question.get('id') or getattr(game, 'current_question_id', None),
        )
    game_state['state_revision'] = max(
        int(game_state.get('state_revision') or 0),
        int(payload.get('state_revision') or 0),
    )
    if getattr(game, 'status', None) == 'completed':
        game_state['scoreboard'] = _single_game_scoreboard(session, step)
    state['game'] = game_state
    state['phase'] = payload.get('phase') or ('complete' if getattr(game, 'status', None) == 'completed' else 'game_waiting')
    state['message'] = payload.get('message') or ''
    return state


def _public_spectator_payload(payload):
    """Keep solution data out of non-reveal responses as a final boundary."""
    if payload.get('phase') == 'reveal' or not payload.get('question'):
        return payload
    public_payload = dict(payload)
    public_question = dict(payload['question'])
    for field in _REVEAL_ONLY_QUESTION_FIELDS:
        public_question.pop(field, None)
    public_payload['question'] = public_question
    return public_payload


def _manual_question_is_in_progress(runtime, manual_current_question):
    return bool(
        manual_current_question
        and runtime.get('question_phase') in _IN_PROGRESS_QUESTION_PHASES
    )


def _single_game_scoreboard(session, step):
    # Imported lazily because games_hub.views imports this module for the state endpoint.
    from games_hub.views import get_post_game_results

    results = get_post_game_results(session, step)
    if not results.get('available'):
        return {
            'available': False,
            'rows': [],
            'message': results.get('message') or 'Keine Spielergebnisse verfuegbar.',
        }
    return {
        'available': True,
        'title': (results.get('game') or {}).get('title') or step.title,
        'rows': [
            {
                'rank': row.get('rank'),
                'participant': row.get('participant'),
                'game_points': row.get('game_points'),
            }
            for row in results.get('rows', [])
        ],
    }


def _resolve_display_game(session):
    steps = list(session.steps.order_by('order'))
    if not steps:
        return None, None

    active = []
    for step in steps:
        game = _get_step_game(step)
        if game and getattr(game, 'status', None) == 'active' and _game_is_current_session_run(game, session):
            active.append((step, game))
    if active:
        return active[0]

    index = min(max(session.current_step_index or 0, 0), len(steps) - 1)
    current_step = steps[index]
    current_game = _get_step_game(current_step)
    if (
        current_game
        and getattr(current_game, 'status', None) in {'active', 'completed'}
        and _game_is_current_session_run(current_game, session)
    ):
        return current_step, current_game
    return None, None


def _get_step_game(step):
    model = GAME_MODELS.get(step.game_key)
    if not model or not step.room_code:
        return None
    return model.objects.filter(room_code=step.room_code).first()


def _game_is_current_session_run(game, session):
    if not session.started_at:
        return True
    started_at = getattr(game, 'started_at', None)
    ended_at = getattr(game, 'ended_at', None)
    if started_at and started_at >= session.started_at:
        return True
    if ended_at and ended_at >= session.started_at:
        return True
    return started_at is None and ended_at is None


def _manual_runtime(game_key, game, session):
    runtime = current_snapshot(game_key, game.room_code, session.code)
    is_current = bool(
        runtime.get('question_flow_mode') == 'manual_three_phase'
        and runtime.get('current_question_id')
        and str(runtime.get('current_question_id')) == str(getattr(game, 'current_question_id', '') or '')
    )
    return runtime, is_current


def _runtime_fields(runtime):
    return {
        key: runtime.get(key)
        for key in (
            'state_revision',
            'server_now',
            'question_flow_mode',
            'question_phase',
            'question_presented_at',
            'question_visible_at',
            'content_revealed_at',
            'answering_started_at',
            'answering_deadline_at',
            'answering_allowed',
        )
    }


def _runtime_timer(runtime, fallback=None):
    if runtime.get('question_flow_mode') != 'manual_three_phase':
        return fallback or _timer_payload(None)
    if runtime.get('question_phase') != 'answering_open':
        return _timer_payload(None)
    deadline = parse_datetime(runtime.get('answering_deadline_at') or '')
    if not deadline:
        return _timer_payload(None)
    starts_at = parse_datetime(runtime.get('answering_started_at') or '')
    now = timezone.now()
    if starts_at and now < starts_at:
        return {
            'active': False,
            'seconds_left': None,
            'starts_at': starts_at.isoformat(),
            'ends_at': deadline.isoformat(),
        }
    seconds_left = max(0, int(math.ceil((deadline - now).total_seconds())))
    return {
        'active': seconds_left > 0,
        'seconds_left': seconds_left,
        'starts_at': starts_at.isoformat() if starts_at else None,
        'ends_at': deadline.isoformat(),
    }


def _question_is_visible(runtime):
    if runtime.get('question_phase') in {'content_visible', 'answering_open'}:
        return True
    visible_at = parse_datetime(runtime.get('question_visible_at') or '')
    return not visible_at or timezone.now() >= visible_at


def _question_progress(question, ordered_questions, *, kind='question'):
    ids = [item.id for item in ordered_questions]
    try:
        current = ids.index(question.id) + 1
    except (ValueError, AttributeError):
        current = None
    return {
        'kind': kind,
        'current': current,
        'total': len(ids) or None,
    }


def _serialize_quick_quiz(quiz, session):
    quiz_session = _safe_session(quiz)
    ordered_questions = _ordered_questions(quiz)
    question, reveal = _current_or_last_question(
        quiz,
        session,
        quiz_session,
        'quiz_answers',
        ordered_questions,
    )
    if not question:
        return _game_waiting_payload(quiz_session)

    runtime, manual_current_question = _manual_runtime('quiz', quiz, session)
    question_phase = runtime.get('question_phase')
    if manual_current_question and question_phase in {
        'prompt_visible',
        'content_visible',
        'answering_open',
    }:
        # A prepared manual question is intentionally inactive until answering opens.
        # That legacy inactive flag must not be interpreted as a completed question.
        reveal = False
    if manual_current_question and not _question_is_visible(runtime):
        waiting = _game_waiting_payload(None)
        waiting.update(_runtime_fields(runtime))
        return waiting
    manual_prompt_only = manual_current_question and question_phase == 'prompt_visible'

    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'type': question.question_type,
    }
    if not manual_prompt_only:
        question_payload['options'] = _quiz_options(question)
    if reveal:
        question_payload['correct_answer'] = question.get_formatted_correct_answer()
        question_payload['explanation'] = question.explanation or ''

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Aktuelle Frage',
        'timer': _runtime_timer(runtime, _timer_payload(quiz_session)),
        'question': question_payload,
        'response_count': _answer_count(quiz, session, 'quiz_answers', question),
        'progress': _question_progress(question, ordered_questions),
        'answer_reveal_stagger_ms': QUIZ_ANSWER_REVEAL_STAGGER_MS,
        'answer_reveal_animation_ms': QUIZ_ANSWER_REVEAL_ANIMATION_MS,
        'answer_reveal_pause_ms': QUIZ_ANSWER_REVEAL_PAUSE_MS,
        **_runtime_fields(runtime),
    }


def _serialize_estimation(quiz, session):
    quiz_session = _safe_session(quiz)
    runtime, manual_current_question = _manual_runtime('estimation', quiz, session)
    ordered_questions = _ordered_questions(quiz)
    if runtime.get('question_flow_mode') == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE:
        question = None
        reveal = False
        if _manual_question_is_in_progress(runtime, manual_current_question):
            question = quiz.current_question
        elif (
            runtime.get('phase') in {'question_result', 'revealed'}
            and str(runtime.get('game_id') or '') == str(quiz.pk)
        ):
            revealed_question_id = str(
                runtime.get('current_question_id')
                or (runtime.get('question') or {}).get('id')
                or ''
            )
            question = next(
                (item for item in ordered_questions if str(item.id) == revealed_question_id),
                None,
            )
            reveal = question is not None
    else:
        question, reveal = _current_or_last_question(
            quiz,
            session,
            quiz_session,
            'estimation_answers',
            ordered_questions,
        )
    if not question:
        waiting = _game_waiting_payload(quiz_session)
        waiting.update(_runtime_fields(runtime))
        return waiting

    if manual_current_question and runtime.get('question_phase') in {
        'prompt_visible',
        'content_visible',
        'answering_open',
    }:
        reveal = False
        if not _question_is_visible(runtime):
            waiting = _game_waiting_payload(None)
            waiting.update(_runtime_fields(runtime))
            return waiting

    prompt_details_visible = (
        not manual_current_question
        or runtime.get('question_phase') in {
            'prompt_visible',
            'content_visible',
            'answering_open',
        }
    )

    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'scoring_mode': quiz.get_effective_scoring_mode(),
    }
    if prompt_details_visible:
        question_payload['unit'] = question.get_unit_display_text()
        question_payload['hint'] = question.hint_text or ''
    if reveal:
        question_payload['correct_answer'] = question.get_formatted_correct_answer()
        question_payload['explanation'] = question.explanation or ''
        if quiz.get_effective_scoring_mode() == 'zones':
            question_payload['zones'] = question.get_zone_reveal_data()

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Schaetzfrage',
        'timer': _runtime_timer(runtime, _timer_payload(quiz_session)),
        'question': question_payload,
        'response_count': _answer_count(quiz, session, 'estimation_answers', question),
        'progress': _question_progress(question, ordered_questions),
        **_runtime_fields(runtime),
    }


def _serialize_where(quiz, session):
    quiz_session = _safe_session(quiz)
    ordered_questions = _ordered_questions(quiz)
    question, reveal = _current_or_last_question(
        quiz,
        session,
        quiz_session,
        'where_answers',
        ordered_questions,
    )
    if not question:
        return _game_waiting_payload(quiz_session)

    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'image_url': _file_url(question.image),
    }
    if reveal:
        question_payload['correct_location'] = {
            'latitude': question.correct_latitude,
            'longitude': question.correct_longitude,
        }
        question_payload['explanation'] = question.explanation or ''

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Aktueller Ort',
        'timer': _timer_payload(quiz_session),
        'question': question_payload,
        'response_count': _answer_count(quiz, session, 'where_answers', question),
        'progress': _question_progress(question, ordered_questions),
    }


def _serialize_who_that(quiz, session):
    quiz_session = _safe_session(quiz)
    runtime, manual_current_question = _manual_runtime('who_that', quiz, session)
    ordered_questions = _ordered_questions(quiz)
    if manual_current_question and runtime.get('question_phase') in {
        'prompt_visible',
        'content_visible',
        'answering_open',
    }:
        if not question_content_is_visible(runtime):
            waiting = _game_waiting_payload(None)
            waiting.update({
                'question_phase': runtime.get('question_phase'),
                'state_revision': runtime.get('state_revision'),
                'server_now': runtime.get('server_now'),
            })
            return waiting
        question, reveal = quiz.current_question, False
    else:
        question, reveal = _current_or_last_question(
            quiz,
            session,
            quiz_session,
            'who_that_answers',
            ordered_questions,
        )
    if not question:
        return _game_waiting_payload(quiz_session)

    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'image_url': _file_url(question.image),
        'category': question.category or '',
    }
    if reveal:
        question_payload['correct_answer'] = question.correct_answer
        question_payload['explanation'] = question.explanation or ''

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Wer ist das?',
        'timer': _runtime_timer(runtime, _timer_payload(quiz_session)),
        'question': question_payload,
        'response_count': _answer_count(quiz, session, 'who_that_answers', question),
        'progress': _question_progress(question, ordered_questions),
        **_runtime_fields(runtime),
    }


def _serialize_who(quiz, session):
    quiz_session = _safe_session(quiz)
    runtime, manual_current_question = _manual_runtime('who', quiz, session)
    ordered_questions = _ordered_questions(quiz)
    question, reveal = _current_or_last_question(
        quiz,
        session,
        quiz_session,
        'who_answers',
        ordered_questions,
    )
    if not question:
        waiting = _game_waiting_payload(quiz_session)
        waiting['message'] = 'Warte auf das naechste Set.'
        return waiting
    if manual_current_question and not _question_is_visible(runtime):
        waiting = _game_waiting_payload(None)
        waiting.update(_runtime_fields(runtime))
        return waiting
    if _manual_question_is_in_progress(runtime, manual_current_question):
        reveal = False

    timer_state = get_question_timer_state(
        question,
        question_start_time=getattr(quiz, 'question_start_time', None),
        question_end_time=getattr(quiz_session, 'question_end_time', None),
        people_count=len(question.people or []),
    )
    current_person = None
    people = question.people or []
    if people and not reveal and (
        not manual_current_question or runtime.get('question_phase') == 'answering_open'
    ):
        current_person = people[min(timer_state.get('current_person_index', 0), len(people) - 1)].get('name', '')

    question_payload = {
        'id': question.id,
        'statement': question.statement,
        'current_person': current_person,
        'people_count': len(people),
        'timer_state': timer_state,
        'set_number': int(getattr(quiz_session, 'current_question_number', 0) or 0),
        'total_sets': len(ordered_questions),
    }
    if reveal:
        question_payload['liars'] = [person.get('name', '') for person in question.get_liars()]
        question_payload['truth_tellers'] = [person.get('name', '') for person in question.get_truth_tellers()]
        question_payload['explanation'] = question.explanation or ''

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Aktuelle Behauptung',
        'timer': _runtime_timer(runtime, _timer_payload(quiz_session)),
        'question': question_payload,
        'response_count': _answer_count(quiz, session, 'who_answers', question),
        'progress': {
            'kind': 'set',
            'current': question_payload['set_number'] or None,
            'total': question_payload['total_sets'] or None,
        },
        **_runtime_fields(runtime),
    }


def _serialize_blackjack(quiz, session):
    quiz_session = _safe_session(quiz)
    runtime, manual_current_question = _manual_runtime('blackjack', quiz, session)
    question, reveal = _current_or_last_blackjack_question(quiz, session, quiz_session)
    if not question:
        return _game_waiting_payload(quiz_session)
    if manual_current_question and not _question_is_visible(runtime):
        waiting = _game_waiting_payload(None)
        waiting.update(_runtime_fields(runtime))
        return waiting
    if _manual_question_is_in_progress(runtime, manual_current_question):
        reveal = False

    set_number = quiz.get_set_number_for_question_id(question.id, active_only=False)
    question_in_set = quiz_session.get_played_question_position_in_set(question.id, set_number, active_only=False) if quiz_session else quiz.get_question_number_in_set(question_id=question.id)
    set_question_count = quiz.get_set_question_count(set_number=set_number)
    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'set_number': set_number,
        'total_sets': quiz.get_total_sets(),
        'question_in_set': question_in_set,
        'set_question_count': set_question_count,
        'scoring_mode': quiz.scoring_mode,
    }
    if reveal:
        question_payload['correct_answer'] = question.correct_answer
        question_payload['target'] = 21
        question_payload['explanation'] = question.explanation or ''

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Black-Jack-Frage',
        'timer': _runtime_timer(runtime, _timer_payload(quiz_session)),
        'question': question_payload,
        'response_count': _answer_count(quiz, session, 'blackjack_answers', question),
        'progress': {
            'kind': 'set_round',
            'set': set_number,
            'set_total': quiz.get_total_sets(),
            'current': question_in_set,
            'total': set_question_count,
        },
        **_runtime_fields(runtime),
    }


def _serialize_clue_rush(game, session):
    game_session = _safe_session(game)
    runtime, manual_current_question = _manual_runtime('clue_rush', game, session)
    ordered_questions = _ordered_questions(game)
    question = game.current_question or _last_question_from_answers(game, session, 'clue_answers')
    if not question:
        return _game_waiting_payload(game_session)
    if manual_current_question and not _question_is_visible(runtime):
        waiting = _game_waiting_payload(None)
        waiting.update(_runtime_fields(runtime))
        return waiting

    reveal = getattr(game, 'status', None) == 'completed' or (
        game_session
        and not game_session.is_question_active
        and not game_session.is_clue_active
        and game.current_question_id
    )
    if _manual_question_is_in_progress(runtime, manual_current_question):
        reveal = False
    revealed_count = 0
    if not manual_current_question or runtime.get('question_phase') == 'answering_open':
        revealed_count = question.get_revealed_clue_count(
            current_clue=game.current_clue,
            current_clue_order=getattr(game_session, 'current_clue_number', 0),
        )
    if reveal:
        revealed_count = question.clues.count()

    clues = [
        {
            'number': index + 1,
            'text': clue.clue_text,
        }
        for index, clue in enumerate(question.clues.order_by('order', 'id')[:revealed_count])
    ]
    answer_locks = [
        {
            'participant': answer.participant.name,
            'clue_number': max(1, int(answer.submitted_clue_number or 1)),
        }
        for answer in game.clue_answers.filter(
            question=question,
            participant__hub_session_code=session.code,
        ).select_related('participant').order_by('submitted_at')
    ]

    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'clues': clues,
        'answer_locks': answer_locks,
    }
    if reveal:
        question_payload['correct_answer'] = question.answer

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Hinweise',
        'timer': _runtime_timer(
            runtime,
            _timer_payload(game_session, end_attr='clue_end_time'),
        ),
        'question': question_payload,
        'response_count': len(answer_locks),
        'progress': _question_progress(question, ordered_questions),
        **_runtime_fields(runtime),
    }


def _serialize_assign(quiz, session):
    quiz_session = _safe_session(quiz)
    runtime, manual_current_question = _manual_runtime('assign', quiz, session)
    ordered_questions = _ordered_questions(quiz)
    question, reveal = _current_or_last_question(
        quiz,
        session,
        quiz_session,
        'assign_answers',
        ordered_questions,
    )
    if not question:
        return _game_waiting_payload(quiz_session)
    if manual_current_question and not _question_is_visible(runtime):
        waiting = _game_waiting_payload(None)
        waiting.update(_runtime_fields(runtime))
        return waiting
    if _manual_question_is_in_progress(runtime, manual_current_question):
        reveal = False

    randomized = question.get_randomized_items(room_code=quiz.room_code)
    set_runtime = current_set_runtime(quiz.room_code, session.code)
    show_assignment = (
        not manual_current_question
        or runtime.get('question_phase') in {'content_visible', 'answering_open'}
        or reveal
    )
    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'left_items': randomized.get('left_items', []) if show_assignment else [],
        'right_items': randomized.get('right_items', []) if show_assignment else [],
        'set_number': getattr(set_runtime, 'set_number', None),
        'round': (
            int(getattr(set_runtime, 'current_round_index', 0) or 0) + 1
            if set_runtime
            else None
        ),
        'total_rounds': len(question.correct_matches or {}),
        'solved_pairs': list((getattr(set_runtime, 'solved_matches', {}) or {}).values()),
    }
    if reveal:
        question_payload['correct_pairs'] = _assign_correct_pairs(question)
        question_payload['explanation'] = question.explanation or ''

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Aufloesung' if reveal else 'Zuordnungsaufgabe',
        'timer': _runtime_timer(runtime, _timer_payload(quiz_session)),
        'question': question_payload,
        'response_count': _answer_count(quiz, session, 'assign_answers', question),
        'progress': _question_progress(question, ordered_questions, kind='set'),
        'assign_reveal_stagger_ms': ASSIGN_REVEAL_STAGGER_MS,
        'assign_reveal_animation_ms': ASSIGN_REVEAL_ANIMATION_MS,
        **_runtime_fields(runtime),
    }


def _serialize_sorting_ladder(game, session):
    game_session = _safe_session(game)
    runtime, manual_current_question = _manual_runtime('sorting_ladder', game, session)
    question = game.current_question
    if not question:
        return _game_waiting_payload(game_session)
    if manual_current_question and not _question_is_visible(runtime):
        waiting = _game_waiting_payload(None)
        waiting.update(_runtime_fields(runtime))
        return waiting

    reveal = bool(
        getattr(game, 'status', None) == 'completed'
        or (
            game_session
            and game_session.reveal_state == SortingLadderSession.REVEAL_REVEALED
        )
    )
    round_number = max(int(getattr(game_session, 'current_round', 0) or 0), 1)
    item_count = question.elements.count()
    reveal_counts = sorting_ladder_reveal_counts(
        item_count=item_count,
        round_number=round_number,
    )
    presentation_open = (
        not manual_current_question
        or runtime.get('question_phase') in {'content_visible', 'answering_open'}
        or reveal
    )
    items_by_id = {
        item.id: item
        for item in question.elements.all()
    }
    try:
        shuffled_ids = [
            int(item_id)
            for item_id in (getattr(game_session, 'shuffled_item_ids', '') or '').split(',')
            if item_id
        ]
    except ValueError:
        shuffled_ids = []
    presentation_order = [
        items_by_id[item_id]
        for item_id in shuffled_ids
        if item_id in items_by_id
    ]
    presentation_order.extend(
        item
        for item in sorted(items_by_id.values(), key=lambda item: item.id)
        if item.id not in shuffled_ids
    )
    fixed_item_id = getattr(question, 'starting_item_id', None)
    presentation_items = [
        _sorting_item_payload(item)
        for item in presentation_order
        if item.id != fixed_item_id
    ] if presentation_open else []
    placed = []
    if game_session and (presentation_open or round_number > 1):
        placed = [
            _sorting_item_payload(item)
            for item in game_session.placed_elements.order_by('correct_rank', 'id')
        ]
    active_element = (
        _sorting_item_payload(game_session.active_element)
        if presentation_open and game_session and game_session.active_element
        else None
    )
    question_payload = {
        'id': question.id,
        'text': question.question_text,
        'description': question.description or '',
        'lower_label': question.lower_label if presentation_open or round_number > 1 else '',
        'upper_label': question.upper_label if presentation_open or round_number > 1 else '',
        'placed_elements': placed,
        'items': presentation_items,
        'fixed_element': (
            _sorting_item_payload(question.starting_item)
            if presentation_open and question.starting_item_id
            else None
        ),
        'active_element': active_element,
        'round': round_number,
        'total_rounds': max(item_count - 1, 0),
    }
    if reveal:
        question_payload['final_order'] = [
            _sorting_item_payload(item, include_rank=True)
            for item in question.elements.order_by('correct_rank', 'id')
        ]

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Finale Reihenfolge' if reveal else 'Sorting Ladder',
        'timer': _runtime_timer(
            runtime,
            _timer_payload(game_session, end_attr='round_end_time'),
        ),
        'question': question_payload,
        'response_count': 0,
        'progress': {
            'kind': 'round',
            'current': round_number,
            'total': max(item_count - 1, 0) or None,
        },
        'sorting_reveal_counts': reveal_counts,
        'sorting_reveal_stagger_ms': SORTING_LADDER_REVEAL_STAGGER_MS,
        'sorting_reveal_animation_ms': SORTING_LADDER_REVEAL_ANIMATION_MS,
        **_runtime_fields(runtime),
    }


def _serialize_buzzer(game, session):
    state = game.serialize_state(session.code)
    round_state = state.get('round') or {}
    return {
        'phase': 'complete' if getattr(game, 'status', None) == 'completed' else 'question',
        'message': 'Buzzer',
        'round': round_state,
        'participants': state.get('participants', []),
        'question': {
            'text': f"Runde {round_state.get('number') or 0}",
            'current_buzz_participant': round_state.get('current_buzz_participant'),
            'buzzer_open': round_state.get('buzzer_open'),
        },
        'response_count': len(round_state.get('buzzes') or []),
        'progress': {
            'kind': 'round',
            'current': round_state.get('number') or None,
            'total': (state.get('game') or {}).get('planned_rounds') or None,
        },
        **_runtime_fields(state),
    }


def _serialize_host_points(game, session):
    state = game.serialize_state(session.code)
    round_state = state.get('round') or {}
    return {
        'phase': 'complete' if getattr(game, 'status', None) == 'completed' else 'question',
        'message': 'Host-Punktevergabe',
        'round': round_state,
        'participants': state.get('participants', []),
        'question': {
            'text': f"Runde {round_state.get('number') or 0}",
            'note': 'Der Host vergibt die Punkte manuell.',
        },
        'response_count': 0,
        'progress': {
            'kind': 'round',
            'current': round_state.get('number') or None,
            'total': None,
        },
        **_runtime_fields(state),
    }


def _serialize_wann_war_das(game, session):
    state = game.serialize_state(session.code)
    visible_at = parse_datetime(state.get('question_visible_at') or '')
    server_now = parse_datetime(state.get('server_now') or '') or timezone.now()
    question_visible = not (
        state.get('question_phase') == 'prompt_visible'
        and visible_at
        and server_now < visible_at
    )
    question = (state.get('question') or {}) if question_visible else {}
    timer = state.get('timer') or {}
    ordered_questions = _ordered_questions(game)
    question_payload = {
        'id': question.get('id'),
        'text': question.get('question_text') or 'Warte auf Frage',
        'current_tolerance': timer.get('current_tolerance'),
        'current_points': timer.get('current_points'),
    }
    if state.get('question_state') == 'revealed':
        question_payload['correct_answer'] = question.get('formatted_correct_answer')
    return {
        'phase': 'complete' if getattr(game, 'status', None) == 'completed' else (
            'reveal' if state.get('question_state') == 'revealed' else 'question'
        ),
        'message': 'Wann war das?',
        'timer': timer,
        'question': question_payload,
        'participants': state.get('participants', []),
        'response_count': len(state.get('answers') or []),
        'progress': {
            'kind': 'question',
            'current': state.get('question_number') or None,
            'total': len(ordered_questions) or None,
        },
        **_runtime_fields(state),
    }


def _serialize_wer_weiss_mehr(game, session):
    state = build_wer_weiss_mehr_state(game, hub_session_code=session.code)
    question = state.get('question')
    if not question:
        return _game_waiting_payload(getattr(game, 'session', None))

    reveal = state.get('phase') == 'set_completed' or getattr(game, 'status', None) == 'completed'
    question_visible = bool(state.get('question_visible'))
    question_payload = {
        'id': question['id'],
        'text': question['question_text'] if question_visible else '',
        'round': state.get('current_round', 0),
        'answer_count': question.get('answer_count', 0),
        'revealed_count': question.get('revealed_count', 0),
        'tiles': [
            {
                **tile,
                'text': tile.get('text') if tile.get('revealed') else '',
            }
            for tile in question.get('tiles', [])
        ],
        'question_visible_at': state.get('question_visible_at'),
        'field_reveal_stagger_ms': state.get('field_reveal_stagger_ms'),
        'field_reveal_animation_ms': state.get('field_reveal_animation_ms'),
        'field_reveal_ready_at': state.get('field_reveal_ready_at'),
    }

    return {
        'phase': 'reveal' if reveal else 'question',
        'message': 'Auflösung' if reveal else 'Wer weiß mehr?',
        'timer': state.get('timer', {}),
        'question': question_payload,
        'question_phase': state.get('question_phase'),
        'server_now': state.get('server_now'),
        'response_count': len([
            response for response in state.get('responses', [])
            if response.get('answer_text') or response.get('has_pending_input')
        ]),
        'progress': {
            'kind': 'set_round',
            'set': state.get('current_set_number') or None,
            'current': state.get('current_round') or None,
            'total': None,
        },
        **_runtime_fields(state),
    }


def _current_or_last_question(quiz, hub_session, runtime_session, answer_relation, ordered_questions):
    if getattr(quiz, 'current_question_id', None):
        reveal = bool(
            getattr(quiz, 'status', None) == 'completed'
            or (
                runtime_session
                and not getattr(runtime_session, 'is_question_active', False)
                and int(getattr(runtime_session, 'total_questions_sent', 0) or getattr(runtime_session, 'current_question_number', 0) or 0) > 0
            )
        )
        return quiz.current_question, reveal

    last_question = _last_question_from_answers(quiz, hub_session, answer_relation)
    if not last_question and runtime_session:
        total_sent = int(getattr(runtime_session, 'total_questions_sent', 0) or getattr(runtime_session, 'current_question_number', 0) or 0)
        if total_sent > 0 and ordered_questions:
            last_question = ordered_questions[min(total_sent, len(ordered_questions)) - 1]

    reveal = bool(
        last_question
        and (
            getattr(quiz, 'status', None) == 'completed'
            or (
                runtime_session
                and not getattr(runtime_session, 'is_question_active', False)
                and int(getattr(runtime_session, 'total_questions_sent', 0) or getattr(runtime_session, 'current_question_number', 0) or 0) > 0
            )
        )
    )
    return last_question, reveal


def _current_or_last_blackjack_question(quiz, hub_session, runtime_session):
    if getattr(quiz, 'current_question_id', None):
        reveal = bool(
            runtime_session
            and not runtime_session.is_question_active
            and int(runtime_session.total_questions_sent or runtime_session.current_question_number or 0) > 0
        )
        return quiz.current_question, reveal

    last_question = None
    if runtime_session:
        asked_ids = runtime_session.get_asked_question_ids()
        if asked_ids:
            from black_jack_quiz.models import BlackJackQuestion
            last_question = BlackJackQuestion.objects.filter(id=asked_ids[-1]).first()
    if not last_question:
        last_question = _last_question_from_answers(quiz, hub_session, 'blackjack_answers')

    reveal = bool(
        last_question
        and runtime_session
        and not runtime_session.is_question_active
        and int(runtime_session.total_questions_sent or runtime_session.current_question_number or 0) > 0
    )
    return last_question, reveal


def _last_question_from_answers(quiz, hub_session, answer_relation):
    answer_manager = getattr(quiz, answer_relation, None)
    if answer_manager is None:
        return None
    answer = (
        answer_manager.filter(participant__hub_session_code=hub_session.code)
        .select_related('question')
        .order_by('-submitted_at')
        .first()
    )
    return answer.question if answer else None


def _answer_count(quiz, hub_session, answer_relation, question):
    if not question:
        return 0
    answer_manager = getattr(quiz, answer_relation, None)
    if answer_manager is None:
        return 0
    return answer_manager.filter(
        question=question,
        participant__hub_session_code=hub_session.code,
    ).count()


def _ordered_questions(quiz):
    field = quiz._meta.get_field('current_question')
    question_model = field.remote_field.model

    ordered_ids = _flatten_question_order(getattr(quiz, 'question_order', []) or [])
    if ordered_ids:
        by_id = question_model.objects.in_bulk(ordered_ids)
        return [by_id[question_id] for question_id in ordered_ids if question_id in by_id]

    selected = getattr(quiz, 'selected_questions', None)
    if selected is not None and selected.exists():
        return list(selected.all())
    return []


def _flatten_question_order(raw_order):
    flattened = []
    for item in raw_order or []:
        if isinstance(item, (list, tuple)):
            flattened.extend(_flatten_question_order(item))
            continue
        try:
            question_id = int(item)
        except (TypeError, ValueError):
            continue
        if question_id > 0 and question_id not in flattened:
            flattened.append(question_id)
    return flattened


def _safe_session(quiz):
    try:
        return quiz.session
    except Exception:
        return None


def _game_waiting_payload(runtime_session=None):
    return {
        'phase': 'game_waiting',
        'message': 'Warte auf die naechste Frage.',
        'timer': _timer_payload(runtime_session),
        'question': None,
        'response_count': 0,
    }


def _timer_payload(runtime_session, end_attr='question_end_time'):
    if not runtime_session:
        return {'active': False, 'seconds_left': None, 'ends_at': None}
    end_time = getattr(runtime_session, end_attr, None)
    if not end_time:
        return {'active': False, 'seconds_left': None, 'ends_at': None}
    seconds_left = max(0, int(math.ceil((end_time - timezone.now()).total_seconds())))
    return {
        'active': seconds_left > 0,
        'seconds_left': seconds_left,
        'ends_at': end_time.isoformat(),
    }


def _quiz_options(question):
    if question.question_type == 'true_false':
        return [{'key': 'True', 'text': 'True'}, {'key': 'False', 'text': 'False'}]
    return [{'key': key, 'text': text} for key, text in question.get_options()]


def _file_url(field_file):
    try:
        return field_file.url if field_file else ''
    except ValueError:
        return ''


def _assign_correct_pairs(question):
    pairs = []
    for left_index, right_index in (question.correct_matches or {}).items():
        try:
            left_pos = int(left_index)
            right_pos = int(right_index)
        except (TypeError, ValueError):
            continue
        left_text = question.left_items[left_pos] if 0 <= left_pos < len(question.left_items) else str(left_index)
        right_text = question.right_items[right_pos] if 0 <= right_pos < len(question.right_items) else str(right_index)
        pairs.append({'left': left_text, 'right': right_text})
    return pairs


def _sorting_item_payload(item, *, include_rank=False):
    if not item:
        return None
    payload = {
        'id': item.id,
        'text': item.text,
        'image_url': item.image_url or '',
    }
    if include_rank:
        payload['rank'] = str(item.correct_rank)
    return payload
