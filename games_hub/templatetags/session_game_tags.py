import logging

from django import template
from django.utils.safestring import mark_safe

from games_hub.models import HubGameStep

register = template.Library()
logger = logging.getLogger(__name__)


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


@register.simple_tag
def session_join_qr_svg(join_url):
    """Render a local QR code; callers retain the visible URL as fallback."""
    if not join_url:
        return ""

    try:
        import qrcode
        from qrcode.image.svg import SvgPathFillImage

        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(str(join_url))
        qr.make(fit=True)
        image = qr.make_image(
            image_factory=SvgPathFillImage,
            attrib={
                "class": "session-join-qr__svg",
                "role": "img",
                "aria-label": "QR-Code zum Beitreten",
            },
        )
        return mark_safe(image.to_string(encoding="unicode"))
    except Exception:
        logger.warning("Session join QR rendering failed", exc_info=True)
        return ""
