import math

from django.db import transaction
from django.utils import timezone
from games_hub.unit_tutorial_runtime import (
    get_scorebox_excluded_tutorial_question_ids,
    get_unit_tutorial_state,
    is_current_unit_tutorial_question,
    is_unit_tutorial_question,
)

from .models import (
    WerWeissMehrAnswerOption,
    WerWeissMehrGame,
    WerWeissMehrParticipant,
    WerWeissMehrParticipantState,
    WerWeissMehrPendingInput,
    WerWeissMehrQuestion,
    WerWeissMehrRound,
    WerWeissMehrRoundResponse,
    WerWeissMehrSession,
)


def get_ordered_questions(quiz):
    questions = list(quiz.selected_questions.filter(is_active=True).prefetch_related('answers'))
    order = quiz.question_order or []
    if order:
        order_map = {int(question_id): index for index, question_id in enumerate(order)}
        questions.sort(key=lambda question: order_map.get(question.id, len(order_map)))
    return questions


def get_ordered_scored_questions(quiz, hub_session_code=None):
    tutorial_question_ids = get_scorebox_excluded_tutorial_question_ids(
        'wer_weiss_mehr',
        quiz.room_code,
        hub_session_code,
    )
    return [question for question in get_ordered_questions(quiz) if question.id not in tutorial_question_ids]


def get_scoped_participants(quiz, hub_session_code=None, active_only=False):
    participants = quiz.participants.all()
    if hub_session_code is not None:
        participants = participants.filter(hub_session_code=hub_session_code)
    if active_only:
        participants = participants.filter(is_active=True)
    return participants.order_by('name')


def start_set(quiz, question_id, hub_session_code=None, time_limit_seconds=None):
    question = WerWeissMehrQuestion.objects.get(id=question_id, is_active=True)
    is_tutorial_round = is_unit_tutorial_question('wer_weiss_mehr', quiz.room_code, hub_session_code, question.id)
    if (
        quiz.selected_questions.exists()
        and not is_tutorial_round
        and not quiz.selected_questions.filter(id=question.id).exists()
    ):
        raise ValueError('Dieses Set gehoert nicht zu diesem Spiel.')
    session, _ = WerWeissMehrSession.objects.get_or_create(quiz=quiz)
    participants = get_scoped_participants(quiz, hub_session_code=hub_session_code, active_only=True)
    return session.start_set(question, participants_qs=participants, time_limit_seconds=time_limit_seconds)


def store_pending_input(quiz, participant, answer_text):
    quiz.refresh_from_db()
    if quiz.status != 'active':
        return None
    session = getattr(quiz, 'session', None)
    question = quiz.current_question
    if not session or not question or session.phase != WerWeissMehrSession.PHASE_ROUND_ACTIVE:
        return None
    if not _participant_can_answer(quiz, participant, question):
        return None
    pending, _ = WerWeissMehrPendingInput.objects.update_or_create(
        quiz=quiz,
        participant=participant,
        question=question,
        round_number=session.current_round,
        defaults={'answer_text': answer_text or ''},
    )
    return pending


def submit_answer(quiz, participant, answer_text):
    quiz.refresh_from_db()
    participant.refresh_from_db()
    if quiz.status != 'active':
        raise ValueError('Das Spiel ist nicht aktiv.')
    session = getattr(quiz, 'session', None)
    question = quiz.current_question
    if not session or not question or session.phase != WerWeissMehrSession.PHASE_ROUND_ACTIVE:
        raise ValueError('Aktuell laeuft keine Runde.')
    if not _participant_can_answer(quiz, participant, question):
        raise ValueError('Du kannst in diesem Set nicht mehr antworten.')

    round_state = WerWeissMehrRound.objects.filter(
        quiz=quiz,
        question=question,
        round_number=session.current_round,
    ).first()
    if not round_state:
        raise ValueError('Rundenstatus fehlt.')

    response, _ = WerWeissMehrRoundResponse.objects.update_or_create(
        quiz=quiz,
        participant=participant,
        question=question,
        round_number=session.current_round,
        defaults={'answer_text': answer_text or ''},
    )
    response.evaluate(revealed_before_round_ids=round_state.revealed_answer_ids_at_start)
    WerWeissMehrPendingInput.objects.filter(
        quiz=quiz,
        participant=participant,
        question=question,
        round_number=session.current_round,
    ).delete()
    return response


