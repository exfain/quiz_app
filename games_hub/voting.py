from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from Assign.models import AssignParticipant, AssignQuiz
from Estimation.models import EstimationParticipant, EstimationQuiz
from QuizGame.models import Quiz as QuizGameModel, QuizParticipant
from black_jack_quiz.models import BlackJackParticipant, BlackJackQuiz
from clue_rush.models import ClueRushGame, ClueRushParticipant
from sorting_ladder.models import SortingLadderGame, SortingLadderParticipant
from wer_weiss_mehr.models import WerWeissMehrGame, WerWeissMehrParticipant
from where_is_this.models import WhereParticipant, WhereQuiz
from who_is_lying.models import WhoParticipant, WhoQuiz
from who_is_that.models import WhoThatParticipant, WhoThatQuiz
from buzzer.models import BuzzerGame, BuzzerParticipant
from host_points.models import HostPointsGame, HostPointsParticipant
from wann_war_das.models import WannWarDasGame, WannWarDasParticipant

from .models import GameVote, HubGameParticipantSnapshot, HubGameStep, HubParticipant, HubSession


GAME_MODELS = {
    'quiz': (QuizGameModel, QuizParticipant, 'Quick Quiz'),
    'clue_rush': (ClueRushGame, ClueRushParticipant, 'Clue Rush'),
    'estimation': (EstimationQuiz, EstimationParticipant, 'Estimation'),
    'assign': (AssignQuiz, AssignParticipant, 'Assign'),
    'who': (WhoQuiz, WhoParticipant, 'Who is Lying?'),
    'who_that': (WhoThatQuiz, WhoThatParticipant, 'Who is That?'),
    'where': (WhereQuiz, WhereParticipant, 'Where is This?'),
    'blackjack': (BlackJackQuiz, BlackJackParticipant, 'Black Jack'),
    'sorting_ladder': (SortingLadderGame, SortingLadderParticipant, 'Sorting Ladder'),
    'wer_weiss_mehr': (WerWeissMehrGame, WerWeissMehrParticipant, 'Wer weiss mehr?'),
    'buzzer': (BuzzerGame, BuzzerParticipant, 'Buzzer'),
    'host_points': (HostPointsGame, HostPointsParticipant, 'Host-Punktevergabe'),
    'wann_war_das': (WannWarDasGame, WannWarDasParticipant, 'Wann war das?'),
}

VOTING_BLOCKED_STATUSES = {'active', 'completed', 'cancelled', 'ended'}
VOTING_STARTABLE_STATUSES = {'waiting', 'inactive'}


def _serialize_dt(value):
    return value.isoformat() if value else None


def _display_score(value):
    value = round(float(value or 0), 2)
    return int(value) if value.is_integer() else value


def _score_value_for_participant(participant):
    for attr in ('total_score', 'overall_points', 'total_points', 'final_score'):
        value = getattr(participant, attr, None)
        if value is not None:
            return value
    return 0


def _game_data_for_step(session, step):
    config = GAME_MODELS.get(step.game_key)
    if not config or not step.room_code:
        return {}
    game_model, participant_model, _ = config
    game = game_model.objects.filter(room_code=step.room_code).first()
    if not game:
        return {}

    participants_qs = participant_model.objects.filter(
        quiz=game,
        hub_session_code=session.code,
    ).select_related('quiz')
    if session.started_at:
        participants_qs = participants_qs.filter(joined_at__gte=session.started_at)
    if session.ended_at:
        participants_qs = participants_qs.filter(joined_at__lte=session.ended_at)

    return {
        participant.name: _display_score(_score_value_for_participant(participant))
        for participant in participants_qs
    }


def _step_game(step):
    config = GAME_MODELS.get(step.game_key)
    if not config or not step.room_code:
        return None
    game_model = config[0]
    return game_model.objects.filter(room_code=step.room_code).first()


def _step_game_status(step):
    game = _step_game(step)
    return getattr(game, 'status', None) if game else None


def is_step_votable(step):
    game = _step_game(step)
    if not game:
        return False

    status = getattr(game, 'status', None)
    if status in VOTING_BLOCKED_STATUSES:
        return False
    return status in VOTING_STARTABLE_STATUSES


def _step_game_title(step):
    config = GAME_MODELS.get(step.game_key)
    type_label = config[2] if config else step.get_game_key_display()
    game_title = ''
    if config and step.room_code:
        game_model = config[0]
        game = game_model.objects.filter(room_code=step.room_code).only('title').first()
        game_title = getattr(game, 'title', '') if game else ''
    title = (step.title or game_title or type_label or '').strip()
    return {
        'title': title,
        'type': type_label,
        'display_name': title or type_label,
    }


