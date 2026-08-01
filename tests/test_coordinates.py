from __future__ import annotations

import math

import pytest

from scripts.coordinates import twd97_many_to_wgs84, twd97_to_wgs84


def test_twd97_to_wgs84_known_taipei_point() -> None:
    latitude, longitude = twd97_to_wgs84(306894.85, 2770248.38) or (None, None)

    assert latitude == pytest.approx(25.0392944, abs=0.0000001)
    assert longitude == pytest.approx(121.5638238, abs=0.0000001)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (None, 2770248.38),
        (math.nan, 2770248.38),
        (math.inf, 2770248.38),
        (0, 0),
        ("not-a-coordinate", 2770248.38),
    ],
)
def test_invalid_or_out_of_taipei_coordinates_return_none(x: object, y: object) -> None:
    assert twd97_to_wgs84(x, y) is None


def test_coordinate_output_is_stably_rounded() -> None:
    result = twd97_to_wgs84(306894.85, 2770248.38)

    assert result is not None
    assert all(len(str(value).split(".")[-1]) <= 7 for value in result)


def test_batch_coordinate_conversion_matches_scalar_and_preserves_invalid_rows() -> None:
    pairs = twd97_many_to_wgs84(
        [306894.85, None, 0, 305000.0],
        [2770248.38, 2770248.38, 0, 2765000.0],
    )

    assert pairs == [
        twd97_to_wgs84(306894.85, 2770248.38),
        None,
        None,
        twd97_to_wgs84(305000.0, 2765000.0),
    ]
