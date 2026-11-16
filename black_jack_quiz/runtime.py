"""Persist Black Jack set runs; serialize mutations and snapshots on the quiz row."""
from functools import wraps
import math
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from games_hub.authoritative_state import (
    _retry_sqlite_locked, attach_snapshot_metadata, get_runtime_state, finish_question_flow,
)
from games_hub.models import GameRuntimeState
from .models import BlackJackQuiz, BlackJackSession, BlackJackParticipant, BlackJackAnswer, BlackJackQuestion


def write_transaction(fn):
    atomic = transaction.atomic(fn)
    @wraps(fn)
    def run(*args, **kwargs):
        return _retry_sqlite_locked(lambda: atomic(*args, **kwargs))
    return run


def pool_filter(code, prefix=''):
    field = prefix + 'hub_session_code'
    return Q(**{field: code}) if code else (Q(**{field: None}) | Q(**{field: ''}))


def pool_code(quiz, supplied=None):
    from games_hub.models import HubGameStep
    session = getattr(quiz, 'session', None)
    bound = bool(quiz.active_hub_session_code or (session and session.set_rounds))
    code = quiz.active_hub_session_code
    if not bound:
        steps = HubGameStep.objects.filter(game_key='blackjack', room_code=quiz.room_code, session__started_at__isnull=False)
        if quiz.started_at:
            steps = steps.filter(session__started_at__lte=quiz.started_at)
        else:
            steps = steps.filter(session__is_active=True, session__ended_at__isnull=True)
        step = steps.select_related('session').order_by('-session__started_at', '-id').first()
        if step:
            code = step.session.code
            bound = bool(quiz.started_at)
        elif quiz.started_at or quiz.status == 'active':
            codes = list(quiz.participants.order_by().values_list('hub_session_code', flat=True).distinct())
            if len(codes) == 1:
                code, bound = codes[0], True
        if supplied is not None and not bound:
            code = supplied
    if supplied is not None and bound and (supplied or '') != (code or ''):
        raise ValueError('Spiel und Session stimmen nicht ueberein.')
    return code or ''


def participants(quiz, code=None, active_only=False):
    qs = quiz.participants.filter(pool_filter(pool_code(quiz, code)))
    return qs.filter(is_active=True) if active_only else qs


def question_snapshot(q):
    return {'id': q.pk, 'question_text': q.question_text, 'correct_answer': q.correct_answer,
            'explanation': q.explanation or '', 'time_limit': q.time_limit}


def current_run(session):
    return next((r for r in session.set_rounds if r['number'] == session.current_set_round_number), None)


def configured_sets(quiz):
    return quiz.get_explicit_question_sets(active_only=False)


def answer_rows(quiz, entry, participant=None):
    qs = quiz.blackjack_answers.filter(set_round_number=0 if entry.get('legacy') else entry['number'],
        question_id__in=entry['question_ids']).filter(pool_filter(entry.get('hub_session_code'), 'participant__'))
    if entry.get('legacy') and entry.get('started_at'):
        qs = qs.filter(submitted_at__gte=entry['started_at'])
    return qs.filter(participant=participant) if participant else qs


def stars(state):
    return max(0, int(state.get('raw_stars', 0)) + int(state.get('adjustment', 0)))


def award(quiz, entry):
    states = entry['states']
    if not quiz.uses_ranking_mode():
        return {key: 0 if st.get('busted') else stars(st) for key, st in states.items()}
    live = [(key, st) for key, st in states.items() if not st.get('busted')]
    live.sort(key=lambda item: (abs(21 - stars(item[1])), int(item[0])))
    result, last, rank = {key: 0 for key in states}, None, 0
    for position, (key, st) in enumerate(live, 1):
        distance = abs(21 - stars(st))
        if last is None or last != distance:
            rank, last = position, distance
        result[key] = max(len(states) - rank + 1, 0)
    return result