def _active_official_participants(session):
    if session.check_in_completed:
        qs = session.participants.filter(
            scoring_eligible=True,
            check_in_excluded_by_host=False,
        )
    else:
        qs = session.participants.filter(check_in_excluded_by_host=False)
    return qs.filter(left_permanently_at__isnull=True).order_by('joined_at', 'nickname')


def _snapshot_participants_for_step(session, step):
    snapshots = list(
        HubGameParticipantSnapshot.objects
        .filter(
            session=session,
            game_step=step,
            included_in_scoring=True,
            participant__check_in_excluded_by_host=False,
            participant__left_permanently_at__isnull=True,
        )
        .select_related('participant')
        .order_by('participant__joined_at', 'participant__nickname')
    )
    if not snapshots:
        return None
    return [snapshot.participant for snapshot in snapshots]


def get_last_completed_step(session):
    for step in session.steps.order_by('-order'):
        if _step_game_status(step) == 'completed':
            return step
    return None


def get_voting_eligible_participants(session):
    mode = session.voting_mode
    if mode == HubSession.VOTING_OFF:
        return [], 'Voting ist ausgeschaltet.'

    if mode == HubSession.VOTING_NORMAL:
        return list(_active_official_participants(session)), ''

    last_step = get_last_completed_step(session)
    if not last_step:
        return [], 'Dieser Modus ist erst nach dem ersten abgeschlossenen Spiel verfuegbar.'

    candidates = _snapshot_participants_for_step(session, last_step)
    if candidates is None:
        candidates = list(_active_official_participants(session))
    if not candidates:
        return [], 'Keine stimmberechtigten Teilnehmer vorhanden.'

    game_scores = _game_data_for_step(session, last_step)
    score_by_participant = {
        participant.nickname: _display_score(game_scores.get(participant.nickname, 0))
        for participant in candidates
    }
    if not score_by_participant:
        return [], 'Keine Spielpunkte fuer das letzte abgeschlossene Spiel vorhanden.'

    target_score = (
        min(score_by_participant.values())
        if mode == HubSession.VOTING_LOSER
        else max(score_by_participant.values())
    )
    eligible = [
        participant
        for participant in candidates
        if score_by_participant.get(participant.nickname) == target_score
    ]
    return eligible, ''


def _get_vote_counts(session):
    steps = [
        step
        for step in session.steps.order_by('order')
        if is_step_votable(step)
    ]
    available_step_ids = [step.id for step in steps]
    vote_map = {}
    if (
        available_step_ids
        and session.current_voting_round > 0
        and session.voting_mode != HubSession.VOTING_OFF
    ):
        vote_qs = (
            GameVote.objects
            .filter(
                session=session,
                voting_round=session.current_voting_round,
                step_id__in=available_step_ids,
            )
            .values('step_id')
            .annotate(count=Count('id'))
        )
        vote_map = {vote['step_id']: vote['count'] for vote in vote_qs}

    result = []
    for step in steps:
        names = _step_game_title(step)
        result.append({
            'step_id': step.id,
            'step_order': step.order,
            'game_key': step.game_key,
            'game_type': names['type'],
            'title': names['title'],
            'display_name': names['display_name'],
            'status': _step_game_status(step),
            'votable': True,
            'count': vote_map.get(step.id, 0),
        })
    return result


