"""Validate TWD97 tree coordinates and convert them to public WGS84 points."""

from __future__ import annotations

import math
from collections.abc import Sequence

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


def twd97_many_to_wgs84(
    x_values: Sequence[object], y_values: Sequence[object]
) -> list[tuple[float, float] | None]:
    """Convert equally sized coordinate columns with one vectorized projection."""
    if len(x_values) != len(y_values):
        raise ValueError("coordinate columns must have equal lengths")
    safe_x: list[float] = []
    safe_y: list[float] = []
    valid: list[bool] = []
    for x, y in zip(x_values, y_values, strict=True):
        try:
            x_value = float(x)  # type: ignore[arg-type]
            y_value = float(y)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            x_value = y_value = math.nan
        is_valid = math.isfinite(x_value) and math.isfinite(y_value)
        safe_x.append(x_value if is_valid else math.nan)
        safe_y.append(y_value if is_valid else math.nan)
        valid.append(is_valid)
    longitudes, latitudes = _TRANSFORMER.transform(safe_x, safe_y)
    results: list[tuple[float, float] | None] = []
    for latitude, longitude, source_valid in zip(latitudes, longitudes, valid, strict=True):
        if (
            not source_valid
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not _TAIPEI_LATITUDE_RANGE[0] <= latitude <= _TAIPEI_LATITUDE_RANGE[1]
            or not _TAIPEI_LONGITUDE_RANGE[0] <= longitude <= _TAIPEI_LONGITUDE_RANGE[1]
        ):
            results.append(None)
        else:
            results.append((round(latitude, 7), round(longitude, 7)))
    return results
