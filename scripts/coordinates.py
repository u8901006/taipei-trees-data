"""Validate TWD97 tree coordinates and convert them to public WGS84 points."""

from __future__ import annotations

import math

from pyproj import Transformer


_TRANSFORMER = Transformer.from_crs("EPSG:3826", "EPSG:4326", always_xy=True)
_TAIPEI_LATITUDE_RANGE = (24.8, 25.3)
_TAIPEI_LONGITUDE_RANGE = (121.3, 121.8)


def twd97_to_wgs84(x: object, y: object) -> tuple[float, float] | None:
    """Return a rounded Taipei WGS84 point, or ``None`` for unsafe input."""
    try:
        x_value = float(x)  # type: ignore[arg-type]
        y_value = float(y)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x_value) or not math.isfinite(y_value):
        return None

    try:
        longitude, latitude = _TRANSFORMER.transform(x_value, y_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if not _TAIPEI_LATITUDE_RANGE[0] <= latitude <= _TAIPEI_LATITUDE_RANGE[1]:
        return None
    if not _TAIPEI_LONGITUDE_RANGE[0] <= longitude <= _TAIPEI_LONGITUDE_RANGE[1]:
        return None
    return round(latitude, 7), round(longitude, 7)
