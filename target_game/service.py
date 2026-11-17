"""Hub identities, serialized writes and read-only role projections for Zielansage.

The persisted deadline alone closes acceptance; GETs project review when due.
No browser connection or scheduler is needed to enforce that boundary.
"""
import hashlib
import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.db.models import F, Count
from django.urls import reverse
from django.utils import timezone
from games_hub.authoritative_state import _retry_sqlite_locked
from games_hub.models import HubGameStep, HubSession
from wer_weiss_mehr.models import WerWeissMehrQuestion
from .models import TargetGame, Run, Enrollment, Round, TeamRound, Answer, Receipt, validate_solutions


class ActionError(ValueError):
    pass


def score_teams(goals, correct, mode):
    """Competition ranks for successes; failed teams share last place/one point."""
    if mode not in {'simple', 'ranking'} or len(goals) != len(correct):
        raise ValueError('Ungültige Wertung.')
    if any(type(g) is not int or g < 1 for g in goals):
        raise ValueError('Zielzahlen müssen positiv sein.')
    successful = [i for i, goal in enumerate(goals) if correct[i] >= goal]
    result = []
    for i, goal in enumerate(goals):
        achieved = i in successful
        rank = 1 + sum(goals[j] > goal for j in successful) if achieved else len(goals)
        points = (goal if achieved else 0) if mode == 'simple' else len(goals) + 2 - rank if achieved else 1
        result.append({'achieved': achieved, 'rank': rank, 'points': points, 'correct_count': correct[i]})
    return result


def match_answer(material, text, threshold):
    # Reuse the actual WWM matcher, including ambiguous matches, aliases and
    # numerical-token protection. This adapter supplies frozen options, no DB.
    options = [SimpleNamespace(id=i, canonical_text=o['text'], aliases=o['aliases'])
               for i, o in enumerate(material['solutions'])]
    question = SimpleNamespace(answers=SimpleNamespace(all=lambda: options))
    match, review = WerWeissMehrQuestion.match_answer(question, text, threshold=threshold)
    return (match.id if match else None), review


def phase_for(round, now=None):
    return 'review' if round.phase == 'answering' and (now or timezone.now()) >= round.deadline else round.phase


