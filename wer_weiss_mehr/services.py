import math

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from games_hub.authoritative_state import (
    QuestionPhaseDecision,
    attach_snapshot_metadata,
    current_snapshot,
    finish_question_flow,
    open_answering,
    present_question,
    reset_question_flow,
)
from games_hub.models import GameRuntimeState
from games_hub.unit_tutorial_runtime import (
    finish_current_unit_tutorial,
    get_scorebox_excluded_tutorial_question_ids,
    get_unit_tutorial_state,
    is_current_unit_tutorial_question,
    is_unit_tutorial_question,
    start_unit_tutorial_if_needed,
)


WER_WEISS_MEHR_FIELD_REVEAL_STAGGER_MS = 120
WER_WEISS_MEHR_FIELD_REVEAL_ANIMATION_MS = 180

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


def prepare_set_start(quiz, question_id, hub_session_code=None):
    tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', quiz.room_code, hub_session_code)
    tutorial_pending = bool(
        tutorial_state.get('requested')
        and tutorial_state.get('tutorial_question_id')
        and not tutorial_state.get('tutorial_has_been_played')
    )
    if not tutorial_pending:
        return {'is_tutorial_round': False, 'tutorial_question_id': None}

    if str(question_id) != str(tutorial_state.get('tutorial_question_id')):
        raise ValueError('Bitte zuerst das Tutorialset starten oder ueberspringen.')

    return start_unit_tutorial_if_needed('wer_weiss_mehr', quiz.room_code, hub_session_code)


def skip_tutorial_set(quiz, hub_session_code=None):
    tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', quiz.room_code, hub_session_code)
    if not tutorial_state.get('requested') or tutorial_state.get('tutorial_has_been_played'):
        return {'is_tutorial_round': False, 'tutorial_question_id': None}

    started = start_unit_tutorial_if_needed('wer_weiss_mehr', quiz.room_code, hub_session_code)
    if not started.get('is_tutorial_round'):
        return started
    return finish_current_unit_tutorial('wer_weiss_mehr', quiz.room_code, hub_session_code)


def get_scoped_participants(quiz, hub_session_code=None, active_only=False):
    participants = quiz.participants.all()
    if hub_session_code is not None:
        participants = participants.filter(hub_session_code=hub_session_code)
    if active_only:
        participants = participants.filter(is_active=True)
    return participants.order_by('name')


def _resolve_set_question(quiz, question_id, hub_session_code=None):
    question = WerWeissMehrQuestion.objects.get(id=question_id, is_active=True)
    tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', quiz.room_code, hub_session_code)
    tutorial_pending = bool(
        tutorial_state.get('requested')
        and tutorial_state.get('tutorial_question_id')
        and not tutorial_state.get('tutorial_has_been_played')
    )
    if tutorial_pending and str(question.id) != str(tutorial_state.get('tutorial_question_id')):
        raise ValueError('Bitte zuerst das Tutorialset starten oder ueberspringen.')
    if (
        tutorial_pending
        and str(question.id) == str(tutorial_state.get('tutorial_question_id'))
        and not tutorial_state.get('current_unit_is_tutorial')
    ):
        raise ValueError('Das Tutorialset muss explizit gestartet werden.')
    is_tutorial_round = is_unit_tutorial_question('wer_weiss_mehr', quiz.room_code, hub_session_code, question.id)
    if (
        quiz.selected_questions.exists()
        and not is_tutorial_round
        and not quiz.selected_questions.filter(id=question.id).exists()
    ):
        raise ValueError('Dieses Set gehoert nicht zu diesem Spiel.')
    return question


def start_set(quiz, question_id, hub_session_code=None, time_limit_seconds=None):
    question = _resolve_set_question(quiz, question_id, hub_session_code)
    session, _ = WerWeissMehrSession.objects.get_or_create(quiz=quiz)
    participants = get_scoped_participants(quiz, hub_session_code=hub_session_code, active_only=True)
    result = session.start_set(
        question,
        participants_qs=participants,
        time_limit_seconds=time_limit_seconds,
    )
    quiz.refresh_from_db()
    return result