@transaction.atomic
def apply_manual_correction(quiz, response_id, target_answer_id):
    quiz.refresh_from_db()
    if quiz.status != 'active':
        raise ValueError('Das Spiel ist nicht aktiv.')
    session = getattr(quiz, 'session', None)
    if not session or session.phase not in {
        WerWeissMehrSession.PHASE_ROUND_ACTIVE,
        WerWeissMehrSession.PHASE_REVIEW,
    }:
        raise ValueError('Korrekturen sind nur waehrend einer laufenden Runde oder in der Review-Phase moeglich.')

    response = WerWeissMehrRoundResponse.objects.select_for_update().select_related(
        'question',
        'participant',
    ).filter(id=response_id, quiz=quiz).first()
    if not response:
        raise ValueError('Antwort wurde nicht gefunden.')
    if response.question_id != quiz.current_question_id or response.round_number != session.current_round:
        raise ValueError('Diese Antwort gehoert nicht zur aktuellen Review-Runde.')
    target = WerWeissMehrAnswerOption.objects.filter(id=target_answer_id, question=response.question).first()
    if not target:
        raise ValueError('Zielantwort gehoert nicht zum aktuellen Set.')
    round_state = WerWeissMehrRound.objects.filter(
        quiz=quiz,
        question=response.question,
        round_number=response.round_number,
    ).first()
    if round_state and int(target.id) in {int(item) for item in (round_state.revealed_answer_ids_at_start or [])}:
        raise ValueError('Diese Zielantwort war bereits vor Beginn dieser Runde aufgedeckt.')
    response.apply_manual_correction(target)
    session.revealed_answers.add(target)
    _apply_correct_response_progress(response)
    if session.phase == WerWeissMehrSession.PHASE_REVIEW:
        session.sync_review_scores()
    return response


def end_current_round(quiz):
    quiz.refresh_from_db()
    if quiz.status != 'active':
        raise ValueError('Das Spiel ist nicht aktiv.')
    session = getattr(quiz, 'session', None)
    if not session:
        raise ValueError('Keine Runtime-Session gefunden.')
    return session.end_current_round()


def next_round_or_finish(quiz):
    quiz.refresh_from_db()
    if quiz.status != 'active':
        raise ValueError('Das Spiel ist nicht aktiv.')
    session = getattr(quiz, 'session', None)
    if not session:
        raise ValueError('Keine Runtime-Session gefunden.')
    if session.phase != WerWeissMehrSession.PHASE_REVIEW:
        raise ValueError('Aktuell ist keine Runde in der Review-Phase.')
    return session.finalize_review()


def start_next_round_after_review(quiz):
    quiz.refresh_from_db()
    if quiz.status != 'active':
        raise ValueError('Das Spiel ist nicht aktiv.')
    session = getattr(quiz, 'session', None)
    if not session:
        raise ValueError('Keine Runtime-Session gefunden.')
    if session.phase != WerWeissMehrSession.PHASE_REVIEW:
        raise ValueError('Aktuell ist keine Runde in der Review-Phase.')
    if not _can_start_next_round(quiz, session, quiz.current_question):
        raise ValueError('Keine weitere Runde moeglich. Bitte das Set beenden.')
    return session.finalize_review()


def finish_set(quiz):
    quiz.refresh_from_db()
    if quiz.status != 'active':
        raise ValueError('Das Spiel ist nicht aktiv.')
    session = getattr(quiz, 'session', None)
    if not session or not quiz.current_question:
        raise ValueError('Aktuell ist kein Set aktiv.')
    if session.phase == WerWeissMehrSession.PHASE_SET_COMPLETED:
        return session
    if session.phase == WerWeissMehrSession.PHASE_ROUND_ACTIVE:
        session.end_current_round()
        session.refresh_from_db()
    if session.phase == WerWeissMehrSession.PHASE_REVIEW:
        session.finalize_review(advance=False)
        session.refresh_from_db()
        return session
    session.finish_current_set()
    session.refresh_from_db()
    return session


