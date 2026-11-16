"""Frozen material for the two device-input duels; no MP runtime writes."""
from io import BytesIO
from PIL import Image, ImageOps
from where_is_this.models import WhereQuiz, WhereQuestion
from where_is_this.geo import validate_map_location
from black_jack_quiz.models import BlackJackQuiz, BlackJackQuestion


INPUT_GAMES = {"where": (WhereQuiz, "Where is this?"), "blackjack": (BlackJackQuiz, "Black Jack Quiz")}


def _image(field):
    if not field: return None
    with field.open("rb") as source: raw = source.read(10*1024*1024+1)
    if len(raw) > 10*1024*1024: raise ValueError("Question image is too large.")
    with Image.open(BytesIO(raw)) as source:
        picture = ImageOps.exif_transpose(source).convert("RGB")
        clean = Image.new("RGB", picture.size); clean.paste(picture)
        output = BytesIO(); clean.save(output, format="PNG")
    if output.tell() > 10*1024*1024: raise ValueError("Question image is too large.")
    return output.getvalue()


def choices(game, key):
    if key == "where":
        return [{"id": q.pk, "question_text": q.question_text} for q in game.selected_questions.filter(is_active=True).order_by("pk")]
    return [{"id": n, "question_text": f"Set {n} ({len(ids)} Fragen)"}
        for n, ids in enumerate(game.get_explicit_question_sets(active_only=True), 1)
        if ids and n != game.tutorial_set_number]


def freeze_material(game, key, selection):
    if key == "where":
        q = game.selected_questions.select_for_update().filter(pk=selection, is_active=True).first()
        if q is None: raise ValueError("Question is not selected.")
        q.full_clean(); validate_map_location(q.map_type, q.correct_latitude, q.correct_longitude)
        questions = [{"id": q.pk, "text": q.question_text, "duration": q.time_limit,
            "map_type": q.map_type, "target": {"latitude": q.correct_latitude, "longitude": q.correct_longitude},
            "hint": q.hint_text or "", "explanation": q.explanation or ""}]
        image = _image(q.image)
    else:
        sets = game.get_explicit_question_sets(active_only=True)
        if not 1 <= selection <= len(sets) or selection == game.tutorial_set_number or not sets[selection-1]:
            raise ValueError("Set is not configured.")
        ids = sets[selection-1]
        rows = {q.pk: q for q in BlackJackQuestion.objects.select_for_update().filter(pk__in=ids, is_active=True)}
        if len(rows) != len(set(ids)): raise ValueError("Configured questions must exist.")
        questions = [{"id": q.pk, "text": q.question_text, "duration": q.time_limit,
            "target": q.correct_answer, "explanation": q.explanation or ""} for q in (rows[i] for i in ids)]
        image = None
    if any(not q["text"].strip() or type(q["duration"]) is not int or not 1 <= q["duration"] <= 86400 for q in questions):
        raise ValueError("Complete questions and valid durations are required.")
    return {"source_game_id": game.pk, "selection": selection, "questions": questions}, image


def distance(question, point):
    """Reuse the existing pure Haversine calculation, not MP rank/zone scoring."""
    target = question["target"]
    return WhereQuestion(correct_latitude=target["latitude"], correct_longitude=target["longitude"]).calculate_distance(point["latitude"], point["longitude"])


def stars(question, value):
    return BlackJackQuestion(correct_answer=question["target"]).calculate_points(value)