def wer_weiss_mehr_reveal_ready_at(question_visible_at, *, round_number, field_count):
    visible_at = question_visible_at
    if isinstance(visible_at, str):
        visible_at = parse_datetime(visible_at)
    if not visible_at:
        return None
    if timezone.is_naive(visible_at):
        visible_at = timezone.make_aware(visible_at, timezone.get_current_timezone())
    animated_fields = int(field_count or 0) if int(round_number or 0) == 1 else 0
    if animated_fields <= 0:
        return visible_at
    reveal_duration_ms = (
        ((animated_fields - 1) * WER_WEISS_MEHR_FIELD_REVEAL_STAGGER_MS)
        + WER_WEISS_MEHR_FIELD_REVEAL_ANIMATION_MS
    )
    return visible_at + timezone.timedelta(milliseconds=reveal_duration_ms)


@transaction.atomic
def present_set_round(
    quiz,
    question_id,
    *,
    hub_session_code=None,
    time_limit_seconds=None,
    action,
    at=None,
):
    quiz = WerWeissMehrGame.objects.select_for_update().get(pk=quiz.pk)
    question = _resolve_set_question(quiz, question_id, hub_session_code)
    session, _ = WerWeissMehrSession.objects.select_for_update().get_or_create(quiz=quiz)
    duration = int(time_limit_seconds or question.round_time_limit or 30)
    decision = present_question(
        game_key='wer_weiss_mehr',
        room_code=quiz.room_code,
        session_code=hub_session_code,
        action=action,
        answer_duration_seconds=duration,
        at=at,
    )
    if decision.accepted and not decision.duplicate:
        participants = get_scoped_participants(
            quiz,
            hub_session_code=hub_session_code,
            active_only=True,
        )
        session.prepare_set(
            question,
            participants_qs=participants,
            time_limit_seconds=duration,
        )
    return decision


@transaction.atomic
def present_next_round(quiz, *, hub_session_code=None, action, at=None):
    quiz = (
        WerWeissMehrGame.objects.select_for_update()
        .select_related('current_question')
        .get(pk=quiz.pk)
    )
    session = WerWeissMehrSession.objects.select_for_update().filter(quiz=quiz).first()
    if not session or not quiz.current_question:
        raise ValueError('Keine Runtime-Session gefunden.')
    if session.phase != WerWeissMehrSession.PHASE_REVIEW:
        raise ValueError('Aktuell ist keine Runde in der Review-Phase.')
    if not _can_start_next_round(quiz, session, quiz.current_question):
        raise ValueError('Keine weitere Runde moeglich. Bitte das Set beenden.')

    decision = present_question(
        game_key='wer_weiss_mehr',
        room_code=quiz.room_code,
        session_code=hub_session_code,
        action=action,
        answer_duration_seconds=session.time_limit_seconds,
        at=at,
    )
    if decision.accepted and not decision.duplicate:
        session.finalize_review(open_round=False)
    return decision


@transaction.atomic
def open_prepared_round(quiz, *, hub_session_code=None, action, at=None):
    quiz = (
        WerWeissMehrGame.objects.select_for_update()
        .select_related('current_question')
        .get(pk=quiz.pk)
    )
    session = WerWeissMehrSession.objects.select_for_update().filter(quiz=quiz).first()
    if not session or not quiz.current_question or session.current_round <= 0:
        raise ValueError('Keine vorbereitete Runde vorhanden.')

    snapshot = current_snapshot('wer_weiss_mehr', quiz.room_code, hub_session_code)
    expected_round_id = str(session.current_round)
    expected_set_id = str(quiz.current_question_id)
    if str(action.get('round_id') or '') != expected_round_id:
        return QuestionPhaseDecision(
            False,
            'stale_action',
            'Die Aktion gehoert zu einer anderen Runde.',
            state_revision=snapshot.get('state_revision'),
            snapshot=snapshot,
        )
    if str(action.get('set_id') or '') != expected_set_id:
        return QuestionPhaseDecision(
            False,
            'stale_action',
            'Die Aktion gehoert zu einem anderen Set.',
            state_revision=snapshot.get('state_revision'),
            snapshot=snapshot,
        )

    transition_at = at or timezone.now()
    reveal_ready_at = wer_weiss_mehr_reveal_ready_at(
        snapshot.get('question_visible_at'),
        round_number=session.current_round,
        field_count=quiz.current_question.answers.count(),
    )
    if not reveal_ready_at or transition_at < reveal_ready_at:
        return QuestionPhaseDecision(
            False,
            'content_reveal_incomplete',
            'Die Antwortfelder sind noch nicht vollstaendig sichtbar.',
            state_revision=snapshot.get('state_revision'),
            snapshot=snapshot,
        )

    decision = open_answering(
        game_key='wer_weiss_mehr',
        room_code=quiz.room_code,
        session_code=hub_session_code,
        action=action,
        answer_duration_seconds=session.time_limit_seconds,
        at=transition_at,
    )
    if decision.accepted and not decision.duplicate:
        started_at = parse_datetime(decision.snapshot.get('answering_started_at') or '')
        round_state = session.open_prepared_round(started_at=started_at)
        if not round_state:
            raise ValueError('Die vorbereitete Runde konnte nicht freigegeben werden.')
        expected_deadline = parse_datetime(decision.snapshot.get('answering_deadline_at') or '')
        if expected_deadline and session.round_end_time != expected_deadline:
            raise ValueError('Die Rundendeadline ist nicht konsistent.')
    return decision


