import math
import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings


WEB_MERCATOR_MAX_LATITUDE = 85.05112878


class WebMercatorCoordinateError(ValueError):
    pass


@lru_cache(maxsize=1)
def map_bounds():
    config = json.loads((Path(settings.BASE_DIR) / 'static/Where_is_this/maps/maps_config.json').read_text(encoding='utf-8'))
    return {('world_mercator' if key == 'world' else key): value['geoBounds'] for key, value in config['maps'].items()}


def validate_map_location(map_type, latitude, longitude):
    web_mercator_lat_lng_to_norm(latitude, longitude)
    if map_type not in map_bounds():
        raise WebMercatorCoordinateError('Unbekannte Karte.')
    west, south, east, north = map_bounds()[map_type]
    if not west <= longitude <= east or not south <= latitude <= north:
        raise WebMercatorCoordinateError('Der Ort liegt außerhalb des gewählten Kartenausschnitts.')


def _normalized_float(value, field_name):
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise WebMercatorCoordinateError(f"{field_name} must be a number.")
    if not math.isfinite(normalized) or normalized < 0 or normalized > 1:
        raise WebMercatorCoordinateError(f"{field_name} must be between 0 and 1.")
    return normalized


def web_mercator_norm_to_lat_lng(x_norm, y_norm):
    """Convert normalized Web-Mercator SVG coordinates to latitude/longitude."""
    x_norm = _normalized_float(x_norm, "x_norm")
    y_norm = _normalized_float(y_norm, "y_norm")
    longitude = x_norm * 360.0 - 180.0
    merc_y = math.pi * (1.0 - 2.0 * y_norm)
    latitude = math.degrees(math.atan(math.sinh(merc_y)))
    return latitude, longitude


def web_mercator_lat_lng_to_norm(latitude, longitude):
    """Convert latitude/longitude to normalized Web-Mercator SVG coordinates."""
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        raise WebMercatorCoordinateError("Latitude and longitude must be numbers.")
    if not math.isfinite(latitude) or latitude < -WEB_MERCATOR_MAX_LATITUDE or latitude > WEB_MERCATOR_MAX_LATITUDE:
        raise WebMercatorCoordinateError(
            f"Latitude must be within Web-Mercator bounds (+/-{WEB_MERCATOR_MAX_LATITUDE})."
        )
    if not math.isfinite(longitude) or longitude < -180 or longitude > 180:
        raise WebMercatorCoordinateError("Longitude must be between -180 and 180.")

    sin_latitude = math.sin(math.radians(latitude))
    x_norm = (longitude + 180.0) / 360.0
    y_norm = 0.5 - math.log((1 + sin_latitude) / (1 - sin_latitude)) / (4 * math.pi)
    return x_norm, y_norm
