import io

import pytest
from PIL import Image

from busybar_dev.radar import (
    MODEL_FALLBACK_S,
    OM_FRESH_S,
    RADAR_FRESH_S,
    RADAR_FUTURE_SKEW_S,
    STATION_FRESH_S,
    WEB_MERCATOR_MAX_LAT,
    dbz_from_rgb,
    decode_coverage_mask,
    decode_radar_tile,
    rainviewer_frame_age,
    resolve_rain,
    sample_dbz,
    tile_pixel,
    web_mercator_contains,
)


def _png_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_tile_pixel_london_z9():
    # Independently computed slippy-map values for London's published centre.
    assert tile_pixel(51.5074, -0.1278, 9) == (255, 170, 209, 64)


def test_tile_pixel_null_island_z1():
    tx, ty, px, py = tile_pixel(0.0, 0.0, 1)
    assert (tx, ty, px, py) == (1, 1, 0, 0)


def test_tile_pixel_clamps_poles_and_wraps_positive_dateline():
    assert tile_pixel(90.0, 180.0, 7) == (0, 0, 0, 0)
    assert tile_pixel(-90.0, 180.0, 7) == (0, 127, 0, 255)
    assert tile_pixel(0.0, 180.0, 7) == tile_pixel(0.0, -180.0, 7)

    # Safe tile math is not permission to treat a clamped polar edge as local
    # radar evidence; the poller uses this predicate to stand down instead.
    assert web_mercator_contains(WEB_MERCATOR_MAX_LAT)
    assert not web_mercator_contains(90.0)
    assert not web_mercator_contains(float("nan"))


def test_official_coverage_mask_is_transparent_for_available_black_for_absent():
    mask = Image.new("RGBA", (256, 256), (0, 0, 0, 255))
    mask.putpixel((10, 12), (0, 0, 0, 0))
    mask.putpixel((11, 12), (0, 0, 0, 1))
    payload = _png_bytes(mask)

    assert decode_coverage_mask(payload, 10, 12)
    assert not decode_coverage_mask(payload, 9, 12)
    # Only fully transparent grants radar precedence. A partially transparent
    # coverage edge fails closed to the global fallback.
    assert not decode_coverage_mask(payload, 11, 12)


@pytest.mark.parametrize(
    "payload, px, py, match",
    [
        (b"not a png", 0, 0, "decodable bounded PNG"),
        (b"x" * ((1 << 20) + 1), 0, 0, "exceeds 1 MiB"),
        (_png_bytes(Image.new("RGBA", (1, 1))), 0, 0, "exactly 256x256"),
        (_png_bytes(Image.new("RGBA", (256, 256))), 256, 0, "inside"),
    ],
)
def test_coverage_mask_decoder_rejects_unbounded_or_wrong_inputs(
    payload, px, py, match,
):
    with pytest.raises(ValueError, match=match):
        decode_coverage_mask(payload, px, py)


def test_radar_tile_decoder_returns_detached_exact_rgba_image():
    source = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    source.putpixel((10, 12), (255, 170, 0, 255))

    decoded = decode_radar_tile(_png_bytes(source))

    assert decoded.mode == "RGBA"
    assert decoded.size == (256, 256)
    assert decoded.getpixel((10, 12)) == (255, 170, 0, 255)


@pytest.mark.parametrize(
    "payload, match",
    [
        (b"not a png", "decodable bounded PNG"),
        (b"x" * ((1 << 20) + 1), "exceeds 1 MiB"),
        (_png_bytes(Image.new("RGBA", (1, 1))), "exactly 256x256"),
    ],
)
def test_radar_tile_decoder_rejects_unbounded_or_wrong_payloads(payload, match):
    with pytest.raises(ValueError, match=match):
        decode_radar_tile(payload)


def test_radar_tile_decoder_rejects_exact_size_non_png_image():
    payload = io.BytesIO()
    Image.new("RGB", (256, 256)).save(payload, format="JPEG")

    with pytest.raises(ValueError, match="not a PNG"):
        decode_radar_tile(payload.getvalue())


def test_rainviewer_frame_age_uses_source_timestamp_not_receipt_time():
    now = 1_800_000_000.0
    assert rainviewer_frame_age(now - 123.25, now_unix=now) == 123.25
    assert rainviewer_frame_age(
        now + RADAR_FUTURE_SKEW_S, now_unix=now) == 0.0


@pytest.mark.parametrize(
    "timestamp",
    [None, True, "1800000000", float("nan"), float("inf"), -1.0],
)
def test_rainviewer_frame_age_rejects_malformed_unix_timestamps(timestamp):
    with pytest.raises(ValueError, match="Unix|finite"):
        rainviewer_frame_age(timestamp, now_unix=1_800_000_000.0)


def test_rainviewer_frame_age_rejects_stale_and_far_future_frames():
    now = 1_800_000_000.0
    with pytest.raises(ValueError, match="stale"):
        rainviewer_frame_age(now - RADAR_FRESH_S, now_unix=now)
    with pytest.raises(ValueError, match="future"):
        rainviewer_frame_age(
            now + RADAR_FUTURE_SKEW_S + 0.001, now_unix=now)