def ensure_history(quiz, session, code=None):
    """Import legacy records lazily, changing only newly added columns."""
    code = pool_code(quiz, code)
    if not quiz.started_at and quiz.status == 'waiting':
        return
    if any((r.get('hub_session_code') or '') == code for r in session.set_rounds):
        return
    answers = quiz.blackjack_answers.filter(pool_filter(code, 'participant__'))
    if quiz.started_at:
        answers = answers.filter(submitted_at__gte=quiz.started_at)
    if session.is_waiting_fresh_start_state():
        return
    if not (answers.exists() or session.asked_question_ids or session.finalized_set_numbers or quiz.current_question_id):
        return
    sets, known = configured_sets(quiz), list(session.finalized_set_numbers or [])
    asked = list(session.asked_question_ids or []) + list(answers.order_by('submitted_at', 'id').values_list('question_id', flat=True))
    if quiz.current_question_id:
        asked.append(quiz.current_question_id)
    for pk in dict.fromkeys(asked):
        n = quiz.get_set_number_for_question_id(pk, active_only=False)
        if n not in known:
            known.append(n)
    roster, entries = list(participants(quiz, code)), list(session.set_rounds)
    for n in known:
        ids = list(sets[n - 1]) if sets and 0 < n <= len(sets) else list(dict.fromkeys(asked))
        entry = {'number': max([r['number'] for r in entries], default=0) + 1,
            'configured_number': n, 'hub_session_code': code, 'question_ids': ids,
            'questions': [question_snapshot(q) for q in BlackJackQuestion.objects.filter(pk__in=ids)],
            'asked_ids': [pk for pk in dict.fromkeys(asked) if pk in ids],
            'status': 'completed' if n in (session.finalized_set_numbers or []) else 'open',
            'is_tutorial': n == quiz.tutorial_set_number, 'legacy': True, 'states': {},
            'expected_count': len(ids) if sets else quiz.get_questions_per_set(),
            'started_at': quiz.started_at.isoformat() if quiz.started_at else None}
        for p in roster:
            rows = list(answers.filter(participant=p, question_id__in=ids))
            raw, adjustment, busted = sum(row.points_earned for row in rows), 0, False
            busted = raw > 21
            if quiz.current_question_id in ids and entry['status'] == 'open':
                busted = p.is_busted or busted
                if not busted:
                    adjustment = p.total_points - raw
            entry['states'][str(p.pk)] = {'raw_stars': raw, 'adjustment': adjustment,
                'busted': busted, 'answered': len(rows), 'visible_stars': 0 if busted else max(0, raw + adjustment)}
            if entry['status'] == 'open' and session.is_question_active and quiz.current_question_id in ids:
                hidden_points = sum(row.points_earned for row in rows if row.question_id == quiz.current_question_id)
                previous = max(0, raw - hidden_points + adjustment)
                previous_bust = previous > 21 or (p.is_busted and not any(row.question_id == quiz.current_question_id for row in rows))
                entry['states'][str(p.pk)].update(visible_stars=0 if previous_bust else previous,
                    visible_busted=previous_bust)
        if entry['status'] == 'completed':
            for key, points in award(quiz, entry).items():
                entry['states'][key]['awarded'] = points
        entries.append(entry)
        if quiz.current_question_id in ids:
            session.current_set_round_number = entry['number']
    completed = [r for r in entries if (r.get('hub_session_code') or '') == code and r['status'] == 'completed' and not r['is_tutorial']]
    if completed:
        for p in roster:
            key = str(p.pk)
            total = sum(r['states'].get(key, {}).get('awarded', 0) for r in completed)
            completed[-1]['states'][key]['awarded'] += p.overall_points - total
    elif entries:
        entries[-1]['prior_overall'] = {str(p.pk): p.overall_points for p in roster}
    if not session.current_set_round_number and entries:
        session.current_set_round_number = entries[-1]['number']
    quiz.active_hub_session_code, session.set_rounds = code, entries
    quiz.save(update_fields=['active_hub_session_code'])
    session.save(update_fields=['set_rounds', 'current_set_round_number'])


def new_run(quiz, session, number, code, tutorial=False):
    sets = configured_sets(quiz)
    ids = list(sets[number - 1]) if sets and 0 < number <= len(sets) else []
    available = BlackJackQuestion.objects.in_bulk(ids)
    ids = [pk for pk in ids if pk in available and available[pk].is_active]
    entry = {'number': max([r['number'] for r in session.set_rounds], default=0) + 1,
        'configured_number': number, 'hub_session_code': code, 'question_ids': ids,
        'questions': [question_snapshot(available[pk]) for pk in ids], 'asked_ids': [],
        'status': 'open', 'is_tutorial': tutorial, 'states': {},
        'expected_count': len(ids) if sets else quiz.get_questions_per_set()}
    for p in participants(quiz, code, active_only=True):
        entry['states'][str(p.pk)] = {'raw_stars': 0, 'adjustment': 0, 'busted': False, 'answered': 0, 'visible_stars': 0}
    session.set_rounds = [*session.set_rounds, entry]
    return entry


def mirror(quiz, entry):
    for p in participants(quiz, entry.get('hub_session_code')):
        st = entry['states'].get(str(p.pk))
        if not st:
            p.total_points, p.is_busted, p.questions_answered, p.final_score = 0, False, 0, 0
        else:
            value = stars(st)
            p.is_busted = bool(st.get('busted'))
            p.total_points = 0 if p.is_busted else value
            p.questions_answered = 0 if entry['is_tutorial'] else st.get('answered', 0)
            p.final_score = max(value, 22) if p.is_busted else abs(21 - value)
        p.save(update_fields=['total_points', 'is_busted', 'questions_answered', 'final_score'])


def sync_overall(quiz, session, code):
    scoped = [r for r in session.set_rounds if (r.get('hub_session_code') or '') == code]
    entries = [r for r in scoped if r['status'] == 'completed' and not r['is_tutorial']]
    for p in participants(quiz, code):
        p.overall_points = sum(r['states'].get(str(p.pk), {}).get('awarded', 0) for r in entries) + sum(
            r.get('prior_overall', {}).get(str(p.pk), 0) for r in scoped)
        p.save(update_fields=['overall_points'])


def strict_integer(value):
    if isinstance(value, bool) or value is None or (isinstance(value, float) and (not math.isfinite(value) or not value.is_integer())):
        raise ValueError('Bitte eine ganze Zahl eingeben.')
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Bitte eine ganze Zahl eingeben.')
    if not -(2 ** 31) <= number <= 2 ** 31 - 1:
        raise ValueError('Die eingegebene Zahl ist zu gross.')
    return number


