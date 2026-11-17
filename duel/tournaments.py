"""Tournament organization over existing, independent Duel encounters."""
from dataclasses import replace
from random import SystemRandom
from uuid import uuid5, NAMESPACE_URL
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone
from django.urls import reverse
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.host_permissions import authorize_game_host
from games_hub.models import HubSession, HubGameStep
from games_hub.game_intro import start_game_intro
from .commands import ActorContext, CommandRejected, HandlerResult, execute_action
from .models import (DuelTournament, DuelTournamentSet, DuelTournamentPair, DuelTournamentSeat,
    DuelGameInstance, DuelUnit, ReceiptActorType, ReceiptStatus)
from .matches import MatchConfig, MatchMode
from .match_service import start_match_in_action

SUPPORTED = frozenset({'estimation', 'quiz', 'where', 'blackjack', 'assign', 'sorting_ladder', 'wer_weiss_mehr', 'bid'})
STATES = ('estimation_round_state', 'quiz_round_state', 'input_round', 'turn_round', 'control_round')


def active_tournament(session):
    return DuelTournament.objects.filter(step__session=session, status='running').select_related('step__session').first()


def page_url(run, role):
    return reverse('duel:tournament_page', args=[run.step.session.code, run.pk, role])


def state_for(instance):
    unit = instance.units.order_by('number').first()
    if unit:
        for name in STATES:
            state = getattr(unit, name, None)
            if state is not None: return state
    return None


def pair_for(instance_id):
    return DuelTournamentPair.objects.filter(game_instance_id=instance_id).select_related('tournament_set__tournament__step').first()


def answering(state):
    return state is None or state.phase in {'prepared', 'answering', 'playing', 'bidding'}


def reveal_ready(pair):
    """A Black Jack question has its own barrier within the shared set."""
    if pair.game_instance.game_key == 'estimation':
        # Each pair plays the same single-question unit. Keep the barrier live
        # in this transaction, without loading 12 units/states on every poll.
        # Missing units/states must continue to block publication.
        phase = DuelUnit.objects.filter(game_instance_id=OuterRef('game_instance_id')).order_by('number').values('estimation_round_state__phase')[:1]
        return not pair.tournament_set.pairs.annotate(_phase=Subquery(phase)).filter(
            Q(_phase__isnull=True) | Q(_phase__in=('prepared', 'answering'))).exists()
    own = state_for(pair.game_instance)
    index = getattr(own, 'question_index', 0)
    for other in pair.tournament_set.pairs.select_related('game_instance'):
        state = state_for(other.game_instance)
        if state and state.phase in {'show_result', 'completed'}: continue
        other_index = getattr(state, 'question_index', 0)
        if other_index < index or (other_index == index and answering(state)): return False
    return True


def set_complete(tset):
    return tset.pairs.exists() and all((s := state_for(p.game_instance)) is not None and s.phase == 'completed'
        for p in tset.pairs.select_related('game_instance'))


def validate_action(instance, action_type, payload, *, organization=False):
    pair = pair_for(instance.pk)
    if pair is None: return
    run = pair.tournament_set.tournament
    if organization:
        if run.coordinator_id != instance.pk: raise CommandRejected('invalid_tournament', 'Falscher Turnierkontext.')
        return
    if run.status != 'running' or pair.tournament_set.number != run.current_set:
        raise CommandRejected('stale_tournament_set', 'Diese Auslosung ist nicht mehr aktiv.')
    if not run.step.session.is_active or run.step.session.ended_at:
        raise CommandRejected('session_closed', 'Diese Session ist nicht aktiv.')
    if action_type.endswith('.prepare_question'):
        raise CommandRejected('tournament_material_fixed', 'Das Material wird gemeinsam für das Turnierset gebunden.')
    if action_type == 'duel.estimation.start_round' and payload.get('duration_seconds') != pair.tournament_set.selection.get('duration_seconds'):
        raise CommandRejected('tournament_duration_fixed', 'Die Antwortdauer ist für das ganze Set festgelegt.')
    release = (any(word in action_type for word in ('show_answers', 'show_solution', 'show_result', '.evaluate', '.reveal', 'complete_round')))
    if release and not reveal_ready(pair):
        raise CommandRejected('tournament_reveal_locked', 'Andere Paarungen können diese Aufgabe noch beantworten.')