def clear_current_set(quiz):
    quiz.refresh_from_db()
    session = getattr(quiz, 'session', None)
    if not session:
        raise ValueError('Keine Runtime-Session gefunden.')
    if session.phase != WerWeissMehrSession.PHASE_SET_COMPLETED:
        raise ValueError('Die Setauswahl ist erst nach einem abgeschlossenen Set moeglich.')
    session.clear_current_set()
    return session


def build_game_state(quiz, hub_session_code=None, participant_name=None):
    session, _ = WerWeissMehrSession.objects.get_or_create(quiz=quiz)
    has_incomplete_start = quiz.status == 'active' and quiz.started_at is None
    question = None if has_incomplete_start else quiz.current_question
    is_participant_view = bool(participant_name)
    participant = None
    if participant_name:
        participant = WerWeissMehrParticipant.objects.filter(
            quiz=quiz,
            name=participant_name,
            hub_session_code=hub_session_code,
        ).first()

    participants = list(get_scoped_participants(quiz, hub_session_code=hub_session_code))
    participant_ids = [participant.id for participant in participants]
    states = {
        state.participant_id: state
        for state in WerWeissMehrParticipantState.objects.filter(
            quiz=quiz,
            participant_id__in=participant_ids,
        ).select_related('question')
    }
    current_states = {}
    if question:
        current_states = {
            state.participant_id: state
            for state in WerWeissMehrParticipantState.objects.filter(
                quiz=quiz,
                question=question,
                participant_id__in=participant_ids,
            )
        }

    response_qs = WerWeissMehrRoundResponse.objects.none()
    if question and session.current_round:
        response_qs = WerWeissMehrRoundResponse.objects.filter(
            quiz=quiz,
            question=question,
            round_number=session.current_round,
            participant_id__in=participant_ids,
        ).select_related('participant', 'matched_answer')

    response_map = {response.participant_id: response for response in response_qs}
    responses = [_serialize_response(response) for response in response_qs.order_by('participant__name')]
    own_response = response_map.get(participant.id) if participant else None
    own_state = current_states.get(participant.id) if participant else None
    own_pending = None
    if participant and question and session.current_round:
        own_pending = WerWeissMehrPendingInput.objects.filter(
            quiz=quiz,
            participant=participant,
            question=question,
            round_number=session.current_round,
        ).first()

    return {
        'success': True,
        'server_now': timezone.now().isoformat(),
        'game_status': 'waiting' if has_incomplete_start else quiz.status,
        'phase': WerWeissMehrSession.PHASE_IDLE if has_incomplete_start else session.phase,
        'room_code': quiz.room_code,
        'title': quiz.title,
        'current_round': 0 if has_incomplete_start else session.current_round,
        'timer': _idle_timer_payload(session) if has_incomplete_start else _timer_payload(session),
        'question': _serialize_question(
            question,
            session,
            hub_session_code=hub_session_code,
            include_hidden_text=not is_participant_view,
        ) if question else None,
        'available_questions': []
        if is_participant_view
        else [_serialize_available_question(question, session) for question in get_ordered_scored_questions(quiz, hub_session_code)],
        'participants': [_serialize_participant(p, current_states.get(p.id)) for p in participants],
        'responses': [] if is_participant_view else responses,
        'target_answers': []
        if is_participant_view
        else (_serialize_target_answers(question, session) if question else []),
        'can_start_next_round': _can_start_next_round(quiz, session, question),
        'scorebox': _serialize_scorebox(quiz, participants, hub_session_code=hub_session_code),
        'participant_state': _serialize_own_state(quiz, participant, question, own_state, own_response, own_pending, session)
        if participant else None,
    }


def _participant_can_answer(quiz, participant, question):
    state = WerWeissMehrParticipantState.objects.filter(
        quiz=quiz,
        participant=participant,
        question=question,
        is_eliminated=False,
    ).exists()
    return bool(participant.is_active and state)