def context_matches(quiz, session, data):
    return (str(data.get('game_id')) == str(quiz.pk)
        and str(data.get('question_id')) == str(quiz.current_question_id)
        and str(data.get('set_id')) == str(session.current_set_round_number)
        and str(data.get('round_id')) == str(session.current_question_number))


def check_host_context(quiz, session, data, code):
    if data is None:
        return
    if not quiz.current_question_id or not context_matches(quiz, session, data):
        raise ValueError('Die Aktion gehoert nicht zur aktuellen Frage oder zum aktuellen Set.')
    runtime = get_runtime_state('blackjack', quiz.room_code, code)
    try:
        revision = int(data.get('state_revision'))
    except (TypeError, ValueError):
        raise ValueError('Aktionskontext fehlt.')
    if not runtime.context_revision <= revision <= runtime.state_revision:
        raise ValueError('Der Spielzustand hat sich geaendert.')


@write_transaction
def prepare_question(quiz, question, *, tutorial=False):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    code = pool_code(quiz)
    ensure_history(quiz, session, code)
    number = quiz.get_set_number_for_question_id(question.pk, active_only=False)
    entry = next((r for r in reversed(session.set_rounds) if r['configured_number'] == number
                  and r['status'] == 'open' and (r.get('hub_session_code') or '') == code), None)
    if not entry:
        entry = new_run(quiz, session, number, code, tutorial)
    if question.pk in entry['asked_ids']:
        raise ValueError('Diese Frage wurde in diesem Set bereits gespielt.')
    if question.pk not in entry['question_ids']:
        if configured_sets(quiz):
            raise ValueError('Diese Frage gehoert nicht zu diesem Set.')
        entry['question_ids'].append(question.pk)
        entry['questions'].append(question_snapshot(question))
    entry['asked_ids'].append(question.pk)
    session.current_set_round_number, session.selected_set_number = entry['number'], number
    session.last_reveal, session.pending_inputs = {}, {}
    session.is_question_active, session.question_end_time = False, None
    session.total_responses_current_question, session.average_points_current_question = 0, 0
    if not tutorial:
        session.current_question_number += 1
        session.total_questions_sent += 1
        session.asked_question_ids = list(dict.fromkeys([*session.asked_question_ids, question.pk]))
        quiz.current_question_number = session.current_question_number
    quiz.current_question, quiz.question_start_time = question, None
    quiz.active_hub_session_code, quiz.status = code, 'active'
    quiz.save()
    session.save()
    mirror(quiz, entry)
    return entry


@write_transaction
def calculate_participant(participant):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=participant.quiz_id)
    session = BlackJackSession.objects.select_for_update().filter(quiz=quiz).first()
    if not session or not session.set_rounds:
        return None
    entry = current_run(session)
    if not entry or (participant.hub_session_code or '') != (entry.get('hub_session_code') or ''):
        return None
    st = entry['states'].get(str(participant.pk))
    if st is None:
        return None
    if entry['status'] != 'open':
        return participant.total_points
    answers = answer_rows(quiz, entry, participant)
    st['raw_stars'], st['answered'] = sum(answers.values_list('points_earned', flat=True)), answers.count()
    st['busted'] = bool(st.get('no_answer') or stars(st) > 21)
    session.save(update_fields=['set_rounds'])
    mirror(quiz, entry)
    participant.refresh_from_db()
    return participant.total_points


@write_transaction
def store_pending(quiz, participant, text, data):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    entry, now = current_run(session), timezone.now()
    if not (entry and quiz.status == 'active' and session.is_question_active and participant.is_active
            and (participant.hub_session_code or '') == quiz.active_hub_session_code
            and str(participant.pk) in entry['states'] and not entry['states'][str(participant.pk)].get('busted')
            and quiz.question_start_time and quiz.question_start_time <= now
            and session.question_end_time and now < session.question_end_time
            and context_matches(quiz, session, data)):
        return False
    if answer_rows(quiz, entry, participant).filter(question_id=quiz.current_question_id).exists():
        return False
    if str(text).strip():
        strict_integer(text)
    session.pending_inputs = {**session.pending_inputs, str(participant.pk): str(text).strip()}
    session.save(update_fields=['pending_inputs'])
    return True