def project(instance, state, role):
    pair = pair_for(instance.pk)
    if pair and role != 'host' and not reveal_ready(pair) and (role == 'spectator' or not answering(state_for(instance))):
        return {'unit_id': state['unit_id'], 'state_revision': state['state_revision'], 'phase': 'prepared',
                'tournament_waiting': True}
    return state


def choices(step):
    model = HubSession._get_game_model_map().get(step.game_key)
    if step.game_key not in SUPPORTED or model is None: return []
    game = model.objects.get(room_code=step.room_code)
    if step.game_key in {'where', 'blackjack'}:
        from .input_material import choices as input_choices
        return input_choices(game, step.game_key)
    if step.game_key == 'bid': return [{'id': t.pk, 'question_text': t.text} for t in game.tasks.filter(is_active=True)]
    rows = game.selected_questions.filter(is_active=True).order_by('pk')
    if step.game_key == 'quiz': rows = [q for q in rows if q.get_effective_question_type() in {'true_false', 'multiple_choice'}]
    return [{'id': q.pk, 'question_text': q.question_text} for q in rows]


def _host(user, step):
    if not authorize_game_host(user, step.game_key, step.room_code, step.session.code).allowed:
        raise CommandRejected('unauthorized', 'Keine Hostberechtigung für diesen Spielslot.')


def start_tournament(*, user, session_code, step_id):
    """Only organization is provisioned here; no question, timer or pairing yet."""
    def write():
        with transaction.atomic():
            session = HubSession.objects.select_for_update().get(code=session_code)
            step = session.steps.select_for_update().get(pk=step_id)
            _host(user, step)
            existing = DuelTournament.objects.filter(step=step).first()
            if existing: return existing
            if session.session_type != 'multiplayer' or not session.started_at or not session.is_active or session.ended_at or not session.check_in_completed:
                raise CommandRejected('session_not_ready', 'Aktiviere die Mehrspielersession und schließe den Check-in ab.')
            if session.readiness_checks.filter(active=True, ended_at__isnull=True).exists():
                raise CommandRejected('readiness_active', 'Beende zuerst den Bereitschaftscheck.')
            if step.game_key not in SUPPORTED or not choices(step):
                raise CommandRejected('buzzer_content_not_supported', 'Dieser Inhalt besitzt keine freigegebene Turniervariante ohne Buzzer.')
            from .encounters import active_encounter
            from games_hub.active_game_guard import _iter_session_games
            if active_tournament(session) or active_encounter(session) or any(game.status == 'active' for _, game in _iter_session_games(session)):
                raise CommandRejected('active_game', 'Beende zuerst das laufende Spiel.')
            if step.intro_started_at or step.result_snapshot or step.game_format != 'standard':
                raise CommandRejected('slot_already_started', 'Dieser Slot wurde bereits gestartet.')
            run = DuelTournament(id=uuid5(NAMESPACE_URL, f'tournament:{session.pk}:{step.pk}'), step=step)
            run.full_clean(); run.save(force_insert=True)
            step.game_format = 'tournament'; step.save(update_fields=['game_format'])
            session.current_step_index = list(session.steps.order_by('order', 'pk').values_list('pk', flat=True)).index(step.pk)
            session.save(update_fields=['current_step_index'])
            start_game_intro(session.code, step.game_key, step.room_code, step_id=step.pk)
            return run
    run = _retry_sqlite_locked(write)
    return {'success': True, 'tournament': True, 'host_url': page_url(run, 'host')}


