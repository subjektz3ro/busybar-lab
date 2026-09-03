"""The observation vocabulary, pinned to what the sources actually publish.

busybar-app law 5b: a mapping keyed on values you assumed rather than values
you observed fails silently, and it fails on the interesting case. Every
defect below was live in the app on 2026-08-27 and none of them raised
anything — the sky just quietly drew the wrong weather.

The enum is api.weather.gov's published `presentWeather.weather`, copied
from its OpenAPI spec on 2026-08-27. Pinning it here means a value the app
does not classify shows up as a failure rather than as a default.
"""

from __future__ import annotations

import pytest

from apps import skystrip as S

# Public landmark fixture: Chicago O'Hare International Airport (KORD), not a
# home or operator location.
CHICAGO_OHARE_LATITUDE = 41.9742
CHICAGO_OHARE_LONGITUDE = -87.9073


@pytest.fixture(autouse=True)
def _chicago(monkeypatch):
    """Pin a real location for anything that renders.

    Skystrip's module defaults are the deliberate 0,0 / UTC fallback for an
    unconfigured install — a point in the Gulf of Guinea. Rendering against
    it put the sun on the horizon at the moment these tests call "midday",
    so a clear sky came out sunset-orange and the tint assertions compared
    the wrong pictures.
    """
    from astral import Observer

    monkeypatch.setattr(
        S,
        "OBSERVER",
        Observer(
            latitude=CHICAGO_OHARE_LATITUDE,
            longitude=CHICAGO_OHARE_LONGITUDE,
        ),
    )
    monkeypatch.setattr(S, "TZ", S.ZoneInfo("America/Chicago"))

# api.weather.gov OpenAPI, 36 values.
NWS_PRESENT_WEATHER = frozenset({
    "blowing_dust", "blowing_sand", "blowing_snow", "drizzle", "dust",
    "dust_storm", "dust_whirls", "fog", "fog_mist", "freezing_drizzle",
    "freezing_fog", "freezing_rain", "freezing_spray", "frost",
    "funnel_cloud", "hail", "haze", "ice_crystals", "ice_fog", "ice_pellets",
    "rain", "rain_showers", "sand", "sand_storm", "sleet", "smoke", "snow",
    "snow_grains", "snow_pellets", "snow_showers", "spray", "squalls",
    "thunderstorms", "unknown", "volcanic_ash", "water_spouts",
})

# Values the scene deliberately has no treatment for. Obscurations dim and
# discolour the sky in ways this app does not draw yet, and inventing a
# rendering for smoke would be claiming an observation it cannot support.
# Listed explicitly so that adding one is a decision, not an oversight.
UNTREATED = frozenset({
    "frost", "ice_crystals", "spray", "freezing_spray", "squalls",
    "funnel_cloud", "water_spouts",  # hazards owned by the CAP alert layer
    "unknown",
})


def _classified() -> frozenset[str]:
    obscurations = frozenset().union(*S.OBS_OBSCURATION_WORDS.values())
    return frozenset(S.OBS_RAIN_WORDS | S.OBS_SNOW_WORDS
                     | S.OBS_THUNDER_WORDS | S.OBS_FOG_WORDS | obscurations)


def test_every_vocabulary_entry_is_a_real_feed_value():
    """`small_hail` sat in OBS_PRECIP_WORDS and is not in the enum. A value
    the feed never sends is dead weight that reads as coverage."""
    invented = _classified() | S.OBS_PRECIP_WORDS
    assert invented <= NWS_PRESENT_WEATHER, sorted(
        invented - NWS_PRESENT_WEATHER)


def test_every_feed_value_is_either_classified_or_explicitly_untreated():
    """The check that would have caught fog. A value that is neither
    classified nor listed as a deliberate omission is one the app is
    silently dropping."""
    accounted = _classified() | UNTREATED
    assert NWS_PRESENT_WEATHER <= accounted, sorted(
        NWS_PRESENT_WEATHER - accounted)


def test_untreated_values_are_not_also_classified():
    assert not (_classified() & UNTREATED)