@write_transaction
def submit(quiz, participant, value, *, data=None, from_pending=False):
    quiz = BlackJackQuiz.objects.select_for_update().select_related('current_question').get(pk=quiz.pk)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    ensure_history(quiz, session)
    entry = current_run(session)
    if not participant or (participant.hub_session_code or '') != quiz.active_hub_session_code:
        raise ValueError('Teilnahme nicht bestaetigt.')
    participant = BlackJackParticipant.objects.select_for_update().get(pk=participant.pk, quiz=quiz)
    st = entry['states'].get(str(participant.pk)) if entry else None
    if not st:
        raise ValueError('Du spielst ab dem naechsten Set mit.')
    if not (quiz.status == 'active' and quiz.current_question_id and session.is_question_active) or st.get('busted') or (not participant.is_active and not from_pending):
        raise ValueError('Du kannst in dieser Frage nicht antworten.')
    if data is not None and not context_matches(quiz, session, data):
        raise ValueError('Die Antwort gehoert nicht zur aktuellen Frage.')
    now = timezone.now()
    if not from_pending and (not quiz.question_start_time or now < quiz.question_start_time
            or not session.question_end_time or now >= session.question_end_time):
        raise ValueError('Die Antwortzeit ist abgelaufen.')
    value = strict_integer(value)
    snapshot = next(q for q in entry['questions'] if q['id'] == quiz.current_question_id)
    if abs(value - snapshot['correct_answer']) > 2 ** 31 - 1:
        raise ValueError('Die Abweichung ist zu gross.')
    answer = answer_rows(quiz, entry, participant).filter(question=quiz.current_question).first()
    if not answer:
        answer = BlackJackAnswer.objects.create(quiz=quiz, participant=participant,
            question=quiz.current_question, set_round_number=entry['number'], user_answer=value,
            time_taken=max(0, (min(now, session.question_end_time) - quiz.question_start_time).total_seconds()))
    session.refresh_from_db()  # Answer.save recalculated the persisted participant state.
    session.pending_inputs = {key: text for key, text in session.pending_inputs.items() if key != str(participant.pk)}
    session.save(update_fields=['pending_inputs'])
    return answer


def final_scores(quiz, code=None):
    return [{'name': p.name, 'total_points': p.overall_points, 'overall_points': p.overall_points,
             'total_score': p.overall_points} for p in participants(quiz, code).order_by('-overall_points', 'name')]


@write_transaction
def end_question(quiz, *, code=None, data=None, complete_game=False):
    quiz = BlackJackQuiz.objects.select_for_update().select_related('current_question').get(pk=quiz.pk)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    code = pool_code(quiz, code)
    ensure_history(quiz, session, code)
    if not quiz.current_question_id or not session.is_question_active or quiz.status != 'active':
        raise ValueError('Es ist keine freigegebene Frage aktiv.')
    check_host_context(quiz, session, data, code)
    entry = current_run(session)
    question = next(q for q in entry['questions'] if q['id'] == quiz.current_question_id)
    if not complete_game:
        for p in participants(quiz, code):
            key = str(p.pk)
            if key not in entry['states']:
                continue
            pending = session.pending_inputs.get(key, '')
            if pending and not answer_rows(quiz, entry, p).filter(question_id=question['id']).exists():
                submit(quiz, p, pending, from_pending=True)
        session.refresh_from_db()
        entry = current_run(session)
        answered = set(answer_rows(quiz, entry).filter(question_id=question['id']).values_list('participant_id', flat=True))
        for key, st in entry['states'].items():
            if int(key) not in answered and not st.get('busted'):
                st['busted'] = st['no_answer'] = True
            st['visible_stars'] = 0 if st.get('busted') else stars(st)
            st['visible_busted'] = bool(st.get('busted'))
    complete = len(entry['asked_ids']) >= entry.get('expected_count', len(entry['question_ids']))
    if complete_game:
        for other in session.set_rounds:
            if other['status'] == 'open' and (other.get('hub_session_code') or '') == code:
                other['status'] = 'discarded'
    elif complete:
        entry['status'] = 'completed'
        awards = {} if entry['is_tutorial'] else award(quiz, entry)
        for key, st in entry['states'].items():
            st['awarded'] = awards.get(key, 0)
        if not entry['is_tutorial']:
            session.finalized_set_numbers = list(dict.fromkeys([*session.finalized_set_numbers, entry['configured_number']]))
            session.completed_sets_count = sum(r['status'] == 'completed' and not r['is_tutorial'] and (r.get('hub_session_code') or '') == code for r in session.set_rounds)
        else:
            from games_hub.unit_tutorial_runtime import finish_current_unit_tutorial
            finish_current_unit_tutorial('blackjack', quiz.room_code, code)
            session.selected_set_number = next((n for n, ids in enumerate(configured_sets(quiz), 1)
                if n != quiz.tutorial_set_number and BlackJackQuestion.objects.filter(pk__in=ids, is_active=True).exists()), 1)
    sync_overall(quiz, session, code)
    result_rows = []
    for p in participants(quiz, code):
        st = entry['states'].get(str(p.pk), {})
        result_rows.append({'id': p.pk, 'name': p.name, 'set_stars': 0 if st.get('busted') else stars(st),
            'awarded_points': st.get('awarded', 0), 'overall_points': p.overall_points,
            'current_points': 0 if complete or complete_game else st.get('visible_stars', 0)})
    tutorial = entry['is_tutorial']
    regular = [i + 1 for i, ids in enumerate(configured_sets(quiz)) if ids and i + 1 != quiz.tutorial_set_number
        and BlackJackQuestion.objects.filter(pk__in=ids, is_active=True).exists()]
    completed = {r['configured_number'] for r in session.set_rounds if r['status'] == 'completed' and not r['is_tutorial'] and (r.get('hub_session_code') or '') == code}
    open_sets = any(r['status'] == 'open' and not r['is_tutorial'] and (r.get('hub_session_code') or '') == code for r in session.set_rounds)
    quiz_complete = complete_game or (bool(regular) and all(n in completed for n in regular) and complete and not tutorial and not open_sets)
    if not configured_sets(quiz):
        quiz_complete = complete_game or complete
    reveal = {'question_id': question['id'], 'correct_answer': question['correct_answer'],
        'explanation': question['explanation'], 'question_text': question['question_text'], 'is_tutorial_round': tutorial}
    event = {'type': 'question_ended', 'message': 'Frage aufgeloest.', 'correct_answer': reveal,
        'quiz_complete': quiz_complete, 'set_complete': complete and not tutorial and not complete_game,
        'set_number': entry['number'], 'set_results': result_rows if complete and not complete_game else [],
        'no_answer_bust_participants': [{'id': p.pk, 'name': p.name, 'hub_session_code': code, 'reason': 'no_answer',
            'total_points': 0, 'overall_points': p.overall_points, 'is_busted': True} for p in participants(quiz, code)
            if entry['states'].get(str(p.pk), {}).get('no_answer')],
        'is_tutorial_round': tutorial, 'hub_session_code': code}
    session.last_reveal = {**event, 'set_id': entry['number'], 'acknowledged': []}
    session.pending_inputs, session.is_question_active, session.question_end_time = {}, False, None
    quiz.current_question, quiz.question_start_time = None, None
    if quiz_complete:
        quiz.status, quiz.ended_at = 'completed', timezone.now()
    quiz.save()
    session.save()
    mirror(quiz, entry)
    if complete or complete_game:
        for p in participants(quiz, code):
            p.total_points = p.overall_points if quiz_complete else 0
            p.is_busted, p.questions_answered, p.final_score = False, 0, 0
            p.save(update_fields=['total_points', 'is_busted', 'questions_answered', 'final_score'])
    phase_runtime = get_runtime_state('blackjack', quiz.room_code, code)
    if phase_runtime.question_phase in {'prompt_visible', 'content_visible'}:
        from games_hub.authoritative_state import reset_question_flow
        reset_question_flow(game_key='blackjack', room_code=quiz.room_code, session_code=code, mode=phase_runtime.question_flow_mode)
        phase_runtime.refresh_from_db()
        phase_runtime.public_snapshot['completed_question_id'] = str(question['id'])
        phase_runtime.save(update_fields=['public_snapshot'])
    else:
        finish_question_flow(game_key='blackjack', room_code=quiz.room_code, session_code=code, question_id=question['id'])
    event['final_scores'] = final_scores(quiz, code) if quiz_complete else []
    session.last_reveal['final_scores'] = event['final_scores']
    session.save(update_fields=['last_reveal'])
    return event