def _timer_payload(session):
    remaining = 0
    if session.phase == WerWeissMehrSession.PHASE_ROUND_ACTIVE and session.round_end_time:
        remaining = max(0, math.ceil((session.round_end_time - timezone.now()).total_seconds()))
    return {
        'time_limit_seconds': session.time_limit_seconds,
        'remaining_seconds': remaining,
        'started_at': session.round_start_time.isoformat() if session.round_start_time else None,
        'ends_at': session.round_end_time.isoformat() if session.round_end_time else None,
    }


def _idle_timer_payload(session):
    return {
        'time_limit_seconds': session.time_limit_seconds,
        'remaining_seconds': 0,
        'started_at': None,
        'ends_at': None,
    }


def _serialize_available_question(question, session=None):
    is_completed = bool(session and question.id in set(session.completed_question_ids or []))
    return {
        'id': question.id,
        'question_text': question.question_text,
        'round_time_limit': question.round_time_limit,
        'answer_count': question.answers.count(),
        'status': 'completed' if is_completed else 'available',
        'is_completed': is_completed,
    }


def _serialize_question(question, session, hub_session_code=None, include_hidden_text=False):
    revealed_ids = set(session.revealed_answers.filter(question=question).values_list('id', flat=True))
    if session.phase == WerWeissMehrSession.PHASE_REVIEW and session.current_round:
        revealed_ids.update(
            WerWeissMehrRoundResponse.objects.filter(
                quiz=session.quiz,
                question=question,
                round_number=session.current_round,
                is_correct=True,
                matched_answer__isnull=False,
            ).values_list('matched_answer_id', flat=True)
        )
    answers = question.answers.order_by('sort_order', 'canonical_text', 'id')
    reveal_all = session.phase == WerWeissMehrSession.PHASE_SET_COMPLETED
    answer_count = answers.count()
    return {
        'id': question.id,
        'question_text': question.question_text,
        'round_time_limit': question.round_time_limit,
        'is_tutorial_round': is_current_unit_tutorial_question(
            'wer_weiss_mehr',
            session.quiz.room_code,
            hub_session_code,
            question.id,
        ),
        'answer_count': answer_count,
        'revealed_count': answer_count if reveal_all else len(revealed_ids),
        'tiles': [
            {
                'id': answer.id,
                'position': index,
                'revealed': answer.id in revealed_ids or reveal_all,
                'text': answer.canonical_text
                if include_hidden_text or answer.id in revealed_ids or reveal_all
                else '',
            }
            for index, answer in enumerate(answers, start=1)
        ],
    }


def _serialize_target_answers(question, session):
    revealed_ids = set(session.revealed_answers.filter(question=question).values_list('id', flat=True))
    return [
        {
            'id': answer.id,
            'text': answer.canonical_text,
            'revealed': answer.id in revealed_ids,
        }
        for answer in question.answers.order_by('sort_order', 'canonical_text', 'id')
    ]


def _serialize_participant(participant, state=None):
    return {
        'id': participant.id,
        'name': participant.name,
        'is_active': participant.is_active,
        'total_score': participant.total_score,
        'is_eliminated': state.is_eliminated if state else False,
        'survived_rounds': state.survived_rounds if state else 0,
        'last_status': state.last_status if state else '',
    }


def _can_start_next_round(quiz, session, question):
    if quiz.status != 'active' or not question or session.phase != WerWeissMehrSession.PHASE_REVIEW:
        return False

    responses = {
        response.participant_id: response
        for response in WerWeissMehrRoundResponse.objects.filter(
            quiz=quiz,
            question=question,
            round_number=session.current_round,
        )
    }
    active_states = WerWeissMehrParticipantState.objects.select_related('participant').filter(
        quiz=quiz,
        question=question,
        is_eliminated=False,
        participant__is_active=True,
    )

    survivor_count = 0
    newly_revealed_ids = set()
    for state in active_states:
        response = responses.get(state.participant_id)
        if response and response.is_correct:
            survivor_count += 1
            if response.matched_answer_id:
                newly_revealed_ids.add(response.matched_answer_id)

    if survivor_count <= 0:
        return False

    revealed_ids = set(session.revealed_answers.filter(question=question).values_list('id', flat=True))
    revealed_ids.update(newly_revealed_ids)
    return question.answers.exclude(id__in=revealed_ids).exists()