def finish_round_question_flow(quiz, *, hub_session_code=None):
    snapshot = current_snapshot('wer_weiss_mehr', quiz.room_code, hub_session_code)
    if snapshot.get('question_phase') != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN:
        return snapshot
    return finish_question_flow(
        game_key='wer_weiss_mehr',
        room_code=quiz.room_code,
        session_code=hub_session_code,
        question_id=quiz.current_question_id,
    )


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


@transaction.atomic
def submit_answer(quiz, participant, answer_text):
    original_quiz = quiz
    quiz = (
        WerWeissMehrGame.objects.select_for_update()
        .select_related('current_question')
        .get(pk=quiz.pk)
    )
    participant = WerWeissMehrParticipant.objects.select_for_update().get(
        pk=participant.pk,
        quiz=quiz,
    )
    if quiz.status != 'active':
        raise ValueError('Das Spiel ist nicht aktiv.')
    session = WerWeissMehrSession.objects.select_for_update().filter(quiz=quiz).first()
    question = quiz.current_question
    if not session or not question or session.phase != WerWeissMehrSession.PHASE_ROUND_ACTIVE:
        raise ValueError('Aktuell laeuft keine Runde.')
    received_at = timezone.now()
    if session.round_end_time and received_at >= session.round_end_time:
        raise ValueError('Die Runde ist bereits beendet.')
    if not _participant_can_answer(quiz, participant, question):
        raise ValueError('Du kannst in diesem Set nicht mehr antworten.')

    round_state = WerWeissMehrRound.objects.filter(
        quiz=quiz,
        question=question,
        round_number=session.current_round,
    ).first()
    if not round_state:
        raise ValueError('Rundenstatus fehlt.')

    existing = WerWeissMehrRoundResponse.objects.filter(
        quiz=quiz,
        participant=participant,
        question=question,
        round_number=session.current_round,
    ).first()
    if existing:
        return existing
    response, created = WerWeissMehrRoundResponse.objects.get_or_create(
        quiz=quiz,
        participant=participant,
        question=question,
        round_number=session.current_round,
        defaults={
            'answer_text': answer_text or '',
            'time_taken': (
                max(0.0, (received_at - session.round_start_time).total_seconds())
                if session.round_start_time
                else 0.0
            ),
        },
    )
    if not created:
        return response
    response.evaluate(revealed_before_round_ids=round_state.revealed_answer_ids_at_start)
    WerWeissMehrPendingInput.objects.filter(
        quiz=quiz,
        participant=participant,
        question=question,
        round_number=session.current_round,
    ).delete()
    original_quiz.refresh_from_db()
    return response


@transaction.atomic
def apply_manual_correction(quiz, response_id, target_answer_id, hub_session_code=None):
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
    if hub_session_code is not None and response.participant.hub_session_code != hub_session_code:
        raise ValueError('Diese Antwort gehoert nicht zur aktuellen Hub-Session.')
    if response.question_id != quiz.current_question_id or response.round_number != session.current_round:
        raise ValueError('Diese Antwort gehoert nicht zur aktuellen Review-Runde.')
    effective_hub_session = hub_session_code or response.participant.hub_session_code
    is_tutorial_round = is_current_unit_tutorial_question(
        'wer_weiss_mehr',
        quiz.room_code,
        effective_hub_session,
        response.question_id,
    )
    target = WerWeissMehrAnswerOption.objects.filter(id=target_answer_id, question=response.question).first()
    if not target:
        raise ValueError('Zielantwort gehoert nicht zum aktuellen Set.')
    round_state = WerWeissMehrRound.objects.filter(
        quiz=quiz,
        question=response.question,
        round_number=response.round_number,
    ).first()
    if (
        round_state
        and not is_tutorial_round
        and int(target.id) in {int(item) for item in (round_state.revealed_answer_ids_at_start or [])}
    ):
        raise ValueError('Diese Zielantwort war bereits vor Beginn dieser Runde aufgedeckt.')
    response.apply_manual_correction(target)
    session.revealed_answers.add(target)
    _apply_correct_response_progress(response, update_total=not is_tutorial_round)
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