@write_transaction
def end_game(quiz, *, code=None, data=None):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    code = pool_code(quiz, code)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    if quiz.status == 'completed':
        return session.last_reveal or {'type': 'quiz_ended', 'quiz_complete': True, 'final_scores': final_scores(quiz, code)}
    if quiz.current_question_id:
        check_host_context(quiz, session, data, code)
        if quiz.status == 'inactive':
            set_paused(quiz, False)
        quiz.status = 'active'
        quiz.save(update_fields=['status'])
        session.is_question_active = True
        session.save(update_fields=['is_question_active'])
        return end_question(quiz, code=code, complete_game=True)
    if quiz.status == 'inactive':
        set_paused(quiz, False)
        session.refresh_from_db()
    quiz.status, quiz.ended_at = 'completed', timezone.now()
    quiz.save(update_fields=['status', 'ended_at'])
    for entry in session.set_rounds:
        if entry['status'] == 'open' and (entry.get('hub_session_code') or '') == code:
            entry['status'] = 'discarded'
    if session.last_reveal:
        session.last_reveal.update(quiz_complete=True, final_scores=final_scores(quiz, code))
    session.is_question_active, session.question_end_time, session.pending_inputs = False, None, {}
    session.save()
    participants(quiz, code).update(total_points=0, is_busted=False, questions_answered=0, final_score=0)
    return {'type': 'quiz_ended', 'quiz_complete': True, 'final_scores': final_scores(quiz, code)}


@write_transaction
def set_paused(quiz, paused, *, at=None):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    if quiz.status != ('active' if paused else 'inactive'):
        return False
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    ensure_history(quiz, session)
    now = at or timezone.now()
    runtime = get_runtime_state('blackjack', quiz.room_code, quiz.active_hub_session_code)
    runtime = GameRuntimeState.objects.select_for_update().get(pk=runtime.pk)
    snap = dict(runtime.public_snapshot or {})
    if paused:
        session.paused_at, snap['_paused_at'] = now, now.isoformat()
    else:
        delta = max(now - (session.paused_at or now), timezone.timedelta(0))
        session.paused_at = None
        snap.pop('_paused_at', None)
        for field in ['question_presented_at', 'content_revealed_at', 'starts_at', 'ends_at']:
            value = getattr(runtime, field)
            if value:
                setattr(runtime, field, value + delta)
        if quiz.question_start_time:
            quiz.question_start_time += delta
        if session.question_end_time:
            session.question_end_time += delta
    runtime.public_snapshot = snap
    runtime.state_revision += 1
    runtime.context_revision = runtime.state_revision
    runtime.save()
    quiz.status = 'inactive' if paused else 'active'
    quiz.save(update_fields=['status', 'question_start_time'])
    session.save(update_fields=['paused_at', 'question_end_time'])
    return True


