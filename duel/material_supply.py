"""Finite selected material, shared by Host choices and exhaustion checks."""
from games_hub.models import HubSession
from .input_material import INPUT_GAMES, choices as input_choices
from .control_games import CONTROL_GAMES
from .oral_material import ORAL_GAMES


def choices(instance):
    key = instance.game_key
    game = HubSession._get_game_model_map()[key].objects.get(room_code=instance.hub_game_step.room_code)
    if key in INPUT_GAMES: return input_choices(game, key)
    if key == 'bid': return [{'id': t.pk, 'question_text': t.text} for t in game.tasks.filter(is_active=True).order_by('pk')]
    if key in CONTROL_GAMES: return [{'id':1, 'question_text':'Neue Runde'}]
    field = ORAL_GAMES[key][2] if key in ORAL_GAMES else 'question_text'
    return [{'id': q.pk, 'question_text':getattr(q, field)} for q in game.selected_questions.filter(is_active=True).order_by('pk')]


def exhausted(instance):
    if instance.game_key in {'buzzer', 'host_points'}: return False  # No finite task supply.
    used = set()
    for unit in instance.units.all():
        if instance.game_key == 'estimation': used.add(unit.estimation_question_binding.question_id)
        elif instance.game_key == 'quiz': used.add(unit.quiz_question_binding.question_id)
        elif instance.game_key in INPUT_GAMES: used.add(unit.input_round.material['selection'])
        elif instance.game_key == 'bid': used.add(unit.control_round.material['task_id'])
        elif instance.game_key in ORAL_GAMES: used.add(unit.oral_round.source_question_id)
        else: used.add(unit.turn_round.source_question_id)
    return bool(used) and not {row['id'] for row in choices(instance)} - used
