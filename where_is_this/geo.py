import math


WEB_MERCATOR_MAX_LATITUDE = 85.05112878


class WebMercatorCoordinateError(ValueError):
    pass


def _normalized_float(value, field_name):
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise WebMercatorCoordinateError(f"{field_name} must be a number.")
    if normalized < 0 or normalized > 1:
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
    if latitude < -WEB_MERCATOR_MAX_LATITUDE or latitude > WEB_MERCATOR_MAX_LATITUDE:
        raise WebMercatorCoordinateError(
            f"Latitude must be within Web-Mercator bounds (+/-{WEB_MERCATOR_MAX_LATITUDE})."
        )
    if longitude < -180 or longitude > 180:
        raise WebMercatorCoordinateError("Longitude must be between -180 and 180.")

    sin_latitude = math.sin(math.radians(latitude))
    x_norm = (longitude + 180.0) / 360.0
    y_norm = 0.5 - math.log((1 + sin_latitude) / (1 - sin_latitude)) / (4 * math.pi)
    return x_norm, y_norm