class TestTheDefectsThisAuditFound:
    """Each of these was the app's real behaviour before 2026-08-27."""

    @staticmethod
    def _obs(*weather: str, text: str = "") -> dict:
        return S._parse_obs({
            "textDescription": text,
            "presentWeather": [{"weather": w} for w in weather],
        })

    def test_fog_reaches_the_scene(self):
        """Fog was in no vocabulary at all. It could only arrive by inference
        from visibility or humidity, so a station reporting fog outright
        while visibility read healthy drew a clear morning."""
        for value in ("fog", "fog_mist", "freezing_fog", "ice_fog"):
            assert self._obs(value)["fog"] is True, value

    def test_ice_pellets_is_matched(self):
        """The old test searched the raw JSON for "ice pellets" with a space
        while the feed sends `ice_pellets` with an underscore, so it matched
        only when textDescription happened to spell it out."""
        assert self._obs("ice_pellets")["snow"] is True

    def test_a_snow_shower_is_not_also_rain(self):
        """`snow_showers` contains the substring "shower", so the old test
        set rain AND snow — the scene drew both falling at once."""
        parsed = self._obs("snow_showers")
        assert parsed["snow"] is True
        assert parsed["rain"] is False

    def test_a_rain_shower_is_still_rain(self):
        parsed = self._obs("rain_showers")
        assert parsed["rain"] is True
        assert parsed["snow"] is False

    def test_showers_in_prose_still_read_as_rain(self):
        assert self._obs(text="Rain Showers")["rain"] is True

    def test_snow_showers_in_prose_are_not_rain(self):
        parsed = self._obs(text="Snow Showers")
        assert parsed["snow"] is True
        assert parsed["rain"] is False

    def test_past_precipitation_covers_showers_and_sleet(self):
        """OBS_PRECIP_WORDS drives the past Time Machine slots. It omitted
        `rain_showers`, `snow_showers` and `sleet`, so an hour of real snow
        showers returned "no observation covers this moment"."""
        for value in ("rain_showers", "snow_showers", "sleet"):
            assert value in S.OBS_PRECIP_WORDS, value

    def test_hail_is_classified_the_same_way_by_both_paths(self):
        """Hail was in the past-slot vocabulary and in none of the live ones,
        so an observation of hail alone drew a clear sky on the bar while the
        Time Machine recorded precipitation for the same hour."""
        assert self._obs("hail")["rain"] is True
        assert "hail" in S.OBS_PRECIP_WORDS

    def test_blowing_snow_colours_the_scene_but_is_not_falling_precipitation(self):
        assert self._obs("blowing_snow")["snow"] is True
        assert "blowing_snow" not in S.OBS_PRECIP_WORDS


class TestTheWmoCodes:
    """Open-Meteo's documented WMO interpretation codes."""

    def test_fog_codes_are_read(self):
        """45 and 48 were absent, which is the model-side half of the same
        gap: Open-Meteo could say "fog" and the scene would not show it."""
        for code in (45, 48):
            assert S._wmo_phenomena(code)[3] is True, code

    @pytest.mark.parametrize("code,expected", [
        (0, (False, False, False, False)),    # clear sky
        (3, (False, False, False, False)),    # overcast
        (45, (False, False, False, True)),    # fog
        (48, (False, False, False, True)),    # depositing rime fog
        (51, (True, False, False, False)),    # light drizzle
        (56, (True, False, False, False)),    # freezing drizzle
        (65, (True, False, False, False)),    # heavy rain
        (67, (True, False, False, False)),    # heavy freezing rain
        (71, (False, True, False, False)),    # slight snowfall
        (77, (False, True, False, False)),    # snow grains
        (82, (True, False, False, False)),    # violent rain showers
        (86, (False, True, False, False)),    # heavy snow showers
        (95, (False, False, True, False)),    # thunderstorm
        (99, (False, False, True, False)),    # thunderstorm with heavy hail
    ])
    def test_the_documented_code_set(self, code, expected):
        assert S._wmo_phenomena(code) == expected

    def test_a_missing_or_absurd_code_claims_nothing(self):
        assert S._wmo_phenomena(None) == (False, False, False, False)
        assert S._wmo_phenomena(500) == (False, False, False, False)
        assert S._wmo_phenomena("rain") == (False, False, False, False)


def test_reported_fog_reaches_the_pixels():
    """The visibility and humidity thresholds are inferences; a source
    saying "fog" is direct evidence and must not need them to agree."""
    from datetime import datetime, timezone

    clear = S.WeatherState(temp_c=14.0, humidity=60.0, visibility_m=16000.0)
    foggy = S.WeatherState(temp_c=14.0, humidity=60.0, visibility_m=16000.0,
                           fog=True)
    when = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def ground_rows(image):
        px = image.load()
        return [px[x, y] for x in range(S.W) for y in range(10, 16)]

    assert ground_rows(S.render_scene(when, clear, seed=3)) != ground_rows(
        S.render_scene(when, foggy, seed=3))