def get_voting_state(session, participant_nickname=None):
    participant_nickname = (participant_nickname or '').strip()
    eligible, eligibility_error = get_voting_eligible_participants(session)
    eligible_names = [participant.nickname for participant in eligible]
    votes = _get_vote_counts(session)
    available_step_ids = [vote['step_id'] for vote in votes]
    current_vote = None
    if participant_nickname and session.current_voting_round > 0 and available_step_ids:
        current_vote = (
            GameVote.objects
            .filter(
                session=session,
                voting_round=session.current_voting_round,
                participant_nickname=participant_nickname,
                step_id__in=available_step_ids,
            )
            .select_related('step')
            .first()
        )

    voted_names = list(
        GameVote.objects
        .filter(
            session=session,
            voting_round=session.current_voting_round,
            step_id__in=available_step_ids,
        )
        .order_by('created_at')
        .values_list('participant_nickname', flat=True)
    ) if session.current_voting_round > 0 and available_step_ids else []

    has_options = bool(votes)
    can_vote = (
        session.voting_open
        and session.voting_mode != HubSession.VOTING_OFF
        and has_options
        and bool(participant_nickname)
        and participant_nickname in eligible_names
    )
    if session.voting_mode == HubSession.VOTING_OFF:
        message = 'Voting ist ausgeschaltet.'
    elif session.voting_open and not has_options:
        message = 'Keine weiteren Spiele verfuegbar.'
    elif session.voting_open and can_vote:
        message = 'Du kannst das naechste Spiel waehlen.'
    elif session.voting_open:
        message = eligibility_error or 'Warte auf die Spielauswahl.'
    else:
        message = 'Voting ist geschlossen.'

    return {
        'success': True,
        'voting': {
            'mode': session.voting_mode,
            'mode_label': session.get_voting_mode_display(),
            'open': session.voting_open,
            'enabled': session.voting_mode != HubSession.VOTING_OFF,
            'round': session.current_voting_round,
            'started_at': _serialize_dt(session.voting_started_at),
            'closed_at': _serialize_dt(session.voting_closed_at),
            'can_vote': can_vote,
            'message': message,
            'eligibility_error': eligibility_error,
            'my_vote_step_order': current_vote.step.order if current_vote else None,
            'available_option_count': len(votes),
        },
        'eligible_participants': eligible_names,
        'voted_participants': voted_names,
        'votes': votes,
    }


@transaction.atomic
def open_session_voting(session, mode):
    mode = (mode or '').strip()
    if mode not in {
        HubSession.VOTING_NORMAL,
        HubSession.VOTING_LOSER,
        HubSession.VOTING_WINNER,
    }:
        return {'success': False, 'error': 'Ungueltiger Votingmodus.'}

    session = HubSession.objects.select_for_update().get(pk=session.pk)
    previous_mode = session.voting_mode
    session.voting_mode = mode
    eligible, eligibility_error = get_voting_eligible_participants(session)
    if mode in {HubSession.VOTING_LOSER, HubSession.VOTING_WINNER} and not eligible:
        session.voting_mode = previous_mode
        return {
            'success': False,
            'error': eligibility_error or 'Keine stimmberechtigten Teilnehmer vorhanden.',
        }

    session.current_voting_round = (session.current_voting_round or 0) + 1
    session.voting_open = True
    session.voting_started_at = timezone.now()
    session.voting_closed_at = None
    session.save(update_fields=[
        'voting_mode',
        'voting_open',
        'current_voting_round',
        'voting_started_at',
        'voting_closed_at',
        'updated_at',
    ])
    return get_voting_state(session)


@transaction.atomic
def close_session_voting(session):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    session.voting_open = False
    session.voting_closed_at = timezone.now()
    session.save(update_fields=['voting_open', 'voting_closed_at', 'updated_at'])
    return get_voting_state(session)


@transaction.atomic
def disable_session_voting(session):
    session = HubSession.objects.select_for_update().get(pk=session.pk)
    session.voting_mode = HubSession.VOTING_OFF
    session.voting_open = False
    session.voting_closed_at = timezone.now()
    session.save(update_fields=['voting_mode', 'voting_open', 'voting_closed_at', 'updated_at'])
    return get_voting_state(session)


@transaction.atomic
def submit_session_vote(session, nickname, step_order):
    nickname = (nickname or '').strip()
    if not nickname:
        return {'success': False, 'error': 'Teilnehmername fehlt.'}
    if step_order is None:
        return {'success': False, 'error': 'Spielauswahl fehlt.'}

    session = HubSession.objects.select_for_update().get(pk=session.pk)
    state = get_voting_state(session, participant_nickname=nickname)
    if not state['voting']['can_vote']:
        return {
            **state,
            'success': False,
            'error': state['voting']['message'] or 'Du bist fuer dieses Voting nicht stimmberechtigt.',
        }

    participant_exists = HubParticipant.objects.filter(session=session, nickname=nickname).exists()
    if not participant_exists:
        return {'success': False, 'error': 'Teilnehmer nicht gefunden.'}

    try:
        step = HubGameStep.objects.get(session=session, order=step_order)
    except HubGameStep.DoesNotExist:
        return {'success': False, 'error': 'Spiel nicht gefunden.'}
    if not is_step_votable(step):
        return {
            **get_voting_state(session, participant_nickname=nickname),
            'success': False,
            'error': 'Dieses Spiel ist nicht mehr als Votingoption verfuegbar.',
        }

    vote, created = GameVote.objects.update_or_create(
        session=session,
        voting_round=session.current_voting_round,
        participant_nickname=nickname,
        defaults={'step': step},
    )
    result = get_voting_state(session, participant_nickname=nickname)
    result.update({'success': True, 'created': created, 'vote_id': vote.id})
    return result