@write_transaction
def start_game(quiz, code=None):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    session, _ = BlackJackSession.objects.select_for_update().get_or_create(quiz=quiz)
    new_pool = code is not None and (code or '') != (pool_code(quiz) or '')
    if new_pool:
        from games_hub.models import HubGameStep, HubSession
        if not HubGameStep.objects.filter(game_key='blackjack', room_code=quiz.room_code,
                session__code=code, session__started_at__isnull=False, session__is_active=True,
                session__ended_at__isnull=True).exists():
            raise ValueError('Die neue Session ist noch nicht gestartet.')
        old_hub = HubSession.objects.filter(code=pool_code(quiz)).first()
        if quiz.started_at and quiz.status != 'completed' and old_hub and not old_hub.ended_at:
            raise ValueError('Das Spiel gehoert noch zu einer laufenden Session.')
        ensure_history(quiz, session)
    if not new_pool and quiz.started_at and quiz.status in {'active', 'inactive', 'completed'}:
        pool_code(quiz, code)
        if quiz.status == 'inactive':
            set_paused(quiz, False)
        elif quiz.status == 'completed':
            quiz.status, quiz.ended_at = 'active', None
            quiz.save(update_fields=['status', 'ended_at'])
        return False
    code = (code or '') if new_pool else pool_code(quiz, code)
    quiz.active_hub_session_code = code
    quiz.status, quiz.started_at, quiz.ended_at = 'active', timezone.now(), None
    quiz.current_question, quiz.question_start_time, quiz.current_question_number = None, None, 0
    quiz.save()
    session.reset_for_new_run()
    session.set_rounds = [r for r in session.set_rounds if (r.get('hub_session_code') or '') != code]
    session.current_set_round_number, session.pending_inputs, session.last_reveal, session.paused_at = 0, {}, {}, None
    session.save()
    participants(quiz, code).update(total_points=0, overall_points=0, questions_answered=0, is_busted=False, final_score=0)
    quiz.blackjack_answers.filter(pool_filter(code, 'participant__')).delete()
    return True


@write_transaction
def set_score(participant, value):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=participant.quiz_id)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    ensure_history(quiz, session)
    entry = current_run(session)
    st = entry['states'].get(str(participant.pk)) if entry else None
    if not st or entry['status'] != 'open' or quiz.status not in {'active', 'inactive'}:
        raise ValueError('Es ist kein offenes Set fuer diesen Teilnehmer vorhanden.')
    value = strict_integer(value)
    if value < 0:
        raise ValueError('Sterne duerfen nicht negativ sein.')
    st['adjustment'] = value - st.get('raw_stars', 0)
    st['no_answer'], st['busted'] = False, value > 21
    st['visible_stars'] = 0 if st['busted'] else value
    st['visible_busted'] = st['busted']
    session.save(update_fields=['set_rounds'])
    mirror(quiz, entry)
    return value


@write_transaction
def select_set(quiz, number, code=None):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    pool_code(quiz, code)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    if quiz.current_question_id:
        raise ValueError('Bitte zuerst die aktuelle Frage beenden.')
    number = strict_integer(number)
    if not 1 <= number <= max(1, len(configured_sets(quiz))):
        raise ValueError('Set wurde nicht gefunden.')
    if quiz.status == 'waiting' and not quiz.started_at:
        session.reset_for_new_run()
    session.selected_set_number = number
    session.save(update_fields=['selected_set_number'])
    if quiz.status == 'completed':
        quiz.status, quiz.ended_at = 'active', None
        quiz.save(update_fields=['status', 'ended_at'])
    return number


def progress(quiz, session, code):
    entries = [r for r in session.set_rounds if (r.get('hub_session_code') or '') == code and not r['is_tutorial'] and r['status'] != 'discarded']
    known = {r['configured_number'] for r in entries}
    future = [n for n, ids in enumerate(configured_sets(quiz), 1) if ids and n != quiz.tutorial_set_number
        and n not in known and BlackJackQuestion.objects.filter(pk__in=ids, is_active=True).exists()]
    current = next((i for i, r in enumerate(entries, 1) if r['number'] == session.current_set_round_number), None)
    return {'current': current, 'total': len(entries) + len(future), 'entries': entries, 'future': future}


def scoreboard(quiz, session, participant, code):
    data, rows = progress(quiz, session, code), []
    for n, entry in enumerate(data['entries'], 1):
        st = entry['states'].get(str(participant.pk), {})
        rows.append({'set_number': entry['number'], 'display_number': n,
            'earned_points': st.get('awarded', 0) if entry['status'] == 'completed' else None,
            'max_points': len(entry['states']) if quiz.uses_ranking_mode() else 21,
            'status': 'played' if entry['status'] == 'completed' else 'current' if entry['number'] == session.current_set_round_number else 'upcoming'})
    for config in data['future']:
        rows.append({'set_number': 'future-' + str(config), 'display_number': len(rows) + 1,
            'earned_points': None, 'max_points': participants(quiz, code).count() if quiz.uses_ranking_mode() else 21, 'status': 'upcoming'})
    return rows


