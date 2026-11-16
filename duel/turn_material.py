"""Concrete frozen material adapters; never invoke Multiplayer runtime/scoring."""
from random import SystemRandom

from Assign.models import AssignQuiz
from sorting_ladder.models import SortingLadderGame
from wer_weiss_mehr.models import WerWeissMehrGame, normalize_answer_text


TURN_GAMES = {
    "assign": (AssignQuiz, "Assign", "question_text"),
    "sorting_ladder": (SortingLadderGame, "Sorting Ladder", "question_text"),
    "wer_weiss_mehr": (WerWeissMehrGame, "Wer weiß mehr?", "question_text"),
}


def freeze_material(key, game, question):
    material = {"question": question.question_text, "source_game_id": game.pk}
    if not material["question"].strip():
        raise ValueError("A question is required.")
    if key == "assign":
        left, right, matches = question.left_items, question.right_items, question.correct_matches
        if (type(left) is not list or type(right) is not list or not left or not right
                or any(type(s) is not str or not s.strip() for s in [*left, *right])
                or type(matches) is not dict or set(matches) != {str(i) for i in range(len(left))}
                or any(type(v) not in (int, str) for v in matches.values())):
            raise ValueError("Every left item needs a valid matching target.")
        # Existing management/runtime accepts integer strings as match indices.
        # Normalize once into the private snapshot; never truncate JSON floats.
        matches = {i: int(v) for i, v in matches.items()}
        if any(v < 0 or v >= len(right) for v in matches.values()):
            raise ValueError("Matching target is outside the configured right side.")
        shuffled = question.get_randomized_items(room_code=game.room_code)
        original_to_position = {original: position for position, original in shuffled["position_to_original"].items()}
        material.update(left=shuffled["left_items"], right=shuffled["right_items"],
            solution={i: original_to_position[v] for i, v in matches.items()})
    elif key == "sorting_ladder":
        elements = list(question.elements.order_by("correct_rank", "pk"))
        if len(elements) < 2 or any(not e.text.strip() or not e.correct_rank.is_finite() for e in elements):
            raise ValueError("At least two valid ranked elements are required.")
        ids = [e.pk for e in elements]
        if question.starting_item_id is not None and question.starting_item_id not in ids:
            raise ValueError("Starting element must belong to this task.")
        shuffled = elements[:]
        SystemRandom().shuffle(shuffled)  # Once, under the existing command lock.
        opaque = {e.pk: i for i, e in enumerate(shuffled)}
        items = [{"id": opaque[e.pk], "text": e.text} for e in shuffled]
        material.update(items=items, solution=[opaque[i] for i in ids], initial=[opaque[question.starting_item_id]] if question.starting_item_id else [],
            description=question.description, upper_label=question.upper_label, lower_label=question.lower_label)
    else:
        answers = sorted(question.answers.all(), key=lambda a: (normalize_answer_text(a.canonical_text), a.canonical_text.lower(), a.pk))
        if not answers or any(not a.canonical_text.strip() for a in answers):
            raise ValueError("At least one answer is required.")
        material["answers"] = [{"id": a.pk, "text": a.canonical_text} for a in answers]
    return material


def board(key, material, actions):
    """Authoritative confirmed progress, independent of display formatting."""
    accepted = [a["selection"] for a in actions if a["correct"]]
    if key == "assign":
        return {str(s["left_id"]): s["right_id"] for s in accepted}
    if key == "sorting_ladder":
        placed = list(material["initial"])
        for selection in accepted:
            placed.insert(selection["position"], selection["item_id"])
        return placed
    return [s["answer_id"] for s in accepted]


def check_selection(key, material, actions, selection):
    """Invalid/stale selections reject; an actual wrong oral attempt ends play."""
    if type(selection) is not dict:
        raise ValueError("A concrete host selection is required.")
    if selection == {"kind": "wrong"}:
        return False
    progress = board(key, material, actions)
    if key == "assign":
        if set(selection) != {"kind", "left_id", "right_id"} or selection["kind"] != "pair":
            raise ValueError("A pair is required.")
        left, right = selection["left_id"], selection["right_id"]
        if (type(left) is not int or type(right) is not int or str(left) not in material["solution"]
                or right not in [x["id"] for x in material["right"]] or str(left) in progress):
            raise ValueError("Pair is invalid or already confirmed.")
        return material["solution"][str(left)] == right
    if key == "sorting_ladder":
        if set(selection) != {"kind", "item_id", "position"} or selection["kind"] != "place":
            raise ValueError("An element and insertion position are required.")
        item, position = selection["item_id"], selection["position"]
        if (type(item) is not int or type(position) is not int or item not in material["solution"]
                or item in progress or not 0 <= position <= len(progress)):
            raise ValueError("Element or insertion position is invalid/already placed.")
        candidate = progress[:]; candidate.insert(position, item)
        ranks = {item: i for i, item in enumerate(material["solution"])}
        return all(ranks[a] < ranks[b] for a, b in zip(candidate, candidate[1:]))
    if set(selection) != {"kind", "answer_id"} or selection["kind"] != "answer":
        raise ValueError("A concrete answer card is required.")
    answer = selection["answer_id"]
    if type(answer) is not int or answer not in [a["id"] for a in material["answers"]] or answer in progress:
        raise ValueError("Answer is invalid or already revealed.")
    return True


def exhausted(key, material, actions):
    progress = board(key, material, actions)
    total = len(material["solution"]) if key != "wer_weiss_mehr" else len(material["answers"])
    return len(progress) == total
