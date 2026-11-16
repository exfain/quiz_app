"""Keep each played set, including repetitions and archived questions."""
from .models import WerWeissMehrParticipantState, WerWeissMehrQuestion
from games_hub.unit_tutorial_runtime import get_scorebox_excluded_tutorial_question_ids


def ordered_sets(quiz, code=None):
    session = getattr(quiz, 'session', None)
    excluded = set(get_scorebox_excluded_tutorial_question_ids('wer_weiss_mehr', quiz.room_code, code))
    history = [entry for entry in (session.question_rounds if session else [])
               if (entry.get('hub_session_code') or None) == (code or None) and not entry.get('is_tutorial')]
    legacy = WerWeissMehrParticipantState.objects.filter(quiz=quiz, set_number=0,
        participant__hub_session_code=code or None, is_tutorial=False).exclude(question_id__in=excluded).order_by('created_at', 'id')
    legacy_ids = list(dict.fromkeys(legacy.values_list('question_id', flat=True)))
    entries = [{'question_id': pk, 'set_number': 0} for pk in legacy_ids] + history
    by_id = WerWeissMehrQuestion.objects.in_bulk([entry['question_id'] for entry in entries])
    rows = [{'question': by_id[entry['question_id']], 'set_number': entry['set_number'], 'played': True}
            for entry in entries if entry['question_id'] in by_id]
    known = {row['question'].pk for row in rows}
    if quiz.current_question_id and quiz.current_question_id not in known and quiz.current_question_id not in excluded:
        rows.append({'question': quiz.current_question, 'set_number': session.current_set_number if session else 0, 'played': True})
        known.add(quiz.current_question_id)
    selected = list(quiz.selected_questions.filter(is_active=True).exclude(pk__in=excluded))
    order = {str(pk): index for index, pk in enumerate(quiz.question_order or [])}
    selected.sort(key=lambda question: order.get(str(question.pk), len(order)))
    rows += [{'question': question, 'set_number': None, 'played': False} for question in selected if question.pk not in known]
    for number, row in enumerate(rows, 1):
        row['number'] = number
    return rows


def progress(quiz, code=None):
    rows = ordered_sets(quiz, code)
    session = getattr(quiz, 'session', None)
    current = next((row['number'] for row in rows if row['question'].pk == quiz.current_question_id
                    and row['set_number'] == (session.current_set_number if session else 0)), None)
    if not quiz.current_question_id:
        current = sum(row['played'] for row in rows) or None
    return {'current': current, 'total': len(rows)}