@write_transaction
def acknowledge(quiz, participant, data):
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    reveal = dict(session.last_reveal)
    if (str(data.get('question_id')) != str(reveal.get('correct_answer', {}).get('question_id'))
            or str(data.get('set_id')) != str(reveal.get('set_id'))
            or (participant.hub_session_code or '') != quiz.active_hub_session_code):
        raise ValueError('Die Aufloesung ist nicht mehr aktuell.')
    reveal['acknowledged'] = list(dict.fromkeys([*reveal.get('acknowledged', []), participant.pk]))
    session.last_reveal = reveal
    session.save(update_fields=['last_reveal'])


@write_transaction
def build_state(quiz, code=None, participant=None, host=False):
    quiz = BlackJackQuiz.objects.select_for_update().select_related('current_question').get(pk=quiz.pk)
    code = pool_code(quiz, code)
    session, _ = BlackJackSession.objects.select_for_update().get_or_create(quiz=quiz)
    ensure_history(quiz, session, code)
    entry, prog = current_run(session), progress(quiz, session, code)
    question = next((dict(q) for q in entry['questions'] if q['id'] == quiz.current_question_id), None) if entry and quiz.current_question_id else None
    public_question = {k: v for k, v in question.items() if k not in {'correct_answer', 'explanation'}} if question else None
    reveal = session.last_reveal if not quiz.current_question_id else {}
    phase = 'question_active' if session.is_question_active else 'question_prepared' if question else 'question_result' if reveal else 'waiting'
    if quiz.status == 'completed':
        phase = 'completed'
    payload = {'success': True, 'game_status': quiz.status, 'phase': phase, 'question': public_question,
        'current_round_id': str(session.current_question_number), 'current_set_id': str(session.current_set_round_number) if entry else None,
        'timer': {'started_at': quiz.question_start_time.isoformat() if quiz.question_start_time else None,
                  'ends_at': session.question_end_time.isoformat() if session.question_end_time else None},
        '_revision_state': {'phase': phase, 'question_id': quiz.current_question_id, 'set_id': session.current_set_round_number,
            'answered_count': answer_rows(quiz, entry).filter(question_id=quiz.current_question_id).count() if entry and quiz.current_question_id else 0}}
    payload = attach_snapshot_metadata(payload, game_key='blackjack', room_code=quiz.room_code, session_code=code)
    payload.update({'current_question_id': str(quiz.current_question_id) if quiz.current_question_id else None,
        'current_round_id': str(session.current_question_number), 'current_set_id': str(session.current_set_round_number) if entry else None,
        'set_number': prog['current'], 'total_sets': prog['total'],
        'configured_set_number': entry['configured_number'] if entry else session.selected_set_number,
        'question_in_set': len(entry['asked_ids']) if entry else 0,
        'set_question_count': entry.get('expected_count', len(entry['question_ids'])) if entry else 0,
        'hub_session_code': code, 'reveal': reveal, 'is_tutorial_round': bool(entry and entry['is_tutorial'])})
    if question:
        lifecycle = {key: payload.get(key) for key in ['game_id', 'state_revision', 'server_now', 'question_phase',
            'question_presented_at', 'question_visible_at', 'question_presentation_duration_ms', 'question_reveal_ms_per_character',
            'answering_started_at', 'answering_deadline_at', 'answering_allowed', 'timer_running', 'remaining_answer_time']}
        payload['question'] = {**(question if host else public_question), **lifecycle,
            'question_end_time': payload['timer']['ends_at'], 'question_in_set': payload['question_in_set'],
            'set_question_count': payload['set_question_count'], 'set_number': entry['number'], 'display_set_number': payload['set_number'],
            'total_sets': payload['total_sets'], 'set_id': payload['current_set_id'], 'is_tutorial_round': payload['is_tutorial_round']}
    if participant:
        st = entry['states'].get(str(participant.pk), {}) if entry else {}
        answer = answer_rows(quiz, entry, participant).filter(question_id=quiz.current_question_id).first() if entry and quiz.current_question_id else None
        own_reveal = reveal if reveal and participant.pk not in reveal.get('acknowledged', []) else None
        participant.refresh_from_db()
        payload.update({'participant_points': st.get('visible_stars', 0), 'overall_points': participant.overall_points,
            'is_busted': st.get('visible_busted', False),
            'has_answered': bool(answer), 'submitted_answer': answer.user_answer if answer else None,
            'pending_answer': session.pending_inputs.get(str(participant.pk), ''),
            'waiting_for_next_set': bool(entry and str(participant.pk) not in entry['states']),
            'can_answer': bool(st and not st.get('busted') and participant.is_active and not answer and quiz.status == 'active' and payload.get('answering_allowed')),
            'scoreboard': scoreboard(quiz, session, participant, code), 'own_reveal': own_reveal})
        if own_reveal:
            reveal_id = reveal.get('correct_answer', {}).get('question_id')
            own_answer = answer_rows(quiz, entry, participant).filter(question_id=reveal_id).first() if entry else None
            payload['revealed_answer'] = {'user_answer': own_answer.user_answer if own_answer else None,
                'points_earned': own_answer.points_earned if own_answer else None, 'total_points': 0 if st.get('busted') else stars(st),
                'overall_points': participant.overall_points, 'is_busted': st.get('busted', False)}
    return payload