def _selection(run, selection):
    expected = {'id', 'duration_seconds'} if run.step.game_key == 'estimation' else {'id'}
    if type(selection) is not dict or set(selection) != expected or type(selection.get('id')) is not int:
        raise ValueError('Wähle eine konkrete Aufgabe.')
    if run.step.game_key == 'estimation' and (type(selection['duration_seconds']) is not int or not 1 <= selection['duration_seconds'] <= 86400):
        raise ValueError('Eine positive Antwortdauer ist erforderlich.')
    if selection['id'] not in {c['id'] for c in choices(run.step)}:
        raise CommandRejected('buzzer_content_not_supported', 'Die ausgewählte Aufgabe ist nicht turnierfähig; Buzzerinhalte sind ausgeschlossen.')


def _create_set(run, selection, number):
    _selection(run, selection)
    pool = list(run.step.session.get_official_participants().select_for_update().filter(is_active=True,
        left_permanently_at__isnull=True).order_by('pk'))
    if len(pool) < 2: raise CommandRejected('not_enough_participants', 'Mindestens zwei anwesende spielberechtigte Teilnehmer sind erforderlich.')
    SystemRandom().shuffle(pool)
    tset = DuelTournamentSet(id=uuid5(run.pk, f'set:{number}'), tournament=run, number=number, selection=selection)
    tset.full_clean(); tset.save(force_insert=True)
    if len(pool) % 2:
        bye = DuelTournamentSeat(tournament_set=tset, participant=pool.pop()); bye.full_clean(); bye.save(force_insert=True)
    for position in range(len(pool)//2):
        a, b = pool[position*2:position*2+2]
        instance = DuelGameInstance(id=uuid5(run.pk, 'coordinator' if number == 1 and position == 0 else f'{number}:pair:{position}'),
            hub_session=run.step.session, hub_game_step=run.step, game_key=run.step.game_key, participant_a=a, participant_b=b)
        instance.full_clean(); instance.save(force_insert=True)
        pair = DuelTournamentPair(tournament_set=tset, game_instance=instance, position=position+1)
        pair.full_clean(); pair.save(force_insert=True)
        for party in (a, b):
            seat = DuelTournamentSeat(tournament_set=tset, participant=party, pair=pair); seat.full_clean(); seat.save(force_insert=True)
    run.current_set = number
    if run.coordinator_id is None: run.coordinator_id = uuid5(run.pk, 'coordinator')
    run.save(update_fields=['current_set', 'coordinator'])
    return tset


def _bind(context, tset):
    from .estimation_questions import bind_estimation_question_in_action
    from .controllers.quiz import bind_quiz_question_in_action
    from .controllers.inputs import bind_input_question_in_action
    from .controllers.turns import bind_turn_question_in_action
    from .controllers.control import bind_control_in_action
    bind = {'estimation': bind_estimation_question_in_action, 'quiz': bind_quiz_question_in_action,
        'where': bind_input_question_in_action, 'blackjack': bind_input_question_in_action,
        'bid': bind_control_in_action}.get(context.instance.game_key, bind_turn_question_in_action)
    for pair in tset.pairs.select_related('game_instance'):
        instance = pair.game_instance
        started = start_match_in_action(replace(context, instance=instance, unit=None), MatchConfig(MatchMode.BEST_OF_N, best_of=1))
        if started.status != ReceiptStatus.APPLIED: raise CommandRejected('invalid_pair', 'Begegnung konnte nicht vorbereitet werden.')
        unit = DuelUnit(game_instance=instance, number=1); unit.full_clean(); unit.save(force_insert=True)
        result = bind(replace(context, instance=instance, unit=unit), tset.selection['id'])
        if result.status != ReceiptStatus.APPLIED: raise CommandRejected(result.data.get('code', 'invalid_material'), 'Das gemeinsame Material konnte nicht gebunden werden.')
        if instance.game_key == 'quiz' and unit.quiz_question_binding.question_type not in {'true_false', 'multiple_choice'}:
            raise CommandRejected('buzzer_content_not_supported', 'Buzzerinhalt wurde abgelehnt.')


def draw_set(*, user, tournament_id, previous_set_id, selection):
    run = DuelTournament.objects.select_related('step__session').get(pk=tournament_id); _host(user, run.step)
    coordinator_id = uuid5(run.pk, 'coordinator')
    created = [False]
    def initialize():
        locked = DuelTournament.objects.select_for_update().select_related('step__session').get(pk=run.pk)
        if locked.coordinator_id: return
        if previous_set_id is not None: raise CommandRejected('stale_set', 'Die erste Auslosung hat keinen Vorgänger.')
        if locked.status != 'running': raise CommandRejected('tournament_closed', 'Das Turnier ist beendet.')
        _create_set(locked, selection, 1); created[0] = True
    def handler(context):
        locked = DuelTournament.objects.select_for_update().select_related('step__session').get(pk=run.pk)
        if locked.status != 'running' or not locked.step.session.is_active or locked.step.session.ended_at:
            raise CommandRejected('tournament_closed', 'Das Turnier ist nicht aktiv.')
        if created[0]: tset = locked.sets.get(number=1)
        else:
            previous = locked.sets.filter(pk=previous_set_id).first() if previous_set_id else None
            if previous_set_id is not None and previous is None:
                raise CommandRejected('stale_set', 'Das Vorgängerset gehört nicht zu diesem Turnier.')
            number = previous.number+1 if previous else 1
            existing = locked.sets.filter(number=number).first()
            if existing:
                if existing.selection != selection: raise CommandRejected('draw_conflict', 'Diese Auslosung besitzt bereits anderes Material.')
                return HandlerResult(ReceiptStatus.REJECTED, {'code': 'already_drawn'})
            if previous is None or previous.number != locked.current_set or not set_complete(previous):
                raise CommandRejected('set_not_complete', 'Schließe zuerst alle Paarungen und ihre Präsentation ab.')
            tset = _create_set(locked, selection, number)
        _bind(context, tset)
        return HandlerResult(ReceiptStatus.APPLIED, {'set_id': str(tset.pk)})
    return execute_action(instance_id=coordinator_id,
        actor=ActorContext(ReceiptActorType.HOST, user.pk), client_action_id=f'tournament-draw:{previous_set_id or "first"}',
        action_type='duel.tournament.draw', payload={'previous_set_id': str(previous_set_id) if previous_set_id else None, 'selection': selection},
        handler=handler, _initialize_instance=initialize, _tournament_organization=True)


def end_tournament(*, user, tournament_id):
    run = DuelTournament.objects.select_related('step__session').get(pk=tournament_id); _host(user, run.step)
    if not run.coordinator_id:
        def cancel_empty():
            with transaction.atomic():
                locked = DuelTournament.objects.select_for_update().get(pk=run.pk)
                if locked.coordinator_id: raise CommandRejected('set_not_complete', 'Die erste Auslosung wurde inzwischen begonnen.')
                if locked.status == 'running':
                    locked.status, locked.completed_at = 'completed', timezone.now()
                    locked.full_clean(); locked.save(update_fields=['status', 'completed_at'])
        _retry_sqlite_locked(cancel_empty)
        return None
    def handler(context):
        locked = DuelTournament.objects.select_for_update().get(pk=run.pk)
        if locked.status == 'completed': return HandlerResult(ReceiptStatus.REJECTED, {'code': 'already_ended'})
        current = locked.sets.select_for_update().get(number=locked.current_set)
        if not set_complete(current): raise CommandRejected('set_not_complete', 'Noch nicht alle Paarungen sind abgeschlossen.')
        locked.status, locked.completed_at = 'completed', timezone.now(); locked.full_clean(); locked.save(update_fields=['status', 'completed_at'])
        return HandlerResult(ReceiptStatus.APPLIED, {'code': 'tournament_completed'})
    return execute_action(instance_id=run.coordinator_id, actor=ActorContext(ReceiptActorType.HOST, user.pk),
        client_action_id='tournament-end', action_type='duel.tournament.end', payload={}, handler=handler, _tournament_organization=True)