def test_low_visibility_still_outweighs_a_bare_fog_flag():
    """The flag is a floor, not a replacement — dense fog must still read as
    denser than patchy fog."""
    from datetime import datetime, timezone

    when = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    patchy = S.WeatherState(temp_c=14.0, humidity=60.0,
                            visibility_m=16000.0, fog=True)
    dense = S.WeatherState(temp_c=14.0, humidity=60.0,
                           visibility_m=400.0, fog=True)

    clear = S.WeatherState(temp_c=14.0, humidity=60.0, visibility_m=16000.0)

    def ground(wx):
        px = S.render_scene(when, wx, seed=3).load()
        return [px[x, y] for x in range(S.W) for y in range(12, 15)]

    def moved(wx):
        """How far the ground rows shift from the unfogged scene.

        Distance, not brightness: fog is bright at noon and dark at dawn,
        so an absolute-brightness metric silently measures the hour instead
        of the fog. This is the same class of mistake as measuring the sky
        without pinning the observer.
        """
        return sum(abs(a - b) for pa, pb in zip(ground(wx), ground(clear))
                   for a, b in zip(pa, pb))

    assert moved(dense) > moved(patchy)


class TestTheObscurations:
    """Haze, smoke, dust, sand and volcanic ash — the whole air column.

    Separate from fog on purpose: fog is condensed water pooling on the
    ground, these fill the air and kill distance.
    """

    @staticmethod
    def _kind(*weather: str, text: str = "") -> str:
        return S._parse_obs({
            "textDescription": text,
            "presentWeather": [{"weather": w} for w in weather],
        })["obscuration"]

    @pytest.mark.parametrize("value,kind", [
        ("haze", "haze"),
        ("smoke", "smoke"),
        ("dust", "dust"),
        ("blowing_dust", "dust"),
        ("dust_whirls", "dust"),
        ("dust_storm", "dust"),
        ("sand", "dust"),
        ("blowing_sand", "dust"),
        ("sand_storm", "dust"),
        ("volcanic_ash", "ash"),
    ])
    def test_every_obscuration_maps_to_a_tint(self, value, kind):
        assert self._kind(value) == kind

    def test_the_most_obscuring_one_wins(self):
        """A station reporting both smoke and haze is having a smoke day."""
        assert self._kind("haze", "smoke") == "smoke"
        assert self._kind("haze", "volcanic_ash") == "ash"
        assert self._kind("dust", "smoke") == "smoke"

    def test_a_clear_sky_reports_no_obscuration(self):
        assert self._kind() == ""
        assert self._kind("rain") == ""

    def test_prose_fallback_is_narrow(self):
        """"dust" and "sand" are ordinary English and would false-positive
        on prose; smoke, haze and volcanic ash are safe to match."""
        assert self._kind(text="smoke") == "smoke"
        assert self._kind(text="haze") == "haze"
        assert self._kind(text="blowing dust") == ""

    def test_the_four_tints_are_distinguishable_on_the_panel(self):
        """The panel cannot resolve under ~30% per channel. The first
        palette put haze and dust one channel apart and they were the same
        colour on the strip."""
        import itertools
        for a, b in itertools.combinations(S.OBSCURATION_TINT, 2):
            deltas = [abs(x - y) / max(x, y, 1)
                      for x, y in zip(S.OBSCURATION_TINT[a],
                                      S.OBSCURATION_TINT[b])]
            clear = sum(1 for d in deltas if d >= 0.30)
            assert clear >= 2, f"{a}/{b} only separates on {clear} channel(s)"

    def test_dust_and_sand_deliberately_share_one_tint(self):
        assert self._kind("sand") == self._kind("dust")