@write_transaction
def submit_action(quiz, participant, value, data):
    from games_hub.authoritative_state import validate_and_reserve_action
    strict_integer(value)
    decision = validate_and_reserve_action(game_key='blackjack', room_code=quiz.room_code,
        session_code=participant.hub_session_code, participant_name=participant.name,
        action_type='participant_submit_answer', action=data)
    if not decision.accepted:
        raise ValueError(decision.message)
    return submit(quiz, participant, value, data=data)


def broadcast(quiz, event):
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    event = {**event, 'hub_session_code': quiz.active_hub_session_code}
    layer = get_channel_layer()
    if layer:
        transaction.on_commit(lambda: async_to_sync(layer.group_send)(f'blackjack_{quiz.room_code}', event))


def question_in_open_set(question_id):
    for session in BlackJackSession.objects.filter(quiz__status__in=['active', 'inactive']).iterator():
        if session.quiz.current_question_id == question_id or any(
                r['status'] == 'open' and question_id in r['question_ids'] for r in session.set_rounds):
            return True
    return False


@write_transaction
def present(quiz, question_id, code, data, duration=None, *, at=None):
    from dataclasses import replace
    from games_hub.authoritative_state import present_question
    from games_hub.unit_tutorial_runtime import get_unit_tutorial_state, start_unit_tutorial_if_needed
    from QuizGame.presentation import question_typewriter_timing
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    code = pool_code(quiz, code)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    if quiz.status != 'active' or quiz.current_question_id:
        # A retried presentation action is resolved idempotently by the phase service below.
        from games_hub.models import ProcessedClientAction
        if not ProcessedClientAction.objects.filter(client_action_id=data.get('client_action_id'),
                runtime_state__game_key='blackjack', runtime_state__room_code=quiz.room_code).exists():
            raise ValueError('Bitte zuerst die aktuelle Frage beenden oder das Spiel starten.')
    tutorial_state = get_unit_tutorial_state('blackjack', quiz.room_code, code)
    tutorial = bool(tutorial_state.get('requested') and not tutorial_state.get('tutorial_has_been_played'))
    if tutorial:
        remaining = session.get_remaining_question_ids_for_set(quiz.tutorial_set_number)
        if not remaining:
            raise ValueError('Das Tutorialset ist bereits abgeschlossen.')
        question_id = remaining[0]
    question = BlackJackQuestion.objects.get(pk=question_id, is_active=True)
    number = quiz.get_set_number_for_question_id(question.pk, active_only=False)
    if not tutorial:
        if quiz.tutorial_set_number == number:
            raise ValueError('Das Tutorialset ist keine gewertete Runde.')
        error = quiz.get_next_question_send_error(question.pk, data.get('selected_set_number', session.selected_set_number))
        if error and not quiz.current_question_id:
            raise ValueError(error)
    decision = present_question(game_key='blackjack', room_code=quiz.room_code, session_code=code,
        action={**data, 'question_id': question.pk}, answer_duration_seconds=int(duration or question.time_limit),
        at=at, **question_typewriter_timing(question.question_text))
    if decision.accepted and not decision.duplicate:
        from games_hub.tutorial_runtime import deactivate_tutorial_runtime
        deactivate_tutorial_runtime('blackjack', quiz.room_code, code, quiz)
        if tutorial:
            start_unit_tutorial_if_needed('blackjack', quiz.room_code, code)
            from games_hub.models import HubGameUnitTutorialRuntime
            HubGameUnitTutorialRuntime.objects.filter(game_step__game_key='blackjack',
                game_step__room_code=quiz.room_code, session__code=code, requested=True,
                tutorial_has_been_played=False).update(tutorial_question_id=question.pk)
        prepare_question(quiz, question, tutorial=tutorial)
    state = build_state(quiz, code)
    return replace(decision, snapshot=state, state_revision=state['state_revision'])


@write_transaction
def open_question(quiz, code, data, *, at=None):
    from dataclasses import replace
    from django.utils.dateparse import parse_datetime
    from games_hub.authoritative_state import open_answering
    quiz = BlackJackQuiz.objects.select_for_update().get(pk=quiz.pk)
    code = pool_code(quiz, code)
    session = BlackJackSession.objects.select_for_update().get(quiz=quiz)
    if quiz.status != 'active' or not context_matches(quiz, session, data):
        raise ValueError('Die Aktion gehoert nicht zur aktuellen Frage.')
    decision = open_answering(game_key='blackjack', room_code=quiz.room_code, session_code=code, action=data, at=at)
    if decision.accepted and not decision.duplicate:
        session.open_answering(quiz.current_question, started_at=parse_datetime(decision.snapshot['answering_started_at']),
            answer_duration_seconds=(parse_datetime(decision.snapshot['answering_deadline_at']) - parse_datetime(decision.snapshot['answering_started_at'])).total_seconds())
    state = build_state(quiz, code)
    return replace(decision, snapshot=state, state_revision=state['state_revision'])