def start_for_hub(game, code):
    """Compose inside the existing locked Hub activation/intro transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Hub activation transaction required.')
    step = HubGameStep.objects.select_for_update().select_related('session').get(
        session__code=code, room_code=game.room_code, game_key='target')
    if step.session.session_type != 'multiplayer' or step.game_format != 'standard':
        raise ActionError('Zielansage ist ein Mehrspieler-Teamspiel.')
    existing = Run.objects.filter(step=step).first()
    created = existing is None
    if existing:
        if existing.completed_at: raise ActionError('Dieses Spiel ist bereits beendet.')
    else:
        game.full_clean()
        if not game.tasks.filter(is_active=True).exists(): raise ActionError('Lege zuerst mindestens eine aktive Aufgabe mit Lösungen an.')
        parties = list(step.session.get_official_participants().filter(is_active=True, left_permanently_at__isnull=True))
        if not parties: raise ActionError('Es sind keine spielberechtigten Teams anwesend.')
        existing = Run.objects.create(step=step, game=game, scoring_mode=game.scoring_mode,
            answer_seconds=game.answer_seconds, text_match_threshold=step.session.text_match_threshold)
        Enrollment.objects.bulk_create([Enrollment(run=existing, participant=p) for p in parties])
    game.status = 'active'; game.active_hub_session_code = code
    game.started_at = timezone.now() if created else game.started_at or timezone.now(); game.ended_at = None
    game.save(update_fields=['status', 'active_hub_session_code', 'started_at', 'ended_at'])
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from games_hub.game_intro import serialize_game_step
    event={'type':'navigate','step':serialize_game_step(step)}
    transaction.on_commit(lambda:async_to_sync(get_channel_layer().group_send)('hub_'+code,event),robust=True)
    return existing


def end_from_hub(game):
    # Hub may explicitly abort a game. Only already published round points count.
    def write():
        with transaction.atomic():
            locked = TargetGame.objects.select_for_update().get(pk=game.pk)
            run = Run.objects.filter(game=locked, step__session__code=locked.active_hub_session_code).first()
            now = timezone.now()
            if run and not run.completed_at:
                run.completed_at = now; run.revision += 1; run.save(update_fields=['completed_at', 'revision'])
            locked.status = 'completed'; locked.ended_at = now; locked.save(update_fields=['status', 'ended_at'])
    return _retry_sqlite_locked(write)


def perform(run_id, *, actor, participant, action_id, action, payload):
    if not isinstance(action_id, str) or not action_id.strip() or len(action_id) > 128:
        raise ActionError('Eine gültige Aktionskennung ist erforderlich.')
    fingerprint = hashlib.sha256(json.dumps({'action':action, 'payload':payload},sort_keys=True,
        separators=(',', ':'),allow_nan=False).encode()).hexdigest()
    actor_key = f'participant:{participant.pk}' if participant else f'host:{actor.pk}'
    def write():
        with transaction.atomic():
            run = Run.objects.select_for_update().select_related('step__session', 'game').get(pk=run_id)
            receipt = Receipt.objects.filter(run=run, actor_key=actor_key, action_id=action_id).first()
            if receipt:
                if receipt.fingerprint != fingerprint: raise ActionError('Diese Aktionskennung wurde bereits mit anderem Inhalt verwendet.')
                return receipt.result | {'replayed':True}
            if (run.completed_at or run.game.status != 'active' or not run.step.session.is_active or run.step.session.ended_at
                    or run.game.active_hub_session_code != run.step.session.code):
                raise ActionError('Dieses Spiel ist nicht aktiv.')
            team = run.teams.filter(participant=participant, is_active=True).first() if participant else None
            if participant and (not team or participant.session_id != run.step.session_id):
                raise ActionError('Dieses Team gehört nicht zum Spiel.')
            if participant:
                participant.refresh_from_db()
                if participant.left_permanently_at or participant.check_in_excluded_by_host:
                    raise ActionError('Dieses Team ist nicht mehr spielberechtigt.')
            current = run.rounds.order_by('-number').first()
            try:
                with transaction.atomic():
                    _apply(run, current, team, actor, action, payload)
            except (ActionError, ValidationError) as exc:
                result = {'success':False, 'error':str(exc), 'revision':run.revision}
            else:
                run.revision += 1; run.save(update_fields=['revision'])
                result = {'success':True, 'revision':run.revision}
            Receipt.objects.create(run=run,actor_key=actor_key,action_id=action_id,fingerprint=fingerprint,result=result)
            return result | {'replayed':False}
    return _retry_sqlite_locked(write)


def _apply(run, current, team, actor, action, payload):
    host = team is None
    if not host and action not in {'ready', 'goal', 'answer'}: raise ActionError('Nur der Host darf diese Aktion ausführen.')
    if host and action in {'pause', 'abort'}:
        if action=='pause':
            run.game.status='inactive';run.game.save(update_fields=['status'])
        else:
            run.completed_at=timezone.now();run.save(update_fields=['completed_at'])
            run.game.status='completed';run.game.ended_at=run.completed_at;run.game.save(update_fields=['status','ended_at'])
        return
    if action == 'prepare' and host:
        if payload.get('previous_round_id') != (current.pk if current else None): raise ActionError('Der Aufgabenstand hat sich geändert.')
        if current and current.phase != 'published': raise ActionError('Veröffentliche zuerst das aktuelle Ergebnis.')
        task = run.game.tasks.filter(pk=payload.get('task_id'),is_active=True).first()
        if not task: raise ActionError('Diese Aufgabe gehört nicht zum Spiel.')
        validate_solutions(task.solutions)
        current = Round.objects.create(run=run, task=task, number=current.number+1 if current else 1,
            material={'question':task.question_text, 'solutions':task.solutions})
        TeamRound.objects.bulk_create([TeamRound(round=current,enrollment=e) for e in run.teams.all()])
        return
    if action == 'finish' and host:
        if current is None or payload.get('round_id') != current.pk or current.phase != 'published':
            raise ActionError('Veröffentliche zuerst das aktuelle Ergebnis.')
        run.completed_at = timezone.now(); run.save(update_fields=['completed_at'])
        run.game.status='completed';run.game.ended_at=run.completed_at;run.game.save(update_fields=['status','ended_at'])
        return
    if current is None or payload.get('round_id') != current.pk: raise ActionError('Diese Aufgabe ist nicht mehr aktuell.')
    now = timezone.now(); phase = phase_for(current,now)
    state = TeamRound.objects.get(round=current,enrollment=team) if team else None
    if action == 'ready' and state and phase == 'nomination':
        if state.ready_at: raise ActionError('Dein Team ist bereits bereit.')
        state.ready_at=now;state.save(update_fields=['ready_at']);return
    if action == 'goal' and state and phase == 'nomination':
        value=payload.get('target')
        if not state.ready_at or state.target is not None: raise ActionError('Die Zielansage ist nicht mehr veränderbar.')
        if type(value) is not int or not 1 <= value <= len(current.material['solutions']):
            raise ActionError('Die Zielzahl muss zwischen 1 und der Anzahl der Lösungen liegen.')
        state.target=value;state.save(update_fields=['target']);return
    if action == 'start' and host and phase == 'nomination':
        if current.teams.filter(target__isnull=True).exists(): raise ActionError('Noch nicht alle Teams haben ihre Zielzahl eingeloggt.')
        if run.step.intro_ends_at and now < run.step.intro_ends_at: raise ActionError('Das Spielintro läuft noch.')
        current.started_at=now;current.deadline=now+timedelta(seconds=run.answer_seconds);current.phase='answering'
        current.save(update_fields=['started_at','deadline','phase']);return
    if action == 'answer' and state and phase == 'answering':
        text=payload.get('text')
        if not isinstance(text,str) or not text.strip() or len(text)>500: raise ActionError('Gib eine Antwort mit 1 bis 500 Zeichen ein.')
        matched,review=match_answer(current.material,text,run.text_match_threshold)
        Answer.objects.create(team=state,text=text.strip(),matched_index=matched,needs_review=review,accepted_at=now);return
    if action == 'correct' and host and phase in {'answering','review'}:
        answer=Answer.objects.filter(pk=payload.get('answer_id'),team__round=current).first();index=payload.get('solution_index')
        if not answer or type(index) is not int or not 0 <= index < len(current.material['solutions']):
            raise ActionError('Wähle die Antwort und die zugehörige richtige Lösung.')
        answer.override_index=index;answer.corrected_by=actor;answer.corrected_at=now
        answer.save(update_fields=['override_index','corrected_by','corrected_at']);return
    if action == 'publish' and host and phase == 'review':
        teams=list(current.teams.select_related('enrollment').order_by('pk'))
        counts=[len({a.override_index if a.override_index is not None else a.matched_index
            for a in s.answers.all()}-{None}) for s in teams]
        scoring=score_teams([s.target for s in teams],counts,run.scoring_mode)
        for state,result in zip(teams,scoring):
            for key,value in result.items():setattr(state,key,value)
            state.save(update_fields=list(result))
            Enrollment.objects.filter(pk=state.enrollment_id).update(total_score=F('total_score')+state.points)
        current.phase='published';current.published_at=now;current.save(update_fields=['phase','published_at']);return
    raise ActionError('Diese Aktion ist in der aktuellen Phase nicht zulässig; die Antwortfrist kann bereits abgelaufen sein.')


@contextmanager
def _snapshot_transaction():
    # Polling is read-only. BEGIN IMMEDIATE would reserve SQLite's sole writer
    # for every device's GET and starve submissions under a 25-device load.
    # Keep a consistent read transaction, without changing settings or the
    # IMMEDIATE mode of writes (including any already enclosing transaction).
    db=connections['default'];db.ensure_connection()
    change_mode=db.vendor=='sqlite' and not db.in_atomic_block
    original=db.transaction_mode if change_mode else None
    if change_mode:db.transaction_mode='DEFERRED'
    try:
        with transaction.atomic(using=db.alias):yield
    finally:
        if change_mode:db.transaction_mode=original


def snapshot(run_id, role, participant=None):
    with _snapshot_transaction():
        run=Run.objects.select_related('game','step__session').get(pk=run_id)
        current=run.rounds.order_by('-number').first();now=timezone.now()
        data={'run_id':str(run.pk),'revision':run.revision,'completed':run.completed_at is not None,
            'game_status':run.game.status,
            'scoring_mode':run.scoring_mode,'answer_seconds':run.answer_seconds,'server_now':now.isoformat(),'round':None,
            'lobby_url':reverse('games_hub:monitor' if role=='host' else 'games_hub:spectate_session' if role=='spectator' else 'games_hub:lobby',args=[run.step.session.code])}
        if role=='host':data['tasks']=list(run.game.tasks.filter(is_active=True).values('id','question_text'))
        if current:
            phase=phase_for(current,now)
            reveal=role=='host' or phase=='published'
            if reveal:
                rows=list(current.teams.select_related('enrollment__participant').order_by('pk'))
                own=next((row for row in rows if participant and row.enrollment.participant_id==participant.pk),None)
                counts={'ready_count':sum(s.ready_at is not None for s in rows),
                        'target_count':sum(s.target is not None for s in rows),'team_count':len(rows)}
            else:
                # Participants only need the common counters and their own row.
                counts=current.teams.aggregate(ready_count=Count('ready_at'),target_count=Count('target'),team_count=Count('pk'))
                own=current.teams.select_related('enrollment__participant').filter(enrollment__participant=participant).first() if participant else None
            r={'id':current.pk,'number':current.number,'phase':phase,'deadline':current.deadline.isoformat() if current.deadline else None,
                **counts,
                'solution_count':len(current.material['solutions'])}
            if role=='host' or phase!='nomination' or own and own.ready_at:r['question']=current.material['question']
            if own:
                r['own']={'name':own.enrollment.participant.nickname,'ready':own.ready_at is not None,'target':own.target,'answers':list(own.answers.order_by('pk').values('id','text')),
                    'can_answer':phase=='answering' and own.enrollment.is_active and not run.completed_at and run.game.status=='active' and run.step.session.is_active and not run.step.session.ended_at}
            if reveal:
                r['solutions']=[o['text'] for o in current.material['solutions']]
                r['teams']=[]
                answers_by_team={}
                for answer in Answer.objects.filter(team__round=current).order_by('pk').values('team_id','id','text','matched_index','override_index','needs_review','corrected_at'):
                    answers_by_team.setdefault(answer.pop('team_id'),[]).append(answer)
                for state in rows:
                    answers=answers_by_team.get(state.pk,[])
                    item={'name':state.enrollment.participant.nickname,'target':state.target,'ready':state.ready_at is not None,
                          'answers':answers,'total_score':state.enrollment.total_score}
                    if phase=='published':item.update(points=state.points,correct_count=state.correct_count,achieved=state.achieved,rank=state.rank)
                    elif role=='host':item['correct_count']=len({a['override_index'] if a['override_index'] is not None else a['matched_index'] for a in answers}-{None})
                    r['teams'].append(item)
            data['round']=r
        if run.completed_at:data['totals']=list(run.teams.order_by('-total_score','pk').values(name=F('participant__nickname'),score=F('total_score')))
        from games_hub.game_intro import serialize_game_intro
        data['intro']=serialize_game_intro(run.step,server_now=now)
        return data


def scores_for_session(session, room):
    return {e.participant.nickname:{'score':e.total_score,'accuracy':0}
        for e in Enrollment.objects.filter(run__step__session=session,run__game__room_code=room).select_related('participant')}


def leave_for_lobby(run_id, participant):
    def write():
        with transaction.atomic():
            run=Run.objects.select_for_update().select_related('game','step__session').get(pk=run_id)
            recall=run.step.session.lobby_return_countdown_ends_at
            if not (run.completed_at or run.game.status=='inactive' or recall and recall>timezone.now()):
                raise ActionError('Die manuelle Lobby-Rückkehr ist erst nach dem Spielende möglich.')
            if not run.teams.filter(participant=participant).update(is_active=False):raise ActionError('Team nicht gefunden.')
    return _retry_sqlite_locked(write)
