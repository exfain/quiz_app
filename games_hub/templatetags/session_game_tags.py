from django import template

from games_hub.models import HubGameStep


register = template.Library()


@register.simple_tag
def session_game_number(hub_session_code, game_key, room_code):
    if not hub_session_code or not game_key or not room_code:
        return ""

    order = (
        HubGameStep.objects.filter(
            session__code=hub_session_code,
            game_key=game_key,
            room_code=room_code,
        )
        .order_by("order")
        .values_list("order", flat=True)
        .first()
    )
    if order is None:
        return ""
    return order + 1
