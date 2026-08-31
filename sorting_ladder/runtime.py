from datetime import timedelta


SORTING_LADDER_REVEAL_STAGGER_MS = 120
SORTING_LADDER_REVEAL_ANIMATION_MS = 160


def sorting_ladder_reveal_counts(*, item_count, round_number):
    """Return only the presentation steps that are new for this round."""
    normalized_items = max(int(item_count or 0), 0)
    normalized_round = max(int(round_number or 1), 1)
    if normalized_round == 1:
        return {
            'label_count': 2,
            'element_count': max(normalized_items - 1, 0),
            'fixed_count': 1 if normalized_items else 0,
            'marker_group_count': 1,
        }
    return {
        'label_count': 0,
        'element_count': 0,
        'fixed_count': 0,
        'marker_group_count': 1,
    }


def sorting_ladder_reveal_step_count(*, item_count, round_number):
    return sum(
        sorting_ladder_reveal_counts(
            item_count=item_count,
            round_number=round_number,
        ).values()
    )


def sorting_ladder_reveal_ready_at(*, content_revealed_at, item_count, round_number):
    if not content_revealed_at:
        return None
    step_count = sorting_ladder_reveal_step_count(
        item_count=item_count,
        round_number=round_number,
    )
    duration_ms = (
        (step_count - 1) * SORTING_LADDER_REVEAL_STAGGER_MS
        + SORTING_LADDER_REVEAL_ANIMATION_MS
        if step_count
        else 0
    )
    return content_revealed_at + timedelta(milliseconds=duration_ms)