class TestTheObscurationNeverLightsTheNightSky:
    """The trap this whole rendering approach exists to avoid.

    A filled bright row reads as a haze of separated dots on a 2.2mm pitch
    and drowns the scene — the failure SNOW_FRACTION is capped at 0.90 to
    prevent. Lerping every pixel toward a smoke colour would do exactly
    that to a black night sky, so the renderer uses the real atmospheric
    model instead: airlight scales with daylight, and at midnight there is
    no sunlight to scatter.
    """

    from datetime import datetime, timezone

    NIGHT = datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc)
    DAY = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)
    BASE = dict(temp_c=20.0, humidity=40.0, visibility_m=16000.0,
                cloud_frac=0.05)

    def _lit(self, when, kind):
        image = S.render_scene(
            when, S.WeatherState(**self.BASE, obscuration=kind),
            seed=5, scene="skyline")
        px = image.load()
        return sum(1 for x in range(S.W) for y in range(S.H)
                   if sum(px[x, y]) > 30)

    @pytest.mark.parametrize("kind", ["haze", "smoke", "dust", "ash"])
    def test_night_gets_darker_never_brighter(self, kind):
        assert self._lit(self.NIGHT, kind) < self._lit(self.NIGHT, "")

    @pytest.mark.parametrize("kind", ["haze", "smoke", "dust", "ash"])
    def test_the_night_sky_is_never_filled(self, kind):
        assert self._lit(self.NIGHT, kind) < S.W * S.H * 0.5

    def test_the_moon_is_swallowed(self):
        """You cannot see the moon through heavy smoke, and the scene
        should not pretend otherwise."""
        def moon_pixels(kind):
            image = S.render_scene(
                self.NIGHT, S.WeatherState(**self.BASE, obscuration=kind),
                seed=5, scene="skyline")
            px = image.load()
            return sum(1 for x in range(S.W) for y in range(0, 8)
                       if px[x, y][0] > 150 and px[x, y][2] > 140)
        assert moon_pixels("") > 0, "the clear night should show a moon"
        assert moon_pixels("smoke") == 0

    def test_daylight_shifts_the_sky_toward_the_tint(self):
        """The other half of the model: at noon there IS sunlight to
        scatter, so the air glows and distance dissolves into it."""
        def sky(kind):
            image = S.render_scene(
                self.DAY, S.WeatherState(**self.BASE, obscuration=kind),
                seed=5, scene="skyline")
            px = image.load()
            rows = [px[x, y] for x in range(S.W) for y in range(0, 5)]
            return tuple(sum(c[i] for c in rows) / len(rows)
                         for i in range(3))
        clear, smoky = sky(""), sky("smoke")
        assert clear[2] > clear[0], "a clear day sky is blue"
        assert smoky[0] > smoky[2], "a smoke day sky is not"

    def test_the_clock_stays_legible_through_the_worst_of_it(self):
        """The status ink is a readout, not weather. It is baked after the
        obscuration for exactly this reason."""
        image = S.render_scene(
            self.DAY, S.WeatherState(**self.BASE, obscuration="smoke"),
            seed=5, scene="skyline")
        px = image.load()
        ink = [px[x, y] for x in range(S.STATUS_CARD_W)
               for y in range(0, 7) if px[x, y] == S.CLOCK_INK]
        assert ink, "the clock must survive a smoke day"


def test_every_style_names_the_obscuration():
    """A strip drawing brown smoke while the voice says "clear skies" is a
    contradiction the listener can see out the window."""
    from datetime import datetime, timezone

    when = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc).astimezone(S.TZ)
    wx = S.WeatherState(temp_c=33.0, cloud_frac=0.05, obscuration="smoke")
    original = S.STYLE
    try:
        for style in ("plain", "chicago", "genz"):
            S.STYLE = style
            spoken = S._compose_report(wx, None, when, None).lower()
            assert "smoke" in spoken, style
            assert "clear skies" not in spoken, style
    finally:
        S.STYLE = original


def test_genz_plays_smoke_and_ash_straight():
    """Wildfire smoke and volcanic ash are somebody's emergency upwind.
    The bit landing there is the same misfire as joking under a warning."""
    original = S.STYLE
    S.STYLE = "genz"
    try:
        for kind in ("smoke", "ash"):
            pool = S.GENZ_CONDITIONS[f"obscured_{kind}"]
            for line in pool:
                assert "cooked" not in line.lower(), line
                assert "crashing out" not in line.lower(), line
                assert "giving" not in line.lower(), line
    finally:
        S.STYLE = original


def test_every_tint_declares_a_transmission():
    assert set(S.OBSCURATION_TINT) == set(S.OBSCURATION_TRANSMISSION)


def test_ash_is_the_densest_and_haze_the_thinnest():
    """Density is what separates an ashfall from a haze. With one shared
    transmission, ash's dark tint let the blue sky through and the scene
    read as a mildly dim afternoon rather than as an ashfall."""
    t = S.OBSCURATION_TRANSMISSION
    assert t["ash"] < t["dust"] < t["haze"]
    assert t["ash"] < t["smoke"] < t["haze"]


def test_ash_kills_the_blue_of_a_daytime_sky():
    from datetime import datetime, timezone

    when = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)
    base = dict(temp_c=20.0, humidity=40.0, visibility_m=16000.0,
                cloud_frac=0.05)

    def blueness(kind):
        px = S.render_scene(when, S.WeatherState(**base, obscuration=kind),
                            seed=5, scene="skyline").load()
        rows = [px[x, y] for x in range(S.STATUS_CARD_W + 2, S.W)
                for y in range(0, 5)]
        red = sum(c[0] for c in rows) / len(rows)
        blue = sum(c[2] for c in rows) / len(rows)
        return blue - red

    assert blueness("") > 60, "a clear day sky is strongly blue"
    assert blueness("ash") < 20, "ash must not read as a blue day"