def test_dbz_from_rgb_official_universal_blue_anchors():
    # Exact anchors from RainViewer's published Universal Blue CSV.
    assert dbz_from_rgb((0, 119, 170)) == 25.0
    assert dbz_from_rgb((255, 238, 0)) == 35.0
    assert dbz_from_rgb((255, 170, 0)) == 40.0
    assert dbz_from_rgb((255, 170, 255)) == 55.0
    assert dbz_from_rgb((0, 163, 224)) == 20.0
    # Mist tans decode BELOW the rain threshold (dry), never as rain
    assert dbz_from_rgb((206, 192, 135)) == 10.0
    # A color far from the whole palette is rejected
    assert dbz_from_rgb((12, 34, 56)) is None


def test_sample_dbz_takes_neighborhood_max_and_clamps():
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    img.putpixel((10, 12), (255, 170, 0, 255))   # 40 dBZ two px from center
    img.putpixel((10, 11), (136, 221, 238, 255)) # 15 dBZ nearer — max wins
    assert sample_dbz(img, 10, 10, radius=2) == 40.0
    assert sample_dbz(img, 200, 200, radius=2) is None
    assert sample_dbz(img, 0, 0, radius=2) is None  # clamped corner, no crash


def _resolve(
    *,
    radar_dbz=None,
    radar_age=1e9,
    om_rain=None,
    om_age=1e9,
    station_rain=None,
    station_age=1e9,
    last_rain=False,
    last_tier=1,
    last_known=False,
    last_age=1e9,
    snowing=False,
):
    return resolve_rain(
        radar_dbz, radar_age,
        om_rain, om_age,
        station_rain, station_age,
        last_rain, last_tier, last_known, last_age, snowing,
    )


def test_resolve_radar_wins_and_decides_dry():
    assert _resolve(radar_dbz=35.0, radar_age=60) == (True, 1, "radar")
    assert _resolve(radar_dbz=25.0, radar_age=60) == (True, 0, "radar")
    assert _resolve(radar_dbz=44.0, radar_age=60) == (True, 2, "radar")
    # Radar says clear even when both lower-precedence sources say rain.
    assert _resolve(
        radar_age=60, om_rain=True, om_age=60,
        station_rain=True, station_age=60,
    ) == (False, 1, "radar")
    assert _resolve(
        radar_dbz=10.0, radar_age=60,
        om_rain=True, om_age=60,
        station_rain=True, station_age=60,
    ) == (False, 1, "radar")


def test_resolve_fallback_chain():
    assert _resolve(
        om_rain=True, om_age=60,
        station_rain=False, station_age=60,
    ) == (True, 1, "nowcast")
    assert _resolve(
        station_rain=True, station_age=60,
    ) == (True, 1, "station")
    assert _resolve(
        station_rain=False, station_age=60,
        last_rain=True, last_tier=2,
    ) == (False, 1, "station")


def test_absent_or_stale_station_preserves_last_good_instead_of_inventing_dry():
    assert _resolve(
        station_rain=None,
        last_rain=True,
        last_tier=2,
        last_known=True,
        last_age=60,
    ) == (True, 2, "last-good")
    assert _resolve(
        station_rain=False,
        station_age=STATION_FRESH_S,
        last_rain=True,
        last_tier=0,
        last_known=True,
        last_age=60,
    ) == (True, 0, "last-good")
    assert _resolve(
        station_rain=False,
        station_age=STATION_FRESH_S - 0.001,
        last_rain=True,
        last_tier=2,
        last_known=True,
    ) == (False, 1, "station")


def test_stale_station_cannot_override_global_or_last_good_evidence():
    assert _resolve(
        om_rain=False,
        om_age=60,
        station_rain=True,
        station_age=1e9,
        last_rain=True,
        last_tier=2,
        last_known=True,
    ) == (False, 1, "nowcast")
    assert _resolve(
        station_rain=True,
        station_age=1e9,
        last_rain=False,
        last_tier=2,
        last_known=True,
        last_age=60,
    ) == (False, 2, "last-good")


def test_cold_start_uses_bounded_model_evidence_not_default_dry():
    assert _resolve(
        om_rain=True,
        om_age=OM_FRESH_S,
        last_known=False,
    ) == (True, 1, "model-aged")
    assert _resolve(
        om_rain=True,
        om_age=MODEL_FALLBACK_S,
        last_known=False,
    ) == (False, 1, "unavailable")
    assert _resolve(
        om_rain=True,
        om_age=OM_FRESH_S,
        last_rain=False,
        last_tier=2,
        last_known=True,
        last_age=60,
    ) == (False, 2, "last-good")


def test_last_good_expires_on_its_own_source_age_not_base_weather_refresh():
    assert _resolve(
        last_rain=True,
        last_tier=2,
        last_known=True,
        last_age=MODEL_FALLBACK_S,
    ) == (True, 2, "last-good")
    assert _resolve(
        last_rain=True,
        last_tier=2,
        last_known=True,
        last_age=MODEL_FALLBACK_S + 0.001,
    ) == (False, 1, "unavailable")


def test_bounded_model_recovery_matches_cold_start_after_last_good_expires():
    assert _resolve(
        om_rain=True,
        om_age=OM_FRESH_S,
        last_rain=False,
        last_tier=2,
        last_known=True,
        last_age=MODEL_FALLBACK_S + 0.001,
    ) == (True, 1, "model-aged")


def test_resolve_snow_owns_precip():
    assert _resolve(
        radar_dbz=50.0,
        radar_age=60,
        om_rain=True,
        om_age=60,
        station_rain=True,
        station_age=60,
        snowing=True,
    ) == (False, 1, "snow")