@transaction.atomic
def end_current_round_with_question_flow(quiz, *, hub_session_code=None):
    round_state = end_current_round(quiz)
    if round_state:
        quiz.refresh_from_db()
        finish_round_question_flow(quiz, hub_session_code=hub_session_code)
    return round_state


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


@transaction.atomic
def finish_set_with_question_flow(quiz, *, hub_session_code=None):
    result = finish_set(quiz)
    quiz.refresh_from_db()
    finish_round_question_flow(quiz, hub_session_code=hub_session_code)
    remaining = current_snapshot('wer_weiss_mehr', quiz.room_code, hub_session_code)
    if remaining.get('question_phase'):
        reset_question_flow(
            game_key='wer_weiss_mehr',
            room_code=quiz.room_code,
            session_code=hub_session_code,
            mode=GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE,
        )
    return result


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
    current_set_number = None
    if question:
        ordered_questions = get_ordered_scored_questions(quiz, hub_session_code)
        current_set_number = next(
            (
                index
                for index, ordered_question in enumerate(ordered_questions, start=1)
                if ordered_question.id == question.id
            ),
            None,
        )
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

    serialized_participants = [
        _serialize_participant(item, current_states.get(item.id))
        for item in participants
    ]
    scorebox = _serialize_scorebox(quiz, participants, hub_session_code=hub_session_code)
    public_question = _serialize_question(
        question,
        session,
        hub_session_code=hub_session_code,
        include_hidden_text=False,
    ) if question else None
    state = {
        'success': True,
        'server_now': timezone.now().isoformat(),
        'game_status': 'waiting' if has_incomplete_start else quiz.status,
        'phase': WerWeissMehrSession.PHASE_IDLE if has_incomplete_start else session.phase,
        'room_code': quiz.room_code,
        'title': quiz.title,
        'current_round': 0 if has_incomplete_start else session.current_round,
        'current_set_number': current_set_number,
        'current_set_id': str(question.id) if question else None,
        'timer': _idle_timer_payload(session) if has_incomplete_start else _timer_payload(session),
        'question': _serialize_question(
            question,
            session,
            hub_session_code=hub_session_code,
            include_hidden_text=not is_participant_view,
        ) if question else None,
        'available_questions': []
        if is_participant_view
        else _serialize_available_questions(quiz, session, hub_session_code),
        'participants': serialized_participants,
        'responses': [] if is_participant_view else responses,
        'target_answers': []
        if is_participant_view
        else (_serialize_target_answers(question, session) if question else []),
        'can_start_next_round': _can_start_next_round(quiz, session, question),
        'scorebox': scorebox,
        'participant_state': _serialize_own_state(quiz, participant, question, own_state, own_response, own_pending, session)
        if participant else None,
        '_revision_state': {
            'game_status': 'waiting' if has_incomplete_start else quiz.status,
            'phase': WerWeissMehrSession.PHASE_IDLE if has_incomplete_start else session.phase,
            'current_round': 0 if has_incomplete_start else session.current_round,
            'question': public_question,
            'participants': serialized_participants,
            'responses': responses,
            'scorebox': scorebox,
        },
    }
    state = attach_snapshot_metadata(
        state,
        game_key='wer_weiss_mehr',
        room_code=quiz.room_code,
        session_code=hub_session_code,
    )
    manual_flow = (
        state.get('question_flow_mode')
        == GameRuntimeState.QUESTION_FLOW_MANUAL_THREE_PHASE
    )
    visible_at = parse_datetime(state.get('question_visible_at') or '')
    now = parse_datetime(state.get('server_now') or '') or timezone.now()
    question_visible = bool(
        question
        and (
            not manual_flow
            or state.get('question_phase') != GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
            or (visible_at and now >= visible_at)
        )
    )
    animated_field_count = (
        question.answers.count()
        if (
            question
            and manual_flow
            and state.get('question_phase') == GameRuntimeState.QUESTION_PHASE_PROMPT_VISIBLE
            and session.current_round == 1
        )
        else 0
    )
    reveal_ready_at = wer_weiss_mehr_reveal_ready_at(
        visible_at,
        round_number=session.current_round,
        field_count=animated_field_count,
    )
    state.update({
        'question_visible': question_visible,
        'field_reveal_stagger_ms': WER_WEISS_MEHR_FIELD_REVEAL_STAGGER_MS,
        'field_reveal_animation_ms': WER_WEISS_MEHR_FIELD_REVEAL_ANIMATION_MS,
        'field_reveal_count': animated_field_count,
        'field_reveal_ready_at': reveal_ready_at.isoformat() if reveal_ready_at else None,
    })
    if (
        state.get('participant_state')
        and manual_flow
        and state.get('question_phase') != GameRuntimeState.QUESTION_PHASE_ANSWERING_OPEN
    ):
        state['participant_state']['can_answer'] = False
    return state


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