def _serialize_response(response):
    auto_status = _normalize_response_status(response.auto_status)
    final_status = (
        WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED
        if response.is_manual_override
        else _normalize_response_status(response.final_status)
    )
    return {
        'id': response.id,
        'participant_id': response.participant_id,
        'participant_name': response.participant.name,
        'round_number': response.round_number,
        'submitted_at': response.submitted_at.isoformat() if response.submitted_at else None,
        'answer_text': response.answer_text,
        'has_pending_input': False,
        'auto_status': auto_status,
        'final_status': final_status,
        'is_correct': response.is_correct,
        'is_manual_override': response.is_manual_override,
        'matched_answer_id': response.matched_answer_id,
        'matched_answer_text': response.matched_answer.canonical_text if response.matched_answer else '',
    }


def _normalize_response_status(status):
    if status in {WerWeissMehrRoundResponse.STATUS_CORRECT, WerWeissMehrRoundResponse.STATUS_MANUAL_CORRECTED}:
        return status
    return WerWeissMehrRoundResponse.STATUS_WRONG


def _apply_correct_response_progress(response):
    if not response.is_correct:
        return None
    state = WerWeissMehrParticipantState.objects.select_for_update().filter(
        quiz=response.quiz,
        participant=response.participant,
        question=response.question,
    ).first()
    if not state:
        return None
    state.is_eliminated = False
    state.survived_rounds = max(state.survived_rounds, response.round_number)
    state.last_status = response.final_status
    state.save(update_fields=['is_eliminated', 'survived_rounds', 'last_status'])
    response.participant.recalculate_total_score()
    return state


def _serialize_own_state(quiz, participant, question, state, response, pending, session):
    if not participant:
        return None
    has_submitted = bool(response)
    can_answer = bool(
        quiz.status == 'active'
        and question
        and session.phase == WerWeissMehrSession.PHASE_ROUND_ACTIVE
        and state
        and not state.is_eliminated
        and not has_submitted
        and participant.is_active
    )
    response_payload = _serialize_response(response) if response else None
    if response_payload and session.phase == WerWeissMehrSession.PHASE_ROUND_ACTIVE:
        response_payload.update({
            'auto_status': 'submitted',
            'final_status': 'submitted',
            'is_correct': False,
            'matched_answer_id': None,
            'matched_answer_text': '',
        })

    return {
        'participant_id': participant.id,
        'name': participant.name,
        'is_eliminated': state.is_eliminated if state else False,
        'survived_rounds': state.survived_rounds if state else 0,
        'last_status': state.last_status if state else '',
        'can_answer': can_answer,
        'has_submitted': has_submitted,
        'submitted_answer': response.answer_text if response else '',
        'pending_answer': pending.answer_text if pending else '',
        'response': response_payload,
        'total_score': participant.total_score,
    }


def _serialize_scorebox(quiz, participants, hub_session_code=None):
    selected_questions = get_ordered_scored_questions(quiz, hub_session_code)
    states = WerWeissMehrParticipantState.objects.filter(
        quiz=quiz,
        participant_id__in=[participant.id for participant in participants],
        question_id__in=[question.id for question in selected_questions],
    )
    state_map = {(state.participant_id, state.question_id): state for state in states}
    rows = []
    for question in selected_questions:
        max_points = question.answers.count()
        rows.append({
            'question_id': question.id,
            'question_text': question.question_text,
            'answer_count': max_points,
            'max_points': max_points,
            'scores': [
                {
                    'participant_id': participant.id,
                    'participant_name': participant.name,
                    'points': state_map[(participant.id, question.id)].survived_rounds
                    if (participant.id, question.id) in state_map else None,
                    'max_points': max_points,
                }
                for participant in participants
            ],
        })
    return rows
