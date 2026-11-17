"""Role-scoped tournament dashboard and existing encounter links."""
from uuid import UUID
from django.db import transaction
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_headers
from games_hub.authoritative_state import _retry_sqlite_locked
from .models import DuelTournament, DuelTournamentPair, DuelUnit, DuelScoreEntry, DuelSubmission
from .encounters import participant_identity, page_url as encounter_url
from .tournaments import (_host, draw_set, end_tournament, choices, state_for, reveal_ready, set_complete, page_url)
from .read_transactions import snapshot_transaction
from .views import _errors, _body


def context(request, session_code, tournament_id, role):
    run = get_object_or_404(DuelTournament.objects.select_related('step__session'), pk=tournament_id,
        step__session__code=session_code, step__game_format='tournament', step__session__session_type='multiplayer')
    if role == 'host': _host(request.user, run.step); party = None
    elif role == 'participant': party = participant_identity(request, run.step.session)
    elif role == 'spectator': party = None
    else:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    return run, party


@never_cache
@ensure_csrf_cookie
@_errors
def page(request, session_code, tournament_id, role):
    # Participant authentication uses its signed browser credential in the API,
    # never a nickname embedded in a page URL.
    run = get_object_or_404(DuelTournament.objects.select_related('step__session'), pk=tournament_id, step__session__code=session_code)
    if role == 'host': _host(request.user, run.step)
    elif role not in {'participant', 'spectator'}:
        from django.http import Http404
        raise Http404
    return render(request, 'duel/tournament.html', {'tournament': run, 'session': run.step.session, 'role': role,
        'api': reverse('duel:tournament_api', args=[session_code, tournament_id, role])})