def _serialize_available_questions(quiz, session, hub_session_code=None):
    tutorial_state = get_unit_tutorial_state('wer_weiss_mehr', quiz.room_code, hub_session_code)
    tutorial_requested = bool(tutorial_state.get('requested') and tutorial_state.get('tutorial_question_id'))
    tutorial_completed = bool(tutorial_state.get('tutorial_has_been_played'))
    tutorial_pending = bool(tutorial_requested and not tutorial_completed)
    questions = []

    if tutorial_requested:
        tutorial_question = (
            WerWeissMehrQuestion.objects
            .filter(id=tutorial_state.get('tutorial_question_id'), is_active=True)
            .prefetch_related('answers')
            .first()
        )
        if tutorial_question:
            questions.append(_serialize_available_question(
                tutorial_question,
                session,
                is_tutorial_set=True,
                tutorial_completed=tutorial_completed,
            ))

    questions.extend(
        _serialize_available_question(
            question,
            session,
            regular_blocked_by_tutorial=tutorial_pending,
        )
        for question in get_ordered_scored_questions(quiz, hub_session_code)
    )
    return questions


def _serialize_available_question(
    question,
    session=None,
    *,
    is_tutorial_set=False,
    tutorial_completed=False,
    regular_blocked_by_tutorial=False,
):
    is_completed = bool(session and question.id in set(session.completed_question_ids or []))
    if is_tutorial_set and tutorial_completed:
        is_completed = True
    status = 'completed' if is_completed else 'available'
    if is_tutorial_set:
        status = 'tutorial_completed' if is_completed else 'tutorial_pending'
    elif regular_blocked_by_tutorial and not is_completed:
        status = 'locked_until_tutorial'
    return {
        'id': question.id,
        'question_text': question.question_text,
        'round_time_limit': question.round_time_limit,
        'answer_count': question.answers.count(),
        'status': status,
        'is_completed': is_completed,
        'is_tutorial_set': is_tutorial_set,
        'is_start_disabled': bool((regular_blocked_by_tutorial and not is_completed) or is_completed),
        'disabled_reason': 'Tutorialset zuerst abschliessen oder ueberspringen.'
        if regular_blocked_by_tutorial and not is_completed else '',
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
    hidden_presentation_index = 0
    serialized_tiles = []
    for index, answer in enumerate(answers, start=1):
        revealed = answer.id in revealed_ids or reveal_all
        presentation_index = None
        if session.current_round == 1 and not revealed:
            presentation_index = hidden_presentation_index
            hidden_presentation_index += 1
        serialized_tiles.append({
            'id': answer.id,
            'position': index,
            'revealed': revealed,
            'text': answer.canonical_text if include_hidden_text or revealed else '',
            'presentation_index': presentation_index,
        })
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
        'tiles': serialized_tiles,
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
        'time_taken': response.time_taken,
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


def _apply_correct_response_progress(response, update_total=True):
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
    if update_total:
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
    for index, question in enumerate(selected_questions, start=1):
        max_points = question.answers.count()
        rows.append({
            'question_id': question.id,
            'round_number': index,
            'label': f'#{index}',
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
