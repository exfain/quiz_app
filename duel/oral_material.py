"""Concrete source adapters for the four supported oral duel games only."""
from io import BytesIO

from PIL import Image, ImageOps
from clue_rush.models import ClueRushGame
from who_is_lying.models import WhoQuiz
from who_is_that.models import WhoThatQuiz
from wann_war_das.models import WannWarDasGame

from .estimation_questions import canonical_target


ORAL_GAMES = {
    "clue_rush": (ClueRushGame, "Clue Rush", "question_text"),
    "who": (WhoQuiz, "Who is lying", "statement"),
    "who_that": (WhoThatQuiz, "Who is that", "question_text"),
    "wann_war_das": (WannWarDasGame, "Wann war das?", "question_text"),
}


def freeze_material(game_key, game, question):
    """Called under the existing DB lock; local image READS have no side effects.

    No source runtime/start/scoring functions are invoked. Future hints, truth
    flags and the numerical target stay exclusively in the private snapshot.
    """
    text = getattr(question, ORAL_GAMES[game_key][2])
    material = {"question": text, "source_game_id": game.pk}
    image = None
    if game_key == "clue_rush":
        clues = [{"text": c.clue_text, "duration_us": c.duration*1_000_000}
                 for c in question.clues.order_by("order", "pk")]
        if not clues or any(c["duration_us"] <= 0 for c in clues):
            raise ValueError("At least one timed clue is required.")
        material.update(clues=clues, solution=question.answer,
                        duration_us=sum(c["duration_us"] for c in clues))
    elif game_key == "who":
        if not question.people or question.time_limit <= 0 or any(
            type(p) is not dict or type(p.get("is_lying")) is not bool
            or type(p.get("name")) is not str or not p["name"].strip() for p in question.people):
            raise ValueError("Valid timed persons are required.")
        order = question.get_randomized_people(game.room_code)["people"]
        people = [{"name": p["name"], "is_lying": question.people[p["original_index"]]["is_lying"]} for p in order]
        material.update(people=people, person_us=question.time_limit*1_000_000,
            duration_us=len(people)*question.time_limit*1_000_000,
            solution=", ".join(p["name"] for p in people if p["is_lying"]) or "Niemand")
    elif game_key == "who_that":
        if not question.image:
            raise ValueError("The question requires an image.")
        # Freeze bytes transactionally, strip filenames/EXIF and never expose
        # the mutable original MEDIA URL, which may itself spell the solution.
        with question.image.open("rb") as source:
            raw = source.read(10*1024*1024+1)
        if len(raw) > 10*1024*1024:
            raise ValueError("The source image is too large.")
        with Image.open(BytesIO(raw)) as original:
            picture = ImageOps.exif_transpose(original).convert("RGB")
            clean = Image.new("RGB", picture.size); clean.paste(picture)
            output = BytesIO(); clean.save(output, format="PNG")
        image = output.getvalue()
        if len(image) > 10*1024*1024:
            raise ValueError("The frozen image is too large.")
        material.update(solution=question.correct_answer, duration_us=question.time_limit*1_000_000)
    else:
        question.full_clean()
        material.update(solution=canonical_target(question.correct_answer), unit=question.unit,
            start_tolerance=canonical_target(question.start_tolerance),
            tolerance_increment=canonical_target(question.tolerance_increment),
            max_tolerance=canonical_target(question.max_tolerance), step_us=question.seconds_per_step*1_000_000,
            duration_us=question.get_effective_time_limit()*1_000_000)
    if not text.strip() or not material["solution"].strip() or material["duration_us"] <= 0:
        raise ValueError("A complete question, solution and positive duration are required.")
    return material, image