def snapshot(run, role, party):
    estimation = run.step.game_key == 'estimation'
    sets = run.sets.order_by('number')
    if estimation:
        # Completed pair pages also poll this router for the next draw. Load
        # the shared set once per consistent read, not again for each row.
        units = DuelUnit.objects.order_by('number').select_related('estimation_round_state', 'outcome').prefetch_related(
            Prefetch('score_entries', queryset=DuelScoreEntry.objects.only('id', 'unit_id', 'participant_id', 'delta')),
            Prefetch('submissions', queryset=DuelSubmission.objects.only('id', 'unit_id', 'participant_id', 'status', 'input_key')))
        pairs = DuelTournamentPair.objects.select_related('game_instance__participant_a', 'game_instance__participant_b', 'game_instance__hub_session').prefetch_related(
            Prefetch('game_instance__units', queryset=units))
        sets = list(sets.prefetch_related(Prefetch('pairs', queryset=pairs)))

    def pair_rows(tset):
        return tset.pairs.all() if estimation else tset.pairs.select_related('game_instance__participant_a', 'game_instance__participant_b')

    def read_state(instance):
        if not estimation:
            return state_for(instance)
        units = list(instance.units.all())
        if len(units) > 1:
            raise DuelUnit.MultipleObjectsReturned('A tournament encounter must have one decision unit.')
        return getattr(units[0], 'estimation_round_state', None) if units else None

    def complete(tset):
        if not estimation:
            return set_complete(tset)
        rows = list(pair_rows(tset))
        return bool(rows) and all((state := read_state(pair.game_instance)) is not None and state.phase == 'completed' for pair in rows)

    def points(unit, participant_id):
        if estimation:
            return sum(entry.delta for entry in unit.score_entries.all() if entry.participant_id == participant_id)
        return sum(DuelScoreEntry.objects.filter(unit=unit, participant_id=participant_id).values_list('delta', flat=True))

    current = next((tset for tset in sets if tset.number == run.current_set), None) if estimation else run.sets.filter(number=run.current_set).first()
    ready = bool(current) and all((state := read_state(pair.game_instance)) is not None and state.phase not in {'prepared', 'answering'}
        for pair in pair_rows(current)) if estimation else None
    data = {'id': str(run.pk), 'role': role, 'status': run.status, 'game_key': run.step.game_key,
        'set_id': str(current.pk) if current else None, 'set_number': run.current_set, 'pairs': [], 'bye': [],
        'complete': bool(current and complete(current)), 'own_url': None, 'own_finished': False,
        'lobby_url': reverse('games_hub:monitor' if role == 'host' else 'games_hub:spectate_session' if role == 'spectator' else 'games_hub:lobby', args=[run.step.session.code])}
    if role == 'host': data['choices'] = choices(run.step)
    if current:
        for pair in pair_rows(current):
            instance = pair.game_instance; state = read_state(instance)
            if not estimation: ready = reveal_ready(pair)
            row = {'position': pair.position, 'names': [instance.participant_a.nickname, instance.participant_b.nickname],
                'phase': state.phase if state else 'prepared', 'reveals_allowed': ready,
                'instance_id': str(instance.pk), 'completed': bool(state and state.phase == 'completed')}
            if state:
                scope = f'question:{state.question_index}' if instance.game_key == 'blackjack' else ''
                if instance.game_key in {'assign', 'sorting_ladder', 'wer_weiss_mehr'}:
                    from .turn_material import board
                    row['confirmed_count'] = len(board(instance.game_key, state.material, state.actions))
                else:
                    row['submitted'] = (len({answer.participant_id for answer in state.unit.submissions.all()
                        if answer.status == 'committed' and answer.input_key == scope}) if estimation else
                        DuelSubmission.objects.filter(unit=state.unit, status='committed', input_key=scope).values('participant_id').distinct().count())
                if instance.game_key == 'bid' and state.phase not in {'prepared', 'bidding'}:
                    row['counter'] = state.counter
                deadline = (state.bid_deadline if instance.game_key == 'bid' and state.phase == 'bidding'
                    else state.answer_deadline if instance.game_key == 'bid' and state.phase == 'answering'
                    else getattr(state, 'deadline', None) if state.phase == 'answering' else None)
                row['deadline'] = deadline.isoformat() if deadline else None
                row['server_now'] = timezone.now().isoformat()
                if 'question' not in data and state.phase != 'prepared':
                    from .views import _snapshot_unfiltered
                    public = _snapshot_unfiltered(instance, state.unit_id, None, 'spectator')['snapshot']
                    data['question'] = public.get('question', '')
                    data['options'] = public.get('options', [])
            if role == 'host': row['url'] = encounter_url(instance, 'host')
            elif role == 'spectator' and ready: row['url'] = encounter_url(instance, 'spectator')
            if ready and state and state.phase in {'show_result', 'completed'}:
                unit = state.unit; outcome = getattr(unit, 'outcome', None)
                if outcome:
                    row['outcome'] = outcome.outcome
                    row['result_reason'] = outcome.reason
                    row['points'] = [points(unit, pk)
                        for pk in (instance.participant_a_id, instance.participant_b_id)]
            if state and state.phase in {'needs_resolution', 'resolution_revealed'}: row['blocked'] = 'Für diesen Fall fehlt weiterhin eine fachliche Wertungsregel.'
            data['pairs'].append(row)
            if party and party.pk in (instance.participant_a_id, instance.participant_b_id):
                data['own_url'] = encounter_url(instance, 'participant'); data['own_instance_id'] = str(instance.pk)
                data['own_finished'] = row['completed']
        data['bye'] = list(current.seats.filter(pair__isnull=True).values_list('participant__nickname', flat=True))
        if role == 'host': data['selection'] = current.selection
    data['results'] = []
    for tset in sets:
        if not complete(tset): continue
        for seat in tset.seats.filter(pair__isnull=True).select_related('participant'):
            data['results'].append({'set': tset.number, 'names': [seat.participant.nickname], 'outcome': 'Freilos', 'points': [0]})
        for pair in pair_rows(tset):
            unit = read_state(pair.game_instance).unit if estimation else pair.game_instance.units.get(); outcome = unit.outcome
            data['results'].append({'set': tset.number, 'names': [pair.game_instance.participant_a.nickname, pair.game_instance.participant_b.nickname],
                'outcome': outcome.outcome, 'result_reason': outcome.reason, 'points': [points(unit, pk)
                    for pk in (pair.game_instance.participant_a_id, pair.game_instance.participant_b_id)]})
    return data


@never_cache
@vary_on_headers('Authorization', 'Cookie')
@require_http_methods(['GET', 'POST'])
@ensure_csrf_cookie
@_errors
def api(request, session_code, tournament_id, role):
    run, party = context(request, session_code, tournament_id, role)
    if request.GET: raise ValueError('No client-selected projection.')
    result = None
    if request.method == 'POST':
        if role != 'host':
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        body = _body(request)
        if body.get('action') == 'draw' and set(body) == {'action', 'previous_set_id', 'selection'}:
            result = draw_set(user=request.user, tournament_id=run.pk,
                previous_set_id=UUID(body['previous_set_id']) if body['previous_set_id'] else None, selection=body['selection'])
        elif body == {'action': 'end'}: result = end_tournament(user=request.user, tournament_id=run.pk)
        else: raise ValueError('Invalid tournament command.')
    def read_snapshot():
        with (snapshot_transaction() if run.step.game_key == 'estimation' else transaction.atomic()):
            current = get_object_or_404(DuelTournament.objects.select_for_update().select_related('step__session'), pk=run.pk)
            data = snapshot(current, role, party)
            if result: data['action'] = {'status': result.status.value, 'replayed': result.replayed, 'code': result.data.get('code')}
        return JsonResponse(data)
    return _retry_sqlite_locked(read_snapshot)
